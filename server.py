from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
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
SECRET = os.getenv("SESSION_SECRET", "dev-secret-change-me").encode()
MAX_BODY = 2_000_000
COLLECTIONS = {"cases", "tasks", "documents", "messages", "ledger", "members"}
lock = threading.Lock()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def empty_data() -> dict:
    return {
        "family": {"name": "Unsere Familie", "person": "Alex", "createdAt": now()},
        "cases": [], "tasks": [], "documents": [], "messages": [], "ledger": [],
        "members": [
            {"id": str(uuid.uuid4()), "name": "Alex", "role": "Leistungsberechtigte Person", "color": "#5a57d9"},
            {"id": str(uuid.uuid4()), "name": "Familie", "role": "Angehörige", "color": "#e57d45"},
        ],
    }


def load_data() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return json.loads(DATA_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        initial = empty_data()
        save_data(initial)
        return initial


def save_data(value: dict) -> None:
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(DATA_FILE)


data = load_data()


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

    def session_valid(self) -> bool:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        item = jar.get("miteinander_session")
        if not item:
            return False
        try:
            expires, signature = item.value.split(".", 1)
            expected = hmac.new(SECRET, expires.encode(), hashlib.sha256).hexdigest()
            return int(expires) >= int(time.time()) and hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def api(self, method: str, path: str):
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        if path == "/api/login" and method == "POST":
            supplied = str(self.read_json().get("password", ""))
            if not hmac.compare_digest(supplied.encode(), PASSWORD.encode()):
                return self.send_json(401, {"error": "Das Passwort stimmt nicht."})
            expires = str(int(time.time()) + 43200)
            signature = hmac.new(SECRET, expires.encode(), hashlib.sha256).hexdigest()
            cookie = f"miteinander_session={expires}.{signature}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"
            return self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
        if path == "/api/logout" and method == "POST":
            return self.send_json(200, {"ok": True}, {"Set-Cookie": "miteinander_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        if not self.session_valid():
            return self.send_json(401, {"error": "Bitte anmelden."})
        if path == "/api/data" and method == "GET":
            return self.send_json(200, data)
        if path == "/api/family" and method == "PUT":
            with lock:
                data["family"].update(clean(self.read_json()))
                data["family"]["updatedAt"] = now()
                save_data(data)
            return self.send_json(200, data["family"])

        parts = path.strip("/").split("/")
        if len(parts) not in (2, 3) or parts[0] != "api" or parts[1] not in COLLECTIONS:
            return self.send_json(404, {"error": "Nicht gefunden."})
        collection = parts[1]
        item_id = parts[2] if len(parts) == 3 else None
        with lock:
            if method == "POST" and not item_id:
                item = {"id": str(uuid.uuid4()), **clean(self.read_json()), "createdAt": now()}
                data[collection].insert(0, item)
                save_data(data)
                return self.send_json(201, item)
            index = next((i for i, item in enumerate(data[collection]) if item["id"] == item_id), -1)
            if index < 0:
                return self.send_json(404, {"error": "Eintrag nicht gefunden."})
            if method == "PUT":
                data[collection][index].update(clean(self.read_json()))
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
