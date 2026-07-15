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
COLLECTIONS = {"cases", "tasks", "documents", "messages", "ledger", "members", "accounts"}
lock = threading.Lock()
sessions: dict[str, dict] = {}
PERMISSION_KEYS = ("cases", "tasks", "documents", "ledger", "family")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def empty_data() -> dict:
    cash_id, savings_id, shared_id, bank_id = (str(uuid.uuid4()) for _ in range(4))
    return {
        "family": {"name": "Unsere Familie", "person": "Linea", "createdAt": now()},
        "cases": [], "tasks": [], "documents": [], "messages": [], "ledger": [],
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
    try:
        loaded = json.loads(DATA_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        loaded = empty_data()
    changed = False
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


def clean(value):
    if isinstance(value, str):
        return value.strip()[:5000]
    if isinstance(value, dict):
        return {str(k)[:100]: clean(v) for k, v in value.items() if k not in {"id", "createdAt", "updatedAt"}}
    if isinstance(value, list):
        return [clean(v) for v in value[:1000]]
    return value


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
            visible = {
                "family": data["family"],
                "cases": data["cases"] if self.allowed(user, "cases") else [],
                "tasks": data["tasks"] if self.allowed(user, "tasks") else [],
                "documents": data["documents"] if self.allowed(user, "documents") else [],
                "messages": [],
                "ledger": data["ledger"] if self.allowed(user, "ledger") else [],
                "accounts": data["accounts"] if self.allowed(user, "ledger") else [],
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
        if path == "/api/family" and method == "PUT":
            if not self.allowed(user, "family"):
                return self.send_json(403, {"error": "Keine Berechtigung."})
            with lock:
                data["family"].update(clean(self.read_json()))
                data["family"]["updatedAt"] = now()
                save_data(data)
            return self.send_json(200, data["family"])

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
        permission = {"cases":"cases", "tasks":"tasks", "documents":"documents", "ledger":"ledger", "accounts":"family", "members":"family", "messages":"tasks"}[collection]
        if not self.allowed(user, permission):
            return self.send_json(403, {"error": "Keine Berechtigung für diesen Bereich."})
        item_id = parts[2] if len(parts) == 3 else None
        with lock:
            if method == "POST" and not item_id:
                payload = self.read_json()
                receipt_image = payload.pop("receiptImage", None) if collection == "ledger" else None
                item = {"id": str(uuid.uuid4()), **clean(payload), "createdAt": now()}
                item["createdByUserId"] = user["id"]
                item["createdByName"] = user["displayName"]
                if receipt_image:
                    item["receiptFile"] = save_receipt(receipt_image)
                data[collection].insert(0, item)
                save_data(data)
                return self.send_json(201, item)
            index = next((i for i, item in enumerate(data[collection]) if item["id"] == item_id), -1)
            if index < 0:
                return self.send_json(404, {"error": "Eintrag nicht gefunden."})
            if method == "PUT":
                payload = self.read_json()
                receipt_image = payload.pop("receiptImage", None) if collection == "ledger" else None
                data[collection][index].update(clean(payload))
                data[collection][index]["updatedByUserId"] = user["id"]
                if receipt_image:
                    data[collection][index]["receiptFile"] = save_receipt(receipt_image)
                data[collection][index]["updatedAt"] = now()
                save_data(data)
                return self.send_json(200, data[collection][index])
            if method == "DELETE":
                data[collection].pop(index)
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
