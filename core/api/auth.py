import base64
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone

from core.models import CalendarFeed, Invitation, Member, Membership, Workspace
from core.services import ADMIN_ROLES, audit, calendar_ics, create_workspace, ensure_rolling, membership_context, parse_json, permissions, safe_user, valid_email_or_empty, valid_legacy_password, valid_totp


User = get_user_model()


def _register_login_failure(rate_key: str):
    failures = int(cache.get(rate_key, 0)) + 1
    cache.set(rate_key, failures, 900)


def _invitation_dict(invitation: Invitation, token: str | None = None) -> dict:
    value = {
        "id": str(invitation.id),
        "workspaceId": str(invitation.workspace_id) if invitation.workspace_id else "",
        "displayName": invitation.display_name,
        "email": invitation.email,
        "role": invitation.role,
        "permissions": invitation.permissions,
        "createPrivateArea": invitation.create_private_area,
        "areaName": invitation.area_name,
        "expiresAt": invitation.expires_at.isoformat().replace("+00:00", "Z"),
        "createdAt": invitation.created_at.isoformat().replace("+00:00", "Z"),
        "acceptedAt": invitation.accepted_at.isoformat().replace("+00:00", "Z") if invitation.accepted_at else None,
        "revokedAt": invitation.revoked_at.isoformat().replace("+00:00", "Z") if invitation.revoked_at else None,
    }
    if token:
        value["token"] = token
    return value


def login_view(request):
    payload = parse_json(request)
    username = str(payload.get("username") or "").strip().lower()
    password = str(payload.get("password") or "")
    if not username or len(username) > 150 or len(password) > 1024:
        return JsonResponse({"error": "Benutzername oder Passwort stimmen nicht."}, status=401)
    remote = request.META.get("REMOTE_ADDR", "unknown")
    rate_key = f"login-failures:{remote}:{username}"
    user_rate_key = f"login-user-failures:{username}"
    if int(cache.get(rate_key, 0)) >= 10 or int(cache.get(user_rate_key, 0)) >= 50:
        return JsonResponse({"error": "Zu viele fehlgeschlagene Anmeldungen. Bitte versuche es in 15 Minuten erneut."}, status=429)
    user = authenticate(request, username=username, password=password)
    if not user:
        candidate = User.objects.filter(username__iexact=username, is_active=True, access_status="active").first()
        if candidate and candidate.legacy_password_hash and valid_legacy_password(password, candidate.legacy_password_hash):
            candidate.set_password(password)
            candidate.legacy_password_hash = ""
            candidate.save(update_fields=("password", "legacy_password_hash"))
            user = candidate
    if not user or not user.is_active or user.access_status != "active":
        _register_login_failure(rate_key)
        _register_login_failure(user_rate_key)
        return JsonResponse({"error": "Benutzername oder Passwort stimmen nicht."}, status=401)
    if user.totp_enabled and not valid_totp(user.totp_secret, str(payload.get("otp") or "")):
        _register_login_failure(rate_key)
        _register_login_failure(user_rate_key)
        return JsonResponse({"error": "Der Sicherheitscode fehlt oder ist ungültig.", "requiresOtp": True}, status=401)
    login(request, user)
    cache.delete(rate_key)
    cache.delete(user_rate_key)
    return JsonResponse({"ok": True, "user": safe_user(user, self_view=True)})


def logout_view(request):
    logout(request)
    response = JsonResponse({"ok": True})
    response["Clear-Site-Data"] = '"cache", "storage"'
    return response


