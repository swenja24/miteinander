import json
import shutil
import tempfile
import uuid
import zipfile
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import FileResponse, JsonResponse
from django.utils import timezone

from core.models import AuditEvent, DeveloperFeedback, Invitation, ManagedFile, Member, Membership, Record, Workspace, WorkspaceOptions, WorkspaceProfile
from core.services import ADMIN_ROLES, COLLECTION_BY_KIND, KIND_BY_COLLECTION, PERMISSION_KEYS, allowed, audit, calendar_items, create_record, create_workspace, ensure_rolling, ensure_workspace_defaults, member_dict, membership_context, parse_json, permissions, profile_dict, record_dict, safe_user, valid_email_or_empty


User = get_user_model()


def _workspace_dict(workspace: Workspace) -> dict:
    return {
        "id": str(workspace.id), "name": workspace.name, "type": workspace.type,
        "personName": workspace.person_name, "status": workspace.status,
        "createdAt": workspace.created_at.isoformat().replace("+00:00", "Z"),
        "deletionAfter": workspace.deletion_after.isoformat() if workspace.deletion_after else None,
    }


def _audit_dict(event: AuditEvent) -> dict:
    return {
        "id": str(event.id), "workspaceId": str(event.workspace_id), "action": event.action,
        "entity": event.entity, "entityId": event.entity_id, "summary": event.summary,
        "at": event.created_at.isoformat().replace("+00:00", "Z"),
        "byUserId": str(event.actor_id) if event.actor_id else "", "byName": event.actor_name,
    }


