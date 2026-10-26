import hashlib
import json
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import AuditEvent, CalendarFeed, DeveloperFeedback, Invitation, ManagedFile, Member, Membership, Workspace, WorkspaceOptions, WorkspaceProfile
from core.services import KIND_BY_COLLECTION, create_record, default_options, permissions


def as_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return uuid.uuid4()


def as_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Importiert die frühere familie.json einmalig in die relationale Datenbank."

    def add_arguments(self, parser):
        parser.add_argument("json_file", type=Path)

    @transaction.atomic
    def handle(self, *args, **options):
        source = options["json_file"].resolve()
        if not source.is_file():
            raise CommandError(f"Datei nicht gefunden: {source}")
        if Workspace.objects.exists():
            self.stdout.write("Import übersprungen: Es gibt bereits mindestens einen Bereich.")
            return
        try:
            data = json.loads(source.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CommandError(f"Die alte JSON-Datei ist nicht lesbar: {error}") from error
        if not isinstance(data, dict):
            raise CommandError("Die alte JSON-Datei enthält kein Objekt.")

        User = get_user_model()
        workspaces = {}
        legacy_workspaces = data.get("workspaces") or [{
            "id": str(uuid.uuid4()), "name": data.get("family", {}).get("name", "Miteinander"),
            "type": "private", "personName": data.get("family", {}).get("person", ""),
        }]
        for value in legacy_workspaces:
            identifier = as_uuid(value.get("id"))
            workspace = Workspace.objects.create(
                id=identifier, name=str(value.get("name") or "Miteinander")[:250],
                type=value.get("type") if value.get("type") in {"private", "shared"} else "private",
                person_name=str(value.get("personName") or "")[:250], status=str(value.get("status") or "active")[:30],
                deletion_after=value.get("deletionAfter") or None,
            )
            workspaces[str(value.get("id"))] = workspace
            profile_data = (data.get("workspaceProfiles") or {}).get(str(value.get("id")), data.get("personProfile", {}) if workspace == list(workspaces.values())[0] else {})
            WorkspaceProfile.objects.create(workspace=workspace, introduction=profile_data.get("introduction", ""), strengths=profile_data.get("strengths", ""), support_needs=profile_data.get("supportNeeds", ""), bei_summary=profile_data.get("beiSummary", ""), wishes=profile_data.get("wishes", ""))
            option_data = (data.get("workspaceOptions") or {}).get(str(value.get("id"))) or default_options()
            WorkspaceOptions.objects.create(workspace=workspace, value=option_data)
        default_workspace = next(iter(workspaces.values()))

        users = {}
        for value in data.get("users", []):
            username = str(value.get("username") or f"import-{len(users)+1}").lower()[:150]
            if User.objects.filter(username=username).exists():
                username = f"{username}-{len(users)+1}"[:150]
            legacy_access = str(value.get("accessStatus") or "active")[:20]
            user = User.objects.create(
                username=username, display_name=str(value.get("displayName") or username)[:200],
                email=str(value.get("notificationEmail") or ""), notification_email=str(value.get("notificationEmail") or ""),
                notification_preference=str(value.get("notificationPreference") or "mentions")[:20],
                is_active=bool(value.get("active", True)) and legacy_access != "deactivated",
                access_status="deactivated" if legacy_access == "deactivated" else "active",
                is_system_admin=bool(value.get("isSystemAdmin")), legacy_password_hash=str(value.get("passwordHash") or ""),
                totp_secret=str(value.get("totpSecret") or ""), totp_enabled=bool(value.get("totpEnabled")),
                onboarding_dismissed=value.get("onboardingDismissedWorkspaceIds") or [],
            )
            user.set_unusable_password()
            user.save()
            users[str(value.get("id"))] = user

        files = {}
        def import_file(workspace, filename, folder, category):
            if not filename:
                return None
            key = (str(workspace.id), folder, Path(str(filename)).name)
            if key in files:
                return files[key]
            path = source.parent / folder / Path(str(filename)).name
            if not path.is_file():
                self.stderr.write(f"Datei fehlt und wurde ausgelassen: {path}")
                return None
            content = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            item = ManagedFile(workspace=workspace, category=category, original_name=path.name, content_type=content_type, size=len(content), sha256=hashlib.sha256(content).hexdigest())
            item.file.save(path.name, ContentFile(content), save=True)
            files[key] = item
            return item

        members = {}
        for value in data.get("members", []):
            workspace = workspaces.get(str(value.get("workspaceId")), default_workspace)
            member = Member.objects.create(
                id=as_uuid(value.get("id")), home_workspace=workspace, name=str(value.get("name") or "Unbenannt")[:250],
                role=str(value.get("role") or "")[:100], email=str(value.get("email") or ""), phone=str(value.get("phone") or "")[:100],
                address=str(value.get("address") or ""), personal_words=str(value.get("personalWords") or ""), color=str(value.get("color") or "#285c4d")[:30],
                photo=import_file(workspace, value.get("photoFile"), "member-files", "member"),
            )
            members[str(value.get("id"))] = member

        for value in data.get("users", []):
            user = users.get(str(value.get("id")))
            member = members.get(str(value.get("memberId")))
            if member and member.user_id is None:
                member.user = user
                member.save(update_fields=("user",))
            legacy_memberships = value.get("workspaceMemberships") or [{"workspaceId": str(default_workspace.id), "memberId": value.get("memberId"), "role": value.get("role"), "isAdmin": value.get("isAdmin"), "permissions": value.get("permissions", {})}]
            for item in legacy_memberships:
                workspace = workspaces.get(str(item.get("workspaceId")))
                if workspace:
                    membership_status = str(item.get("accessStatus") or value.get("accessStatus") or "active")
                    Membership.objects.get_or_create(workspace=workspace, user=user, defaults={"member": members.get(str(item.get("memberId"))) or member, "role": str(item.get("role") or "")[:100], "is_admin": bool(item.get("isAdmin")), "access_status": "paused" if membership_status == "paused" else "active", "permissions": permissions(item.get("permissions"), full=bool(item.get("isAdmin")))})

        attachment_info = {
            "cases": ("case-files", "case"), "correspondence": ("case-files", "case"),
            "documents": ("document-files", "document"), "ledger": ("receipts", "receipt"),
            "beis": ("about-files", "bei"), "routinePlans": ("routine-files", "routine"),
        }
        imported_occurrences = set()
        for collection, kind in KIND_BY_COLLECTION.items():
            for value in data.get(collection, []):
                workspace = workspaces.get(str(value.get("workspaceId")), default_workspace)
                series_id = value.get("recurrenceSeriesId") or value.get("seriesId")
                date_key = "due" if kind == "task" else "date" if kind == "calendar_event" else None
                occurrence_key = (
                    str(workspace.id), kind, str(series_id), str(value.get(date_key))
                ) if series_id and date_key else None
                if occurrence_key and occurrence_key in imported_occurrences:
                    self.stderr.write(
                        f"Doppeltes Serienereignis ausgelassen: {kind} / {series_id} / {value.get(date_key)}"
                    )
                    continue
                folder_category = attachment_info.get(collection)
                filename = value.get("receiptFile") if collection == "ledger" else value.get("attachmentFile")
                attachment = import_file(workspace, filename, *folder_category) if folder_category and filename else None
                creator = users.get(str(value.get("createdByUserId")))
                record = create_record(workspace, kind, value, creator, attachment, record_id=as_uuid(value.get("id")), created_at=as_time(value.get("createdAt")))
                if occurrence_key:
                    imported_occurrences.add(occurrence_key)
                if not record.created_by_name:
                    record.created_by_name = str(value.get("createdByName") or "")[:250]
                    record.save(update_fields=("created_by_name",))

        for workspace_id, profile_data in (data.get("workspaceProfiles") or {}).items():
            profile = WorkspaceProfile.objects.filter(workspace=workspaces.get(str(workspace_id))).first()
            workspace = workspaces.get(str(workspace_id))
            if profile and workspace and profile_data.get("photoFile"):
                profile.photo = import_file(workspace, profile_data.get("photoFile"), "about-files", "profile")
                profile.save(update_fields=("photo",))

        for value in data.get("invitations", []):
            Invitation.objects.create(workspace=workspaces.get(str(value.get("workspaceId"))), token_hash=str(value.get("tokenHash") or uuid.uuid4().hex), display_name=str(value.get("displayName") or "")[:250], email=str(value.get("email") or ""), role=str(value.get("role") or "")[:100], permissions=permissions(value.get("permissions")), expires_at=as_time(value.get("expiresAt")) or timezone.now(), accepted_at=as_time(value.get("acceptedAt")), revoked_at=as_time(value.get("revokedAt")))
        for value in data.get("calendarFeeds", []):
            user = users.get(str(value.get("userId")))
            workspace = workspaces.get(str(value.get("workspaceId")))
            if user and workspace and value.get("tokenHash"):
                CalendarFeed.objects.get_or_create(user=user, workspace=workspace, defaults={"token_hash": value["tokenHash"]})
        for value in data.get("auditLog", []):
            workspace = workspaces.get(str(value.get("workspaceId")), default_workspace)
            AuditEvent.objects.create(workspace=workspace, action=str(value.get("action") or "imported")[:80], entity=str(value.get("entity") or "")[:80], entity_id=str(value.get("entityId") or "")[:100], summary=str(value.get("summary") or "")[:1000], actor=users.get(str(value.get("byUserId"))), actor_name=str(value.get("byName") or "")[:250])
        for value in data.get("developerFeedback", []):
            DeveloperFeedback.objects.create(category=str(value.get("category") or "other")[:100], message=str(value.get("message") or ""), created_by=users.get(str(value.get("createdByUserId"))), created_by_name=str(value.get("createdByName") or "")[:250], workspace_type=str(value.get("workspaceType") or "")[:20], email_sent=bool(value.get("emailSent")))

        self.stdout.write(self.style.SUCCESS(f"Import abgeschlossen: {len(workspaces)} Bereiche, {len(users)} Zugänge und {sum(len(data.get(name, [])) for name in KIND_BY_COLLECTION)} Facheinträge."))