def account_view(request):
    payload = parse_json(request)
    user = request.user
    display_name = str(payload.get("displayName") or user.label).strip()[:200]
    if not display_name:
        return JsonResponse({"error": "Bitte gib einen Anzeigenamen an."}, status=400)
    new_password = str(payload.get("newPassword") or "")
    if new_password:
        if len(new_password) > 1024:
            return JsonResponse({"error": "Das neue Passwort ist zu lang."}, status=400)
        rate_key = f"account-password-failures:{user.id}"
        if int(cache.get(rate_key, 0)) >= 10:
            return JsonResponse({"error": "Zu viele fehlgeschlagene Versuche. Bitte versuche es in 15 Minuten erneut."}, status=429)
        current_password = str(payload.get("currentPassword") or "")
        if not user.check_password(current_password):
            _register_login_failure(rate_key)
            return JsonResponse({"error": "Das aktuelle Passwort stimmt nicht."}, status=403)
        candidate = User(username=user.username, display_name=display_name, email=user.email)
        try:
            validate_password(new_password, candidate)
        except ValidationError as error:
            return JsonResponse({"error": " ".join(error.messages)}, status=400)
        user.set_password(new_password)
        cache.delete(rate_key)
    user.display_name = display_name
    user.save(update_fields=("display_name", "password") if new_password else ("display_name",))
    Member.objects.filter(user=user).update(name=display_name)
    if new_password:
        update_session_auth_hash(request, user)
    memberships = list(Membership.objects.filter(user=user).select_related("workspace"))
    membership = next((item for item in memberships if item.access_status == "active" and item.workspace.status == "active"), None)
    for current in memberships:
        audit(current.workspace, user, "updated", "own-account", user.id, "Eigene Kontodaten geändert")
    return JsonResponse({"ok": True, "user": safe_user(user, membership, self_view=True)})


def accept_invitation(request, raw_token: str):
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    payload = parse_json(request)
    username = str(payload.get("username") or "").strip().lower()
    password = str(payload.get("password") or "")
    if len(username) < 3 or len(username) > 150 or User.objects.filter(username__iexact=username).exists():
        return JsonResponse({"error": "Der Benutzername ist zu kurz oder bereits vergeben."}, status=400)
    if len(password) > 1024:
        return JsonResponse({"error": "Das Passwort ist zu lang."}, status=400)
    with transaction.atomic():
        invitation = Invitation.objects.select_for_update().filter(token_hash=token_hash).first()
        if not invitation or invitation.accepted_at or invitation.revoked_at or invitation.expires_at <= timezone.now():
            return JsonResponse({"error": "Diese Einladung ist ungültig oder abgelaufen."}, status=410)
        display_name = str(payload.get("displayName") or invitation.display_name or username).strip()[:250]
        email = valid_email_or_empty(payload.get("email") or invitation.email)
        candidate = User(username=username, display_name=display_name, email=email)
        try:
            validate_password(password, candidate)
        except ValidationError as error:
            return JsonResponse({"error": " ".join(error.messages)}, status=400)
        try:
            with transaction.atomic():
                user = User.objects.create_user(username=username, password=password, display_name=display_name, email=email, notification_email=email, notification_preference="mentions", access_status="active")
        except IntegrityError:
            return JsonResponse({"error": "Der Benutzername ist bereits vergeben."}, status=409)
        if invitation.create_private_area:
            workspace, membership = create_workspace(user, invitation.area_name or f"{display_name}s Bereich", Workspace.Type.PRIVATE, display_name)
        else:
            if not invitation.workspace:
                return JsonResponse({"error": "Der eingeladene Bereich existiert nicht mehr."}, status=410)
            is_admin = invitation.role in ADMIN_ROLES
            photo = None
            if payload.get("photoData"):
                from core.services import validate_and_store_file
                photo = validate_and_store_file(invitation.workspace, user, str(payload["photoData"]), str(payload.get("photoName") or "Profilfoto"), "member", images_only=True)
            member = Member.objects.create(home_workspace=invitation.workspace, user=user, name=display_name, role=invitation.role or "Assistenz", email=email, phone=str(payload.get("phone") or "")[:100], personal_words=str(payload.get("personalWords") or "")[:5000], photo=photo)
            membership = Membership.objects.create(workspace=invitation.workspace, user=user, member=member, role=invitation.role or "Assistenz", is_admin=is_admin, permissions=permissions(invitation.permissions, full=is_admin))
        invitation.accepted_at = timezone.now()
        invitation.accepted_by = user
        invitation.save(update_fields=("accepted_at", "accepted_by"))
    login(request, user)
    return JsonResponse({"ok": True, "user": safe_user(user, membership, self_view=True)}, status=201)