def data_view(request):
    workspace, membership = membership_context(request)
    if workspace.status in {"paused", "deletion-pending"}:
        return JsonResponse({"error": "Dieser Bereich ist vorübergehend gesperrt."}, status=423)
    ensure_workspace_defaults(workspace)
    ensure_rolling(workspace, "task")
    ensure_rolling(workspace, "calendar_event")
    profile = WorkspaceProfile.objects.select_related("photo", "updated_by").get(workspace=workspace)
    options = WorkspaceOptions.objects.get(workspace=workspace).value
    records = list(Record.objects.filter(workspace=workspace).select_related("attachment", "created_by").order_by("-created_at"))
    grouped = {collection: [] for collection in KIND_BY_COLLECTION}
    for record in records:
        grouped[COLLECTION_BY_KIND[record.kind]].append(record_dict(record))
    memberships = list(Membership.objects.filter(workspace=workspace).select_related("user", "member", "member__photo"))
    visible_members = []
    seen_members = set()
    for current in memberships:
        if current.member and current.member_id not in seen_members:
            visible_members.append(member_dict(current.member))
            seen_members.add(current.member_id)
    if workspace.type == Workspace.Type.PRIVATE:
        for member in Member.objects.filter(home_workspace=workspace).select_related("photo"):
            if member.id not in seen_members:
                visible_members.append(member_dict(member))
                seen_members.add(member.id)
    visible_users = []
    for current in memberships:
        if membership.is_admin:
            visible_users.append(safe_user(current.user, current, self_view=current.user_id == request.user.id))
        elif current.access_status == "active" and current.user.is_active and current.user.access_status == "active":
            visible_users.append({"id": str(current.user_id), "displayName": current.user.label, "username": current.user.username})
    invitation_values = []
    if membership.is_admin:
        for invitation in Invitation.objects.filter(workspace=workspace).order_by("-created_at"):
            invitation_values.append({
                "id": str(invitation.id), "workspaceId": str(workspace.id), "displayName": invitation.display_name,
                "email": invitation.email, "role": invitation.role, "expiresAt": invitation.expires_at.isoformat().replace("+00:00", "Z"),
                "acceptedAt": invitation.accepted_at.isoformat().replace("+00:00", "Z") if invitation.accepted_at else None,
                "revokedAt": invitation.revoked_at.isoformat().replace("+00:00", "Z") if invitation.revoked_at else None,
            })
    permitted = {key: allowed(membership, key) and not (workspace.type == Workspace.Type.SHARED and key in {"cases", "documents", "family"}) for key in PERMISSION_KEYS}
    current_user = safe_user(request.user, membership, self_view=True)
    value = {
        "family": {"name": workspace.name, "person": workspace.person_name},
        "workspace": _workspace_dict(workspace),
        "workspaces": [_workspace_dict(item.workspace) for item in Membership.objects.filter(user=request.user, access_status="active", workspace__status="active").select_related("workspace")],
        "personProfile": profile_dict(profile) if workspace.type == Workspace.Type.PRIVATE else {},
        "goals": grouped["goals"] if workspace.type == Workspace.Type.PRIVATE else [],
        "rules": grouped["rules"] if workspace.type == Workspace.Type.PRIVATE else [],
        "aboutComments": grouped["aboutComments"] if workspace.type == Workspace.Type.PRIVATE else [],
        "importantContacts": grouped["importantContacts"],
        "contactOptions": options.get("contact", {"categories": []}),
        "beis": grouped["beis"] if workspace.type == Workspace.Type.PRIVATE else [],
        "routinePlans": grouped["routinePlans"] if permitted["tasks"] else [],
        "cases": grouped["cases"] if permitted["cases"] and workspace.type == Workspace.Type.PRIVATE else [],
        "correspondence": grouped["correspondence"] if permitted["cases"] and workspace.type == Workspace.Type.PRIVATE else [],
        "tasks": [item for item in grouped["tasks"] if membership.is_admin or not item.get("deletedAt")] if permitted["tasks"] else [],
        "calendarEvents": calendar_items(workspace, membership),
        "calendarFeedActive": request.user.calendar_feeds.filter(workspace=workspace).exists(),
        "taskOptions": options.get("task", {"categories": []}) if permitted["tasks"] else {"categories": []},
        "documents": grouped["documents"] if permitted["documents"] and workspace.type == Workspace.Type.PRIVATE else [],
        "messages": [],
        "announcements": grouped["announcements"],
        "topicComments": [item for item in grouped["topicComments"] if item.get("targetType") == "goal" or item.get("targetType") == "case" and permitted["cases"] or item.get("targetType") == "task" and permitted["tasks"]],
        "ledger": grouped["ledger"] if permitted["ledger"] else [],
        "accounts": grouped["accounts"] if permitted["ledger"] else [],
        "ledgerOptions": options.get("ledger", {"descriptions": [], "categories": []}) if permitted["ledger"] else {"descriptions": [], "categories": []},
        "members": visible_members,
        "currentUser": current_user,
        "users": visible_users,
        "invitations": invitation_values,
        "capabilities": permitted | {"manageAccess": membership.is_admin},
        "emailNotificationsConfigured": bool(settings.EMAIL_HOST),
        "auditLog": [_audit_dict(item) for item in AuditEvent.objects.filter(workspace=workspace)[:100]] if membership.is_admin else [],
        "onboardingDismissed": str(workspace.id) in request.user.onboarding_dismissed,
        "operatorInfo": {"name": settings.DATA_CONTROLLER_NAME, "contact": settings.DATA_CONTROLLER_CONTACT},
    }
    return JsonResponse(value)


def create_workspace_view(request):
    current_workspace, membership = membership_context(request)
    if not membership.is_admin:
        return JsonResponse({"error": "Nur Administrator*innen dürfen gemeinsame Bereiche anlegen."}, status=403)
    payload = parse_json(request)
    if str(payload.get("type") or "shared") != Workspace.Type.SHARED:
        return JsonResponse({"error": "Persönliche Bereiche werden über die Betreiberverwaltung angelegt."}, status=403)
    name = str(payload.get("name") or "").strip()[:250]
    if not name:
        return JsonResponse({"error": "Der Bereich benötigt einen Namen."}, status=400)
    workspace, owner_membership = create_workspace(request.user, name, Workspace.Type.SHARED)
    valid_candidates = Membership.objects.filter(workspace=current_workspace, user_id__in=payload.get("memberUserIds", [])).select_related("user", "member")
    for candidate in valid_candidates:
        if candidate.user_id != request.user.id:
            Membership.objects.get_or_create(workspace=workspace, user=candidate.user, defaults={"member": candidate.member, "role": "WG-Team", "permissions": {"tasks": True, "calendar": True, "ledger": True, "cases": False, "documents": False, "family": False}})
    audit(workspace, request.user, "created", "workspace", workspace.id, f"Bereich „{name}“ angelegt")
    return JsonResponse(_workspace_dict(workspace), status=201)


