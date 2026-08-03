from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import base64
import re
import secrets
import smtplib
import threading
import time
import uuid
import calendar
import csv
import io
import struct
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.getenv("PORT", "3000"))
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_FILE = DATA_DIR / "familie.json"
PUBLIC_DIR = Path(__file__).parent.joinpath("public").resolve()
PASSWORD = os.getenv("APP_PASSWORD", "miteinander")
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "miteinander@localhost")
SMTP_TLS = os.getenv("SMTP_TLS", "true").lower() not in {"0", "false", "no"}
DEVELOPER_CONTACT_EMAIL = os.getenv("DEVELOPER_CONTACT_EMAIL", "")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
MAX_BODY = 8_000_000
AUDIT_RETENTION_DAYS = max(30, int(os.getenv("AUDIT_RETENTION_DAYS", "730")))
RECEIPTS_DIR = DATA_DIR / "receipts"
CASE_FILES_DIR = DATA_DIR / "case-files"
DOCUMENT_FILES_DIR = DATA_DIR / "document-files"
ABOUT_FILES_DIR = DATA_DIR / "about-files"
ROUTINE_FILES_DIR = DATA_DIR / "routine-files"
MEMBER_FILES_DIR = DATA_DIR / "member-files"
COLLECTIONS = {"cases", "correspondence", "tasks", "documents", "messages", "ledger", "members", "accounts", "calendarEvents"}
lock = threading.Lock()
sessions: dict[str, dict] = {}
login_attempts: dict[str, list[float]] = {}
PERMISSION_KEYS = ("cases", "tasks", "calendar", "documents", "ledger", "family")
WORKSPACE_COLLECTIONS = ("cases", "correspondence", "tasks", "calendarEvents", "documents", "ledger", "accounts", "members", "announcements", "topicComments", "goals", "rules", "aboutComments", "importantContacts", "beis", "routinePlans")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def empty_data() -> dict:
    cash_id, savings_id, bank_id = (str(uuid.uuid4()) for _ in range(3))
    return {
        "family": {"name": "Unsere Familie", "person": "Linea", "createdAt": now()},
        "cases": [], "correspondence": [], "tasks": [], "calendarEvents": [], "documents": [], "messages": [], "ledger": [],
        "announcements": [], "topicComments": [],
        "personProfile": {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""},
        "goals": [], "rules": [], "aboutComments": [], "importantContacts": [], "beis": [], "routinePlans": [], "invitations": [], "auditLog": [], "calendarFeeds": [], "developerFeedback": [],
        "workspaceProfiles": {}, "workspaceOptions": {},
        "contactOptions": {"categories": ["Eltern / Familie", "Fahrdienst / Busunternehmen", "WfB / Arbeit", "Ärzt*innen", "Therapie", "Pflege", "Behörde", "Wohnen", "Notfallkontakt", "Sonstiges"]},
        "ledgerOptions": {"descriptions": [], "categories": []},
        "taskOptions": {"categories": []},
        "accounts": [
            {"id": cash_id, "name": "Lineas Barkasse", "type": "Bargeld", "color": "#285c4d"},
            {"id": savings_id, "name": "Lineas Bargeld-Spardose", "type": "Bargeld", "color": "#5b57c8"},
            {"id": bank_id, "name": "Wohnungs- und Einkommenskonto", "type": "Bankkonto", "color": "#397a92"},
        ],
        "members": [
            {"id": str(uuid.uuid4()), "name": "Linea", "role": "Leistungsberechtigte Person", "color": "#5a57d9"},
        ],
    }


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256": return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def full_permissions() -> dict:
    return {key: True for key in PERMISSION_KEYS}


