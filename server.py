from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import base64
import secrets
import threading
import time
import uuid
import calendar
from datetime import date, timedelta
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
MAX_BODY = 8_000_000
RECEIPTS_DIR = DATA_DIR / "receipts"
CASE_FILES_DIR = DATA_DIR / "case-files"
DOCUMENT_FILES_DIR = DATA_DIR / "document-files"
COLLECTIONS = {"cases", "correspondence", "tasks", "documents", "messages", "ledger", "members", "accounts"}
lock = threading.Lock()
sessions: dict[str, dict] = {}
PERMISSION_KEYS = ("cases", "tasks", "documents", "ledger", "family")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def empty_data() -> dict:
    cash_id, savings_id, shared_id, bank_id = (str(uuid.uuid4()) for _ in range(4))
    return {
        "family": {"name": "Unsere Familie", "person": "Linea", "createdAt": now()},
        "cases": [], "correspondence": [], "tasks": [], "documents": [], "messages": [], "ledger": [],
        "personProfile": {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""},
        "goals": [], "rules": [], "aboutComments": [], "importantContacts": [],
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
    try:
        loaded = json.loads(DATA_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        loaded = empty_data()
    changed = False
    for key, default in {
        "personProfile": {"introduction": "", "strengths": "", "supportNeeds": "", "beiSummary": "", "wishes": ""},
        "goals": [], "rules": [], "aboutComments": [], "importantContacts": [],
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
        return next((u for u in data.get("users", []) if u["id"] == session["userId"]), None)

    @staticmethod
    def allowed(user: dict, permission: str) -> bool:
        return bool(user.get("isAdmin") or user.get("permissions", {}).get(permission))

    @staticmethod
    def public_user(user: dict) -> dict:
        return {key: value for key, value in user.items() if key != "passwordHash"}

    def api(self, method: str, path: str):
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        if path == "/api/login" and method == "POST":
            credentials = self.read_json()
            username = str(credentials.get("username") or "linea").strip().lower()
            supplied = str(credentials.get("password", ""))
            user = next((u for u in data.get("users", []) if u.get("active", True) and u.get("username", "").lower() == username), None)
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
        user = self.session_user()
        if not user:
            return self.send_json(401, {"error": "Bitte anmelden."})
        if path == "/api/data" and method == "GET":
            with lock:
                if ensure_rolling_tasks(data):
                    save_data(data)
            visible = {
                "family": data["family"],
                "personProfile": data["personProfile"],
                "goals": data["goals"],
                "rules": data["rules"],
                "aboutComments": data["aboutComments"],
                "importantContacts": data["importantContacts"],
                "cases": data["cases"] if self.allowed(user, "cases") else [],
                "correspondence": data["correspondence"] if self.allowed(user, "cases") else [],
                "tasks": [task for task in data["tasks"] if user.get("isAdmin") or not task.get("deletedAt")] if self.allowed(user, "tasks") else [],
                "taskOptions": data.get("taskOptions", {"categories": []}) if self.allowed(user, "tasks") else {"categories": []},
                "documents": data["documents"] if self.allowed(user, "documents") else [],
                "messages": [],
                "ledger": data["ledger"] if self.allowed(user, "ledger") else [],
                "accounts": data["accounts"] if self.allowed(user, "ledger") else [],
                "ledgerOptions": data.get("ledgerOptions", {"descriptions": [], "categories": []}) if self.allowed(user, "ledger") else {"descriptions": [], "categories": []},
                "members": data["members"] if self.allowed(user, "tasks") or self.allowed(user, "family") else [],
                "currentUser": self.public_user(user),
                "users": [self.public_user(u) for u in data["users"]] if user.get("isAdmin") else [{"id": u["id"], "displayName": u["displayName"]} for u in data["users"]],
                "capabilities": {key: self.allowed(user, key) for key in PERMISSION_KEYS} | {"manageAccess": bool(user.get("isAdmin"))},
            }
            return self.send_json(200, visible)
        if path.startswith("/api/receipts/") and method == "GET":
            if not self.allowed(user, "ledger"):
                return self.send_json(403, {"error": "Kein Zugriff auf das Kassenbuch."})
            filename = path.rsplit("/", 1)[-1]
            if not filename or filename != Path(filename).name:
                return self.send_json(404, {"error": "Beleg nicht gefunden."})
            receipt = RECEIPTS_DIR / filename
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
        if path == "/api/family" and method == "PUT":
            if not self.allowed(user, "family"):
                return self.send_json(403, {"error": "Keine Berechtigung."})
            with lock:
                data["family"].update(clean(self.read_json()))
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

        user_parts = path.strip("/").split("/")
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
                is_admin = role in {"Leistungsberechtigte Person", "Gesetzliche Betreuung"}
                created = {
                    "id": str(uuid.uuid4()), "username": username,
                    "displayName": clean(str(payload.get("displayName") or username)), "role": role,
                    "memberId": payload.get("memberId") or None, "isAdmin": is_admin,
                    "active": True,
                    "permissions": full_permissions() if is_admin else {key: bool(payload.get("permissions", {}).get(key)) for key in PERMISSION_KEYS},
                    "passwordHash": hash_password(password), "createdAt": now(),
                }
                with lock: data["users"].append(created); save_data(data)
                return self.send_json(201, self.public_user(created))
            target = next((u for u in data["users"] if u["id"] == target_id), None)
            if not target: return self.send_json(404, {"error": "Zugang nicht gefunden."})
            if method == "PUT":
                payload = self.read_json(); role = str(payload.get("role") or target["role"])
                target.update({"displayName": clean(str(payload.get("displayName") or target["displayName"])), "role": role, "memberId": payload.get("memberId") or None})
                target["isAdmin"] = role in {"Leistungsberechtigte Person", "Gesetzliche Betreuung"}
                target["permissions"] = full_permissions() if target["isAdmin"] else {key: bool(payload.get("permissions", {}).get(key)) for key in PERMISSION_KEYS}
                if payload.get("password"):
                    if len(str(payload["password"])) < 8: return self.send_json(400, {"error": "Das Passwort muss mindestens 8 Zeichen lang sein."})
                    target["passwordHash"] = hash_password(str(payload["password"]))
                with lock: save_data(data)
                return self.send_json(200, self.public_user(target))
            if method == "DELETE":
                if target["id"] == user["id"]: return self.send_json(400, {"error": "Du kannst deinen eigenen Zugang nicht löschen."})
                with lock:
                    target["active"] = False
                    for token, session in list(sessions.items()):
                        if session["userId"] == target["id"]: sessions.pop(token, None)
                    save_data(data)
                return self.send_json(200, {"ok": True})

        parts = path.strip("/").split("/")
        if len(parts) not in (2, 3) or parts[0] != "api" or parts[1] not in COLLECTIONS:
            return self.send_json(404, {"error": "Nicht gefunden."})
        collection = parts[1]
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
                        occurrence = {"id": str(uuid.uuid4()), **clean(payload), "due": occurrence_due, "recurrenceSeriesId": series_id, "createdAt": now(), "createdByUserId": user["id"], "createdByName": user["displayName"], "history": []}
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
                item = {"id": str(uuid.uuid4()), **clean(payload), "createdAt": now()}
                item["createdByUserId"] = user["id"]
                item["createdByName"] = user["displayName"]
                if receipt_image:
                    item["receiptFile"] = save_receipt(receipt_image)
                    item["receiptStatus"] = "available"
                if case_file:
                    item.update(save_case_file(case_file, str(case_file_name)))
                if document_file:
                    item.update(save_document_file(document_file, str(document_file_name)))
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
            index = next((i for i, item in enumerate(data[collection]) if item["id"] == item_id), -1)
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