@transaction.atomic
def workspace_membership_view(request, workspace_id):
    workspace, actor = membership_context(request)
    if workspace.id != workspace_id or not actor.is_admin:
        return JsonResponse({"error": "Bereich nicht gefunden."}, status=404)
    workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
    actor = Membership.objects.select_for_update().filter(pk=actor.pk, access_status="active").first()
    if not actor or not actor.is_admin:
        return JsonResponse({"error": "Keine Administrationsberechtigung."}, status=403)
    payload = parse_json(request)
    target = Membership.objects.select_for_update().filter(workspace=workspace, user_id=payload.get("userId")).select_related("user").first()
    if not target:
        return JsonResponse({"error": "Die Person gehört nicht zu diesem Bereich."}, status=404)
    if request.method == "DELETE":
        if target.user_id == request.user.id:
            return JsonResponse({"error": "Die eigene Mitgliedschaft kann hier nicht entfernt werden."}, status=400)
        target_user = target.user
        target_user_id = target.user_id
        name = target_user.label
        member = target.member
        target.delete()
        if member and not member.memberships.exists() and member.user_id:
            member.user = None
            member.save(update_fields=("user",))
        audit(workspace, request.user, "deleted", "workspace-membership", target_user_id, f"Mitgliedschaft von {name} entfernt")
        if not target_user.is_system_admin and not target_user.workspace_memberships.exists():
            target_user.delete()
        return JsonResponse({"ok": True})
    next_admin = bool(payload.get("isAdmin", target.is_admin))
    if target.is_admin and not next_admin and workspace.memberships.filter(is_admin=True, access_status="active").count() <= 1:
        return JsonResponse({"error": "Die letzte aktive Administration des Bereichs kann nicht entfernt werden."}, status=409)
    target.role = str(payload.get("role") or target.role)[:100]
    target.is_admin = next_admin
    target.permissions = permissions(payload.get("permissions"), full=target.is_admin)
    target.save()
    audit(workspace, request.user, "updated", "workspace-membership", target.user_id, f"Mitgliedschaft für {target.user.label} geändert")
    return JsonResponse(safe_user(target.user, target, self_view=target.user_id == request.user.id))