def join_invitation(request, raw_token: str):
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with transaction.atomic():
        invitation = Invitation.objects.select_for_update().filter(token_hash=token_hash).first()
        if not invitation or invitation.create_private_area or invitation.accepted_at or invitation.revoked_at or invitation.expires_at <= timezone.now() or not invitation.workspace:
            return JsonResponse({"error": "Diese Einladung ist ungültig oder abgelaufen."}, status=410)
        is_admin = invitation.role in ADMIN_ROLES
        member = getattr(request.user, "member_profile", None)
        Membership.objects.get_or_create(
            workspace=invitation.workspace,
            user=request.user,
            defaults={"member": member, "role": invitation.role or "Mitglied", "is_admin": is_admin, "permissions": permissions(invitation.permissions, full=is_admin)},
        )
        invitation.accepted_at = timezone.now()
        invitation.accepted_by = request.user
        invitation.save(update_fields=("accepted_at", "accepted_by"))
    return JsonResponse({"ok": True, "workspaceId": str(invitation.workspace_id)})


def totp_view(request, action: str):
    user = request.user
    confirm_rate_key = f"totp-confirm-failures:{user.id}"
    disable_rate_key = f"totp-disable-failures:{user.id}"
    if action == "setup" and request.method == "POST":
        if user.totp_enabled:
            return JsonResponse({"error": "Zwei-Faktor-Anmeldung ist bereits aktiv. Schalte sie zuerst mit Passwort und aktuellem Code aus."}, status=409)
        cache.delete(confirm_rate_key)
        user.pending_totp_secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        user.pending_totp_created_at = timezone.now()
        user.save(update_fields=("pending_totp_secret", "pending_totp_created_at"))
        return JsonResponse({"secret": user.pending_totp_secret, "account": user.username})
    if action == "confirm" and request.method == "POST":
        if user.totp_enabled:
            return JsonResponse({"error": "Zwei-Faktor-Anmeldung ist bereits aktiv."}, status=409)
        if int(cache.get(confirm_rate_key, 0)) >= 10:
            return JsonResponse({"error": "Zu viele fehlgeschlagene Versuche. Bitte versuche es in 15 Minuten erneut."}, status=429)
        supplied = str(parse_json(request).get("code") or "")
        if not user.pending_totp_created_at or user.pending_totp_created_at < timezone.now() - timedelta(minutes=15):
            user.pending_totp_secret = ""
            user.pending_totp_created_at = None
            user.save(update_fields=("pending_totp_secret", "pending_totp_created_at"))
            return JsonResponse({"error": "Die Einrichtung ist abgelaufen. Bitte starte sie erneut."}, status=410)
        if not valid_totp(user.pending_totp_secret, supplied):
            _register_login_failure(confirm_rate_key)
            return JsonResponse({"error": "Der Sicherheitscode ist ungültig."}, status=400)
        user.totp_secret = user.pending_totp_secret
        user.pending_totp_secret = ""
        user.pending_totp_created_at = None
        user.totp_enabled = True
        user.save(update_fields=("totp_secret", "pending_totp_secret", "pending_totp_created_at", "totp_enabled"))
        cache.delete(confirm_rate_key)
        return JsonResponse({"ok": True})
    if action == "disable" and request.method == "DELETE":
        if int(cache.get(disable_rate_key, 0)) >= 10:
            return JsonResponse({"error": "Zu viele fehlgeschlagene Versuche. Bitte versuche es in 15 Minuten erneut."}, status=429)
        payload = parse_json(request)
        supplied = str(payload.get("code") or "")
        password = str(payload.get("password") or "")
        if not user.check_password(password) or not valid_totp(user.totp_secret, supplied):
            _register_login_failure(disable_rate_key)
            return JsonResponse({"error": "Passwort und aktueller Sicherheitscode werden zum Ausschalten benötigt."}, status=403)
        user.totp_secret = ""
        user.pending_totp_secret = ""
        user.pending_totp_created_at = None
        user.totp_enabled = False
        user.save(update_fields=("totp_secret", "pending_totp_secret", "pending_totp_created_at", "totp_enabled"))
        cache.delete(disable_rate_key)
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "Methode nicht erlaubt."}, status=405)