def totp_code(secret: str, timestamp: float | None = None) -> str:
    key = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
    counter = int((timestamp or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 1_000_000
    return f"{value:06d}"


def valid_totp(secret: str, supplied: str) -> bool:
    return any(hmac.compare_digest(totp_code(secret, time.time() + step * 30), supplied.strip()) for step in (-1, 0, 1))


def create_private_area(owner: dict, person_name: str, area_name: str) -> dict:
    workspace = {"id": str(uuid.uuid4()), "name": area_name, "type": "private", "personName": person_name, "createdAt": now(), "createdByUserId": owner["id"]}
    member = {"id": str(uuid.uuid4()), "workspaceId": workspace["id"], "name": person_name, "role": "Leistungsberechtigte Person", "email": owner.get("notificationEmail", ""), "color": "#5a57d9", "createdAt": now()}
    owner.setdefault("workspaceMemberships", []).append({"workspaceId": workspace["id"], "memberId": member["id"], "role": "Leistungsberechtigte Person", "isAdmin": True, "permissions": full_permissions()})
    data["workspaces"].append(workspace); data["members"].append(member)
    data["accounts"].extend([
        {"id": str(uuid.uuid4()), "workspaceId": workspace["id"], "name": f"{person_name}s Barkasse", "type": "Bargeld", "color": "#285c4d", "createdAt": now()},
        {"id": str(uuid.uuid4()), "workspaceId": workspace["id"], "name": f"{person_name}s Bargeld-Spardose", "type": "Bargeld", "color": "#5b57c8", "createdAt": now()},
        {"id": str(uuid.uuid4()), "workspaceId": workspace["id"], "name": "Wohnungs- und Einkommenskonto", "type": "Bankkonto", "color": "#397a92", "createdAt": now()},
    ])
    data.setdefault("workspaceProfiles", {})[workspace["id"]] = {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""}
    data.setdefault("workspaceOptions", {})[workspace["id"]] = {"contact": {"categories": list(data.get("contactOptions", {}).get("categories", []))}, "ledger": {"descriptions": [], "categories": []}, "task": {"categories": []}}
    return workspace


def calendar_items_for(user: dict, workspace_id: str) -> list[dict]:
    if not (user.get("isAdmin") or user.get("permissions", {}).get("calendar")):
        return []
    items = []
    if user.get("isAdmin") or user.get("permissions", {}).get("tasks"):
        items.extend({"id": task["id"], "source": "task", "title": task.get("title", "Aufgabe"), "date": task.get("due", ""), "allDay": True, "status": task.get("status", ""), "notes": task.get("notes", "")} for task in data.get("tasks", []) if task.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id and task.get("due") and not task.get("deletedAt"))
    if user.get("isAdmin") or user.get("permissions", {}).get("cases"):
        case_names = {case["id"]: case.get("title", "Antrag") for case in data.get("cases", []) if case.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id}
        items.extend({"id": entry["id"], "source": "case", "title": f"Frist: {entry.get('subject') or case_names.get(entry.get('caseId'), 'Antrag')}", "date": entry.get("date", ""), "allDay": True, "status": entry.get("deadlineStatus", "open"), "notes": entry.get("notes", "")} for entry in data.get("correspondence", []) if entry.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id and entry.get("eventType") == "deadline" and entry.get("date"))
    items.extend({"id": event["id"], "source": "event", "title": event.get("title", "Termin"), "date": event.get("date", ""), "startTime": event.get("startTime", ""), "endDate": event.get("endDate", ""), "endTime": event.get("endTime", ""), "allDay": bool(event.get("allDay")), "category": event.get("category", ""), "notes": event.get("notes", ""), "recurrence": event.get("recurrence", "once")} for event in data.get("calendarEvents", []) if event.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id and event.get("date"))
    return sorted(items, key=lambda item: (item.get("date", ""), item.get("startTime", ""), item.get("title", "")))


def ical_escape(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def calendar_ics(user: dict, workspace_id: str, workspace_name: str) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Miteinander//Kalender//DE", "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{ical_escape(workspace_name)}"]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for item in calendar_items_for(user, workspace_id):
        day = item["date"].replace("-", ""); end_day = (item.get("endDate") or item["date"]).replace("-", "")
        lines.extend(["BEGIN:VEVENT", f"UID:{item['source']}-{item['id']}@miteinander", f"DTSTAMP:{stamp}", f"SUMMARY:{ical_escape(item['title'])}"])
        if item.get("allDay") or not item.get("startTime"):
            exclusive_end = (date.fromisoformat(item.get("endDate") or item["date"]) + timedelta(days=1)).strftime("%Y%m%d")
            lines.extend([f"DTSTART;VALUE=DATE:{day}", f"DTEND;VALUE=DATE:{exclusive_end}"])
        else:
            lines.append(f"DTSTART:{day}T{item['startTime'].replace(':', '')}00")
            if item.get("endTime"): lines.append(f"DTEND:{end_day}T{item['endTime'].replace(':', '')}00")
        if item.get("notes"): lines.append(f"DESCRIPTION:{ical_escape(item['notes'])}")
        lines.extend(["END:VEVENT"])
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def load_data() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    CASE_FILES_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENT_FILES_DIR.mkdir(parents=True, exist_ok=True)
    ABOUT_FILES_DIR.mkdir(parents=True, exist_ok=True)
    ROUTINE_FILES_DIR.mkdir(parents=True, exist_ok=True)
    MEMBER_FILES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        loaded = json.loads(DATA_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        loaded = empty_data()
    changed = False
    for key, default in {
        "personProfile": {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""},
        "goals": [], "rules": [], "aboutComments": [], "importantContacts": [], "beis": [], "routinePlans": [], "invitations": [], "calendarEvents": [], "calendarFeeds": [], "developerFeedback": [],
        "announcements": [], "topicComments": [], "auditLog": [],
        "workspaceProfiles": {}, "workspaceOptions": {},
        "contactOptions": {"categories": ["Eltern / Familie", "Fahrdienst / Busunternehmen", "WfB / Arbeit", "Ärzt*innen", "Therapie", "Pflege", "Behörde", "Wohnen", "Notfallkontakt", "Sonstiges"]},
    }.items():
        if key not in loaded:
            loaded[key] = default
            changed = True
    if "correspondence" not in loaded:
        loaded["correspondence"] = []
        changed = True
    if loaded.get("family", {}).get("person") == "Alex":
        loaded["family"]["person"] = "Linea"
        for member in loaded.get("members", []):
            if member.get("name") == "Alex" and member.get("role") == "Leistungsberechtigte Person":
                member["name"] = "Linea"
        changed = True
    if not loaded.get("accounts"):
        defaults = empty_data()["accounts"]
        loaded["accounts"] = defaults
        for entry in loaded.get("ledger", []):
            entry.setdefault("accountId", defaults[0]["id"])
        changed = True
    if "ledgerOptions" not in loaded:
        loaded["ledgerOptions"] = {
            "descriptions": sorted({str(entry.get("description", "")).strip() for entry in loaded.get("ledger", []) if entry.get("description")}),
            "categories": sorted({str(entry.get("category", "")).strip() for entry in loaded.get("ledger", []) if entry.get("category")}),
        }
        changed = True
    if "taskOptions" not in loaded:
        loaded["taskOptions"] = {"categories": sorted({str(task.get("category", "")).strip() for task in loaded.get("tasks", []) if task.get("category")})}
        changed = True
    if not loaded.get("users"):
        person = loaded.get("family", {}).get("person") or "Linea"
        member = next((m for m in loaded.get("members", []) if m.get("role") == "Leistungsberechtigte Person"), None)
        loaded["users"] = [{
            "id": str(uuid.uuid4()), "username": "linea", "displayName": person,
            "role": "Leistungsberechtigte Person", "memberId": member.get("id") if member else None,
            "isAdmin": True, "active": True, "permissions": full_permissions(),
            "passwordHash": hash_password(PASSWORD), "createdAt": now(),
        }]
        changed = True
    for existing_user in loaded.get("users", []):
        if "accessStatus" not in existing_user:
            existing_user["accessStatus"] = "active" if existing_user.get("active", True) else "deactivated"
            changed = True
        permissions = existing_user.setdefault("permissions", {})
        if "calendar" not in permissions:
            permissions["calendar"] = bool(existing_user.get("isAdmin") or permissions.get("tasks")); changed = True
        for membership in existing_user.get("workspaceMemberships", []):
            member_permissions = membership.setdefault("permissions", {})
            if "calendar" not in member_permissions:
                member_permissions["calendar"] = bool(membership.get("isAdmin") or member_permissions.get("tasks")); changed = True
    if not loaded.get("workspaces"):
        workspace_id = str(uuid.uuid4())
        loaded["workspaces"] = [{"id": workspace_id, "name": loaded.get("family", {}).get("name") or "Lineas Bereich", "type": "private", "personName": loaded.get("family", {}).get("person") or "Linea", "createdAt": now()}]
        for key in WORKSPACE_COLLECTIONS:
            for item in loaded.get(key, []): item.setdefault("workspaceId", workspace_id)
        for existing_user in loaded.get("users", []):
            existing_user["workspaceMemberships"] = [{"workspaceId": workspace_id, "role": existing_user.get("role", "Assistenz"), "isAdmin": bool(existing_user.get("isAdmin")), "permissions": existing_user.get("permissions", {})}]
        changed = True
    default_workspace_id = loaded["workspaces"][0]["id"]
    profiles = loaded.setdefault("workspaceProfiles", {})
    options_by_workspace = loaded.setdefault("workspaceOptions", {})
    if default_workspace_id not in profiles:
        profiles[default_workspace_id] = loaded.get("personProfile", {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""}); changed = True
    if default_workspace_id not in options_by_workspace:
        options_by_workspace[default_workspace_id] = {"contact": loaded.get("contactOptions", {"categories": []}), "ledger": loaded.get("ledgerOptions", {"descriptions": [], "categories": []}), "task": loaded.get("taskOptions", {"categories": []})}; changed = True
    for existing_workspace in loaded["workspaces"]:
        if existing_workspace.get("type") == "private" and existing_workspace["id"] not in profiles: profiles[existing_workspace["id"]] = {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""}; changed = True
        if existing_workspace["id"] not in options_by_workspace: options_by_workspace[existing_workspace["id"]] = {"contact": {"categories": []}, "ledger": {"descriptions": [], "categories": []}, "task": {"categories": []}}; changed = True
    for existing_user in loaded.get("users", []):
        if not existing_user.get("workspaceMemberships"):
            existing_user["workspaceMemberships"] = [{"workspaceId": default_workspace_id, "role": existing_user.get("role", "Assistenz"), "isAdmin": bool(existing_user.get("isAdmin")), "permissions": existing_user.get("permissions", {})}]
            changed = True
        for membership in existing_user.get("workspaceMemberships", []):
            if not membership.get("memberId") and existing_user.get("memberId"): membership["memberId"] = existing_user["memberId"]; changed = True
    if loaded.get("users") and not any(user.get("isSystemAdmin") for user in loaded["users"]): loaded["users"][0]["isSystemAdmin"] = True; changed = True
    deletion_due = {area["id"] for area in loaded.get("workspaces", []) if area.get("deletionAfter") and area["deletionAfter"] < date.today().isoformat()}
    if deletion_due:
        file_locations = [
            ("ledger", "receiptFile", RECEIPTS_DIR), ("cases", "attachmentFile", CASE_FILES_DIR),
            ("correspondence", "attachmentFile", CASE_FILES_DIR), ("documents", "attachmentFile", DOCUMENT_FILES_DIR),
            ("beis", "attachmentFile", ABOUT_FILES_DIR), ("routinePlans", "attachmentFile", ROUTINE_FILES_DIR),
            ("members", "photoFile", MEMBER_FILES_DIR),
        ]
        for collection, field, directory in file_locations:
            for item in loaded.get(collection, []):
                if item.get("workspaceId", default_workspace_id) in deletion_due and item.get(field): (directory / Path(item[field]).name).unlink(missing_ok=True)
        for area_id in deletion_due:
            profile_file = loaded.get("workspaceProfiles", {}).get(area_id, {}).get("photoFile")
            if profile_file: (ABOUT_FILES_DIR / Path(profile_file).name).unlink(missing_ok=True)
        loaded["workspaces"] = [area for area in loaded["workspaces"] if area["id"] not in deletion_due]
        for collection in WORKSPACE_COLLECTIONS: loaded[collection] = [item for item in loaded.get(collection, []) if item.get("workspaceId", default_workspace_id) not in deletion_due]
        for existing_user in loaded.get("users", []): existing_user["workspaceMemberships"] = [membership for membership in existing_user.get("workspaceMemberships", []) if membership.get("workspaceId") not in deletion_due]
        for area_id in deletion_due: loaded.get("workspaceProfiles", {}).pop(area_id, None); loaded.get("workspaceOptions", {}).pop(area_id, None)
        changed = True
    audit_cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - AUDIT_RETENTION_DAYS * 86400))
    retained_audit = [item for item in loaded.get("auditLog", []) if item.get("at", "") >= audit_cutoff]
    if len(retained_audit) != len(loaded.get("auditLog", [])): loaded["auditLog"] = retained_audit; changed = True
    migrations = loaded.setdefault("schemaMigrations", [])
    if "remove-shared-private-placeholders-v1" not in migrations:
        shared_account = next((account for account in loaded.get("accounts", []) if account.get("name") == "Gemeinsame WG-Barkasse" and account.get("workspaceId", default_workspace_id) == default_workspace_id), None)
        if shared_account:
            if any(entry.get("accountId") == shared_account["id"] for entry in loaded.get("ledger", [])):
                shared_account["name"] = "Bisherige WG-Barkasse (bitte zuordnen)"
            else:
                loaded["accounts"].remove(shared_account)
        placeholder = next((member for member in loaded.get("members", []) if member.get("name") == "Familie" and member.get("role") == "Angehörige"), None)
        if placeholder:
            referenced = any(user.get("memberId") == placeholder["id"] for user in loaded.get("users", [])) or any(task.get("assignee") == placeholder["id"] for task in loaded.get("tasks", []))
            if referenced: placeholder["name"] = "Angehörige Person (bitte benennen)"
            else: loaded["members"].remove(placeholder)
        migrations.append("remove-shared-private-placeholders-v1"); changed = True
    if changed or not DATA_FILE.exists():
        save_data(loaded)
    return loaded


def save_data(value: dict) -> None:
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(DATA_FILE)


data = load_data()


def save_receipt(data_url: str) -> str:
    if not data_url.startswith("data:image/") or ";base64," not in data_url:
        raise ValueError("Ungültiges Belegbild")
    header, encoded = data_url.split(",", 1)
    media_type = header[5:].split(";", 1)[0]
    extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if media_type not in extensions:
        raise ValueError("Unterstützt werden JPG, PNG und WebP")
    content = base64.b64decode(encoded, validate=True)
    if len(content) > 5_000_000:
        raise ValueError("Das Belegbild ist zu groß")
    filename = f"{uuid.uuid4()}{extensions[media_type]}"
    (RECEIPTS_DIR / filename).write_bytes(content)
    return filename


def save_uploaded_file(data_url: str, original_name: str, directory: Path) -> dict:
    if not data_url.startswith("data:") or ";base64," not in data_url:
        raise ValueError("Ungültige Datei")
    header, encoded = data_url.split(",", 1)
    media_type = header[5:].split(";", 1)[0]
    extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
    if media_type not in extensions:
        raise ValueError("Unterstützt werden JPG, PNG, WebP und PDF")
    content = base64.b64decode(encoded, validate=True)
    if len(content) > 5_000_000:
        raise ValueError("Die Datei ist zu groß (maximal 5 MB)")
    filename = f"{uuid.uuid4()}{extensions[media_type]}"
    (directory / filename).write_bytes(content)
    return {"attachmentFile": filename, "attachmentName": clean(original_name) or "Dokument", "attachmentType": media_type}


def save_case_file(data_url: str, original_name: str = "Dokument") -> dict:
    return save_uploaded_file(data_url, original_name, CASE_FILES_DIR)


def save_document_file(data_url: str, original_name: str = "Dokument") -> dict:
    return save_uploaded_file(data_url, original_name, DOCUMENT_FILES_DIR)


def save_about_file(data_url: str, original_name: str = "Datei") -> dict:
    return save_uploaded_file(data_url, original_name, ABOUT_FILES_DIR)


def save_routine_file(data_url: str, original_name: str = "Ablaufplan") -> dict:
    return save_uploaded_file(data_url, original_name, ROUTINE_FILES_DIR)


def save_member_photo(data_url: str, original_name: str = "Profilfoto") -> dict:
    saved = save_uploaded_file(data_url, original_name, MEMBER_FILES_DIR)
    if not saved["attachmentType"].startswith("image/"):
        (MEMBER_FILES_DIR / saved["attachmentFile"]).unlink(missing_ok=True)
        raise ValueError("Als Profilfoto wird ein Bild benötigt")
    return {"photoFile": saved["attachmentFile"], "photoName": saved["attachmentName"]}


def decode_statement(data_url: str) -> bytes:
    if "," not in data_url: raise ValueError("Die Kontoauszugsdatei ist ungültig.")
    try: content = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except Exception as error: raise ValueError("Die Kontoauszugsdatei konnte nicht gelesen werden.") from error
    if len(content) > 5_000_000: raise ValueError("Der Kontoauszug darf höchstens 5 MB groß sein.")
    return content


def statement_date(value: str) -> str:
    value = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y"):
        try: return time.strftime("%Y-%m-%d", time.strptime(value[:10], pattern))
        except ValueError: pass
    return ""


def statement_amount(value: str) -> float:
    raw = str(value or "").strip().replace("EUR", "").replace("€", "").replace(" ", "")
    if "," in raw: raw = raw.replace(".", "").replace(",", ".")
    try: return round(float(raw), 2)
    except ValueError: raise ValueError(f"Betrag „{value}“ konnte nicht gelesen werden.")


def parse_csv_statement(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace"); sample = "\n".join(text.splitlines()[:8])
    try: dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error: dialect = csv.excel; dialect.delimiter = ";"
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    if not rows or not rows[0]: raise ValueError("Die CSV-Datei enthält keine erkennbaren Tabellenzeilen.")
    aliases = {"date": ("buchungstag", "buchungsdatum", "datum", "valutadatum", "wertstellung"), "amount": ("betrag", "umsatz", "betrag eur", "umsatz eur"), "description": ("verwendungszweck", "buchungstext", "beschreibung", "text"), "payee": ("beguenstigter/zahlungspflichtiger", "begünstigter/zahlungspflichtiger", "zahlungspartner", "empfaenger", "empfänger", "name")}
    normalized = {str(header or "").strip().lower(): header for header in rows[0]}
    fields = {key: next((normalized[name] for name in names if name in normalized), None) for key, names in aliases.items()}
    if not fields["date"] or not fields["amount"]: raise ValueError("Benötigt werden mindestens eine Datums- und eine Betragsspalte.")
    parsed = []
    for row in rows:
        day = statement_date(row.get(fields["date"], ""))
        if not day: continue
        amount = statement_amount(row.get(fields["amount"], ""))
        description = clean(str(row.get(fields["description"], "") if fields["description"] else "")) or "Bankumsatz"
        payee = clean(str(row.get(fields["payee"], "") if fields["payee"] else ""))
        parsed.append({"date": day, "amount": amount, "description": description, "payee": payee})
    if not parsed: raise ValueError("Es wurden keine Buchungen mit gültigem Datum gefunden.")
    return parsed


def xml_text(node: ET.Element, path: str) -> str:
    found = node.find(path); return (found.text or "").strip() if found is not None else ""


def parse_camt_statement(content: bytes) -> list[dict]:
    try: root = ET.fromstring(content)
    except ET.ParseError as error: raise ValueError("Die CAMT-Datei ist kein gültiges XML-Dokument.") from error
    parsed = []
    for entry in root.findall(".//{*}Ntry"):
        day = statement_date(xml_text(entry, "./{*}BookgDt/{*}Dt") or xml_text(entry, "./{*}ValDt/{*}Dt")); amount_text = xml_text(entry, "./{*}Amt")
        if not day or not amount_text: continue
        amount = statement_amount(amount_text); amount = -abs(amount) if xml_text(entry, "./{*}CdtDbtInd") == "DBIT" else abs(amount)
        purpose = xml_text(entry, ".//{*}RmtInf/{*}Ustrd") or xml_text(entry, "./{*}AddtlNtryInf") or "Bankumsatz"
        payee = xml_text(entry, ".//{*}RltdPties/{*}Cdtr/{*}Nm") or xml_text(entry, ".//{*}RltdPties/{*}Dbtr/{*}Nm")
        parsed.append({"date": day, "amount": amount, "description": clean(purpose), "payee": clean(payee)})
    if not parsed: raise ValueError("Die CAMT-Datei enthält keine lesbaren Buchungen.")
    return parsed


def parse_statement(data_url: str, filename: str) -> list[dict]:
    content = decode_statement(data_url)
    return parse_camt_statement(content) if filename.lower().endswith((".xml", ".camt", ".camt053")) or content.lstrip().startswith(b"<") else parse_csv_statement(content)


def import_fingerprint(workspace_id: str, account_id: str, row: dict) -> str:
    source = "|".join([workspace_id, account_id, row["date"], f"{row['amount']:.2f}", row["description"].strip().lower(), row.get("payee", "").strip().lower()])
    return hashlib.sha256(source.encode()).hexdigest()


def clean(value):
    if isinstance(value, str):
        return value.strip()[:5000]
    if isinstance(value, dict):
        return {str(k)[:100]: clean(v) for k, v in value.items() if k not in {"id", "createdAt", "updatedAt"}}
    if isinstance(value, list):
        return [clean(v) for v in value[:1000]]
    return value


def audit(workspace_id: str, user: dict, action: str, entity: str, entity_id: str = "", summary: str = "") -> None:
    data.setdefault("auditLog", []).insert(0, {"id": str(uuid.uuid4()), "workspaceId": workspace_id, "action": action, "entity": entity, "entityId": entity_id, "summary": clean(summary), "at": now(), "byUserId": user["id"], "byName": user["displayName"]})
    del data["auditLog"][2000:]


def recurring_dates(start_value: str, recurrence: str, until_value: str | None) -> list[str]:
    """Return the initial due date plus future occurrences, capped for safety."""
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
    if recurrence == "daily": return current + timedelta(days=1)
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
    raise ValueError("Unbekannte Wiederholung")


def ensure_rolling_tasks(value: dict) -> bool:
    """Keep seven non-past occurrences for every open-ended recurring series."""
    today_value = date.today().isoformat()
    series_ids = {task.get("recurrenceSeriesId") for task in value.get("tasks", []) if task.get("recurrenceSeriesId") and not task.get("recurrenceUntil")}
    changed = False
    for series_id in series_ids:
        series = [task for task in value["tasks"] if task.get("recurrenceSeriesId") == series_id]
        active = [task for task in series if not task.get("deletedAt") and task.get("due", "") >= today_value]
        if not series or len(active) >= 7:
            continue
        template = max(series, key=lambda task: task.get("due", ""))
        current = date.fromisoformat(template["due"])
        existing_dates = {task.get("due") for task in series}
        while len(active) < 7:
            current = next_recurring_date(current, template.get("recurrence", "once"))
            if current.isoformat() in existing_dates:
                continue
            occurrence = {key: clean(item) for key, item in template.items() if key not in {"id", "due", "history", "deletedAt", "deletedByUserId", "deletedByName", "updatedAt", "updatedByUserId"}}
            occurrence.update({"id": str(uuid.uuid4()), "due": current.isoformat(), "status": template.get("status") if template.get("status") in {"open", "planned"} else "planned", "history": [], "createdAt": now(), "generatedAt": now()})
            value["tasks"].append(occurrence)
            active.append(occurrence)
            existing_dates.add(occurrence["due"])
            changed = True
    return changed


def ensure_rolling_calendar_events(value: dict) -> bool:
    today_value = date.today().isoformat(); changed = False
    series_ids = {item.get("recurrenceSeriesId") for item in value.get("calendarEvents", []) if item.get("recurrenceSeriesId") and not item.get("recurrenceUntil")}
    for series_id in series_ids:
        series = [item for item in value["calendarEvents"] if item.get("recurrenceSeriesId") == series_id]
        active = [item for item in series if item.get("date", "") >= today_value]
        if not series or len(active) >= 7: continue
        template = max(series, key=lambda item: item.get("date", "")); current = date.fromisoformat(template["date"]); existing_dates = {item.get("date") for item in series}
        duration = (date.fromisoformat(template.get("endDate") or template["date"]) - current).days
        while len(active) < 7:
            current = next_recurring_date(current, template.get("recurrence", "once"))
            if current.isoformat() in existing_dates: continue
            occurrence = {key: clean(item) for key, item in template.items() if key not in {"id", "date", "endDate", "createdAt", "updatedAt"}}
            occurrence.update({"id": str(uuid.uuid4()), "date": current.isoformat(), "createdAt": now(), "generatedAt": now()})
            if template.get("endDate"): occurrence["endDate"] = (current + timedelta(days=duration)).isoformat()
            value["calendarEvents"].append(occurrence); active.append(occurrence); existing_dates.add(current.isoformat()); changed = True
    return changed


def sync_deadline_reminder(entry: dict, user: dict) -> None:
    task_id = entry.get("reminderTaskId")
    existing = next((task for task in data.get("tasks", []) if task.get("id") == task_id), None)
    days = str(entry.get("reminderDays") or "none")
    if entry.get("eventType") != "deadline" or days == "none" or not entry.get("date") or entry.get("deadlineStatus") in {"met", "cancelled"}:
        if existing:
            data["tasks"].remove(existing)
        entry.pop("reminderTaskId", None)
        return
    application = next((case for case in data.get("cases", []) if case.get("id") == entry.get("caseId")), {})
    reminder_date = date.fromisoformat(entry["date"]) - timedelta(days=int(days))
    due = max(reminder_date, date.today()).isoformat()
    values = {
        "title": f"Frist: {entry.get('subject') or 'Antrag prüfen'} – {application.get('title') or 'Antrag'}",
        "category": "Anträge", "assignee": application.get("assignee") or "", "due": due,
        "status": "planned", "recurrence": "once",
        "notes": f"Fristdatum: {entry['date']}\n{entry.get('source') or 'Frist aus der Antragsakte'}\n{entry.get('notes') or ''}".strip(),
        "caseDeadlineId": entry["id"], "updatedAt": now(),
    }
    if existing:
        existing.update(values)
    else:
        task = {"id": str(uuid.uuid4()), **values, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"], "history": []}
        data["tasks"].insert(0, task)
        entry["reminderTaskId"] = task["id"]


def mentioned_user_ids(text: str, workspace_id: str | None = None) -> list[str]:
    lowered = text.lower()
    return [user["id"] for user in data.get("users", []) if user.get("username") and (not workspace_id or any(m.get("workspaceId") == workspace_id for m in user.get("workspaceMemberships", []))) and f"@{user['username'].lower()}" in lowered]


def send_notification_email(recipient: str) -> None:
    if not SMTP_HOST or not recipient:
        return
    message = EmailMessage()
    message["Subject"] = "Neue Information bei Miteinander"
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message.set_content("Du hast bei Miteinander eine neue Information erhalten.\n\n" + (f"Öffnen: {APP_BASE_URL}\n\n" if APP_BASE_URL else "") + "Aus Datenschutzgründen enthält diese E-Mail keine weiteren Details.")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as client:
            if SMTP_TLS: client.starttls()
            if SMTP_USER: client.login(SMTP_USER, SMTP_PASSWORD)
            client.send_message(message)
    except Exception as error:
        print(f"E-Mail-Benachrichtigung fehlgeschlagen: {error}")


def send_feedback_email(feedback: dict) -> None:
    if not SMTP_HOST or not DEVELOPER_CONTACT_EMAIL: return
    message = EmailMessage(); message["Subject"] = f"Miteinander-Feedback: {feedback['category']}"; message["From"] = SMTP_FROM; message["To"] = DEVELOPER_CONTACT_EMAIL
    message.set_content(f"Neuer Verbesserungswunsch von {feedback['createdByName']}\n\nKategorie: {feedback['category']}\n\n{feedback['message']}\n\nIn der Betreiberverwaltung kann das Feedback gesammelt eingesehen werden.")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as client:
            if SMTP_TLS: client.starttls()
            if SMTP_USER: client.login(SMTP_USER, SMTP_PASSWORD)
            client.send_message(message)
    except Exception as error: print(f"Feedback-E-Mail fehlgeschlagen: {error}")


def notify_users(author_id: str, mentions: list[str], notify_all: bool = False, workspace_id: str | None = None) -> None:
    for recipient in data.get("users", []):
        if workspace_id and not any(m.get("workspaceId") == workspace_id for m in recipient.get("workspaceMemberships", [])): continue
        if recipient["id"] == author_id or not recipient.get("active", True) or recipient.get("accessStatus", "active") != "active": continue
        preference = recipient.get("notificationPreference", "mentions")
        should_notify = recipient["id"] in mentions or notify_all and preference == "all"
        if should_notify and preference != "none" and recipient.get("notificationEmail"):
            threading.Thread(target=send_notification_email, args=(recipient["notificationEmail"],), daemon=True).start()


class Handler(SimpleHTTPRequestHandler):
    server_version = "Miteinander/0.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, status: int, payload: dict, headers: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_calendar(self, body: bytes):
        self.send_response(200); self.send_header("Content-Type", "text/calendar; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Disposition", "inline; filename=miteinander.ics"); self.end_headers(); self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("Anfrage zu groß")
        return json.loads(self.rfile.read(length) or b"{}")

    def session_user(self) -> dict | None:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        item = jar.get("miteinander_session")
        if not item:
            return None
        session = sessions.get(item.value)
        if not session or session["expires"] < time.time():
            sessions.pop(item.value, None)
            return None
        return next((u for u in data.get("users", []) if u["id"] == session["userId"] and u.get("active", True) and u.get("accessStatus", "active") == "active"), None)

    @staticmethod
    def allowed(user: dict, permission: str) -> bool:
        return bool(user.get("isAdmin") or user.get("permissions", {}).get(permission))

    @staticmethod
    def public_user(user: dict) -> dict:
        return {key: value for key, value in user.items() if key != "passwordHash"}

    def workspace_context(self, base_user: dict) -> tuple[str, dict, dict]:
        memberships = base_user.get("workspaceMemberships", [])
        requested = self.headers.get("X-Workspace-ID", "")
        membership = next((item for item in memberships if item.get("workspaceId") == requested), None) if requested else (memberships[0] if memberships else None)
        if not membership: raise PermissionError("Kein Bereich zugeordnet.")
        workspace = next((item for item in data.get("workspaces", []) if item["id"] == membership["workspaceId"]), None)
        if not workspace: raise PermissionError("Bereich nicht gefunden.")
        contextual = {**base_user, "role": membership.get("role", base_user.get("role")), "memberId": membership.get("memberId", base_user.get("memberId")), "isAdmin": bool(membership.get("isAdmin")), "permissions": membership.get("permissions", {})}
        return workspace["id"], workspace, contextual

    def api(self, method: str, path: str):
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        feed_parts = path.strip("/").split("/")
        if len(feed_parts) == 3 and feed_parts[:2] == ["api", "calendar-feed"] and method == "GET":
            raw_token = feed_parts[2].removesuffix(".ics"); token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            feed = next((item for item in data.get("calendarFeeds", []) if item.get("tokenHash") == token_hash), None)
            feed_user = next((item for item in data.get("users", []) if feed and item["id"] == feed.get("userId") and item.get("active", True) and item.get("accessStatus", "active") == "active"), None)
            workspace = next((item for item in data.get("workspaces", []) if feed and item["id"] == feed.get("workspaceId")), None)
            membership = next((item for item in feed_user.get("workspaceMemberships", []) if workspace and item.get("workspaceId") == workspace["id"]), None) if feed_user else None
            if not feed or not feed_user or not workspace or not membership: return self.send_json(404, {"error": "Kalenderlink ungültig."})
            contextual = {**feed_user, "isAdmin": bool(membership.get("isAdmin")), "permissions": membership.get("permissions", {})}
            if not self.allowed(contextual, "calendar"): return self.send_json(403, {"error": "Keine Kalenderberechtigung."})
            return self.send_calendar(calendar_ics(contextual, workspace["id"], workspace["name"]))
        invite_parts = path.strip("/").split("/")
        if len(invite_parts) == 4 and invite_parts[:2] == ["api", "invitations"] and invite_parts[3] == "accept" and method == "POST":
            token = invite_parts[2]
            invitation = next((item for item in data.get("invitations", []) if item.get("tokenHash") == hashlib.sha256(token.encode()).hexdigest()), None)
            if not invitation or invitation.get("acceptedAt") or invitation.get("revokedAt") or invitation.get("expiresAt", "") < now():
                return self.send_json(410, {"error": "Diese Einladung ist ungültig oder abgelaufen."})
            payload = self.read_json(); username = str(payload.get("username", "")).strip().lower(); password = str(payload.get("password", ""))
            if len(username) < 3 or any(u["username"].lower() == username for u in data.get("users", [])):
                return self.send_json(400, {"error": "Der Benutzername ist zu kurz oder bereits vergeben."})
            if len(password) < 8: return self.send_json(400, {"error": "Das Passwort muss mindestens 8 Zeichen lang sein."})
            display_name = clean(str(payload.get("displayName") or invitation.get("displayName") or username)); role = invitation.get("role") or "Assistenz"
            invitation_workspace_id = invitation.get("workspaceId") or data["workspaces"][0]["id"]
            member = {"id": str(uuid.uuid4()), "workspaceId": invitation_workspace_id, "name": display_name, "role": role, "email": clean(str(payload.get("email") or invitation.get("email") or "")), "phone": clean(str(payload.get("phone") or "")), "personalWords": clean(str(payload.get("personalWords") or "")), "color": "#285c4d", "createdAt": now()}
            photo_data = str(payload.get("photoData") or "")
            if photo_data: member.update(save_member_photo(photo_data, str(payload.get("photoName") or "Profilfoto")))
            is_admin = role in {"Leistungsberechtigte Person", "Gesetzliche Betreuung", "Enge Angehörige"}
            membership = {"workspaceId": invitation_workspace_id, "memberId": member["id"], "role": role, "isAdmin": is_admin, "permissions": full_permissions() if is_admin else invitation.get("permissions", {})}
            created = {"id": str(uuid.uuid4()), "username": username, "displayName": display_name, "role": role, "memberId": member["id"], "isAdmin": is_admin, "active": True, "accessStatus": "active", "permissions": membership["permissions"], "workspaceMemberships": [membership], "notificationEmail": member["email"], "notificationPreference": "mentions", "passwordHash": hash_password(password), "createdAt": now()}
            with lock:
                if invitation.get("createPrivateArea"):
                    created["workspaceMemberships"] = []; workspace = create_private_area(created, display_name, clean(str(invitation.get("areaName") or f"{display_name}s Bereich")))
                    member = next(item for item in data["members"] if item.get("workspaceId") == workspace["id"] and item.get("name") == display_name)
                    created["memberId"] = member["id"]
                else: data["members"].append(member)
                data["users"].append(created); invitation["acceptedAt"] = now(); invitation["acceptedByUserId"] = created["id"]; save_data(data)
            token_value = secrets.token_urlsafe(32); sessions[token_value] = {"userId": created["id"], "expires": time.time() + 43200}
            return self.send_json(201, {"ok": True, "user": self.public_user(created)}, {"Set-Cookie": f"miteinander_session={token_value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200{'; Secure' if COOKIE_SECURE else ''}"})
        if path == "/api/login" and method == "POST":
            credentials = self.read_json()
            username = str(credentials.get("username") or "linea").strip().lower()
            supplied = str(credentials.get("password", ""))
            client_key = f"{self.client_address[0]}:{username}"
            recent = [attempt for attempt in login_attempts.get(client_key, []) if attempt > time.time() - 900]
            login_attempts[client_key] = recent
            if len(recent) >= 8: return self.send_json(429, {"error": "Zu viele Anmeldeversuche. Bitte warte 15 Minuten."})
            user = next((u for u in data.get("users", []) if u.get("active", True) and u.get("accessStatus", "active") == "active" and u.get("username", "").lower() == username), None)
            if not user or not password_matches(supplied, user.get("passwordHash", "")):
                recent.append(time.time())
                return self.send_json(401, {"error": "Benutzername oder Passwort stimmen nicht."})
            if user.get("totpEnabled") and not valid_totp(user.get("totpSecret", ""), str(credentials.get("otp") or "")):
                recent.append(time.time())
                return self.send_json(401, {"error": "Der Sicherheitscode fehlt oder ist ungültig.", "requiresOtp": True})
            login_attempts.pop(client_key, None)
            token = secrets.token_urlsafe(32)
            sessions[token] = {"userId": user["id"], "expires": time.time() + 43200}
            cookie = f"miteinander_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200{'; Secure' if COOKIE_SECURE else ''}"
            return self.send_json(200, {"ok": True, "user": self.public_user(user)}, {"Set-Cookie": cookie})
        if path == "/api/logout" and method == "POST":
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            if jar.get("miteinander_session"):
                sessions.pop(jar["miteinander_session"].value, None)
            return self.send_json(200, {"ok": True}, {"Set-Cookie": f"miteinander_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{'; Secure' if COOKIE_SECURE else ''}"})
        base_user = self.session_user()
        if not base_user:
            return self.send_json(401, {"error": "Bitte anmelden."})
        try:
            workspace_id, workspace, user = self.workspace_context(base_user)
        except PermissionError as error:
            return self.send_json(403, {"error": str(error)})
        if path == "/api/operator" and method == "GET":
            if not base_user.get("isSystemAdmin"): return self.send_json(403, {"error": "Keine Betreiberberechtigung."})
            summaries = []
            for area in data.get("workspaces", []):
                memberships = [candidate for candidate in data.get("users", []) if any(item.get("workspaceId") == area["id"] for item in candidate.get("workspaceMemberships", []))]
                summaries.append({"id": area["id"], "name": area["name"], "type": area.get("type"), "createdAt": area.get("createdAt"), "memberCount": len(memberships), "status": area.get("status", "active")})
            return self.send_json(200, {"workspaces": summaries, "users": len(data.get("users", [])), "feedback": data.get("developerFeedback", [])[:200]})
        operator_parts = path.strip("/").split("/")
        if len(operator_parts) == 4 and operator_parts[:2] == ["api", "operator"] and operator_parts[3] == "status" and method == "PUT":
            if not base_user.get("isSystemAdmin"): return self.send_json(403, {"error": "Keine Betreiberberechtigung."})
            target = next((item for item in data.get("workspaces", []) if item["id"] == operator_parts[2]), None); status = str(self.read_json().get("status") or "")
            if not target or status not in {"active", "paused"}: return self.send_json(400, {"error": "Bereich oder Status ungültig."})
            target["status"] = status
            if status == "active": target.pop("deletionRequestedAt", None); target.pop("deletionAfter", None)
            save_data(data); return self.send_json(200, {"ok": True})
        if path == "/api/operator/invitations" and method == "POST":
            if not base_user.get("isSystemAdmin"): return self.send_json(403, {"error": "Keine Betreiberberechtigung."})
            payload = self.read_json(); display_name = clean(str(payload.get("displayName") or "")); token = secrets.token_urlsafe(32)
            if not display_name: return self.send_json(400, {"error": "Bitte gib einen Namen an."})
            days = max(1, min(30, int(payload.get("validDays") or 7))); expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))
            invitation = {"id": str(uuid.uuid4()), "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "displayName": display_name, "email": clean(str(payload.get("email") or "")), "role": "Leistungsberechtigte Person", "createPrivateArea": True, "areaName": clean(str(payload.get("areaName") or f"{display_name}s Bereich")), "expiresAt": expires, "createdAt": now(), "createdByName": base_user["displayName"]}
            data.setdefault("invitations", []).insert(0, invitation); save_data(data)
            return self.send_json(201, {**{key: value for key, value in invitation.items() if key != "tokenHash"}, "token": token})
        join_parts = path.strip("/").split("/")
        if len(join_parts) == 4 and join_parts[:2] == ["api", "invitations"] and join_parts[3] == "join" and method == "POST":
            invitation = next((item for item in data.get("invitations", []) if item.get("tokenHash") == hashlib.sha256(join_parts[2].encode()).hexdigest()), None)
            if not invitation or invitation.get("createPrivateArea") or invitation.get("acceptedAt") or invitation.get("revokedAt") or invitation.get("expiresAt", "") < now(): return self.send_json(410, {"error": "Diese Einladung ist ungültig oder abgelaufen."})
            target_id = invitation.get("workspaceId"); target = next((item for item in data["workspaces"] if item["id"] == target_id), None)
            if not target: return self.send_json(404, {"error": "Bereich nicht gefunden."})
            if not any(item.get("workspaceId") == target_id for item in base_user.get("workspaceMemberships", [])):
                base_user.setdefault("workspaceMemberships", []).append({"workspaceId": target_id, "memberId": base_user.get("memberId"), "role": invitation.get("role", "Mitglied"), "isAdmin": invitation.get("role") in {"Leistungsberechtigte Person", "Gesetzliche Betreuung", "Enge Angehörige"}, "permissions": invitation.get("permissions", {})})
            invitation["acceptedAt"] = now(); invitation["acceptedByUserId"] = base_user["id"]; save_data(data)
            return self.send_json(200, {"ok": True, "workspaceId": target_id})
        if path == "/api/security/totp/setup" and method == "POST":
            secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="); base_user["pendingTotpSecret"] = secret; save_data(data)
            return self.send_json(200, {"secret": secret, "account": base_user.get("username", "")})
        if path == "/api/security/totp/confirm" and method == "POST":
            secret = base_user.get("pendingTotpSecret", ""); supplied = str(self.read_json().get("code") or "")
            if not secret or not valid_totp(secret, supplied): return self.send_json(400, {"error": "Der Sicherheitscode ist ungültig."})
            base_user["totpSecret"] = secret; base_user["totpEnabled"] = True; base_user.pop("pendingTotpSecret", None); save_data(data); return self.send_json(200, {"ok": True})
        if path == "/api/security/totp" and method == "DELETE":
            base_user.pop("totpSecret", None); base_user.pop("pendingTotpSecret", None); base_user["totpEnabled"] = False; save_data(data); return self.send_json(200, {"ok": True})
        if path == "/api/privacy/export" and method == "GET":
            exported = {key: [item for item in data.get(key, []) if item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id] for key in WORKSPACE_COLLECTIONS}
            exported.update({"exportedAt": now(), "workspace": workspace, "profile": data.get("workspaceProfiles", {}).get(workspace_id, {}), "options": data.get("workspaceOptions", {}).get(workspace_id, {})})
            return self.send_json(200, exported, {"Content-Disposition": f'attachment; filename="miteinander-export-{workspace_id}.json"'})
        if path == "/api/privacy/workspace" and method == "DELETE":
            if not user.get("isAdmin"): return self.send_json(403, {"error": "Nur die Administration dieses Bereichs darf die Löschung anfordern."})
            payload = self.read_json()
            if clean(str(payload.get("confirmation") or "")) != workspace["name"]: return self.send_json(400, {"error": "Der Bereichsname wurde nicht korrekt bestätigt."})
            workspace["deletionRequestedAt"] = now(); workspace["deletionAfter"] = (date.today() + timedelta(days=30)).isoformat(); workspace["status"] = "deletion-pending"
            audit(workspace_id, user, "deletion-requested", "workspace", workspace_id, "Löschung des Bereichs mit 30 Tagen Widerrufsfrist angefordert"); save_data(data)
            return self.send_json(200, {"ok": True, "deletionAfter": workspace["deletionAfter"]})
        if workspace.get("status") in {"paused", "deletion-pending"}:
            return self.send_json(423, {"error": "Dieser Bereich ist vorübergehend gesperrt."})
        if path == "/api/developer-feedback" and method == "POST":
            payload = self.read_json(); message = clean(str(payload.get("message") or "")); category = clean(str(payload.get("category") or "Verbesserung"))
            if len(message) < 10: return self.send_json(400, {"error": "Bitte beschreibe deinen Wunsch mit mindestens 10 Zeichen."})
            if len(message) > 4000: return self.send_json(400, {"error": "Der Text darf höchstens 4.000 Zeichen enthalten."})
            feedback = {"id": str(uuid.uuid4()), "category": category, "message": message, "createdAt": now(), "createdByUserId": base_user["id"], "createdByName": base_user["displayName"], "workspaceType": workspace.get("type"), "status": "new", "emailSent": bool(SMTP_HOST and DEVELOPER_CONTACT_EMAIL)}
            data.setdefault("developerFeedback", []).insert(0, feedback); save_data(data)
            if feedback["emailSent"]: threading.Thread(target=send_feedback_email, args=(feedback,), daemon=True).start()
            return self.send_json(201, {"ok": True, "emailSent": feedback["emailSent"]})
        if workspace.get("type") == "shared" and (path.startswith("/api/about/") or path.startswith("/api/contacts") or path in {"/api/family", "/api/person-profile", "/api/profile-photo", "/api/contact-options"}):
            return self.send_json(403, {"error": "Dieser Inhalt gehört in einen persönlichen Bereich."})
        current_profile = data.setdefault("workspaceProfiles", {}).setdefault(workspace_id, {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""})
        current_options = data.setdefault("workspaceOptions", {}).setdefault(workspace_id, {"contact": {"categories": []}, "ledger": {"descriptions": [], "categories": []}, "task": {"categories": []}})
        if path == "/api/data" and method == "GET":
            with lock:
                tasks_changed = ensure_rolling_tasks(data); calendar_changed = ensure_rolling_calendar_events(data)
                if tasks_changed or calendar_changed:
                    save_data(data)
            scoped = lambda key: [item for item in data.get(key, []) if item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id]
            workspace_user_ids = {candidate["id"] for candidate in data["users"] if any(m.get("workspaceId") == workspace_id for m in candidate.get("workspaceMemberships", []))}
            workspace_member_ids = {membership.get("memberId", candidate.get("memberId")) for candidate in data["users"] for membership in candidate.get("workspaceMemberships", []) if membership.get("workspaceId") == workspace_id and membership.get("memberId", candidate.get("memberId"))}
            workspace_options = data.setdefault("workspaceOptions", {}).setdefault(workspace_id, {"contact": {"categories": []}, "ledger": {"descriptions": [], "categories": []}, "task": {"categories": []}})
            visible = {
                "family": {"name": workspace["name"], "person": workspace.get("personName", "")},
                "workspace": workspace,
                "workspaces": [space for space in data["workspaces"] if any(m.get("workspaceId") == space["id"] for m in base_user.get("workspaceMemberships", []))],
                "personProfile": data.setdefault("workspaceProfiles", {}).setdefault(workspace_id, {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""}) if workspace.get("type") == "private" else {},
                "goals": scoped("goals") if workspace.get("type") == "private" else [],
                "rules": scoped("rules") if workspace.get("type") == "private" else [],
                "aboutComments": scoped("aboutComments") if workspace.get("type") == "private" else [],
                "importantContacts": scoped("importantContacts"),
                "contactOptions": workspace_options["contact"],
                "beis": scoped("beis") if workspace.get("type") == "private" else [],
                "routinePlans": scoped("routinePlans") if self.allowed(user, "tasks") else [],
                "cases": scoped("cases") if self.allowed(user, "cases") and workspace.get("type") == "private" else [],
                "correspondence": scoped("correspondence") if self.allowed(user, "cases") and workspace.get("type") == "private" else [],
                "tasks": [task for task in scoped("tasks") if user.get("isAdmin") or not task.get("deletedAt")] if self.allowed(user, "tasks") else [],
                "calendarEvents": calendar_items_for(user, workspace_id),
                "calendarFeedActive": any(feed.get("userId") == base_user["id"] and feed.get("workspaceId") == workspace_id for feed in data.get("calendarFeeds", [])),
                "taskOptions": workspace_options["task"] if self.allowed(user, "tasks") else {"categories": []},
                "documents": scoped("documents") if self.allowed(user, "documents") and workspace.get("type") == "private" else [],
                "messages": [],
                "announcements": scoped("announcements"),
                "topicComments": [item for item in scoped("topicComments") if item.get("targetType") == "goal" or item.get("targetType") == "case" and self.allowed(user, "cases") or item.get("targetType") == "task" and self.allowed(user, "tasks")],
                "ledger": scoped("ledger") if self.allowed(user, "ledger") else [],
                "accounts": scoped("accounts") if self.allowed(user, "ledger") else [],
                "ledgerOptions": workspace_options["ledger"] if self.allowed(user, "ledger") else {"descriptions": [], "categories": []},
                "members": scoped("members") if workspace.get("type") == "private" else [member for member in data.get("members", []) if member["id"] in workspace_member_ids],
                "currentUser": self.public_user(user),
                "users": [self.public_user(u) for u in data["users"] if u["id"] in workspace_user_ids] if user.get("isAdmin") else [{"id": u["id"], "displayName": u["displayName"], "username": u.get("username", "")} for u in data["users"] if u["id"] in workspace_user_ids and u.get("active", True)],
                "invitations": [{key: value for key, value in item.items() if key != "tokenHash"} for item in data.get("invitations", []) if item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id] if user.get("isAdmin") else [],
                "capabilities": {key: self.allowed(user, key) and not (workspace.get("type") == "shared" and key in {"cases", "documents", "family"}) for key in PERMISSION_KEYS} | {"manageAccess": bool(user.get("isAdmin"))},
                "emailNotificationsConfigured": bool(SMTP_HOST),
                "auditLog": [item for item in data.get("auditLog", []) if item.get("workspaceId") == workspace_id][:100] if user.get("isAdmin") else [],
                "onboardingDismissed": workspace_id in base_user.get("onboardingDismissedWorkspaceIds", []),
                "operatorInfo": {"name": os.getenv("DATA_CONTROLLER_NAME", ""), "contact": os.getenv("DATA_CONTROLLER_CONTACT", "")},
            }
            return self.send_json(200, visible)
        if path == "/api/onboarding" and method == "PUT":
            dismissed = base_user.setdefault("onboardingDismissedWorkspaceIds", [])
            if workspace_id not in dismissed: dismissed.append(workspace_id)
            save_data(data); return self.send_json(200, {"ok": True})
        if path == "/api/calendar-feed" and method == "POST":
            if not self.allowed(user, "calendar"): return self.send_json(403, {"error": "Keine Kalenderberechtigung."})
            token = secrets.token_urlsafe(32); feeds = data.setdefault("calendarFeeds", [])
            feeds[:] = [feed for feed in feeds if not (feed.get("userId") == base_user["id"] and feed.get("workspaceId") == workspace_id)]
            feeds.append({"id": str(uuid.uuid4()), "userId": base_user["id"], "workspaceId": workspace_id, "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "createdAt": now()}); save_data(data)
            base = APP_BASE_URL or f"http://{self.headers.get('Host', 'localhost')}"
            return self.send_json(201, {"url": f"{base}/api/calendar-feed/{token}.ics"})
        if path == "/api/calendar-feed" and method == "DELETE":
            feeds = data.setdefault("calendarFeeds", []); feeds[:] = [feed for feed in feeds if not (feed.get("userId") == base_user["id"] and feed.get("workspaceId") == workspace_id)]; save_data(data)
            return self.send_json(200, {"ok": True})
        if path in {"/api/bank-import/preview", "/api/bank-import/commit"} and method == "POST":
            if not self.allowed(user, "ledger"): return self.send_json(403, {"error": "Keine Berechtigung für das Kassenbuch."})
            payload = self.read_json(); account_id = str(payload.get("accountId") or "")
            if not any(account["id"] == account_id and account.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id for account in data.get("accounts", [])): return self.send_json(400, {"error": "Bitte wähle ein gültiges Konto aus."})
            rows = parse_statement(str(payload.get("fileData") or ""), str(payload.get("fileName") or "kontoauszug.csv"))
            existing = {entry.get("importFingerprint") for entry in data.get("ledger", []) if entry.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id}
            preview = [{**row, "duplicate": import_fingerprint(workspace_id, account_id, row) in existing} for row in rows]
            if path.endswith("preview"): return self.send_json(200, {"rows": preview[:500], "total": len(preview), "newCount": sum(not row["duplicate"] for row in preview), "duplicateCount": sum(row["duplicate"] for row in preview)})
            batch_id = str(uuid.uuid4()); created = []
            for row in rows:
                fingerprint = import_fingerprint(workspace_id, account_id, row)
                if fingerprint in existing: continue
                entry = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, "accountId": account_id, "type": "income" if row["amount"] >= 0 else "expense", "amount": f"{abs(row['amount']):.2f}", "date": row["date"], "description": row["description"], "payee": row.get("payee", ""), "category": "", "receiptStatus": "none", "notes": "Manuell aus einem Kontoauszug importiert.", "reviewStatus": "pending", "importFingerprint": fingerprint, "importBatchId": batch_id, "importFileName": clean(str(payload.get("fileName") or "Kontoauszug")), "importedAt": now(), "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                data["ledger"].insert(0, entry); existing.add(fingerprint); created.append(entry)
            audit(workspace_id, user, "imported", "ledger", batch_id, f"{len(created)} Bankumsätze importiert"); save_data(data)
            return self.send_json(201, {"imported": len(created), "duplicates": len(rows)-len(created), "batchId": batch_id})
        review_parts = path.strip("/").split("/")
        if len(review_parts) == 4 and review_parts[:2] == ["api", "ledger"] and review_parts[3] == "review" and method == "PUT":
            if not self.allowed(user, "ledger"): return self.send_json(403, {"error": "Keine Berechtigung für das Kassenbuch."})
            entry = next((item for item in data.get("ledger", []) if item["id"] == review_parts[2] and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id), None)
            if not entry: return self.send_json(404, {"error": "Buchung nicht gefunden."})
            entry["reviewStatus"] = "reviewed"; entry["reviewedAt"] = now(); entry["reviewedByName"] = user["displayName"]; save_data(data)
            return self.send_json(200, entry)
        workspace_parts = path.strip("/").split("/")
        if workspace_parts[:2] == ["api", "workspaces"]:
            target_workspace_id = workspace_parts[2] if len(workspace_parts) >= 3 else None
            action = workspace_parts[3] if len(workspace_parts) == 4 else None
            if method == "POST" and not target_workspace_id:
                payload = self.read_json(); workspace_type = str(payload.get("type") or "shared")
                if workspace_type not in {"private", "shared"}: return self.send_json(400, {"error": "Ungültiger Bereichstyp."})
                if workspace_type == "private": return self.send_json(403, {"error": "Persönliche Bereiche werden ausschließlich über eine Einladung der Betreiberverwaltung angelegt."})
                if workspace_type == "shared" and not user.get("isAdmin"): return self.send_json(403, {"error": "Nur Administrator*innen dürfen gemeinsame Bereiche anlegen."})
                person_name = clean(str(payload.get("personName") or base_user.get("displayName") or "")) if workspace_type == "private" else ""
                name = clean(str(payload.get("name") or (f"{person_name}s Bereich" if person_name else "")))
                if not name: return self.send_json(400, {"error": "Der Bereich benötigt einen Namen."})
                created_workspace = {"id": str(uuid.uuid4()), "name": name, "type": workspace_type, "personName": person_name, "createdAt": now(), "createdByUserId": user["id"]}
                member = None
                if workspace_type == "private":
                    member = {"id": str(uuid.uuid4()), "workspaceId": created_workspace["id"], "name": person_name, "role": "Leistungsberechtigte Person", "email": base_user.get("notificationEmail", ""), "color": "#5a57d9", "createdAt": now()}
                membership = {"workspaceId": created_workspace["id"], "memberId": member["id"] if member else user.get("memberId"), "role": "Leistungsberechtigte Person" if member else "Bereichsverwaltung", "isAdmin": True, "permissions": full_permissions()}
                accounts = ([
                    {"id": str(uuid.uuid4()), "workspaceId": created_workspace["id"], "name": f"{person_name}s Barkasse", "type": "Bargeld", "color": "#285c4d", "createdAt": now()},
                    {"id": str(uuid.uuid4()), "workspaceId": created_workspace["id"], "name": f"{person_name}s Bargeld-Spardose", "type": "Bargeld", "color": "#5b57c8", "createdAt": now()},
                    {"id": str(uuid.uuid4()), "workspaceId": created_workspace["id"], "name": "Wohnungs- und Einkommenskonto", "type": "Bankkonto", "color": "#397a92", "createdAt": now()},
                ] if member else [{"id": str(uuid.uuid4()), "workspaceId": created_workspace["id"], "name": f"{name} – Gemeinschaftskasse", "type": "Bargeld", "color": "#285c4d", "createdAt": now()}])
                with lock:
                    data["workspaces"].append(created_workspace); base_user.setdefault("workspaceMemberships", []).append(membership); data["accounts"].extend(accounts)
                    data.setdefault("workspaceProfiles", {})[created_workspace["id"]] = {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""}
                    data.setdefault("workspaceOptions", {})[created_workspace["id"]] = {"contact": {"categories": list(data.get("contactOptions", {}).get("categories", []))}, "ledger": {"descriptions": [], "categories": []}, "task": {"categories": []}}
                    if member: data["members"].append(member)
                    for target_id in payload.get("memberUserIds", []) if workspace_type == "shared" else []:
                        target_user = next((candidate for candidate in data["users"] if candidate["id"] == target_id and candidate["id"] != base_user["id"]), None)
                        if target_user and not any(m.get("workspaceId") == created_workspace["id"] for m in target_user.get("workspaceMemberships", [])):
                            target_user.setdefault("workspaceMemberships", []).append({"workspaceId": created_workspace["id"], "memberId": target_user.get("memberId"), "role": "WG-Team", "isAdmin": False, "permissions": {"tasks": True, "calendar": True, "ledger": True, "cases": False, "documents": False, "family": False}})
                    audit(created_workspace["id"], user, "created", "workspace", created_workspace["id"], f"Bereich „{name}“ angelegt"); save_data(data)
                return self.send_json(201, created_workspace)
            if not user.get("isAdmin"): return self.send_json(403, {"error": "Nur Administrator*innen dürfen Bereiche verwalten."})
            target_workspace = next((space for space in data["workspaces"] if space["id"] == target_workspace_id), None)
            if not target_workspace or target_workspace_id != workspace_id: return self.send_json(404, {"error": "Bereich nicht gefunden."})
            if action == "memberships" and method == "POST":
                payload = self.read_json(); target_user = next((candidate for candidate in data["users"] if candidate["id"] == payload.get("userId")), None)
                if not target_user: return self.send_json(404, {"error": "Person nicht gefunden."})
                existing = next((m for m in target_user.get("workspaceMemberships", []) if m.get("workspaceId") == workspace_id), None)
                permissions = {key: bool(payload.get("permissions", {}).get(key)) for key in PERMISSION_KEYS}; permissions.update({"tasks": True, "calendar": True, "ledger": True})
                values = {"workspaceId": workspace_id, "memberId": target_user.get("memberId"), "role": clean(str(payload.get("role") or "Mitglied")), "isAdmin": bool(payload.get("isAdmin")), "permissions": full_permissions() if payload.get("isAdmin") else permissions}
                if existing: existing.update(values)
                else: target_user.setdefault("workspaceMemberships", []).append(values)
                audit(workspace_id, user, "updated", "workspace-membership", target_user["id"], f"Mitgliedschaft für {target_user['displayName']} geändert"); save_data(data); return self.send_json(200, self.public_user(target_user))
            if action == "memberships" and method == "DELETE":
                payload = self.read_json(); target_user = next((candidate for candidate in data["users"] if candidate["id"] == payload.get("userId")), None)
                if not target_user or target_user["id"] == user["id"]: return self.send_json(400, {"error": "Diese Mitgliedschaft kann nicht entfernt werden."})
                target_user["workspaceMemberships"] = [m for m in target_user.get("workspaceMemberships", []) if m.get("workspaceId") != workspace_id]; audit(workspace_id, user, "deleted", "workspace-membership", target_user["id"], f"Mitgliedschaft von {target_user['displayName']} entfernt"); save_data(data); return self.send_json(200, {"ok": True})
        if path == "/api/notification-preferences" and method == "PUT":
            payload = self.read_json(); preference = str(payload.get("notificationPreference") or "mentions")
            if preference not in {"all", "mentions", "none"}: return self.send_json(400, {"error": "Ungültige Benachrichtigungseinstellung."})
            base_user["notificationPreference"] = preference; base_user["notificationEmail"] = clean(str(payload.get("notificationEmail") or "")); save_data(data)
            return self.send_json(200, self.public_user({**user, "notificationPreference": preference, "notificationEmail": base_user["notificationEmail"]}))
        communication_parts = path.strip("/").split("/")
        if communication_parts[:2] == ["api", "announcements"]:
            announcement_id = communication_parts[2] if len(communication_parts) >= 3 else None
            action = communication_parts[3] if len(communication_parts) == 4 else None
            announcement = next((item for item in data.get("announcements", []) if item["id"] == announcement_id and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id), None) if announcement_id else None
            if method == "POST" and not announcement_id:
                payload = self.read_json(); text = clean(str(payload.get("text") or "")); title = clean(str(payload.get("title") or ""))
                if not title or not text: return self.send_json(400, {"error": "Überschrift und Information werden benötigt."})
                mentions = mentioned_user_ids(text, workspace_id)
                item = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, "title": title, "text": text, "importance": payload.get("importance") if payload.get("importance") in {"normal", "important"} else "normal", "validUntil": clean(str(payload.get("validUntil") or "")), "mentionedUserIds": mentions, "readByUserIds": [user["id"]], "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                with lock: data.setdefault("announcements", []).insert(0, item); save_data(data)
                notify_users(user["id"], mentions, notify_all=True, workspace_id=workspace_id); return self.send_json(201, item)
            if not announcement: return self.send_json(404, {"error": "Information nicht gefunden."})
            if method == "PUT" and action == "read":
                if user["id"] not in announcement.setdefault("readByUserIds", []): announcement["readByUserIds"].append(user["id"]); save_data(data)
                return self.send_json(200, announcement)
            if method == "DELETE" and not action:
                if not (user.get("isAdmin") or announcement.get("createdByUserId") == user["id"]): return self.send_json(403, {"error": "Du kannst diese Information nicht löschen."})
                data["announcements"].remove(announcement); save_data(data); return self.send_json(200, {"ok": True})
        if communication_parts[:2] == ["api", "topic-comments"]:
            comment_id = communication_parts[2] if len(communication_parts) == 3 else None
            if method == "POST" and not comment_id:
                payload = self.read_json(); target_type = str(payload.get("targetType") or ""); target_id = str(payload.get("targetId") or ""); text = clean(str(payload.get("text") or ""))
                collection = {"case": "cases", "task": "tasks", "goal": "goals"}.get(target_type)
                permission = {"case": "cases", "task": "tasks", "goal": None}.get(target_type)
                if not text: return self.send_json(400, {"error": "Der Beitrag darf nicht leer sein."})
                if not collection or not any(item["id"] == target_id and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id for item in data.get(collection, [])): return self.send_json(400, {"error": "Das zugehörige Thema wurde nicht gefunden."})
                if permission and not self.allowed(user, permission): return self.send_json(403, {"error": "Keine Berechtigung für dieses Thema."})
                mentions = mentioned_user_ids(text, workspace_id); comment = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, "targetType": target_type, "targetId": target_id, "text": text, "mentionedUserIds": mentions, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                with lock: data.setdefault("topicComments", []).append(comment); save_data(data)
                notify_users(user["id"], mentions, workspace_id=workspace_id); return self.send_json(201, comment)
            comment = next((item for item in data.get("topicComments", []) if item["id"] == comment_id and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id), None)
            if not comment: return self.send_json(404, {"error": "Beitrag nicht gefunden."})
            if method == "DELETE":
                if not (user.get("isAdmin") or comment.get("createdByUserId") == user["id"]): return self.send_json(403, {"error": "Du kannst diesen Beitrag nicht löschen."})
                data["topicComments"].remove(comment); save_data(data); return self.send_json(200, {"ok": True})
        if path.startswith("/api/receipts/") and method == "GET":
            if not self.allowed(user, "ledger"):
                return self.send_json(403, {"error": "Kein Zugriff auf das Kassenbuch."})
            filename = path.rsplit("/", 1)[-1]
            if not filename or filename != Path(filename).name:
                return self.send_json(404, {"error": "Beleg nicht gefunden."})
            receipt = RECEIPTS_DIR / filename
            if not any(item.get("receiptFile") == filename and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id for item in data.get("ledger", [])):
                return self.send_json(404, {"error": "Beleg nicht gefunden."})
            if not receipt.is_file():
                return self.send_json(404, {"error": "Beleg nicht gefunden."})
            content = receipt.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(receipt)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if path.startswith("/api/case-files/") and method == "GET":
            if not self.allowed(user, "cases"):
                return self.send_json(403, {"error": "Kein Zugriff auf die Antragsakte."})
            filename = path.rsplit("/", 1)[-1]
            if not filename or filename != Path(filename).name:
                return self.send_json(404, {"error": "Datei nicht gefunden."})
            attachment = CASE_FILES_DIR / filename
            if not any(item.get("attachmentFile") == filename and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id for collection in ("cases", "correspondence") for item in data.get(collection, [])):
                return self.send_json(404, {"error": "Datei nicht gefunden."})
            if not attachment.is_file():
                return self.send_json(404, {"error": "Datei nicht gefunden."})
            content = attachment.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(attachment)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f"inline; filename={filename}")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if path.startswith("/api/document-files/") and method == "GET":
            if not self.allowed(user, "documents"):
                return self.send_json(403, {"error": "Kein Zugriff auf Dokumente."})
            filename = path.rsplit("/", 1)[-1]
            if not filename or filename != Path(filename).name:
                return self.send_json(404, {"error": "Datei nicht gefunden."})
            attachment = DOCUMENT_FILES_DIR / filename
            if not any(item.get("attachmentFile") == filename and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id for item in data.get("documents", [])):
                return self.send_json(404, {"error": "Datei nicht gefunden."})
            if not attachment.is_file():
                return self.send_json(404, {"error": "Datei nicht gefunden."})
            content = attachment.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(attachment)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f"inline; filename={filename}")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if path.startswith("/api/about-files/") and method == "GET":
            filename = path.rsplit("/", 1)[-1]
            if not filename or filename != Path(filename).name:
                return self.send_json(404, {"error": "Datei nicht gefunden."})
            attachment = ABOUT_FILES_DIR / filename
            permitted = current_profile.get("photoFile") == filename or any(item.get("attachmentFile") == filename and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id for item in data.get("beis", []))
            if not permitted: return self.send_json(404, {"error": "Datei nicht gefunden."})
            if not attachment.is_file():
                return self.send_json(404, {"error": "Datei nicht gefunden."})
            content = attachment.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(attachment)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f"inline; filename={filename}")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if path.startswith("/api/routine-files/") and method == "GET":
            if not self.allowed(user, "tasks"): return self.send_json(403, {"error": "Kein Zugriff auf Ablaufpläne."})
            filename = path.rsplit("/", 1)[-1]
            if not filename or filename != Path(filename).name: return self.send_json(404, {"error": "Datei nicht gefunden."})
            attachment = ROUTINE_FILES_DIR / filename
            if not any(item.get("attachmentFile") == filename and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id for item in data.get("routinePlans", [])): return self.send_json(404, {"error": "Datei nicht gefunden."})
            if not attachment.is_file(): return self.send_json(404, {"error": "Datei nicht gefunden."})
            content = attachment.read_bytes(); self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(attachment)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content))); self.send_header("Content-Disposition", f"inline; filename={filename}")
            self.send_header("Cache-Control", "private, max-age=3600"); self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers(); self.wfile.write(content); return
        if path.startswith("/api/member-files/") and method == "GET":
            filename = path.rsplit("/", 1)[-1]
            if not filename or filename != Path(filename).name: return self.send_json(404, {"error": "Foto nicht gefunden."})
            attachment = MEMBER_FILES_DIR / filename
            if not any(member.get("photoFile") == filename and (member.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id or any(candidate.get("memberId") == member.get("id") and any(m.get("workspaceId") == workspace_id for m in candidate.get("workspaceMemberships", [])) for candidate in data.get("users", []))) for member in data.get("members", [])):
                return self.send_json(404, {"error": "Foto nicht gefunden."})
            if not attachment.is_file(): return self.send_json(404, {"error": "Foto nicht gefunden."})
            content = attachment.read_bytes(); self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(attachment)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(content))); self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(content); return
        if path == "/api/family" and method == "PUT":
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "Keine Berechtigung."})
            with lock:
                values = clean(self.read_json())
                workspace["name"] = values.get("name") or workspace["name"]; workspace["personName"] = values.get("person") or workspace.get("personName", "")
                if workspace_id == data["workspaces"][0]["id"]: data["family"].update(values); data["family"]["updatedAt"] = now()
                audit(workspace_id, user, "updated", "workspace-settings", workspace_id, "Grunddaten des Bereichs geändert")
                save_data(data)
            return self.send_json(200, {"name": workspace["name"], "person": workspace.get("personName", "")})

        if path == "/api/person-profile" and method == "PUT":
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "Das Personenprofil darf nur von Administratoren geändert werden."})
            with lock:
                current_profile.update(clean(self.read_json()))
                current_profile["updatedAt"] = now()
                current_profile["updatedByName"] = user["displayName"]
                save_data(data)
            return self.send_json(200, current_profile)

        if path == "/api/profile-photo" and method == "PUT":
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "Das Profilfoto darf nur von Administratoren geändert werden."})
            payload = self.read_json(); photo_data = str(payload.get("photoData") or "")
            if not photo_data.startswith("data:image/"):
                return self.send_json(400, {"error": "Bitte wähle ein Bild aus."})
            saved = save_about_file(photo_data, str(payload.get("photoName") or "Profilfoto"))
            old_photo = current_profile.get("photoFile")
            with lock:
                current_profile.update({"photoFile": saved["attachmentFile"], "photoName": saved["attachmentName"], "photoType": saved["attachmentType"], "updatedAt": now(), "updatedByName": user["displayName"]})
                if old_photo: (ABOUT_FILES_DIR / Path(old_photo).name).unlink(missing_ok=True)
                save_data(data)
            return self.send_json(200, current_profile)

        bei_parts = path.strip("/").split("/")
        if len(bei_parts) in (3, 4) and bei_parts[:3] == ["api", "about", "beis"]:
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "BEIs dürfen nur von Administratoren verwaltet werden."})
            bei_id = bei_parts[3] if len(bei_parts) == 4 else None
            if method == "POST" and not bei_id:
                payload = self.read_json(); file_data = str(payload.pop("beiFile", "")); file_name = str(payload.pop("beiFileName", "BEI.pdf"))
                if not file_data.startswith("data:application/pdf"):
                    return self.send_json(400, {"error": "Ein BEI muss als PDF hochgeladen werden."})
                bei = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, **clean(payload), **save_about_file(file_data, file_name), "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                with lock: data["beis"].insert(0, bei); save_data(data)
                return self.send_json(201, bei)
            bei = next((item for item in data["beis"] if item["id"] == bei_id and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id), None)
            if not bei: return self.send_json(404, {"error": "BEI nicht gefunden."})
            if method == "PUT":
                payload = self.read_json(); file_data = str(payload.pop("beiFile", "")); file_name = str(payload.pop("beiFileName", "BEI.pdf"))
                bei.update(clean(payload)); bei["updatedAt"] = now(); bei["updatedByName"] = user["displayName"]
                if file_data:
                    if not file_data.startswith("data:application/pdf"): return self.send_json(400, {"error": "Ein BEI muss als PDF hochgeladen werden."})
                    old_file = bei.get("attachmentFile"); bei.update(save_about_file(file_data, file_name))
                    if old_file: (ABOUT_FILES_DIR / Path(old_file).name).unlink(missing_ok=True)
                with lock: save_data(data)
                return self.send_json(200, bei)
            if method == "DELETE":
                with lock:
                    data["beis"].remove(bei)
                    if bei.get("attachmentFile"): (ABOUT_FILES_DIR / Path(bei["attachmentFile"]).name).unlink(missing_ok=True)
                    save_data(data)
                return self.send_json(200, {"ok": True})

        if path == "/api/contact-options" and method == "PUT":
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "Kontaktkategorien dürfen nur von Administratoren geändert werden."})
            values = self.read_json().get("categories")
            if not isinstance(values, list):
                return self.send_json(400, {"error": "Kategorien müssen als Liste übermittelt werden."})
            with lock:
                current_options["contact"]["categories"] = list(dict.fromkeys(clean(str(value)) for value in values if str(value).strip()))[:200]
                save_data(data)
            return self.send_json(200, current_options["contact"])

        contact_parts = path.strip("/").split("/")
        if len(contact_parts) in (2, 3) and contact_parts[:2] == ["api", "contacts"]:
            contact_id = contact_parts[2] if len(contact_parts) == 3 else None
            if method == "POST" and not contact_id:
                payload = clean(self.read_json())
                if not payload.get("name"):
                    return self.send_json(400, {"error": "Bitte gib einen Namen oder eine Organisation an."})
                contact = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, **payload, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                with lock:
                    data["importantContacts"].insert(0, contact)
                    save_data(data)
                return self.send_json(201, contact)
            contact = next((entry for entry in data["importantContacts"] if entry["id"] == contact_id and entry.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id), None)
            if not contact:
                return self.send_json(404, {"error": "Kontakt nicht gefunden."})
            may_change = bool(user.get("isAdmin") or contact.get("createdByUserId") == user["id"])
            if method == "PUT":
                if not may_change:
                    return self.send_json(403, {"error": "Du kannst nur eigene Kontakte bearbeiten."})
                contact.update(clean(self.read_json())); contact["updatedAt"] = now(); contact["updatedByName"] = user["displayName"]
                with lock: save_data(data)
                return self.send_json(200, contact)
            if method == "DELETE":
                if not may_change:
                    return self.send_json(403, {"error": "Du kannst nur eigene Kontakte löschen."})
                with lock:
                    data["importantContacts"].remove(contact)
                    save_data(data)
                return self.send_json(200, {"ok": True})

        about_parts = path.strip("/").split("/")
        if len(about_parts) in (3, 4) and about_parts[:2] == ["api", "about"] and about_parts[2] in {"goals", "rules", "comments"}:
            kind = about_parts[2]
            collection = {"goals": "goals", "rules": "rules", "comments": "aboutComments"}[kind]
            item_id = about_parts[3] if len(about_parts) == 4 else None
            if method == "POST" and not item_id:
                if kind == "goals" and not user.get("isAdmin"):
                    return self.send_json(403, {"error": "Ziele dürfen nur von Administratoren angelegt werden."})
                payload = clean(self.read_json())
                if kind == "comments":
                    target_collection = "goals" if payload.get("targetType") == "goal" else "rules" if payload.get("targetType") == "rule" else None
                    if not target_collection or not any(item["id"] == payload.get("targetId") and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id for item in data[target_collection]):
                        return self.send_json(400, {"error": "Das Ziel oder die Regel wurde nicht gefunden."})
                item = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, **payload, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                if kind == "rules":
                    item["approvalStatus"] = "pending"
                with lock:
                    data[collection].insert(0, item)
                    save_data(data)
                return self.send_json(201, item)
            item = next((entry for entry in data[collection] if entry["id"] == item_id and entry.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id), None)
            if not item:
                return self.send_json(404, {"error": "Eintrag nicht gefunden."})
            if method == "PUT":
                if kind in {"goals", "rules"} and not user.get("isAdmin"):
                    return self.send_json(403, {"error": "Diese Änderung muss durch einen Administrator erfolgen."})
                if kind == "comments" and not (user.get("isAdmin") or item.get("createdByUserId") == user["id"]):
                    return self.send_json(403, {"error": "Du kannst nur eigene Kommentare ändern."})
                item.update(clean(self.read_json()))
                item["updatedAt"] = now(); item["updatedByName"] = user["displayName"]
                if kind == "rules" and item.get("approvalStatus") in {"approved", "rejected"}:
                    item["reviewedAt"] = now(); item["reviewedByName"] = user["displayName"]
                with lock: save_data(data)
                return self.send_json(200, item)
            if method == "DELETE":
                if kind in {"goals", "rules"} and not user.get("isAdmin"):
                    return self.send_json(403, {"error": "Nur Administratoren dürfen diesen Eintrag löschen."})
                if kind == "comments" and not (user.get("isAdmin") or item.get("createdByUserId") == user["id"]):
                    return self.send_json(403, {"error": "Du kannst nur eigene Kommentare löschen."})
                with lock:
                    data[collection].remove(item)
                    if kind in {"goals", "rules"}:
                        target_type = "goal" if kind == "goals" else "rule"
                        data["aboutComments"] = [comment for comment in data["aboutComments"] if not (comment.get("targetType") == target_type and comment.get("targetId") == item_id)]
                    save_data(data)
                return self.send_json(200, {"ok": True})

        if path == "/api/ledger-options" and method == "PUT":
            if not self.allowed(user, "ledger"):
                return self.send_json(403, {"error": "Keine Berechtigung für das Kassenbuch."})
            payload = self.read_json()
            with lock:
                for key in ("descriptions", "categories"):
                    values = payload.get(key)
                    if isinstance(values, list):
                        current_options["ledger"][key] = list(dict.fromkeys(clean(str(value)) for value in values if str(value).strip()))[:200]
                save_data(data)
            return self.send_json(200, current_options["ledger"])

        if path == "/api/task-options" and method == "PUT":
            if not self.allowed(user, "tasks"):
                return self.send_json(403, {"error": "Keine Berechtigung für Aufgaben."})
            payload = self.read_json()
            values = payload.get("categories")
            if not isinstance(values, list):
                return self.send_json(400, {"error": "Kategorien müssen als Liste übermittelt werden."})
            with lock:
                current_options["task"] = {"categories": list(dict.fromkeys(clean(str(value)) for value in values if str(value).strip()))[:200]}
                save_data(data)
            return self.send_json(200, current_options["task"])

        routine_parts = path.strip("/").split("/")
        if len(routine_parts) in (2, 3, 4) and routine_parts[:2] == ["api", "routine-plans"]:
            if not self.allowed(user, "tasks"):
                return self.send_json(403, {"error": "Keine Berechtigung für Ablaufpläne."})
            plan_id = routine_parts[2] if len(routine_parts) >= 3 else None
            action = routine_parts[3] if len(routine_parts) == 4 else None
            plan = next((item for item in data["routinePlans"] if item["id"] == plan_id and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id), None) if plan_id else None
            if action == "review" and method == "PUT":
                if not user.get("isAdmin"): return self.send_json(403, {"error": "Ablaufpläne dürfen nur von Administratoren freigegeben werden."})
                if not plan: return self.send_json(404, {"error": "Ablaufplan nicht gefunden."})
                decision = self.read_json().get("approvalStatus")
                if decision not in {"approved", "rejected"}: return self.send_json(400, {"error": "Ungültige Entscheidung."})
                with lock:
                    if decision == "approved":
                        for other in data["routinePlans"]:
                            if other.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id and other.get("seriesId") == plan.get("seriesId") and other.get("approvalStatus") == "approved": other["approvalStatus"] = "superseded"
                    plan["approvalStatus"] = decision; plan["reviewedAt"] = now(); plan["reviewedByName"] = user["displayName"]
                    save_data(data)
                return self.send_json(200, plan)
            if method == "POST" and not plan_id:
                payload = self.read_json(); file_data = str(payload.pop("planFile", "")); file_name = str(payload.pop("planFileName", "Ablaufplan"))
                plan = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, "seriesId": str(uuid.uuid4()), "version": 1, **clean(payload), "approvalStatus": "pending", "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                if file_data: plan.update(save_routine_file(file_data, file_name))
                with lock: data["routinePlans"].insert(0, plan); save_data(data)
                return self.send_json(201, plan)
            if not plan: return self.send_json(404, {"error": "Ablaufplan nicht gefunden."})
            if method == "PUT" and not action:
                payload = self.read_json(); file_data = str(payload.pop("planFile", "")); file_name = str(payload.pop("planFileName", "Ablaufplan"))
                version = max((item.get("version", 1) for item in data["routinePlans"] if item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id and item.get("seriesId") == plan.get("seriesId")), default=0) + 1
                inherited = {key: plan.get(key) for key in ("attachmentFile", "attachmentName", "attachmentType") if plan.get(key)}
                revision = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, "seriesId": plan["seriesId"], "version": version, **clean(payload), **inherited, "approvalStatus": "pending", "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"], "basedOnId": plan["id"]}
                if file_data: revision.update(save_routine_file(file_data, file_name))
                with lock: data["routinePlans"].insert(0, revision); save_data(data)
                return self.send_json(201, revision)
            if method == "DELETE":
                if not (user.get("isAdmin") or plan.get("createdByUserId") == user["id"] and plan.get("approvalStatus") == "pending"):
                    return self.send_json(403, {"error": "Du kannst nur eigene, noch nicht freigegebene Entwürfe löschen."})
                with lock:
                    data["routinePlans"].remove(plan)
                    filename = plan.get("attachmentFile")
                    if filename and not any(item.get("attachmentFile") == filename for item in data["routinePlans"]): (ROUTINE_FILES_DIR / Path(filename).name).unlink(missing_ok=True)
                    save_data(data)
                return self.send_json(200, {"ok": True})

        user_parts = path.strip("/").split("/")
        if len(user_parts) in (2, 3) and user_parts[:2] == ["api", "invitations"]:
            if not user.get("isAdmin"): return self.send_json(403, {"error": "Nur Administrator*innen dürfen Einladungen verwalten."})
            invitation_id = user_parts[2] if len(user_parts) == 3 else None
            if method == "POST" and not invitation_id:
                payload = self.read_json(); role = str(payload.get("role") or "Assistenz"); token = secrets.token_urlsafe(32)
                days = max(1, min(30, int(payload.get("validDays") or 7)))
                expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))
                is_admin = role in {"Leistungsberechtigte Person", "Gesetzliche Betreuung", "Enge Angehörige"}
                invitation = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "displayName": clean(str(payload.get("displayName") or "")), "email": clean(str(payload.get("email") or "")), "role": role, "permissions": full_permissions() if is_admin else {key: bool(payload.get("permissions", {}).get(key)) for key in PERMISSION_KEYS}, "expiresAt": expires, "createdAt": now(), "createdByName": user["displayName"]}
                with lock: data.setdefault("invitations", []).insert(0, invitation); audit(workspace_id, user, "created", "invitation", invitation["id"], f"Einladung für {invitation['displayName'] or invitation['email']} erstellt"); save_data(data)
                return self.send_json(201, {**{key: value for key, value in invitation.items() if key != "tokenHash"}, "token": token})
            invitation = next((item for item in data.get("invitations", []) if item["id"] == invitation_id), None)
            if not invitation: return self.send_json(404, {"error": "Einladung nicht gefunden."})
            if method == "DELETE":
                invitation["revokedAt"] = now(); invitation["revokedByName"] = user["displayName"]; audit(workspace_id, user, "revoked", "invitation", invitation["id"], "Einladung widerrufen"); save_data(data); return self.send_json(200, {"ok": True})
            return self.send_json(405, {"error": "Methode nicht erlaubt."})
        if len(user_parts) in (2, 3) and user_parts[:2] == ["api", "users"]:
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "Nur Linea und die gesetzliche Betreuung dürfen Zugänge verwalten."})
            target_id = user_parts[2] if len(user_parts) == 3 else None
            if method == "POST" and not target_id:
                payload = self.read_json()
                username = str(payload.get("username", "")).strip().lower()
                password = str(payload.get("password", ""))
                if len(username) < 3 or any(u["username"].lower() == username for u in data["users"]):
                    return self.send_json(400, {"error": "Der Benutzername ist zu kurz oder bereits vergeben."})
                if len(password) < 8:
                    return self.send_json(400, {"error": "Das Passwort muss mindestens 8 Zeichen lang sein."})
                role = str(payload.get("role") or "Angehörige")
                is_admin = role in {"Leistungsberechtigte Person", "Gesetzliche Betreuung", "Enge Angehörige"}
                created = {
                    "id": str(uuid.uuid4()), "username": username,
                    "displayName": clean(str(payload.get("displayName") or username)), "role": role,
                    "memberId": payload.get("memberId") or None, "isAdmin": is_admin,
                    "active": True, "accessStatus": "active",
                    "notificationEmail": clean(str(payload.get("notificationEmail") or "")), "notificationPreference": "mentions",
                    "permissions": full_permissions() if is_admin else {key: bool(payload.get("permissions", {}).get(key)) for key in PERMISSION_KEYS},
                    "passwordHash": hash_password(password), "createdAt": now(),
                }
                created["workspaceMemberships"] = [{"workspaceId": workspace_id, "role": role, "isAdmin": is_admin, "permissions": created["permissions"]}]
                with lock: data["users"].append(created); save_data(data)
                audit(workspace_id, user, "created", "user", created["id"], f"Zugang für {created['displayName']} angelegt"); save_data(data)
                return self.send_json(201, self.public_user(created))
            target = next((u for u in data["users"] if u["id"] == target_id), None)
            if not target: return self.send_json(404, {"error": "Zugang nicht gefunden."})
            if method == "PUT":
                payload = self.read_json(); role = str(payload.get("role") or target["role"])
                target.update({"displayName": clean(str(payload.get("displayName") or target["displayName"])), "role": role})
                if "memberId" in payload: target["memberId"] = payload.get("memberId") or None
                target["isAdmin"] = role in {"Leistungsberechtigte Person", "Gesetzliche Betreuung", "Enge Angehörige"}
                if "permissions" in payload: target["permissions"] = full_permissions() if target["isAdmin"] else {key: bool(payload.get("permissions", {}).get(key)) for key in PERMISSION_KEYS}
                membership = next((item for item in target.get("workspaceMemberships", []) if item.get("workspaceId") == workspace_id), None)
                if membership:
                    membership.update({"role": role, "isAdmin": target["isAdmin"], "permissions": target["permissions"]})
                if payload.get("accessStatus") in {"active", "paused"}: target["accessStatus"] = payload["accessStatus"]; target["active"] = True
                if payload.get("password"):
                    if len(str(payload["password"])) < 8: return self.send_json(400, {"error": "Das Passwort muss mindestens 8 Zeichen lang sein."})
                    target["passwordHash"] = hash_password(str(payload["password"]))
                audit(workspace_id, user, "updated", "user", target["id"], f"Zugang von {target['displayName']} geändert"); save_data(data)
                return self.send_json(200, self.public_user(target))
            if method == "DELETE":
                if target["id"] == user["id"]: return self.send_json(400, {"error": "Du kannst deinen eigenen Zugang nicht löschen."})
                with lock:
                    target["active"] = False; target["accessStatus"] = "deactivated"
                    for token, session in list(sessions.items()):
                        if session["userId"] == target["id"]: sessions.pop(token, None)
                    audit(workspace_id, user, "deactivated", "user", target["id"], f"Zugang von {target['displayName']} deaktiviert"); save_data(data)
                return self.send_json(200, {"ok": True})

        parts = path.strip("/").split("/")
        if len(parts) not in (2, 3) or parts[0] != "api" or parts[1] not in COLLECTIONS:
            return self.send_json(404, {"error": "Nicht gefunden."})
        collection = parts[1]
        item_id = parts[2] if len(parts) == 3 else None
        if workspace.get("type") == "shared" and collection in {"cases", "correspondence", "documents"}:
            return self.send_json(403, {"error": "Dieser Inhalt gehört in einen persönlichen Bereich."})
        permission = {"cases":"cases", "correspondence":"cases", "tasks":"tasks", "calendarEvents":"calendar", "documents":"documents", "ledger":"ledger", "accounts":"family", "members":"family", "messages":"tasks"}[collection]
        editing_own_member = collection == "members" and method == "PUT" and item_id and item_id == user.get("memberId")
        if not self.allowed(user, permission) and not editing_own_member:
            return self.send_json(403, {"error": "Keine Berechtigung für diesen Bereich."})
        if collection == "accounts" and method in {"POST", "PUT", "DELETE"} and not user.get("isAdmin"):
            return self.send_json(403, {"error": "Konten dürfen nur von Administratoren geändert werden."})
        with lock:
            if method == "POST" and not item_id:
                payload = self.read_json()
                if collection == "correspondence" and not any(case["id"] == payload.get("caseId") for case in data["cases"]):
                    return self.send_json(400, {"error": "Der zugehörige Antrag wurde nicht gefunden."})
                if collection == "cases" and payload.get("parentCaseId") and not any(case["id"] == payload.get("parentCaseId") for case in data["cases"]):
                    return self.send_json(400, {"error": "Der Hauptantrag wurde nicht gefunden."})
                if collection == "tasks":
                    due = str(payload.get("due") or "")
                    if payload.get("recurrenceUntil") and due and str(payload["recurrenceUntil"]) < due: return self.send_json(400, {"error": "Das Ende der Wiederholung darf nicht vor dem ersten Datum liegen."})
                    if due and due < date.today().isoformat() and not user.get("isAdmin"):
                        return self.send_json(403, {"error": "Vergangene Aufgaben dürfen nur von Administratoren ergänzt werden."})
                    if payload.get("status") not in {None, "open", "planned", "done", "refused"}:
                        return self.send_json(400, {"error": "Ungültiger Aufgabenstatus."})
                    recurrence = str(payload.get("recurrence") or "once")
                    try:
                        dates = recurring_dates(due, recurrence, str(payload.get("recurrenceUntil") or "") or None)
                    except ValueError:
                        return self.send_json(400, {"error": "Das Datum der Wiederholung ist ungültig."})
                    series_id = str(uuid.uuid4()) if recurrence != "once" else None
                    created = []
                    for occurrence_due in dates or [due]:
                        occurrence = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, **clean(payload), "due": occurrence_due, "recurrenceSeriesId": series_id, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"], "history": []}
                        created.append(occurrence)
                    category = clean(str(payload.get("category") or ""))
                    choices = current_options["task"].setdefault("categories", [])
                    if category and category not in choices: choices.append(category)
                    data["tasks"][0:0] = created
                    audit(workspace_id, user, "created", "tasks", created[0]["id"], f"Aufgabe „{created[0].get('title', '')}“ angelegt")
                    save_data(data)
                    return self.send_json(201, created[0])
                if collection == "calendarEvents":
                    start_value = str(payload.get("date") or ""); recurrence = str(payload.get("recurrence") or "once")
                    if payload.get("endDate") and start_value and str(payload["endDate"]) < start_value: return self.send_json(400, {"error": "Das Terminende darf nicht vor dem Beginn liegen."})
                    if payload.get("recurrenceUntil") and start_value and str(payload["recurrenceUntil"]) < start_value: return self.send_json(400, {"error": "Das Ende der Wiederholung darf nicht vor dem ersten Termin liegen."})
                    try: dates = recurring_dates(start_value, recurrence, str(payload.get("recurrenceUntil") or "") or None)
                    except ValueError: return self.send_json(400, {"error": "Das Datum der Wiederholung ist ungültig."})
                    series_id = str(uuid.uuid4()) if recurrence != "once" else None; created = []
                    duration = (date.fromisoformat(str(payload.get("endDate") or start_value)) - date.fromisoformat(start_value)).days if start_value else 0
                    for occurrence_date in dates or [start_value]:
                        occurrence = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, **clean(payload), "date": occurrence_date, "recurrenceSeriesId": series_id, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                        if payload.get("endDate"): occurrence["endDate"] = (date.fromisoformat(occurrence_date) + timedelta(days=duration)).isoformat()
                        created.append(occurrence)
                    data["calendarEvents"][0:0] = created; audit(workspace_id, user, "created", "calendarEvents", created[0]["id"], f"Termin „{created[0].get('title', '')}“ angelegt"); save_data(data)
                    return self.send_json(201, created[0])
                receipt_image = payload.pop("receiptImage", None) if collection == "ledger" else None
                case_file = payload.pop("caseFile", None) if collection in {"cases", "correspondence"} else None
                case_file_name = payload.pop("caseFileName", "Dokument") if collection in {"cases", "correspondence"} else "Dokument"
                document_file = payload.pop("documentFile", None) if collection == "documents" else None
                document_file_name = payload.pop("documentFileName", "Dokument") if collection == "documents" else "Dokument"
                member_photo = payload.pop("photoData", None) if collection == "members" else None
                member_photo_name = payload.pop("photoName", "Profilfoto") if collection == "members" else "Profilfoto"
                item = {"id": str(uuid.uuid4()), "workspaceId": workspace_id, **clean(payload), "createdAt": now()}
                item["createdByUserId"] = user["id"]
                item["createdByName"] = user["displayName"]
                if receipt_image:
                    item["receiptFile"] = save_receipt(receipt_image)
                    item["receiptStatus"] = "available"
                if case_file:
                    item.update(save_case_file(case_file, str(case_file_name)))
                if document_file:
                    item.update(save_document_file(document_file, str(document_file_name)))
                if member_photo:
                    item.update(save_member_photo(member_photo, str(member_photo_name)))
                if collection == "correspondence":
                    sync_deadline_reminder(item, user)
                if collection == "ledger":
                    for field, key in (("description", "descriptions"), ("category", "categories")):
                        value = item.get(field)
                        choices = current_options["ledger"].setdefault(key, [])
                        if value and value not in choices: choices.append(value)
                data[collection].insert(0, item)
                audit(workspace_id, user, "created", collection, item["id"], f"{collection}: {item.get('title') or item.get('description') or item.get('name') or 'Eintrag'}")
                save_data(data)
                return self.send_json(201, item)
            index = next((i for i, item in enumerate(data[collection]) if item["id"] == item_id and (item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id or collection == "members" and editing_own_member)), -1)
            if index < 0:
                return self.send_json(404, {"error": "Eintrag nicht gefunden."})
            if method == "PUT":
                payload = self.read_json()
                if collection == "members" and editing_own_member and not user.get("isAdmin"):
                    allowed_profile_fields = {"email", "phone", "address", "personalWords", "photoData", "photoName", "color"}
                    payload = {key: value for key, value in payload.items() if key in allowed_profile_fields}
                series_scope = str(payload.pop("_seriesScope", "single")); existing_series = data[collection][index].get("recurrenceSeriesId")
                if collection in {"tasks", "calendarEvents"} and existing_series and series_scope in {"future", "all"}:
                    date_field = "due" if collection == "tasks" else "date"; old_date = data[collection][index].get(date_field, ""); new_date = str(payload.get(date_field) or old_date)
                    if payload.get("recurrenceUntil") and new_date and str(payload["recurrenceUntil"]) < new_date: return self.send_json(400, {"error": "Das Ende der Wiederholung darf nicht vor dem ersten Datum liegen."})
                    shift = (date.fromisoformat(new_date) - date.fromisoformat(old_date)).days if old_date and new_date else 0
                    targets = [entry for entry in data[collection] if entry.get("recurrenceSeriesId") == existing_series and (series_scope == "all" or entry.get(date_field, "") >= old_date)]
                    common = clean({key: value for key, value in payload.items() if key not in {"due", "date", "endDate"}})
                    for target in targets:
                        target.update(common)
                        if shift and target.get(date_field): target[date_field] = (date.fromisoformat(target[date_field]) + timedelta(days=shift)).isoformat()
                        if collection == "calendarEvents" and shift and target.get("endDate"): target["endDate"] = (date.fromisoformat(target["endDate"]) + timedelta(days=shift)).isoformat()
                        target["updatedAt"] = now(); target["updatedByUserId"] = user["id"]
                    audit(workspace_id, user, "updated-series", collection, existing_series, f"{len(targets)} Vorkommnisse einer Serie geändert"); save_data(data)
                    return self.send_json(200, data[collection][index])
                if collection == "tasks":
                    existing = data[collection][index]
                    if existing.get("deletedAt"):
                        return self.send_json(409, {"error": "Eine gelöschte Aufgabe kann nicht bearbeitet werden."})
                    due = str(payload.get("due") or existing.get("due") or "")
                    if due and due < date.today().isoformat() and not user.get("isAdmin") and due != existing.get("due"):
                        return self.send_json(403, {"error": "Vergangene Aufgaben dürfen nur von Administratoren ergänzt werden."})
                    old_due = existing.get("due") or ""
                    if due != old_due:
                        existing.setdefault("history", []).append({"type": "rescheduled", "from": old_due, "to": due, "at": now(), "byUserId": user["id"], "byName": user["displayName"]})
                    category = clean(str(payload.get("category") or existing.get("category") or ""))
                    choices = current_options["task"].setdefault("categories", [])
                    if category and category not in choices: choices.append(category)
                receipt_image = payload.pop("receiptImage", None) if collection == "ledger" else None
                case_file = payload.pop("caseFile", None) if collection in {"cases", "correspondence"} else None
                case_file_name = payload.pop("caseFileName", "Dokument") if collection in {"cases", "correspondence"} else "Dokument"
                document_file = payload.pop("documentFile", None) if collection == "documents" else None
                document_file_name = payload.pop("documentFileName", "Dokument") if collection == "documents" else "Dokument"
                member_photo = payload.pop("photoData", None) if collection == "members" else None
                member_photo_name = payload.pop("photoName", "Profilfoto") if collection == "members" else "Profilfoto"
                data[collection][index].update(clean(payload))
                data[collection][index]["updatedByUserId"] = user["id"]
                if receipt_image:
                    data[collection][index]["receiptFile"] = save_receipt(receipt_image)
                    data[collection][index]["receiptStatus"] = "available"
                if case_file:
                    old_attachment = data[collection][index].get("attachmentFile")
                    data[collection][index].update(save_case_file(case_file, str(case_file_name)))
                    if old_attachment:
                        (CASE_FILES_DIR / Path(old_attachment).name).unlink(missing_ok=True)
                if document_file:
                    old_attachment = data[collection][index].get("attachmentFile")
                    data[collection][index].update(save_document_file(document_file, str(document_file_name)))
                    if old_attachment:
                        (DOCUMENT_FILES_DIR / Path(old_attachment).name).unlink(missing_ok=True)
                if member_photo:
                    old_photo = data[collection][index].get("photoFile")
                    data[collection][index].update(save_member_photo(member_photo, str(member_photo_name)))
                    if old_photo:
                        (MEMBER_FILES_DIR / Path(old_photo).name).unlink(missing_ok=True)
                if collection == "correspondence":
                    sync_deadline_reminder(data[collection][index], user)
                elif collection == "ledger" and data[collection][index].get("receiptStatus") == "none":
                    old_receipt = data[collection][index].pop("receiptFile", None)
                    data[collection][index].pop("receipt", None)
                    if old_receipt:
                        (RECEIPTS_DIR / Path(old_receipt).name).unlink(missing_ok=True)
                if collection == "ledger":
                    for field, key in (("description", "descriptions"), ("category", "categories")):
                        value = data[collection][index].get(field)
                        choices = current_options["ledger"].setdefault(key, [])
                        if value and value not in choices: choices.append(value)
                data[collection][index]["updatedAt"] = now()
                audit(workspace_id, user, "updated", collection, data[collection][index]["id"], f"{collection}: Eintrag geändert")
                save_data(data)
                return self.send_json(200, data[collection][index])
            if method == "DELETE":
                delete_payload = self.read_json(); series_scope = str(delete_payload.get("_seriesScope") or "single"); selected = data[collection][index]; series_id = selected.get("recurrenceSeriesId")
                if collection in {"tasks", "calendarEvents"} and series_id and series_scope in {"future", "all"}:
                    date_field = "due" if collection == "tasks" else "date"; selected_date = selected.get(date_field, "")
                    targets = [entry for entry in data[collection] if entry.get("recurrenceSeriesId") == series_id and (series_scope == "all" or entry.get(date_field, "") >= selected_date)]
                    if collection == "tasks" and not user.get("isAdmin"):
                        for task in targets: task["deletedAt"] = now(); task["deletedByUserId"] = user["id"]; task["deletedByName"] = user["displayName"]
                        save_data(data); return self.send_json(200, {"ok": True, "pendingAdminConfirmation": True, "affected": len(targets)})
                    target_ids = {entry["id"] for entry in targets}; data[collection] = [entry for entry in data[collection] if entry["id"] not in target_ids]
                    audit(workspace_id, user, "deleted-series", collection, series_id, f"{len(targets)} Vorkommnisse einer Serie gelöscht"); save_data(data)
                    return self.send_json(200, {"ok": True, "affected": len(targets)})
                if collection == "tasks" and not user.get("isAdmin"):
                    task = data[collection][index]
                    task["deletedAt"] = now()
                    task["deletedByUserId"] = user["id"]
                    task["deletedByName"] = user["displayName"]
                    audit(workspace_id, user, "delete-requested", "tasks", task["id"], f"Löschung von „{task.get('title', '')}“ vorgemerkt")
                    save_data(data)
                    return self.send_json(200, {"ok": True, "pendingAdminConfirmation": True})
                removed = data[collection].pop(index)
                if collection == "correspondence" and removed.get("reminderTaskId"):
                    data["tasks"] = [task for task in data["tasks"] if task.get("id") != removed["reminderTaskId"]]
                if collection in {"cases", "correspondence"} and removed.get("attachmentFile"):
                    (CASE_FILES_DIR / Path(removed["attachmentFile"]).name).unlink(missing_ok=True)
                if collection == "documents" and removed.get("attachmentFile"):
                    (DOCUMENT_FILES_DIR / Path(removed["attachmentFile"]).name).unlink(missing_ok=True)
                if collection == "members" and removed.get("photoFile"):
                    (MEMBER_FILES_DIR / Path(removed["photoFile"]).name).unlink(missing_ok=True)
                audit(workspace_id, user, "deleted", collection, removed["id"], f"{collection}: {removed.get('title') or removed.get('description') or removed.get('name') or 'Eintrag'} gelöscht")
                save_data(data)
                return self.send_json(200, {"ok": True})
        return self.send_json(405, {"error": "Methode nicht erlaubt."})

    def serve_static(self, path: str):
        relative = "index.html" if path == "/" else path.lstrip("/")
        file = (PUBLIC_DIR / relative).resolve()
        if PUBLIC_DIR not in file.parents or not file.is_file():
            self.send_error(404)
            return
        content = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        # Static assets deliberately use stable, human-readable names. Revalidate
        # them on every request so browsers and reverse proxies cannot retain a
        # previous frontend after a container update.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(content)

    def route(self, method: str):
        try:
            path = urlparse(self.path).path
            return self.api(method, path) if path.startswith("/api/") else self.serve_static(path)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            print(f"Fehler: {error}")
            self.send_json(500, {"error": "Das hat nicht geklappt."})

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")
    def do_DELETE(self): self.route("DELETE")


if __name__ == "__main__":
    if PASSWORD == "miteinander":
        print("WARNUNG: APP_PASSWORD wurde nicht gesetzt.")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Miteinander läuft auf Port {PORT}")
    server.serve_forever()