@transaction.atomic
def user_view(request, user_id=None):
    workspace, actor = membership_context(request)
    if not actor.is_admin:
        return JsonResponse({"error": "Nur Administrator*innen dürfen Zugänge verwalten."}, status=403)
    workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
    actor = Membership.objects.select_for_update().filter(pk=actor.pk, access_status="active").first()
    if not actor or not actor.is_admin:
        return JsonResponse({"error": "Keine Administrationsberechtigung."}, status=403)
    if request.method == "POST" and user_id is None:
        payload = parse_json(request)
        username = str(payload.get("username") or "").strip().lower()
        password = str(payload.get("password") or "")
        if len(username) < 3 or len(username) > 150 or User.objects.filter(username__iexact=username).exists():
            return JsonResponse({"error": "Der Benutzername ist zu kurz oder bereits vergeben."}, status=400)
        if len(password) > 1024:
            return JsonResponse({"error": "Das Passwort ist zu lang."}, status=400)
        display_name = str(payload.get("displayName") or username).strip()[:200]
        email = valid_email_or_empty(payload.get("notificationEmail"))
        candidate = User(username=username, display_name=display_name, email=email)
        try:
            validate_password(password, candidate)
        except ValidationError as error:
            return JsonResponse({"error": " ".join(error.messages)}, status=400)
        role = str(payload.get("role") or "Angehörige")[:100]
        is_admin = role in ADMIN_ROLES
        member = None
        member_id = payload.get("memberId")
        if member_id:
            member = Member.objects.select_for_update().filter(pk=member_id, home_workspace=workspace).first()
            if not member:
                return JsonResponse({"error": "Das ausgewählte Teamprofil wurde nicht gefunden."}, status=400)
            if member.user_id:
                return JsonResponse({"error": "Dieses Teamprofil ist bereits mit einem Zugang verknüpft."}, status=409)
        try:
            with transaction.atomic():
                user = User.objects.create_user(username=username, password=password, display_name=display_name, email=email, notification_email=email)
        except IntegrityError:
            return JsonResponse({"error": "Der Benutzername ist bereits vergeben."}, status=409)
        if member:
            member.user = user
            member.save(update_fields=("user",))
        membership = Membership.objects.create(workspace=workspace, user=user, member=member, role=role, is_admin=is_admin, permissions=permissions(payload.get("permissions"), full=is_admin))
        audit(workspace, request.user, "created", "user", user.id, f"Zugang für {user.label} angelegt")
        return JsonResponse(safe_user(user, membership), status=201)
    target_membership = Membership.objects.select_for_update().filter(workspace=workspace, user_id=user_id).select_related("user").first()
    if not target_membership:
        return JsonResponse({"error": "Zugang nicht gefunden."}, status=404)
    target = target_membership.user
    payload = parse_json(request)
    if request.method == "DELETE":
        if target.id == request.user.id:
            return JsonResponse({"error": "Du kannst deinen eigenen Zugang nicht entfernen."}, status=400)
        target_id = target.id
        target_name = target.label
        member = target_membership.member
        target_membership.delete()
        if member and not member.memberships.exists() and member.user_id == target.id:
            member.user = None
            member.save(update_fields=("user",))
        audit(workspace, request.user, "deleted", "workspace-membership", target_id, f"Zugang von {target_name} aus diesem Bereich entfernt")
        if not target.is_system_admin and not target.workspace_memberships.exists():
            target.delete()
        return JsonResponse({"ok": True})
    if "displayName" in payload or payload.get("password"):
        return JsonResponse(
            {"error": "Persönliche Kontodaten und Passwörter kann nur die Person selbst ändern."},
            status=403,
        )
    role = str(payload.get("role") or target_membership.role)[:100]
    next_admin = role in ADMIN_ROLES
    if target_membership.is_admin and not next_admin and workspace.memberships.filter(is_admin=True, access_status="active").count() <= 1:
        return JsonResponse({"error": "Die letzte aktive Administration des Bereichs kann nicht entfernt werden."}, status=409)
    if payload.get("accessStatus") == "paused" and target.id == request.user.id:
        return JsonResponse({"error": "Du kannst deinen eigenen Zugang zu diesem Bereich nicht pausieren."}, status=400)
    if payload.get("accessStatus") == "paused" and target_membership.is_admin and workspace.memberships.filter(is_admin=True, access_status="active").count() <= 1:
        return JsonResponse({"error": "Die letzte aktive Administration des Bereichs kann nicht pausiert werden."}, status=409)
    target_membership.role = role
    target_membership.is_admin = next_admin
    if "permissions" in payload:
        target_membership.permissions = permissions(payload.get("permissions"), full=target_membership.is_admin)
    if payload.get("accessStatus") in {"active", "paused"}:
        if payload["accessStatus"] == "active" and (target.access_status != "active" or not target.is_active) and not request.user.is_system_admin:
            return JsonResponse({"error": "Dieser Zugang ist installationsweit gesperrt und kann nur durch die Betreiberverwaltung aktiviert werden."}, status=409)
        target_membership.access_status = payload["accessStatus"]
        # Früher importierte Pausen waren kontoweit. Sobald eine Verwaltung
        # mit Betreiberrechten den Bereich wieder aktiviert, wird auch dieser
        # Altstatus aufgehoben.
        if payload["accessStatus"] == "active" and request.user.is_system_admin and (target.access_status != "active" or not target.is_active):
            target.access_status = "active"
            target.is_active = True
    target.save()
    target_membership.save()
    audit(workspace, request.user, "updated", "user", target.id, f"Zugang von {target.label} geändert")
    return JsonResponse(safe_user(target, target_membership, self_view=target.id == request.user.id))