def create_invitation(request, *, independent=False):
    workspace = None
    membership = None
    if independent:
        if not request.user.is_system_admin:
            return JsonResponse({"error": "Keine Betreiberberechtigung."}, status=403)
    else:
        workspace, membership = membership_context(request)
        if not membership.is_admin:
            return JsonResponse({"error": "Nur Administrator*innen dürfen Einladungen verwalten."}, status=403)
    payload = parse_json(request)
    role = "Leistungsberechtigte Person" if independent else str(payload.get("role") or "Assistenz")[:100]
    raw_token = secrets.token_urlsafe(32)
    valid_days = max(1, min(30, int(payload.get("validDays") or 7)))
    is_admin = role in ADMIN_ROLES
    invitation = Invitation.objects.create(
        workspace=workspace,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        display_name=str(payload.get("displayName") or "").strip()[:250],
        email=valid_email_or_empty(payload.get("email")),
        role=role,
        permissions=permissions(payload.get("permissions"), full=is_admin),
        create_private_area=independent,
        area_name=str(payload.get("areaName") or "").strip()[:250],
        expires_at=timezone.now() + timedelta(days=valid_days),
        created_by=request.user,
    )
    if workspace:
        audit(workspace, request.user, "created", "invitation", invitation.id, f"Einladung für {invitation.display_name or invitation.email} erstellt")
    return JsonResponse(_invitation_dict(invitation, raw_token), status=201)


def revoke_invitation(request, invitation_id):
    workspace, membership = membership_context(request)
    if not membership.is_admin:
        return JsonResponse({"error": "Nur Administrator*innen dürfen Einladungen verwalten."}, status=403)
    invitation = Invitation.objects.filter(pk=invitation_id, workspace=workspace).first()
    if not invitation:
        return JsonResponse({"error": "Einladung nicht gefunden."}, status=404)
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=("revoked_at",))
    audit(workspace, request.user, "revoked", "invitation", invitation.id, "Einladung widerrufen")
    return JsonResponse({"ok": True})


def calendar_feed(request, *, token=None):
    if token is not None:
        token_hash = hashlib.sha256(token.removesuffix(".ics").encode()).hexdigest()
        feed = CalendarFeed.objects.select_related("workspace", "user").filter(token_hash=token_hash, user__is_active=True, user__access_status="active").first()
        if not feed:
            return JsonResponse({"error": "Kalenderlink ungültig."}, status=404)
        if feed.workspace.status in {"paused", "deletion-pending"}:
            return JsonResponse({"error": "Dieser Bereich ist vorübergehend gesperrt."}, status=423)
        membership = Membership.objects.filter(workspace=feed.workspace, user=feed.user, access_status="active").first()
        if not membership or not (membership.is_admin or membership.permissions.get("calendar")):
            return JsonResponse({"error": "Keine Kalenderberechtigung."}, status=403)
        ensure_rolling(feed.workspace, "task")
        ensure_rolling(feed.workspace, "calendar_event")
        from django.http import HttpResponse

        response = HttpResponse(calendar_ics(feed.workspace, membership), content_type="text/calendar; charset=utf-8")
        response["Content-Disposition"] = "inline; filename=miteinander.ics"
        response["Cache-Control"] = "no-store"
        return response
    workspace, membership = membership_context(request)
    if request.method == "DELETE":
        CalendarFeed.objects.filter(workspace=workspace, user=request.user).delete()
        return JsonResponse({"ok": True})
    if not (membership.is_admin or membership.permissions.get("calendar")):
        return JsonResponse({"error": "Keine Kalenderberechtigung."}, status=403)
    raw_token = secrets.token_urlsafe(32)
    CalendarFeed.objects.update_or_create(workspace=workspace, user=request.user, defaults={"token_hash": hashlib.sha256(raw_token.encode()).hexdigest()})
    base = settings.APP_BASE_URL or request.build_absolute_uri("/").rstrip("/")
    return JsonResponse({"url": f"{base}/api/calendar-feed/{raw_token}.ics"}, status=201)
