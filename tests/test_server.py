import json
import os
import base64
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        env = os.environ | {"PORT": "8765", "DATA_DIR": cls.temp.name, "APP_PASSWORD": "test-passwort", "SESSION_SECRET": "test-secret"}
        cls.process = subprocess.Popen(["python3", "server.py"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for _ in range(30):
            try:
                urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=1)
                break
            except Exception:
                time.sleep(.1)

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate(); cls.process.wait(timeout=5); cls.temp.cleanup()

    def call(self, path, method="GET", body=None, cookie=None):
        request = urllib.request.Request("http://127.0.0.1:8765" + path, method=method)
        if body is not None:
            request.data = json.dumps(body).encode(); request.add_header("Content-Type", "application/json")
        if cookie: request.add_header("Cookie", cookie)
        response = urllib.request.urlopen(request)
        return response, json.loads(response.read())

    def login(self, username="linea", password="test-passwort"):
        response, _ = self.call("/api/login", "POST", {"username": username, "password": password})
        return response.headers["Set-Cookie"].split(";", 1)[0]

    def test_health_and_authentication(self):
        _, result = self.call("/api/health")
        self.assertTrue(result["ok"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/data")
        self.assertEqual(error.exception.code, 401)

    def test_create_and_persist_task(self):
        cookie = self.login()
        _, item = self.call("/api/tasks", "POST", {"title": "Bescheid prüfen", "status": "open"}, cookie)
        _, data = self.call("/api/data", cookie=cookie)
        self.assertEqual(data["tasks"][0]["id"], item["id"])
        with open(os.path.join(self.temp.name, "familie.json"), encoding="utf-8") as file:
            stored = json.loads(file.read())
        self.assertEqual(stored["tasks"][0]["title"], "Bescheid prüfen")

    def test_accounts_and_protected_receipt_upload(self):
        cookie = self.login()
        _, initial = self.call("/api/data", cookie=cookie)
        self.assertEqual(len(initial["accounts"]), 4)
        png = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"test-receipt"
        ).decode()
        _, entry = self.call("/api/ledger", "POST", {
            "description": "Einkauf",
            "accountId": initial["accounts"][0]["id"],
            "type": "expense",
            "amount": "12.50",
            "receiptImage": "data:image/png;base64," + png,
        }, cookie)
        self.assertEqual(entry["createdByUserId"], initial["currentUser"]["id"])
        self.assertTrue(entry["receiptFile"].endswith(".png"))
        request = urllib.request.Request(
            "http://127.0.0.1:8765/api/receipts/" + entry["receiptFile"],
            headers={"Cookie": cookie},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen("http://127.0.0.1:8765/api/receipts/" + entry["receiptFile"])
        self.assertEqual(error.exception.code, 401)

    def test_individual_permissions_are_enforced(self):
        admin_cookie = self.login()
        _, created = self.call("/api/users", "POST", {
            "username": "assistenz",
            "displayName": "Assistenz",
            "password": "sicheres-passwort",
            "role": "Assistenz",
            "permissions": {"tasks": True, "ledger": False},
        }, admin_cookie)
        self.assertFalse(created["isAdmin"])
        user_cookie = self.login("assistenz", "sicheres-passwort")
        _, visible = self.call("/api/data", cookie=user_cookie)
        self.assertTrue(visible["capabilities"]["tasks"])
        self.assertFalse(visible["capabilities"]["ledger"])
        self.assertEqual(visible["ledger"], [])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/ledger", "POST", {"description": "Nicht erlaubt"}, user_cookie)
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__": unittest.main()