def profile_view(request):
    workspace, membership = membership_context(request)
    if workspace.type != Workspace.Type.PRIVATE or not membership.is_admin:
        return JsonResponse({"error": "Das Personenprofil darf nur von Administrator*innen geändert werden."}, status=403)
    payload = parse_json(request)
    profile, _ = WorkspaceProfile.objects.get_or_create(workspace=workspace)
    mapping = {"introduction": "introduction", "strengths": "strengths", "supportNeeds": "support_needs", "beiSummary": "bei_summary", "wishes": "wishes"}
    for source, target in mapping.items():
        if source in payload:
            setattr(profile, target, str(payload[source])[:5000])
    profile.updated_by = request.user
    profile.save()
    return JsonResponse(profile_dict(profile))


def family_view(request):
    workspace, membership = membership_context(request)
    if not membership.is_admin:
        return JsonResponse({"error": "Keine Berechtigung."}, status=403)
    payload = parse_json(request)
    workspace.name = str(payload.get("name") or workspace.name)[:250]
    workspace.person_name = str(payload.get("person") or workspace.person_name)[:250]
    workspace.save(update_fields=("name", "person_name"))
    audit(workspace, request.user, "updated", "workspace-settings", workspace.id, "Grunddaten des Bereichs geändert")
    return JsonResponse({"name": workspace.name, "person": workspace.person_name})


def onboarding_view(request):
    workspace, membership = membership_context(request)
    values = list(request.user.onboarding_dismissed)
    if str(workspace.id) not in values:
        values.append(str(workspace.id))
        request.user.onboarding_dismissed = values
        request.user.save(update_fields=("onboarding_dismissed",))
    return JsonResponse({"ok": True})


def notification_preferences(request):
    workspace, membership = membership_context(request)
    payload = parse_json(request)
    preference = str(payload.get("notificationPreference") or "mentions")
    if preference not in {"all", "mentions", "none"}:
        return JsonResponse({"error": "Ungültige Benachrichtigungseinstellung."}, status=400)
    request.user.notification_preference = preference
    request.user.notification_email = valid_email_or_empty(payload.get("notificationEmail"))
    request.user.save(update_fields=("notification_preference", "notification_email"))
    return JsonResponse(safe_user(request.user, membership, self_view=True))


def privacy_workspace(request):
    workspace, membership = membership_context(request)
    if not membership.is_admin:
        return JsonResponse({"error": "Nur die Administration dieses Bereichs darf die Löschung anfordern."}, status=403)
    if str(parse_json(request).get("confirmation") or "").strip() != workspace.name:
        return JsonResponse({"error": "Der Bereichsname wurde nicht korrekt bestätigt."}, status=400)
    workspace.deletion_requested_at = timezone.now()
    workspace.deletion_after = timezone.localdate() + timedelta(days=30)
    workspace.status = "deletion-pending"
    workspace.save(update_fields=("deletion_requested_at", "deletion_after", "status"))
    audit(workspace, request.user, "deletion-requested", "workspace", workspace.id, "Löschung des Bereichs mit 30 Tagen Widerrufsfrist angefordert")
    return JsonResponse({"ok": True, "deletionAfter": workspace.deletion_after.isoformat()})


