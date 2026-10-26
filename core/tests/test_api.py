import base64
import json
import tempfile
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Invitation, ManagedFile, Membership, Record, Workspace
from core.services import create_record, create_workspace, permissions


@override_settings(
    SECURE_SSL_REDIRECT=False,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    CELERY_TASK_ALWAYS_EAGER=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class ApiTestCase(TestCase):
    def setUp(self):
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
        response = self.api("get", "/api/data", client=assistant_client)
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

    def test_secure_file_checks_module_permission(self):
        content = b"%PDF-1.4\n%%EOF"
        managed = ManagedFile.objects.create(workspace=self.workspace, category="document", original_name="test.pdf", content_type="application/pdf", size=len(content), sha256="0" * 64, uploaded_by=self.admin)
        managed.file.save("test.pdf", base64_file(content), save=True)
        assistant_client = Client()
        assistant_client.force_login(self.assistant)
        response = self.api("get", f"/api/document-files/{managed.id}", client=assistant_client)
        self.assertEqual(response.status_code, 404)

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
