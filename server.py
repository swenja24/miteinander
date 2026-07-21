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
MAX_BODY = 8_000_000
RECEIPTS_DIR = DATA_DIR / "receipts"
CASE_FILES_DIR = DATA_DIR / "case-files"
DOCUMENT_FILES_DIR = DATA_DIR / "document-files"
ABOUT_FILES_DIR = DATA_DIR / "about-files"
ROUTINE_FILES_DIR = DATA_DIR / "routine-files"
MEMBER_FILES_DIR = DATA_DIR / "member-files"
COLLECTIONS = {"cases", "correspondence", "tasks", "documents", "messages", "ledger", "members", "accounts"}
lock = threading.Lock()
sessions: dict[str, dict] = {}
PERMISSION_KEYS = ("cases", "tasks", "documents", "ledger", "family")
WORKSPACE_COLLECTIONS = ("cases", "correspondence", "tasks", "documents", "ledger", "accounts", "members", "announcements", "topicComments", "goals", "rules", "aboutComments", "importantContacts", "beis", "routinePlans")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def empty_data() -> dict:
    cash_id, savings_id, shared_id, bank_id = (str(uuid.uuid4()) for _ in range(4))
    return {
        "family": {"name": "Unsere Familie", "person": "Linea", "createdAt": now()},
        "cases": [], "correspondence": [], "tasks": [], "documents": [], "messages": [], "ledger": [],
        "announcements": [], "topicComments": [],
        "personProfile": {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""},
        "goals": [], "rules": [], "aboutComments": [], "importantContacts": [], "beis": [], "routinePlans": [], "invitations": [],
        "contactOptions": {"categories": ["Eltern / Familie", "Fahrdienst / Busunternehmen", "WfB / Arbeit", "Ärzt*innen", "Therapie", "Pflege", "Behörde", "Wohnen", "Notfallkontakt", "Sonstiges"]},
        "ledgerOptions": {"descriptions": [], "categories": []},
        "taskOptions": {"categories": []},
        "accounts": [
            {"id": cash_id, "name": "Lineas Barkasse", "type": "Bargeld", "color": "#285c4d"},
            {"id": savings_id, "name": "Lineas Bargeld-Spardose", "type": "Bargeld", "color": "#5b57c8"},
            {"id": shared_id, "name": "Gemeinsame WG-Barkasse", "type": "Bargeld", "color": "#d86f3f"},
            {"id": bank_id, "name": "Wohnungs- und Einkommenskonto", "type": "Bankkonto", "color": "#397a92"},
        ],
        "members": [
            {"id": str(uuid.uuid4()), "name": "Linea", "role": "Leistungsberechtigte Person", "color": "#5a57d9"},
            {"id": str(uuid.uuid4()), "name": "Familie", "role": "Angehörige", "color": "#e57d45"},
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
        "goals": [], "rules": [], "aboutComments": [], "importantContacts": [], "beis": [], "routinePlans": [], "invitations": [],
        "announcements": [], "topicComments": [],
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
    if not loaded.get("workspaces"):
        workspace_id = str(uuid.uuid4())
        loaded["workspaces"] = [{"id": workspace_id, "name": loaded.get("family", {}).get("name") or "Lineas Bereich", "type": "private", "personName": loaded.get("family", {}).get("person") or "Linea", "createdAt": now()}]
        for key in WORKSPACE_COLLECTIONS:
            for item in loaded.get(key, []): item.setdefault("workspaceId", workspace_id)
        for existing_user in loaded.get("users", []):
            existing_user["workspaceMemberships"] = [{"workspaceId": workspace_id, "role": existing_user.get("role", "Assistenz"), "isAdmin": bool(existing_user.get("isAdmin")), "permissions": existing_user.get("permissions", {})}]
        changed = True
    default_workspace_id = loaded["workspaces"][0]["id"]
    for existing_user in loaded.get("users", []):
        if not existing_user.get("workspaceMemberships"):
            existing_user["workspaceMemberships"] = [{"workspaceId": default_workspace_id, "role": existing_user.get("role", "Assistenz"), "isAdmin": bool(existing_user.get("isAdmin")), "permissions": existing_user.get("permissions", {})}]
            changed = True
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


def clean(value):
    if isinstance(value, str):
        return value.strip()[:5000]
    if isinstance(value, dict):
        return {str(k)[:100]: clean(v) for k, v in value.items() if k not in {"id", "createdAt", "updatedAt"}}
    if isinstance(value, list):
        return [clean(v) for v in value[:1000]]
    return value


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
    if recurrence in {"weekly", "biweekly"}:
        return current + timedelta(days=7 if recurrence == "weekly" else 14)
    if recurrence == "monthly":
        month = current.month + 1
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
        contextual = {**base_user, "role": membership.get("role", base_user.get("role")), "isAdmin": bool(membership.get("isAdmin")), "permissions": membership.get("permissions", {})}
        return workspace["id"], workspace, contextual

    def api(self, method: str, path: str):
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
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
            membership = {"workspaceId": invitation_workspace_id, "role": role, "isAdmin": is_admin, "permissions": full_permissions() if is_admin else invitation.get("permissions", {})}
            created = {"id": str(uuid.uuid4()), "username": username, "displayName": display_name, "role": role, "memberId": member["id"], "isAdmin": is_admin, "active": True, "accessStatus": "active", "permissions": membership["permissions"], "workspaceMemberships": [membership], "notificationEmail": member["email"], "notificationPreference": "mentions", "passwordHash": hash_password(password), "createdAt": now()}
            with lock:
                data["members"].append(member); data["users"].append(created); invitation["acceptedAt"] = now(); invitation["acceptedByUserId"] = created["id"]; save_data(data)
            token_value = secrets.token_urlsafe(32); sessions[token_value] = {"userId": created["id"], "expires": time.time() + 43200}
            return self.send_json(201, {"ok": True, "user": self.public_user(created)}, {"Set-Cookie": f"miteinander_session={token_value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"})
        if path == "/api/login" and method == "POST":
            credentials = self.read_json()
            username = str(credentials.get("username") or "linea").strip().lower()
            supplied = str(credentials.get("password", ""))
            user = next((u for u in data.get("users", []) if u.get("active", True) and u.get("accessStatus", "active") == "active" and u.get("username", "").lower() == username), None)
            if not user or not password_matches(supplied, user.get("passwordHash", "")):
                return self.send_json(401, {"error": "Benutzername oder Passwort stimmen nicht."})
            token = secrets.token_urlsafe(32)
            sessions[token] = {"userId": user["id"], "expires": time.time() + 43200}
            cookie = f"miteinander_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"
            return self.send_json(200, {"ok": True, "user": self.public_user(user)}, {"Set-Cookie": cookie})
        if path == "/api/logout" and method == "POST":
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            if jar.get("miteinander_session"):
                sessions.pop(jar["miteinander_session"].value, None)
            return self.send_json(200, {"ok": True}, {"Set-Cookie": "miteinander_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        base_user = self.session_user()
        if not base_user:
            return self.send_json(401, {"error": "Bitte anmelden."})
        try:
            workspace_id, workspace, user = self.workspace_context(base_user)
        except PermissionError as error:
            return self.send_json(403, {"error": str(error)})
        if workspace.get("type") == "shared" and (path.startswith("/api/about/") or path.startswith("/api/contacts") or path in {"/api/family", "/api/person-profile", "/api/profile-photo", "/api/contact-options"}):
            return self.send_json(403, {"error": "Dieser Inhalt gehört in einen persönlichen Bereich."})
        if path == "/api/data" and method == "GET":
            with lock:
                if ensure_rolling_tasks(data):
                    save_data(data)
            scoped = lambda key: [item for item in data.get(key, []) if item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id]
            workspace_user_ids = {candidate["id"] for candidate in data["users"] if any(m.get("workspaceId") == workspace_id for m in candidate.get("workspaceMemberships", []))}
            workspace_member_ids = {candidate.get("memberId") for candidate in data["users"] if candidate["id"] in workspace_user_ids and candidate.get("memberId")}
            visible = {
                "family": {"name": workspace["name"], "person": workspace.get("personName", "")},
                "workspace": workspace,
                "workspaces": [space for space in data["workspaces"] if any(m.get("workspaceId") == space["id"] for m in base_user.get("workspaceMemberships", []))],
                "personProfile": data["personProfile"] if workspace.get("type") == "private" else {},
                "goals": scoped("goals") if workspace.get("type") == "private" else [],
                "rules": scoped("rules") if workspace.get("type") == "private" else [],
                "aboutComments": scoped("aboutComments") if workspace.get("type") == "private" else [],
                "importantContacts": scoped("importantContacts"),
                "contactOptions": data["contactOptions"],
                "beis": scoped("beis") if workspace.get("type") == "private" else [],
                "routinePlans": scoped("routinePlans") if self.allowed(user, "tasks") else [],
                "cases": scoped("cases") if self.allowed(user, "cases") and workspace.get("type") == "private" else [],
                "correspondence": scoped("correspondence") if self.allowed(user, "cases") and workspace.get("type") == "private" else [],
                "tasks": [task for task in scoped("tasks") if user.get("isAdmin") or not task.get("deletedAt")] if self.allowed(user, "tasks") else [],
                "taskOptions": (data.get("taskOptions", {"categories": []}) if workspace.get("type") == "private" else {"categories": sorted({str(item.get("category", "")).strip() for item in scoped("tasks") if item.get("category")})}) if self.allowed(user, "tasks") else {"categories": []},
                "documents": scoped("documents") if self.allowed(user, "documents") and workspace.get("type") == "private" else [],
                "messages": [],
                "announcements": scoped("announcements"),
                "topicComments": [item for item in scoped("topicComments") if item.get("targetType") == "goal" or item.get("targetType") == "case" and self.allowed(user, "cases") or item.get("targetType") == "task" and self.allowed(user, "tasks")],
                "ledger": scoped("ledger") if self.allowed(user, "ledger") else [],
                "accounts": scoped("accounts") if self.allowed(user, "ledger") else [],
                "ledgerOptions": (data.get("ledgerOptions", {"descriptions": [], "categories": []}) if workspace.get("type") == "private" else {"descriptions": sorted({str(item.get("description", "")).strip() for item in scoped("ledger") if item.get("description")}), "categories": sorted({str(item.get("category", "")).strip() for item in scoped("ledger") if item.get("category")})}) if self.allowed(user, "ledger") else {"descriptions": [], "categories": []},
                "members": scoped("members") if workspace.get("type") == "private" else [member for member in data.get("members", []) if member["id"] in workspace_member_ids],
                "currentUser": self.public_user(user),
                "users": [self.public_user(u) for u in data["users"] if u["id"] in workspace_user_ids] if user.get("isAdmin") else [{"id": u["id"], "displayName": u["displayName"], "username": u.get("username", "")} for u in data["users"] if u["id"] in workspace_user_ids and u.get("active", True)],
                "invitations": [{key: value for key, value in item.items() if key != "tokenHash"} for item in data.get("invitations", []) if item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id] if user.get("isAdmin") else [],
                "capabilities": {key: self.allowed(user, key) and not (workspace.get("type") == "shared" and key in {"cases", "documents", "family"}) for key in PERMISSION_KEYS} | {"manageAccess": bool(user.get("isAdmin"))},
                "emailNotificationsConfigured": bool(SMTP_HOST),
            }
            return self.send_json(200, visible)
        workspace_parts = path.strip("/").split("/")
        if workspace_parts[:2] == ["api", "workspaces"]:
            if not user.get("isAdmin"): return self.send_json(403, {"error": "Nur Administrator*innen dürfen Bereiche verwalten."})
            target_workspace_id = workspace_parts[2] if len(workspace_parts) >= 3 else None
            action = workspace_parts[3] if len(workspace_parts) == 4 else None
            if method == "POST" and not target_workspace_id:
                payload = self.read_json(); name = clean(str(payload.get("name") or ""))
                if not name: return self.send_json(400, {"error": "Der Bereich benötigt einen Namen."})
                created_workspace = {"id": str(uuid.uuid4()), "name": name, "type": "shared", "personName": "", "createdAt": now(), "createdByUserId": user["id"]}
                membership = {"workspaceId": created_workspace["id"], "role": "Bereichsverwaltung", "isAdmin": True, "permissions": full_permissions()}
                account = {"id": str(uuid.uuid4()), "workspaceId": created_workspace["id"], "name": f"{name} – Gemeinschaftskasse", "type": "Bargeld", "color": "#285c4d", "createdAt": now()}
                with lock:
                    data["workspaces"].append(created_workspace); base_user.setdefault("workspaceMemberships", []).append(membership); data["accounts"].append(account)
                    for target_id in payload.get("memberUserIds", []):
                        target_user = next((candidate for candidate in data["users"] if candidate["id"] == target_id and candidate["id"] != base_user["id"]), None)
                        if target_user and not any(m.get("workspaceId") == created_workspace["id"] for m in target_user.get("workspaceMemberships", [])):
                            target_user.setdefault("workspaceMemberships", []).append({"workspaceId": created_workspace["id"], "role": "WG-Team", "isAdmin": False, "permissions": {"tasks": True, "ledger": True, "cases": False, "documents": False, "family": False}})
                    save_data(data)
                return self.send_json(201, created_workspace)
            target_workspace = next((space for space in data["workspaces"] if space["id"] == target_workspace_id), None)
            if not target_workspace or target_workspace_id != workspace_id: return self.send_json(404, {"error": "Bereich nicht gefunden."})
            if action == "memberships" and method == "POST":
                payload = self.read_json(); target_user = next((candidate for candidate in data["users"] if candidate["id"] == payload.get("userId")), None)
                if not target_user: return self.send_json(404, {"error": "Person nicht gefunden."})
                existing = next((m for m in target_user.get("workspaceMemberships", []) if m.get("workspaceId") == workspace_id), None)
                permissions = {key: bool(payload.get("permissions", {}).get(key)) for key in PERMISSION_KEYS}; permissions.update({"tasks": True, "ledger": True})
                values = {"workspaceId": workspace_id, "role": clean(str(payload.get("role") or "Mitglied")), "isAdmin": bool(payload.get("isAdmin")), "permissions": full_permissions() if payload.get("isAdmin") else permissions}
                if existing: existing.update(values)
                else: target_user.setdefault("workspaceMemberships", []).append(values)
                save_data(data); return self.send_json(200, self.public_user(target_user))
            if action == "memberships" and method == "DELETE":
                payload = self.read_json(); target_user = next((candidate for candidate in data["users"] if candidate["id"] == payload.get("userId")), None)
                if not target_user or target_user["id"] == user["id"]: return self.send_json(400, {"error": "Diese Mitgliedschaft kann nicht entfernt werden."})
                target_user["workspaceMemberships"] = [m for m in target_user.get("workspaceMemberships", []) if m.get("workspaceId") != workspace_id]; save_data(data); return self.send_json(200, {"ok": True})
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
                values = clean(self.read_json()); data["family"].update(values)
                workspace["name"] = values.get("name") or workspace["name"]; workspace["personName"] = values.get("person") or workspace.get("personName", "")
                data["family"]["updatedAt"] = now()
                save_data(data)
            return self.send_json(200, data["family"])

        if path == "/api/person-profile" and method == "PUT":
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "Das Personenprofil darf nur von Administratoren geändert werden."})
            with lock:
                data["personProfile"].update(clean(self.read_json()))
                data["personProfile"]["updatedAt"] = now()
                data["personProfile"]["updatedByName"] = user["displayName"]
                save_data(data)
            return self.send_json(200, data["personProfile"])

        if path == "/api/profile-photo" and method == "PUT":
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "Das Profilfoto darf nur von Administratoren geändert werden."})
            payload = self.read_json(); photo_data = str(payload.get("photoData") or "")
            if not photo_data.startswith("data:image/"):
                return self.send_json(400, {"error": "Bitte wähle ein Bild aus."})
            saved = save_about_file(photo_data, str(payload.get("photoName") or "Profilfoto"))
            old_photo = data["personProfile"].get("photoFile")
            with lock:
                data["personProfile"].update({"photoFile": saved["attachmentFile"], "photoName": saved["attachmentName"], "photoType": saved["attachmentType"], "updatedAt": now(), "updatedByName": user["displayName"]})
                if old_photo: (ABOUT_FILES_DIR / Path(old_photo).name).unlink(missing_ok=True)
                save_data(data)
            return self.send_json(200, data["personProfile"])

        bei_parts = path.strip("/").split("/")
        if len(bei_parts) in (3, 4) and bei_parts[:3] == ["api", "about", "beis"]:
            if not user.get("isAdmin"):
                return self.send_json(403, {"error": "BEIs dürfen nur von Administratoren verwaltet werden."})
            bei_id = bei_parts[3] if len(bei_parts) == 4 else None
            if method == "POST" and not bei_id:
                payload = self.read_json(); file_data = str(payload.pop("beiFile", "")); file_name = str(payload.pop("beiFileName", "BEI.pdf"))
                if not file_data.startswith("data:application/pdf"):
                    return self.send_json(400, {"error": "Ein BEI muss als PDF hochgeladen werden."})
                bei = {"id": str(uuid.uuid4()), **clean(payload), **save_about_file(file_data, file_name), "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                with lock: data["beis"].insert(0, bei); save_data(data)
                return self.send_json(201, bei)
            bei = next((item for item in data["beis"] if item["id"] == bei_id), None)
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
                data["contactOptions"]["categories"] = list(dict.fromkeys(clean(str(value)) for value in values if str(value).strip()))[:200]
                save_data(data)
            return self.send_json(200, data["contactOptions"])

        contact_parts = path.strip("/").split("/")
        if len(contact_parts) in (2, 3) and contact_parts[:2] == ["api", "contacts"]:
            contact_id = contact_parts[2] if len(contact_parts) == 3 else None
            if method == "POST" and not contact_id:
                payload = clean(self.read_json())
                if not payload.get("name"):
                    return self.send_json(400, {"error": "Bitte gib einen Namen oder eine Organisation an."})
                contact = {"id": str(uuid.uuid4()), **payload, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                with lock:
                    data["importantContacts"].insert(0, contact)
                    save_data(data)
                return self.send_json(201, contact)
            contact = next((entry for entry in data["importantContacts"] if entry["id"] == contact_id), None)
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
                    if not target_collection or not any(item["id"] == payload.get("targetId") for item in data[target_collection]):
                        return self.send_json(400, {"error": "Das Ziel oder die Regel wurde nicht gefunden."})
                item = {"id": str(uuid.uuid4()), **payload, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"]}
                if kind == "rules":
                    item["approvalStatus"] = "pending"
                with lock:
                    data[collection].insert(0, item)
                    save_data(data)
                return self.send_json(201, item)
            item = next((entry for entry in data[collection] if entry["id"] == item_id), None)
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
                        data.setdefault("ledgerOptions", {})[key] = list(dict.fromkeys(clean(str(value)) for value in values if str(value).strip()))[:200]
                save_data(data)
            return self.send_json(200, data["ledgerOptions"])

        if path == "/api/task-options" and method == "PUT":
            if not self.allowed(user, "tasks"):
                return self.send_json(403, {"error": "Keine Berechtigung für Aufgaben."})
            payload = self.read_json()
            values = payload.get("categories")
            if not isinstance(values, list):
                return self.send_json(400, {"error": "Kategorien müssen als Liste übermittelt werden."})
            with lock:
                data["taskOptions"] = {"categories": list(dict.fromkeys(clean(str(value)) for value in values if str(value).strip()))[:200]}
                save_data(data)
            return self.send_json(200, data["taskOptions"])

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
                with lock: data.setdefault("invitations", []).insert(0, invitation); save_data(data)
                return self.send_json(201, {**{key: value for key, value in invitation.items() if key != "tokenHash"}, "token": token})
            invitation = next((item for item in data.get("invitations", []) if item["id"] == invitation_id), None)
            if not invitation: return self.send_json(404, {"error": "Einladung nicht gefunden."})
            if method == "DELETE":
                invitation["revokedAt"] = now(); invitation["revokedByName"] = user["displayName"]; save_data(data); return self.send_json(200, {"ok": True})
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
                return self.send_json(201, self.public_user(created))
            target = next((u for u in data["users"] if u["id"] == target_id), None)
            if not target: return self.send_json(404, {"error": "Zugang nicht gefunden."})
            if method == "PUT":
                payload = self.read_json(); role = str(payload.get("role") or target["role"])
                target.update({"displayName": clean(str(payload.get("displayName") or target["displayName"])), "role": role})
                if "memberId" in payload: target["memberId"] = payload.get("memberId") or None
                target["isAdmin"] = role in {"Leistungsberechtigte Person", "Gesetzliche Betreuung", "Enge Angehörige"}
                if "permissions" in payload: target["permissions"] = full_permissions() if target["isAdmin"] else {key: bool(payload.get("permissions", {}).get(key)) for key in PERMISSION_KEYS}
                if payload.get("accessStatus") in {"active", "paused"}: target["accessStatus"] = payload["accessStatus"]; target["active"] = True
                if payload.get("password"):
                    if len(str(payload["password"])) < 8: return self.send_json(400, {"error": "Das Passwort muss mindestens 8 Zeichen lang sein."})
                    target["passwordHash"] = hash_password(str(payload["password"]))
                with lock: save_data(data)
                return self.send_json(200, self.public_user(target))
            if method == "DELETE":
                if target["id"] == user["id"]: return self.send_json(400, {"error": "Du kannst deinen eigenen Zugang nicht löschen."})
                with lock:
                    target["active"] = False; target["accessStatus"] = "deactivated"
                    for token, session in list(sessions.items()):
                        if session["userId"] == target["id"]: sessions.pop(token, None)
                    save_data(data)
                return self.send_json(200, {"ok": True})

        parts = path.strip("/").split("/")
        if len(parts) not in (2, 3) or parts[0] != "api" or parts[1] not in COLLECTIONS:
            return self.send_json(404, {"error": "Nicht gefunden."})
        collection = parts[1]
        if workspace.get("type") == "shared" and collection in {"cases", "correspondence", "documents"}:
            return self.send_json(403, {"error": "Dieser Inhalt gehört in einen persönlichen Bereich."})
        permission = {"cases":"cases", "correspondence":"cases", "tasks":"tasks", "documents":"documents", "ledger":"ledger", "accounts":"family", "members":"family", "messages":"tasks"}[collection]
        if not self.allowed(user, permission):
            return self.send_json(403, {"error": "Keine Berechtigung für diesen Bereich."})
        if collection == "accounts" and method in {"POST", "PUT", "DELETE"} and not user.get("isAdmin"):
            return self.send_json(403, {"error": "Konten dürfen nur von Administratoren geändert werden."})
        item_id = parts[2] if len(parts) == 3 else None
        with lock:
            if method == "POST" and not item_id:
                payload = self.read_json()
                if collection == "correspondence" and not any(case["id"] == payload.get("caseId") for case in data["cases"]):
                    return self.send_json(400, {"error": "Der zugehörige Antrag wurde nicht gefunden."})
                if collection == "cases" and payload.get("parentCaseId") and not any(case["id"] == payload.get("parentCaseId") for case in data["cases"]):
                    return self.send_json(400, {"error": "Der Hauptantrag wurde nicht gefunden."})
                if collection == "tasks":
                    due = str(payload.get("due") or "")
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
                    choices = data.setdefault("taskOptions", {}).setdefault("categories", [])
                    if category and category not in choices: choices.append(category)
                    data["tasks"][0:0] = created
                    save_data(data)
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
                        choices = data.setdefault("ledgerOptions", {}).setdefault(key, [])
                        if value and value not in choices: choices.append(value)
                data[collection].insert(0, item)
                save_data(data)
                return self.send_json(201, item)
            index = next((i for i, item in enumerate(data[collection]) if item["id"] == item_id and item.get("workspaceId", data["workspaces"][0]["id"]) == workspace_id), -1)
            if index < 0:
                return self.send_json(404, {"error": "Eintrag nicht gefunden."})
            if method == "PUT":
                payload = self.read_json()
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
                    choices = data.setdefault("taskOptions", {}).setdefault("categories", [])
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
                        choices = data.setdefault("ledgerOptions", {}).setdefault(key, [])
                        if value and value not in choices: choices.append(value)
                data[collection][index]["updatedAt"] = now()
                save_data(data)
                return self.send_json(200, data[collection][index])
            if method == "DELETE":
                if collection == "tasks" and not user.get("isAdmin"):
                    task = data[collection][index]
                    task["deletedAt"] = now()
                    task["deletedByUserId"] = user["id"]
                    task["deletedByName"] = user["displayName"]
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
