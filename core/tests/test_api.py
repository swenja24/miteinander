import base64
import json
import tempfile
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Invitation, ManagedFile, Membership, Record, Workspace
from core.services import create_record, create_workspace, permissions
from core.tasks import send_workspace_notification


@override_settings(
    SECURE_SSL_REDIRECT=False,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.admin = User.objects.create_user(username="admin", password="Ein-sehr-sicheres-Passwort!", display_name="Admin", is_system_admin=True)
        self.workspace, self.admin_membership = create_workspace(self.admin, "Lineas Bereich", Workspace.Type.PRIVATE, "Linea")
        self.other_admin = User.objects.create_user(username="other", password="Ein-sehr-sicheres-Passwort!", display_name="Andere Person")
        self.other_workspace, _ = create_workspace(self.other_admin, "Anderer Bereich", Workspace.Type.PRIVATE, "Robin")
        self.assistant = User.objects.create_user(username="assistenz", password="Ein-sehr-sicheres-Passwort!", display_name="Assistenz")
        Membership.objects.create(workspace=self.workspace, user=self.assistant, role="Assistenz", permissions=permissions({"tasks": True, "calendar": True}))
        self.client = Client()
        self.client.force_login(self.admin)

    def api(self, method, path, payload=None, workspace=None, client=None):
        target = client or self.client
        kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace or self.workspace.id)}
        if method.lower() != "get":
            kwargs.update({"data": json.dumps(payload or {}), "content_type": "application/json"})
        return getattr(target, method.lower())(path, **kwargs)

    def test_data_uses_allowlist_and_never_exposes_authentication_secrets(self):
        self.admin.totp_secret = "SUPERSECRET"
        self.admin.pending_totp_secret = "PENDING"
        self.admin.save()
        response = self.api("get", "/api/data")
        self.assertEqual(response.status_code, 200)
        raw = response.content.decode()
        self.assertNotIn("SUPERSECRET", raw)
        self.assertNotIn("PENDING", raw)
        self.assertNotIn("password", raw.lower())

    def test_browser_login_requires_csrf_cookie_and_header(self):
        browser = Client(enforce_csrf_checks=True)
        page = browser.get("/")
        self.assertEqual(page.status_code, 200)
        token = page.cookies["csrftoken"].value
        payload = json.dumps({"username": "admin", "password": "Ein-sehr-sicheres-Passwort!"})
        rejected = browser.post("/api/login", data=payload, content_type="application/json")
        accepted = browser.post("/api/login", data=payload, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 200)

    def test_records_are_strictly_scoped_to_selected_workspace(self):
        foreign = create_record(self.other_workspace, "task", {"title": "Fremd", "due": "2030-01-01"}, self.other_admin)
        response = self.api("put", f"/api/tasks/{foreign.id}", {"title": "Manipuliert"})
        self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.title, "Fremd")

    def test_unknown_workspace_header_never_falls_back_to_another_area(self):
        response = self.api(
            "post",
            "/api/tasks",
            {"title": "Darf nirgendwo landen", "due": "2030-01-01"},
            workspace="00000000-0000-0000-0000-000000000000",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Record.objects.filter(title="Darf nirgendwo landen").exists())

    def test_paused_workspace_rejects_direct_write_endpoints(self):
        self.workspace.status = "paused"
        self.workspace.save(update_fields=("status",))
        response = self.api("post", "/api/announcements", {"title": "Nicht speichern", "text": "Gesperrt"})
        self.assertEqual(response.status_code, 423)
        self.assertFalse(Record.objects.filter(workspace=self.workspace, kind="announcement").exists())

    def test_assistant_only_receives_granted_modules(self):
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        response = self.api("get", "/api/data", client=assistant_client)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["capabilities"]["tasks"])
        self.assertFalse(body["capabilities"]["cases"])
        denied = self.api("post", "/api/cases", {"title": "Nicht erlaubt"}, client=assistant_client)
        self.assertEqual(denied.status_code, 403)

    def test_shared_area_refuses_personal_case_records(self):
        shared, _ = create_workspace(self.admin, "WG", Workspace.Type.SHARED)
        response = self.api("post", "/api/cases", {"title": "Privater Antrag"}, workspace=shared.id)
        self.assertEqual(response.status_code, 403)

    def test_recurring_task_without_end_keeps_seven_occurrences(self):
        response = self.api("post", "/api/tasks", {"title": "Montagsaufgabe", "due": (timezone.localdate() + timedelta(days=1)).isoformat(), "status": "planned", "recurrence": "weekly"})
        self.assertEqual(response.status_code, 201)
        series = response.json()["recurrenceSeriesId"]
        self.assertEqual(Record.objects.filter(workspace=self.workspace, kind="task", series_id=series).count(), 7)

    def test_database_rejects_duplicate_occurrence_in_same_series(self):
        series = "8c8ce2d0-1bd1-4f12-a744-4fab265fe821"
        values = {"title": "Montagsaufgabe", "due": "2030-01-07", "recurrence": "weekly", "recurrenceSeriesId": series}
        create_record(self.workspace, "task", values, self.admin)
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_record(self.workspace, "task", values, self.admin)

    def test_case_with_correspondence_cannot_be_deleted(self):
        case = self.api("post", "/api/cases", {"title": "Eingliederungshilfe"}).json()
        response = self.api(
            "post",
            "/api/correspondence",
            {"caseId": case["id"], "subject": "Eingangsbestätigung", "date": "2030-01-02", "eventType": "letter"},
        )
        self.assertEqual(response.status_code, 201)
        deleted = self.api("delete", f"/api/cases/{case['id']}")
        self.assertEqual(deleted.status_code, 409)
        self.assertTrue(Record.objects.filter(pk=case["id"], kind="case").exists())

    def test_deleting_deadline_correspondence_removes_its_reminder(self):
        case = self.api("post", "/api/cases", {"title": "Pflegegrad"}).json()
        correspondence = self.api(
            "post",
            "/api/correspondence",
            {
                "caseId": case["id"], "subject": "Unterlagen nachreichen", "date": "2030-06-30",
                "eventType": "deadline", "reminderDays": "14", "deadlineStatus": "open",
            },
        ).json()
        reminder_id = correspondence["reminderTaskId"]
        self.assertTrue(Record.objects.filter(pk=reminder_id, kind="task").exists())
        deleted = self.api("delete", f"/api/correspondence/{correspondence['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(Record.objects.filter(pk=reminder_id, kind="task").exists())

    def test_file_content_is_verified_instead_of_trusting_declared_type(self):
        fake = "data:image/png;base64," + base64.b64encode(b"this is not an image").decode()
        response = self.api("post", "/api/ledger", {"description": "Test", "accountId": str(Record.objects.filter(workspace=self.workspace, kind="account").first().id), "type": "expense", "amount": "1.00", "date": "2030-01-01", "receiptImage": fake})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ManagedFile.objects.count(), 0)

    def test_pdf_with_active_content_is_rejected(self):
        active_pdf = b"%PDF-1.4\n1 0 obj << /OpenAction 2 0 R /JavaScript (alert) >>\n%%EOF"
        encoded = "data:application/pdf;base64," + base64.b64encode(active_pdf).decode()
        response = self.api("post", "/api/documents", {"title": "Unsicher", "documentFile": encoded, "documentFileName": "aktiv.pdf"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ManagedFile.objects.filter(original_name="aktiv.pdf").exists())

    def test_malformed_bank_statement_returns_a_user_error(self):
        account = Record.objects.filter(workspace=self.workspace, kind="account").first()
        file_data = "data:text/csv;base64," + base64.b64encode(b"not a usable statement").decode()
        response = self.api(
            "post",
            "/api/bank-import/preview",
            {"accountId": str(account.id), "fileName": "konto.csv", "fileData": file_data},
        )
        self.assertEqual(response.status_code, 400)

    def test_invitation_can_only_be_used_once(self):
        invitation = self.api("post", "/api/invitations", {"displayName": "Neue Assistenz", "role": "Assistenz", "validDays": 7, "permissions": {"tasks": True}}).json()
        anonymous = Client()
        payload = {"displayName": "Neue Assistenz", "username": "neu", "password": "Noch-ein-sehr-sicheres-Passwort!"}
        first = anonymous.post(f"/api/invitations/{invitation['token']}/accept", data=json.dumps(payload), content_type="application/json")
        second = Client().post(f"/api/invitations/{invitation['token']}/accept", data=json.dumps({**payload, "username": "neu2"}), content_type="application/json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 410)

    def test_member_cannot_modify_another_profile(self):
        admin_member = self.admin_membership.member
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        response = self.api("put", f"/api/members/{admin_member.id}", {"personalWords": "Geändert"}, client=assistant_client)
        self.assertEqual(response.status_code, 403)

    def test_deactivated_access_is_rejected_at_login(self):
        self.assistant.access_status = "paused"
        self.assistant.save(update_fields=("access_status",))
        response = Client().post("/api/login", data=json.dumps({"username": "assistenz", "password": "Ein-sehr-sicheres-Passwort!"}), content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_invalid_totp_attempts_are_rate_limited(self):
        self.assistant.totp_enabled = True
        self.assistant.totp_secret = "JBSWY3DPEHPK3PXP"
        self.assistant.save(update_fields=("totp_enabled", "totp_secret"))
        payload = json.dumps({"username": "assistenz", "password": "Ein-sehr-sicheres-Passwort!", "otp": "000000"})
        with patch("core.api.auth.valid_totp", return_value=False):
            for _ in range(10):
                self.assertEqual(Client().post("/api/login", data=payload, content_type="application/json").status_code, 401)
            blocked = Client().post("/api/login", data=payload, content_type="application/json")
        self.assertEqual(blocked.status_code, 429)

    def test_active_totp_cannot_be_replaced_without_disabling_it(self):
        self.assistant.totp_enabled = True
        self.assistant.totp_secret = "JBSWY3DPEHPK3PXP"
        self.assistant.save(update_fields=("totp_enabled", "totp_secret"))
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        response = self.api("post", "/api/security/totp/setup", {}, client=assistant_client)
        self.assertEqual(response.status_code, 409)
        self.assistant.refresh_from_db()
        self.assertEqual(self.assistant.totp_secret, "JBSWY3DPEHPK3PXP")

    def test_workspace_admin_cannot_change_another_users_global_identity(self):
        Membership.objects.create(workspace=self.other_workspace, user=self.assistant, role="Assistenz", permissions=permissions({"tasks": True}))
        response = self.api(
            "put",
            f"/api/users/{self.assistant.id}",
            {"displayName": "Übernommen", "password": "Ein-neues-sehr-sicheres-Passwort!"},
        )
        self.assertEqual(response.status_code, 403)
        self.assistant.refresh_from_db()
        self.assertEqual(self.assistant.display_name, "Assistenz")
        self.assertTrue(self.assistant.check_password("Ein-sehr-sicheres-Passwort!"))

    def test_removing_last_membership_deletes_orphaned_regular_account(self):
        User = get_user_model()
        assistant_id = self.assistant.id
        response = self.api("delete", f"/api/users/{assistant_id}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=assistant_id).exists())

    def test_workspace_admin_cannot_deactivate_global_operator_account(self):
        User = get_user_model()
        operator = User.objects.create_user(username="operator", password="Ein-sehr-sicheres-Passwort!", display_name="Betreiber", is_system_admin=True)
        Membership.objects.create(workspace=self.workspace, user=operator, role="Assistenz", permissions=permissions({"tasks": True}))
        response = self.api("delete", f"/api/users/{operator.id}")
        self.assertEqual(response.status_code, 200)
        operator.refresh_from_db()
        self.assertTrue(operator.is_active)
        self.assertTrue(operator.is_system_admin)
        self.assertFalse(Membership.objects.filter(workspace=self.workspace, user=operator).exists())

    def test_user_can_change_own_account_and_session_stays_valid(self):
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        response = self.api(
            "put",
            "/api/account",
            {
                "displayName": "Neue Assistenz",
                "currentPassword": "Ein-sehr-sicheres-Passwort!",
                "newPassword": "Noch-ein-sehr-sicheres-Passwort!",
            },
            client=assistant_client,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.api("get", "/api/data", client=assistant_client).status_code, 200)
        self.assistant.refresh_from_db()
        self.assertEqual(self.assistant.display_name, "Neue Assistenz")
        self.assertTrue(self.assistant.check_password("Noch-ein-sehr-sicheres-Passwort!"))

    def test_paused_workspace_member_receives_no_notification(self):
        self.assistant.notification_email = "assistenz@example.org"
        self.assistant.notification_preference = "all"
        self.assistant.save(update_fields=("notification_email", "notification_preference"))
        Membership.objects.filter(workspace=self.workspace, user=self.assistant).update(access_status="paused")
        send_workspace_notification.run(str(self.workspace.id), str(self.admin.id), [], "Neue Information")
        self.assertEqual(len(mail.outbox), 0)

    def test_paused_member_is_not_exposed_to_regular_users(self):
        User = get_user_model()
        viewer = User.objects.create_user(username="viewer", password="Ein-sehr-sicheres-Passwort!", display_name="Viewer")
        Membership.objects.create(workspace=self.workspace, user=viewer, role="Assistenz", permissions=permissions({"tasks": True}))
        Membership.objects.filter(workspace=self.workspace, user=self.assistant).update(access_status="paused")
        viewer_client = Client()
        viewer_client.force_login(viewer)
        users = self.api("get", "/api/data", client=viewer_client).json()["users"]
        self.assertNotIn(str(self.assistant.id), {item["id"] for item in users})

    def test_pausing_access_invalidates_an_existing_session(self):
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        self.assistant.access_status = "paused"
        self.assistant.save(update_fields=("access_status",))
        response = self.api("get", "/api/data", client=assistant_client)
        self.assertEqual(response.status_code, 401)

    def test_workspace_pause_does_not_block_other_independent_workspace(self):
        Membership.objects.create(
            workspace=self.other_workspace,
            user=self.assistant,
            role="Assistenz",
            permissions=permissions({"tasks": True}),
        )
        paused = self.api(
            "put",
            f"/api/users/{self.assistant.id}",
            {"accessStatus": "paused"},
        )
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(
            Membership.objects.get(workspace=self.workspace, user=self.assistant).access_status,
            "paused",
        )
        self.assertEqual(
            Membership.objects.get(workspace=self.other_workspace, user=self.assistant).access_status,
            "active",
        )
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        response = self.api("get", "/api/data", workspace=self.other_workspace.id, client=assistant_client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workspace"]["id"], str(self.other_workspace.id))

    def test_last_workspace_admin_cannot_remove_or_pause_own_administration(self):
        demoted = self.api(
            "put",
            f"/api/users/{self.admin.id}",
            {"role": "Assistenz", "permissions": {"tasks": True}},
        )
        paused = self.api(
            "put",
            f"/api/users/{self.admin.id}",
            {"accessStatus": "paused"},
        )
        self.assertEqual(demoted.status_code, 409)
        self.assertEqual(paused.status_code, 400)
        self.admin_membership.refresh_from_db()
        self.assertTrue(self.admin_membership.is_admin)
        self.assertEqual(self.admin_membership.access_status, "active")

    def test_privacy_export_only_contains_permitted_modules(self):
        create_record(self.workspace, "case", {"title": "Vertraulicher Antrag"}, self.admin)
        create_record(self.workspace, "task", {"title": "Sichtbare Aufgabe", "due": "2030-01-01"}, self.admin)
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        response = self.api("get", "/api/privacy/export", client=assistant_client)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["cases"], [])
        self.assertEqual(body["tasks"][0]["title"], "Sichtbare Aufgabe")

    def test_invalid_and_overly_nested_json_is_rejected(self):
        invalid_number = self.client.post(
            "/api/tasks",
            data='{"title":"Test","due":"2030-01-01","value":NaN}',
            content_type="application/json",
            HTTP_X_WORKSPACE_ID=str(self.workspace.id),
        )
        nested = value = {}
        for _ in range(20):
            value["child"] = {}
            value = value["child"]
        too_deep = self.api("post", "/api/tasks", {"title": "Test", "due": "2030-01-01", "extra": nested})
        self.assertEqual(invalid_number.status_code, 400)
        self.assertEqual(too_deep.status_code, 400)

    def test_invalid_status_and_calendar_time_are_rejected(self):
        status = self.api("post", "/api/tasks", {"title": "Test", "due": "2030-01-01", "status": "<img src=x>"})
        clock = self.api("post", "/api/calendarEvents", {"title": "Termin", "date": "2030-01-01", "startTime": "<script>"})
        self.assertEqual(status.status_code, 400)
        self.assertEqual(clock.status_code, 400)

    def test_account_with_ledger_entries_cannot_be_deleted(self):
        account = Record.objects.filter(workspace=self.workspace, kind="account").first()
        created = self.api(
            "post",
            "/api/ledger",
            {"description": "Miete", "accountId": str(account.id), "type": "expense", "amount": "10.00", "date": "2030-01-01", "receiptStatus": "none"},
        )
        self.assertEqual(created.status_code, 201)
        deleted = self.api("delete", f"/api/accounts/{account.id}")
        self.assertEqual(deleted.status_code, 409)
        self.assertTrue(Record.objects.filter(pk=account.id).exists())

    def test_linked_team_profile_cannot_be_deleted(self):
        member = self.admin_membership.member
        response = self.api("delete", f"/api/members/{member.id}")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(type(member).objects.filter(pk=member.id).exists())

    def test_deleting_task_removes_its_topic_comments(self):
        task = self.api("post", "/api/tasks", {"title": "Bus üben", "due": "2030-01-01"}).json()
        comment = self.api("post", "/api/topic-comments", {"targetType": "task", "targetId": task["id"], "text": "Hat gut geklappt"})
        self.assertEqual(comment.status_code, 201)
        self.assertEqual(self.api("delete", f"/api/tasks/{task['id']}").status_code, 200)
        self.assertFalse(Record.objects.filter(pk=comment.json()["id"]).exists())

    def test_invalid_invitation_email_is_rejected(self):
        response = self.api("post", "/api/invitations", {"displayName": "Test", "email": "keine-adresse", "role": "Assistenz"})
        self.assertEqual(response.status_code, 400)

    def test_secure_file_checks_module_permission(self):
        content = b"%PDF-1.4\n%%EOF"
        managed = ManagedFile.objects.create(workspace=self.workspace, category="document", original_name="test.pdf", content_type="application/pdf", size=len(content), sha256="0" * 64, uploaded_by=self.admin)
        managed.file.save("test.pdf", base64_file(content), save=True)
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        response = self.api("get", f"/api/document-files/{managed.id}", client=assistant_client)
        self.assertEqual(response.status_code, 404)

    def test_secure_file_uses_restrictive_browser_headers(self):
        content = b"%PDF-1.4\n%%EOF"
        managed = ManagedFile.objects.create(workspace=self.workspace, category="case", original_name="test.pdf", content_type="application/pdf", size=len(content), sha256="0" * 64, uploaded_by=self.admin)
        managed.file.save("headers.pdf", base64_file(content), save=True)
        response = self.api("get", f"/api/case-files/{managed.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        response.close()

    def test_rolled_back_file_deletion_keeps_the_physical_blob(self):
        content = b"%PDF-1.4\n%%EOF"
        managed = ManagedFile.objects.create(workspace=self.workspace, category="document", original_name="rollback.pdf", content_type="application/pdf", size=len(content), sha256="0" * 64, uploaded_by=self.admin)
        managed.file.save("rollback.pdf", base64_file(content), save=True)
        storage = managed.file.storage
        with patch.object(storage, "delete") as delete_blob:
            try:
                with transaction.atomic():
                    ManagedFile.objects.get(pk=managed.pk).delete()
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
        delete_blob.assert_not_called()
        self.assertTrue(ManagedFile.objects.filter(pk=managed.pk).exists())

    def test_about_contacts_routines_and_communication_flow(self):
        goal = self.api("post", "/api/about/goals", {"title": "Bus fahren", "status": "active"})
        self.assertEqual(goal.status_code, 201)
        comment = self.api("post", "/api/about/comments", {"targetType": "goal", "targetId": goal.json()["id"], "text": "Erster Schritt geschafft"})
        self.assertEqual(comment.status_code, 201)
        contact = self.api("post", "/api/contacts", {"name": "Fahrdienst", "category": "Fahrdienst / Busunternehmen"})
        self.assertEqual(contact.status_code, 201)
        routine = self.api("post", "/api/routine-plans", {"title": "Morgenroutine", "planType": "day", "schedule": "08:00 | Frühstück"})
        self.assertEqual(routine.status_code, 201)
        reviewed = self.api("put", f"/api/routine-plans/{routine.json()['id']}/review", {"approvalStatus": "approved"})
        self.assertEqual(reviewed.status_code, 200)
        announcement = self.api("post", "/api/announcements", {"title": "Info", "text": "Heute später"})
        self.assertEqual(announcement.status_code, 201)
        read = self.api("put", f"/api/announcements/{announcement.json()['id']}/read", {})
        self.assertEqual(read.status_code, 200)

    def test_bei_pdf_is_stored_and_downloadable_for_workspace_member(self):
        pdf = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4\n%%EOF").decode()
        response = self.api("post", "/api/about/beis", {"title": "BEI Wohnen", "beiFile": pdf, "beiFileName": "bei.pdf"})
        self.assertEqual(response.status_code, 201)
        file_id = response.json()["attachmentFile"]
        downloaded = self.api("get", f"/api/about-files/{file_id}")
        self.assertEqual(downloaded.status_code, 200)
        downloaded.close()


def base64_file(content):
    from django.core.files.base import ContentFile
    return ContentFile(content)


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class LegacyImportTestCase(TestCase):
    def test_minimal_legacy_json_is_imported_once(self):
        legacy = {
            "family": {"name": "Lineas Bereich", "person": "Linea"},
            "workspaces": [{"id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "name": "Lineas Bereich", "type": "private", "personName": "Linea"}],
            "users": [{"id": "u1", "username": "linea", "displayName": "Linea", "isAdmin": True, "active": True, "passwordHash": "pbkdf2_sha256$310000$00$00", "workspaceMemberships": [{"workspaceId": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "isAdmin": True, "role": "Leistungsberechtigte Person", "permissions": {"tasks": True}}]}],
            "members": [],
            "tasks": [{"id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8", "workspaceId": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "title": "Importierte Aufgabe", "due": "2030-01-01", "status": "planned"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "familie.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            call_command("import_legacy_json", path, verbosity=0)
            call_command("import_legacy_json", path, verbosity=0)
        self.assertEqual(Workspace.objects.count(), 1)
        self.assertEqual(Record.objects.filter(kind="task", title="Importierte Aufgabe").count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class BootstrapTestCase(TestCase):
    def test_operator_and_supported_person_receive_separate_profiles(self):
        environment = {
            "BOOTSTRAP_ADMIN_USERNAME": "swenja",
            "BOOTSTRAP_ADMIN_PASSWORD": "Ein-langes-sicheres-Passwort!",
            "BOOTSTRAP_ADMIN_NAME": "Swenja",
            "BOOTSTRAP_ADMIN_ROLE": "Enge Angehörige",
            "BOOTSTRAP_PERSON_NAME": "Linea",
            "BOOTSTRAP_WORKSPACE_NAME": "Lineas Bereich",
        }
        with patch.dict("os.environ", environment):
            call_command("bootstrap_miteinander", verbosity=0)
        workspace = Workspace.objects.get()
        self.assertEqual(set(workspace.home_members.values_list("name", flat=True)), {"Linea", "Swenja"})
        membership = Membership.objects.get()
        self.assertEqual(membership.member.name, "Swenja")
        self.assertEqual(membership.role, "Enge Angehörige")
