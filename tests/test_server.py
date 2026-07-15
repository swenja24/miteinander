import json
import os
import base64
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta


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

    def test_frontend_assets_cannot_be_served_from_an_old_cache(self):
        with urllib.request.urlopen("http://127.0.0.1:8765/") as response:
            html = response.read().decode()
            self.assertEqual(response.headers["Cache-Control"], "no-cache, no-store, must-revalidate")
            self.assertIn('/app.js?v=20260715-7', html)
        with urllib.request.urlopen("http://127.0.0.1:8765/app.js?v=20260715-7") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-cache, no-store, must-revalidate")
            self.assertIn("Rechnung fotografieren oder hochladen", response.read().decode())

    def test_create_and_persist_task(self):
        cookie = self.login()
        _, item = self.call("/api/tasks", "POST", {"title": "Bescheid prüfen", "status": "open"}, cookie)
        _, data = self.call("/api/data", cookie=cookie)
        self.assertEqual(data["tasks"][0]["id"], item["id"])
        with open(os.path.join(self.temp.name, "familie.json"), encoding="utf-8") as file:
            stored = json.loads(file.read())
        self.assertEqual(stored["tasks"][0]["title"], "Bescheid prüfen")

    def test_case_file_correspondence_and_supplementary_application(self):
        cookie = self.login()
        _, initial = self.call("/api/data", cookie=cookie)
        pdf = base64.b64encode(b"%PDF-1.4\nTestbrief").decode()
        _, application = self.call("/api/cases", "POST", {
            "title": "Antrag auf Assistenz", "authority": "Sozialamt",
            "assignee": initial["members"][0]["id"], "status": "submitted",
            "caseFile": "data:application/pdf;base64," + pdf, "caseFileName": "Antrag.pdf",
        }, cookie)
        self.assertEqual(application["status"], "submitted")
        self.assertEqual(application["attachmentType"], "application/pdf")
        _, child = self.call("/api/cases", "POST", {
            "title": "Ergänzung Hilfsmittel", "parentCaseId": application["id"], "status": "draft",
        }, cookie)
        self.assertEqual(child["parentCaseId"], application["id"])
        _, letter = self.call("/api/correspondence", "POST", {
            "caseId": application["id"], "direction": "incoming", "date": date.today().isoformat(),
            "subject": "Rückfrage der Behörde", "caseFile": "data:application/pdf;base64," + pdf,
            "caseFileName": "Rueckfrage.pdf",
        }, cookie)
        self.assertEqual(letter["caseId"], application["id"])
        request = urllib.request.Request("http://127.0.0.1:8765/api/case-files/" + letter["attachmentFile"], headers={"Cookie": cookie})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.headers.get_content_type(), "application/pdf")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen("http://127.0.0.1:8765/api/case-files/" + letter["attachmentFile"])
        self.assertEqual(error.exception.code, 401)

    def test_recurring_tasks_history_past_guard_and_soft_delete(self):
        admin_cookie = self.login()
        _, helper = self.call("/api/users", "POST", {
            "username": "taskhelper", "displayName": "Aufgabenhilfe",
            "password": "sicheres-passwort", "role": "Assistenz",
            "permissions": {"tasks": True},
        }, admin_cookie)
        helper_cookie = self.login("taskhelper", "sicheres-passwort")
        start = date.today() + timedelta(days=1)
        end = start + timedelta(days=15)
        _, first = self.call("/api/tasks", "POST", {
            "title": "Medikamente stellen", "category": "Gesundheit",
            "due": start.isoformat(), "status": "planned", "recurrence": "weekly",
            "recurrenceUntil": end.isoformat(),
        }, helper_cookie)
        _, helper_data = self.call("/api/data", cookie=helper_cookie)
        series = [task for task in helper_data["tasks"] if task.get("recurrenceSeriesId") == first["recurrenceSeriesId"]]
        self.assertEqual(len(series), 3)
        moved = (start + timedelta(days=2)).isoformat()
        _, updated = self.call("/api/tasks/" + first["id"], "PUT", {"due": moved}, helper_cookie)
        self.assertEqual(updated["history"][-1]["from"], start.isoformat())
        self.assertEqual(updated["history"][-1]["to"], moved)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/tasks", "POST", {"title": "Nachtragen", "due": (date.today()-timedelta(days=1)).isoformat()}, helper_cookie)
        self.assertEqual(error.exception.code, 403)
        _, deletion = self.call("/api/tasks/" + first["id"], "DELETE", cookie=helper_cookie)
        self.assertTrue(deletion["pendingAdminConfirmation"])
        _, hidden = self.call("/api/data", cookie=helper_cookie)
        self.assertNotIn(first["id"], [task["id"] for task in hidden["tasks"]])
        _, admin_data = self.call("/api/data", cookie=admin_cookie)
        deleted = next(task for task in admin_data["tasks"] if task["id"] == first["id"])
        self.assertEqual(deleted["deletedByUserId"], helper["id"])
        self.call("/api/tasks/" + first["id"], "DELETE", cookie=admin_cookie)
        _, final_data = self.call("/api/data", cookie=admin_cookie)
        self.assertNotIn(first["id"], [task["id"] for task in final_data["tasks"]])

    def test_open_ended_series_always_has_seven_current_occurrences(self):
        admin_cookie = self.login()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _, first = self.call("/api/tasks", "POST", {
            "title": "Wöchentliche Planung", "due": yesterday,
            "status": "planned", "recurrence": "weekly", "recurrenceUntil": "",
        }, admin_cookie)
        _, visible = self.call("/api/data", cookie=admin_cookie)
        series = [task for task in visible["tasks"] if task.get("recurrenceSeriesId") == first["recurrenceSeriesId"]]
        current = [task for task in series if task["due"] >= date.today().isoformat()]
        past = [task for task in series if task["due"] < date.today().isoformat()]
        self.assertEqual(len(current), 7)
        self.assertEqual(len(past), 1)

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

    def test_only_administrators_can_rename_accounts(self):
        admin_cookie = self.login()
        _, initial = self.call("/api/data", cookie=admin_cookie)
        account = initial["accounts"][0]
        _, renamed = self.call("/api/accounts/" + account["id"], "PUT", {
            "name": "Lineas Haushaltskasse", "type": account["type"], "color": account["color"],
        }, admin_cookie)
        self.assertEqual(renamed["name"], "Lineas Haushaltskasse")
        self.call("/api/users", "POST", {
            "username": "accounthelper", "displayName": "Kontohilfe",
            "password": "sicheres-passwort", "role": "Assistenz",
            "permissions": {"family": True, "ledger": True},
        }, admin_cookie)
        helper_cookie = self.login("accounthelper", "sicheres-passwort")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/accounts/" + account["id"], "PUT", {"name": "Nicht erlaubt"}, helper_cookie)
        self.assertEqual(error.exception.code, 403)

    def test_ledger_suggestions_and_no_receipt(self):
        cookie = self.login()
        _, options = self.call("/api/ledger-options", "PUT", {
            "descriptions": ["Lebensmittel", "Fahrtkosten"],
            "categories": ["Haushalt", "Mobilität"],
        }, cookie)
        self.assertEqual(options["categories"], ["Haushalt", "Mobilität"])
        _, entry = self.call("/api/ledger", "POST", {
            "description": "Medikamente", "category": "Gesundheit",
            "amount": "12.34", "receiptStatus": "none",
        }, cookie)
        self.assertEqual(entry["amount"], "12.34")
        self.assertEqual(entry["receiptStatus"], "none")
        _, visible = self.call("/api/data", cookie=cookie)
        self.assertIn("Medikamente", visible["ledgerOptions"]["descriptions"])
        self.assertIn("Gesundheit", visible["ledgerOptions"]["categories"])

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