def privacy_export(request, zipped=False):
    workspace, membership = membership_context(request, allow_locked=True)
    records = list(Record.objects.filter(workspace=workspace).select_related("attachment"))
    permissions_by_kind = {
        "case": "cases", "correspondence": "cases", "task": "tasks", "routine_plan": "tasks",
        "calendar_event": "calendar", "document": "documents", "ledger": "ledger", "account": "ledger",
    }
    personal_kinds = {"case", "correspondence", "document", "goal", "rule", "about_comment", "bei", "contact"}
    visible_records = []
    for record in records:
        required = permissions_by_kind.get(record.kind)
        if required and not allowed(membership, required):
            continue
        if workspace.type == Workspace.Type.SHARED and record.kind in personal_kinds:
            continue
        if record.kind == "topic_comment":
            required = {"case": "cases", "task": "tasks"}.get(record.payload.get("targetType"))
            if required and not allowed(membership, required):
                continue
        visible_records.append(record)
    payload = {collection: [] for collection in KIND_BY_COLLECTION}
    for record in visible_records:
        payload[COLLECTION_BY_KIND[record.kind]].append(record_dict(record))
    ensure_workspace_defaults(workspace)
    options = WorkspaceOptions.objects.get(workspace=workspace).value
    visible_options = {"contact": options.get("contact", {"categories": []})}
    if allowed(membership, "tasks"):
        visible_options["task"] = options.get("task", {"categories": []})
    if allowed(membership, "ledger"):
        visible_options["ledger"] = options.get("ledger", {"categories": [], "descriptions": []})
    profile = WorkspaceProfile.objects.select_related("photo", "updated_by").get(workspace=workspace)
    visible_member_models = []
    seen_member_ids = set()
    for current in Membership.objects.filter(workspace=workspace).select_related("member", "member__photo"):
        if current.member and current.member_id not in seen_member_ids:
            visible_member_models.append(current.member)
            seen_member_ids.add(current.member_id)
    if workspace.type == Workspace.Type.PRIVATE:
        for member in Member.objects.filter(home_workspace=workspace).select_related("photo"):
            if member.id not in seen_member_ids:
                visible_member_models.append(member)
                seen_member_ids.add(member.id)
    payload.update({
        "exportedAt": timezone.now().isoformat(), "workspace": _workspace_dict(workspace),
        "profile": profile_dict(profile) if workspace.type == Workspace.Type.PRIVATE else {},
        "options": visible_options,
        "members": [member_dict(member) for member in visible_member_models],
    })
    if not zipped:
        return JsonResponse(payload)
    buffer = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024, mode="w+b")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("miteinander-export.json", json.dumps(payload, ensure_ascii=False, indent=2))
        file_ids = {record.attachment_id for record in visible_records if record.attachment_id}
        if workspace.type == Workspace.Type.PRIVATE and profile.photo_id:
            file_ids.add(profile.photo_id)
        file_ids.update(member.photo_id for member in visible_member_models if member.photo_id)
        for managed in ManagedFile.objects.filter(id__in=file_ids):
            try:
                safe_name = managed.original_name.replace("\\", "/").rsplit("/", 1)[-1]
                with managed.file.open("rb") as source, archive.open(f"dateien/{managed.id}-{safe_name}", "w") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
            except FileNotFoundError:
                continue
    buffer.seek(0)
    response = FileResponse(buffer, as_attachment=True, filename=f"miteinander-{workspace.id}.zip", content_type="application/zip")
    response["Cache-Control"] = "private, no-store"
    return response


def operator_view(request):
    if not request.user.is_system_admin:
        return JsonResponse({"error": "Keine Betreiberberechtigung."}, status=403)
    feedback = [{
        "id": str(item.id), "category": item.category, "message": item.message,
        "createdAt": item.created_at.isoformat().replace("+00:00", "Z"), "createdByName": item.created_by_name,
        "workspaceType": item.workspace_type, "status": item.status, "emailSent": item.email_sent,
    } for item in DeveloperFeedback.objects.all()[:200]]
    areas = [{**_workspace_dict(area), "memberCount": area.memberships.count()} for area in Workspace.objects.all()]
    return JsonResponse({"workspaces": areas, "users": User.objects.count(), "feedback": feedback})


def operator_status(request, workspace_id):
    if not request.user.is_system_admin:
        return JsonResponse({"error": "Keine Betreiberberechtigung."}, status=403)
    workspace = Workspace.objects.filter(pk=workspace_id).first()
    status = str(parse_json(request).get("status") or "")
    if not workspace or status not in {"active", "paused"}:
        return JsonResponse({"error": "Bereich oder Status ungültig."}, status=400)
    workspace.status = status
    if status == "active":
        workspace.deletion_requested_at = None
        workspace.deletion_after = None
    workspace.save(update_fields=("status", "deletion_requested_at", "deletion_after"))
    audit(workspace, request.user, "updated", "workspace-status", workspace.id, f"Bereichsstatus auf {status} gesetzt")
    return JsonResponse({"ok": True})
