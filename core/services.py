from __future__ import annotations

import base64
import calendar
import csv
import hashlib
import hmac
import io
import json
import math
import re
import secrets
import struct
import uuid
import warnings
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from defusedxml import ElementTree as ET
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Sum
from django.http import HttpRequest
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from .models import AuditEvent, ManagedFile, Member, Membership, Record, Workspace, WorkspaceOptions, WorkspaceProfile, default_options, default_permissions


User = get_user_model()
PERMISSION_KEYS = ("cases", "tasks", "calendar", "documents", "ledger", "family")
KIND_BY_COLLECTION = {
    "cases": "case",
    "correspondence": "correspondence",
    "tasks": "task",
    "calendarEvents": "calendar_event",
    "documents": "document",
    "ledger": "ledger",
    "accounts": "account",
    "announcements": "announcement",
    "topicComments": "topic_comment",
    "goals": "goal",
    "rules": "rule",
    "aboutComments": "about_comment",
    "importantContacts": "contact",
    "beis": "bei",
    "routinePlans": "routine_plan",
}
COLLECTION_BY_KIND = {value: key for key, value in KIND_BY_COLLECTION.items()}
ADMIN_ROLES = {"Leistungsberechtigte Person", "Gesetzliche Betreuung", "Enge Angehörige"}


class WorkspaceLockedError(PermissionError):
    pass


def safe_color(value, fallback="#285c4d"):
    candidate = str(value or "")
    return candidate if re.fullmatch(r"#[0-9a-fA-F]{6}", candidate) else fallback


def clean(value, *, _depth=0):
    if _depth > 12:
        raise ValueError("Die Eingabe ist zu tief verschachtelt.")
    if isinstance(value, str):
        return value.strip()[:5000]
    if isinstance(value, dict):
        if len(value) > 250:
            raise ValueError("Die Eingabe enthält zu viele Felder.")
        return {str(key)[:100]: clean(item, _depth=_depth + 1) for key, item in value.items() if key not in {"id", "workspaceId", "createdAt", "updatedAt"}}
    if isinstance(value, list):
        if len(value) > 1000:
            raise ValueError("Die Eingabe enthält zu viele Listeneinträge.")
        return [clean(item, _depth=_depth + 1) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Zahlen müssen endlich sein.")
    return value


def parse_json(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        value = json.loads(
            request.body,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Ungültiger Zahlenwert.")),
        )
    except RecursionError as error:
        raise ValueError("Die Eingabe ist zu tief verschachtelt.") from error
    if not isinstance(value, dict):
        raise ValueError("Die Anfrage muss ein JSON-Objekt enthalten.")
    clean(value)
    return value


def valid_email_or_empty(value) -> str:
    email = str(value or "").strip()
    if not email:
        return ""
    if len(email) > 254:
        raise ValueError("Bitte gib eine gültige E-Mail-Adresse an.")
    try:
        validate_email(email)
    except ValidationError as error:
        raise ValueError("Bitte gib eine gültige E-Mail-Adresse an.") from error
    return email


def permissions(value: dict | None = None, *, full: bool = False) -> dict:
    return {key: True if full else bool((value or {}).get(key)) for key in PERMISSION_KEYS}


def safe_user(user: User, membership: Membership | None = None, *, self_view: bool = False) -> dict:
    """Return an explicit allow-list. Authentication secrets never leave the server."""
    result = {
        "id": str(user.id),
        "username": user.username,
        "displayName": user.label,
        "active": user.is_active,
        "accessStatus": user.access_status,
    }
    if membership:
        result.update({
            "role": membership.role,
            "memberId": str(membership.member_id) if membership.member_id else None,
            "isAdmin": membership.is_admin,
            "accessStatus": membership.access_status,
            "permissions": permissions(membership.permissions, full=membership.is_admin),
        })
    if self_view:
        result.update({
            "notificationEmail": user.notification_email,
            "notificationPreference": user.notification_preference,
            "totpEnabled": user.totp_enabled,
            "isSystemAdmin": user.is_system_admin,
            "onboardingDismissedWorkspaceIds": user.onboarding_dismissed,
        })
    return result


def membership_context(request: HttpRequest, *, allow_locked=False) -> tuple[Workspace, Membership]:
    queryset = Membership.objects.select_related("workspace", "member").filter(user=request.user)
    requested = request.headers.get("X-Workspace-ID", "")
    if requested:
        try:
            membership = queryset.filter(workspace_id=requested, access_status="active").first()
        except ValidationError:
            membership = None
        if not membership:
            raise PermissionError("Du hast keinen aktiven Zugriff auf diesen Bereich.")
    else:
        membership = queryset.filter(access_status="active", workspace__status="active").order_by("created_at").first()
        if not membership:
            membership = queryset.filter(access_status="active").order_by("created_at").first()
    if not membership:
        raise PermissionError("Dir ist derzeit kein aktiver Bereich zugeordnet.")
    if not allow_locked and membership.workspace.status in {"paused", "deletion-pending"}:
        raise WorkspaceLockedError("Dieser Bereich ist vorübergehend gesperrt.")
    return membership.workspace, membership


def allowed(membership: Membership, permission: str) -> bool:
    return membership.is_admin or bool(membership.permissions.get(permission))


def audit(workspace: Workspace, user: User, action: str, entity: str, entity_id: object = "", summary: str = ""):
    AuditEvent.objects.create(
        workspace=workspace,
        action=action,
        entity=entity,
        entity_id=str(entity_id or "")[:100],
        summary=str(clean(summary))[:1000],
        actor=user,
        actor_name=user.label,
    )


def ensure_workspace_defaults(workspace: Workspace):
    WorkspaceProfile.objects.get_or_create(workspace=workspace)
    WorkspaceOptions.objects.get_or_create(workspace=workspace, defaults={"value": default_options()})


def create_workspace(owner: User, name: str, workspace_type: str, person_name: str = "") -> tuple[Workspace, Membership]:
    with transaction.atomic():
        workspace = Workspace.objects.create(name=name, type=workspace_type, person_name=person_name, created_by=owner)
        member = None
        role = "Bereichsverwaltung"
        if workspace_type == Workspace.Type.PRIVATE:
            role = "Leistungsberechtigte Person"
            member = Member.objects.create(home_workspace=workspace, user=owner if not hasattr(owner, "member_profile") else None, name=person_name, role=role, email=owner.notification_email, color="#5a57d9")
        membership = Membership.objects.create(workspace=workspace, user=owner, member=member or getattr(owner, "member_profile", None), role=role, is_admin=True, permissions=permissions(full=True))
        ensure_workspace_defaults(workspace)
        account_names = [f"{person_name}s Barkasse", f"{person_name}s Bargeld-Spardose", "Wohnungs- und Einkommenskonto"] if workspace_type == Workspace.Type.PRIVATE else [f"{name} – Gemeinschaftskasse"]
        for index, account_name in enumerate(account_names):
            create_record(workspace, "account", {"name": account_name, "type": "Bankkonto" if index == 2 else "Bargeld", "color": ("#285c4d", "#5b57c8", "#397a92")[min(index, 2)]}, owner)
    return workspace, membership


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_uuid(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _record_fields(kind: str, payload: dict) -> dict:
    date_key = {"task": "due", "calendar_event": "date", "ledger": "date", "correspondence": "date"}.get(kind)
    amount = None
    if kind == "ledger" and payload.get("amount") not in (None, ""):
        try:
            candidate = Decimal(str(payload["amount"]).replace(",", "."))
            if not candidate.is_finite() or candidate < 0 or candidate >= Decimal("1000000000000"):
                raise InvalidOperation
            amount = candidate.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as error:
            raise ValueError("Der Betrag ist ungültig.") from error
    return {
        "title": str(payload.get("title") or payload.get("description") or payload.get("name") or "")[:500],
        "status": str(payload.get("status") or payload.get("approvalStatus") or "")[:50],
        "event_date": _as_date(payload.get(date_key)) if date_key else None,
        "parent_id": _as_uuid(payload.get("parentCaseId") or payload.get("caseId") or payload.get("targetId")),
        "series_id": _as_uuid(payload.get("recurrenceSeriesId") or payload.get("seriesId")),
        "account_id": _as_uuid(payload.get("accountId")),
        "amount": amount,
    }


def create_record(workspace: Workspace, kind: str, payload: dict, user: User | None, attachment: ManagedFile | None = None, *, record_id=None, created_at=None) -> Record:
    values = clean(payload)
    record = Record.objects.create(
        id=record_id or uuid.uuid4(),
        workspace=workspace,
        kind=kind,
        payload=values,
        attachment=attachment,
        created_by=user,
        created_by_name=(user.label if user else str(values.get("createdByName") or "")),
        **_record_fields(kind, values),
    )
    if created_at:
        Record.objects.filter(pk=record.pk).update(created_at=created_at)
        record.created_at = created_at
    return record


def update_record(record: Record, payload: dict, user: User, attachment: ManagedFile | None = None) -> Record:
    record.payload.update(clean(payload))
    for key, value in _record_fields(record.kind, record.payload).items():
        setattr(record, key, value)
    if attachment:
        record.attachment = attachment
    record.payload["updatedByUserId"] = str(user.id)
    record.save()
    return record


def record_dict(record: Record) -> dict:
    value = dict(record.payload)
    if record.kind == "account":
        value["color"] = safe_color(value.get("color"))
    if record.kind == "correspondence":
        if str(value.get("reminderDays") or "none") not in {"none", "2", "7", "14", "30"}:
            value["reminderDays"] = "none"
        if value.get("deadlineStatus") not in {None, "", "open", "met", "cancelled"}:
            value["deadlineStatus"] = "open"
    value.update({
        "id": str(record.id),
        "workspaceId": str(record.workspace_id),
        "createdAt": record.created_at.isoformat().replace("+00:00", "Z"),
        "createdByUserId": str(record.created_by_id) if record.created_by_id else value.get("createdByUserId", ""),
        "createdByName": record.created_by_name or value.get("createdByName", ""),
    })
    if record.updated_at:
        value["updatedAt"] = record.updated_at.isoformat().replace("+00:00", "Z")
    if record.event_date:
        key = {"task": "due", "calendar_event": "date", "ledger": "date", "correspondence": "date"}.get(record.kind)
        if key:
            value[key] = record.event_date.isoformat()
    if record.series_id:
        value["recurrenceSeriesId" if record.kind in {"task", "calendar_event"} else "seriesId"] = str(record.series_id)
    if record.parent_id:
        key = "parentCaseId" if record.kind == "case" else "caseId" if record.kind == "correspondence" else "targetId"
        value[key] = str(record.parent_id)
    if record.account_id:
        value["accountId"] = str(record.account_id)
    if record.amount is not None:
        value["amount"] = f"{record.amount:.2f}"
    if record.attachment_id:
        if record.kind == "ledger":
            value.update({"receiptFile": str(record.attachment_id), "receiptStatus": value.get("receiptStatus", "available")})
        else:
            value.update({"attachmentFile": str(record.attachment_id), "attachmentName": record.attachment.original_name, "attachmentType": record.attachment.content_type})
    return value


def member_dict(member: Member) -> dict:
    value = {
        "id": str(member.id), "name": member.name, "role": member.role, "email": member.email,
        "phone": member.phone, "address": member.address, "personalWords": member.personal_words,
        "color": safe_color(member.color), "workspaceId": str(member.home_workspace_id) if member.home_workspace_id else "",
        "userId": str(member.user_id) if member.user_id else "",
    }
    if member.photo_id:
        value.update({"photoFile": str(member.photo_id), "photoName": member.photo.original_name})
    return value


def profile_dict(profile: WorkspaceProfile) -> dict:
    value = {
        "introduction": profile.introduction, "strengths": profile.strengths,
        "supportNeeds": profile.support_needs, "beiSummary": profile.bei_summary, "wishes": profile.wishes,
        "updatedAt": profile.updated_at.isoformat().replace("+00:00", "Z"),
        "updatedByName": profile.updated_by.label if profile.updated_by else "",
    }
    if profile.photo_id:
        value.update({"photoFile": str(profile.photo_id), "photoName": profile.photo.original_name, "photoType": profile.photo.content_type})
    return value


def validate_and_store_file(workspace: Workspace, user: User, data_url: str, original_name: str, category: str, *, images_only=False, pdf_only=False) -> ManagedFile:
    if not data_url.startswith("data:") or ";base64," not in data_url:
        raise ValueError("Ungültige Datei.")
    header, encoded = data_url.split(",", 1)
    claimed = header[5:].split(";", 1)[0].lower()
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception as error:
        raise ValueError("Die Datei konnte nicht gelesen werden.") from error
    if not content or len(content) > 5_000_000:
        raise ValueError("Die Datei ist leer oder größer als 5 MB.")
    used_bytes = ManagedFile.objects.filter(workspace=workspace).aggregate(total=Sum("size"))["total"] or 0
    if used_bytes + len(content) > settings.WORKSPACE_STORAGE_LIMIT_BYTES:
        raise ValueError("Der Speicherplatz dieses Bereichs ist ausgeschöpft. Bitte lösche nicht mehr benötigte Dateien oder erhöhe das Speicherlimit.")

    actual = None
    extension = None
    if content.startswith(b"%PDF-"):
        active_pdf_markers = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/OpenAction", b"/AA")
        if b"%%EOF" in content[-2048:] and not any(marker in content for marker in active_pdf_markers):
            actual, extension = "application/pdf", ".pdf"
    else:
        try:
            Image.MAX_IMAGE_PIXELS = 20_000_000
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    image.verify()
                    formats = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}
                    actual, extension = formats.get(image.format, (None, None))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            pass
    if not actual or actual != claimed:
        raise ValueError("Dateityp und Dateiinhalt stimmen nicht überein.")
    if images_only and not actual.startswith("image/"):
        raise ValueError("Hier wird ein Bild benötigt.")
    if pdf_only and actual != "application/pdf":
        raise ValueError("Hier wird eine PDF-Datei benötigt.")
    digest = hashlib.sha256(content).hexdigest()
    normalized_name = str(original_name or "Dokument").replace("\\", "/")
    safe_name = Path(normalized_name).name
    safe_name = "".join(character for character in safe_name if ord(character) >= 32 and ord(character) != 127)[:255] or "Dokument"
    managed = ManagedFile(
        workspace=workspace, category=category, original_name=safe_name,
        content_type=actual, size=len(content), sha256=digest, uploaded_by=user,
    )
    managed.file.save(f"{uuid.uuid4()}{extension}", ContentFile(content), save=False)
    managed.save()
    return managed


def recurring_dates(start_value: str, recurrence: str, until_value: str | None) -> list[str]:
    if recurrence == "once" or not start_value:
        return [start_value] if start_value else []
    current = date.fromisoformat(start_value)
    until = date.fromisoformat(until_value) if until_value else None
    result = []
    while len(result) < 100 and (until is None or current <= until):
        result.append(current.isoformat())
        if until is None and len(result) >= 7:
            break
        current = next_recurring_date(current, recurrence)
    return result


def next_recurring_date(current: date, recurrence: str) -> date:
    if recurrence == "daily":
        return current + timedelta(days=1)
    if recurrence in {"weekly", "biweekly"}:
        return current + timedelta(days=7 if recurrence == "weekly" else 14)
    if recurrence in {"monthly", "quarterly"}:
        month = current.month + (3 if recurrence == "quarterly" else 1)
        year = current.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        return date(year, month, min(current.day, calendar.monthrange(year, month)[1]))
    if recurrence == "yearly":
        year = current.year + 1
        return date(year, current.month, min(current.day, calendar.monthrange(year, current.month)[1]))
    raise ValueError("Unbekannte Wiederholung.")


@transaction.atomic
def ensure_rolling(workspace: Workspace, kind: str):
    Workspace.objects.select_for_update().filter(pk=workspace.pk).exists()
    date_key = "due" if kind == "task" else "date"
    series_ids = Record.objects.filter(workspace=workspace, kind=kind, series_id__isnull=False).values_list("series_id", flat=True).distinct()
    today = timezone.localdate()
    for series_id in series_ids:
        series = list(Record.objects.filter(workspace=workspace, kind=kind, series_id=series_id).order_by("event_date"))
        if not series or series[0].payload.get("recurrenceUntil"):
            continue
        active = [item for item in series if item.event_date and item.event_date >= today and not item.payload.get("deletedAt")]
        if len(active) >= 7:
            continue
        template = series[-1]
        current = template.event_date
        existing = {item.event_date for item in series}
        while current and len(active) < 7:
            current = next_recurring_date(current, template.payload.get("recurrence", "once"))
            if current in existing:
                continue
            payload = dict(template.payload)
            for key in ("deletedAt", "deletedByUserId", "deletedByName", "history"):
                payload.pop(key, None)
            payload[date_key] = current.isoformat()
            payload["generatedAt"] = timezone.now().isoformat()
            if kind == "task" and payload.get("status") not in {"open", "planned"}:
                payload["status"] = "planned"
            created = create_record(workspace, kind, payload, template.created_by)
            created.series_id = series_id
            created.save(update_fields=("series_id",))
            active.append(created)
            existing.add(current)


def totp_code(secret: str, timestamp: float | None = None) -> str:
    key = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
    counter = int((timestamp or timezone.now().timestamp()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def valid_totp(secret: str, supplied: str) -> bool:
    if not secret or not re.fullmatch(r"\d{6}", supplied or ""):
        return False
    stamp = timezone.now().timestamp()
    return any(hmac.compare_digest(totp_code(secret, stamp + step * 30), supplied) for step in (-1, 0, 1))


def valid_legacy_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def ical_escape(value) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def calendar_items(workspace: Workspace, membership: Membership) -> list[dict]:
    if not allowed(membership, "calendar"):
        return []
    items = []
    if allowed(membership, "tasks"):
        for record in Record.objects.filter(workspace=workspace, kind="task").order_by("event_date"):
            item = record_dict(record)
            if item.get("due") and not item.get("deletedAt"):
                items.append({"id": item["id"], "source": "task", "title": item.get("title", "Aufgabe"), "date": item["due"], "allDay": True, "status": item.get("status", ""), "notes": item.get("notes", "")})
    if allowed(membership, "cases") and workspace.type == Workspace.Type.PRIVATE:
        case_names = {str(item.id): item.title for item in Record.objects.filter(workspace=workspace, kind="case")}
        for record in Record.objects.filter(workspace=workspace, kind="correspondence", payload__eventType="deadline"):
            item = record_dict(record)
            if item.get("date"):
                items.append({"id": item["id"], "source": "case", "title": f"Frist: {item.get('subject') or case_names.get(item.get('caseId'), 'Antrag')}", "date": item["date"], "allDay": True, "status": item.get("deadlineStatus", "open"), "notes": item.get("notes", "")})
    for record in Record.objects.filter(workspace=workspace, kind="calendar_event"):
        item = record_dict(record)
        if item.get("date"):
            items.append({"id": item["id"], "source": "event", "title": item.get("title", "Termin"), "date": item["date"], "startTime": item.get("startTime", ""), "endDate": item.get("endDate", ""), "endTime": item.get("endTime", ""), "allDay": bool(item.get("allDay")), "category": item.get("category", ""), "notes": item.get("notes", ""), "recurrence": item.get("recurrence", "once")})
    return sorted(items, key=lambda item: (item.get("date", ""), item.get("startTime", ""), item.get("title", "")))


def calendar_ics(workspace: Workspace, membership: Membership) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Miteinander//Kalender//DE", "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{ical_escape(workspace.name)}"]
    stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
    for item in calendar_items(workspace, membership):
        day = item["date"].replace("-", "")
        lines.extend(["BEGIN:VEVENT", f"UID:{item['source']}-{item['id']}@miteinander", f"DTSTAMP:{stamp}", f"SUMMARY:{ical_escape(item['title'])}"])
        if item.get("allDay") or not item.get("startTime"):
            exclusive_end = (date.fromisoformat(item.get("endDate") or item["date"]) + timedelta(days=1)).strftime("%Y%m%d")
            lines.extend([f"DTSTART;VALUE=DATE:{day}", f"DTEND;VALUE=DATE:{exclusive_end}"])
        else:
            lines.append(f"DTSTART:{day}T{item['startTime'].replace(':', '')}00")
            if item.get("endTime"):
                end_day = (item.get("endDate") or item["date"]).replace("-", "")
                lines.append(f"DTEND:{end_day}T{item['endTime'].replace(':', '')}00")
        if item.get("notes"):
            lines.append(f"DESCRIPTION:{ical_escape(item['notes'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode()


def statement_date(value: str) -> str:
    value = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value[:10], pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def statement_amount(value: str) -> Decimal:
    normalized = str(value or "").strip().replace("€", "").replace(" ", "")
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    return Decimal(normalized).quantize(Decimal("0.01"))


def decode_data_url(data_url: str) -> bytes:
    try:
        content = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except Exception as error:
        raise ValueError("Die Datei konnte nicht gelesen werden.") from error
    if len(content) > 5_000_000:
        raise ValueError("Die Datei darf höchstens 5 MB groß sein.")
    return content


def parse_statement(data_url: str, filename: str) -> list[dict]:
    content = decode_data_url(data_url)
    if filename.lower().endswith((".xml", ".camt", ".camt053")):
        try:
            root = ET.fromstring(content)
        except Exception as error:
            raise ValueError("Die CAMT-/XML-Datei ist nicht lesbar.") from error
        rows = []
        for entry in root.findall(".//{*}Ntry")[:1000]:
            amount_node = entry.find("./{*}Amt")
            try:
                amount = statement_amount(amount_node.text if amount_node is not None else "0")
            except (InvalidOperation, ValueError):
                continue
            if entry.findtext("./{*}CdtDbtInd") == "DBIT":
                amount = -abs(amount)
            booked = entry.findtext("./{*}BookgDt/{*}Dt") or entry.findtext("./{*}ValDt/{*}Dt") or ""
            description = entry.findtext(".//{*}AddtlNtryInf") or entry.findtext(".//{*}Ustrd") or "Bankumsatz"
            payee = entry.findtext(".//{*}RltdPties/{*}Cdtr/{*}Nm") or entry.findtext(".//{*}RltdPties/{*}Dbtr/{*}Nm") or ""
            if statement_date(booked):
                rows.append({"date": statement_date(booked), "amount": amount, "description": clean(description), "payee": clean(payee)})
        return rows
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t,")
    except csv.Error as error:
        raise ValueError("Die CSV-Datei konnte nicht erkannt werden.") from error
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = []
    for source in list(reader)[:1000]:
        normalized = {str(key).strip().lower(): value for key, value in source.items()}
        date_value = next((normalized[key] for key in normalized if any(term in key for term in ("buchungstag", "datum", "date"))), "")
        amount_value = next((normalized[key] for key in normalized if any(term in key for term in ("betrag", "amount", "umsatz"))), "")
        description = next((normalized[key] for key in normalized if any(term in key for term in ("verwendungszweck", "beschreibung", "purpose"))), "Bankumsatz")
        payee = next((normalized[key] for key in normalized if any(term in key for term in ("empfänger", "auftraggeber", "name", "payee"))), "")
        if statement_date(date_value):
            try:
                amount = statement_amount(amount_value)
            except (InvalidOperation, ValueError):
                continue
            rows.append({"date": statement_date(date_value), "amount": amount, "description": clean(description), "payee": clean(payee)})
    return rows


def import_fingerprint(workspace_id, account_id, row: dict) -> str:
    source = "|".join((str(workspace_id), str(account_id), str(row.get("date")), str(row.get("amount")), str(row.get("description")), str(row.get("payee"))))
    return hashlib.sha256(source.encode()).hexdigest()
