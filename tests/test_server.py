import json
import os
import base64
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import hashlib
import hmac
import struct
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

    def call(self, path, method="GET", body=None, cookie=None, workspace_id=None):
        request = urllib.request.Request("http://127.0.0.1:8765" + path, method=method)
        if body is not None:
            request.data = json.dumps(body).encode(); request.add_header("Content-Type", "application/json")
        if cookie: request.add_header("Cookie", cookie)
        if workspace_id: request.add_header("X-Workspace-ID", workspace_id)
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
        self.assertIn('/app.js?v=20260803-4', html)
        with urllib.request.urlopen("http://127.0.0.1:8765/app.js?v=20260803-2") as response:
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

    def test_onboarding_and_admin_audit_log(self):
        cookie = self.login()
        _, initial = self.call("/api/data", cookie=cookie)
        self.assertFalse(initial["onboardingDismissed"])
        self.call("/api/onboarding", "PUT", {}, cookie)
        self.call("/api/tasks", "POST", {"title": "Protokollierte Aufgabe", "status": "open"}, cookie)
        _, current = self.call("/api/data", cookie=cookie)
        self.assertTrue(current["onboardingDismissed"])
        self.assertTrue(any(entry["entity"] == "tasks" for entry in current["auditLog"]))
        self.assertIn("operatorInfo", current)

    def test_calendar_and_ical_respect_source_permissions(self):
        admin = self.login()
        _, application = self.call("/api/cases", "POST", {"title": "Pflegeantrag", "status": "submitted"}, admin)
        self.call("/api/correspondence", "POST", {"caseId": application["id"], "eventType": "deadline", "date": "2026-09-12", "subject": "Entscheidungsfrist"}, admin)
        self.call("/api/tasks", "POST", {"title": "Medikamente bestellen", "due": "2026-09-11", "status": "planned"}, admin)
        self.call("/api/calendarEvents", "POST", {"title": "Sommerfest", "date": "2026-09-10", "startTime": "15:00"}, admin)
        _, calendar_user = self.call("/api/users", "POST", {"username": "kalenderhilfe", "displayName": "Kalenderhilfe", "password": "sicheres-passwort", "role": "Assistenz", "permissions": {"calendar": True, "tasks": True, "cases": False}}, admin)
        assistant = self.login("kalenderhilfe", "sicheres-passwort")
        _, visible = self.call("/api/data", cookie=assistant)
        titles = [item["title"] for item in visible["calendarEvents"]]
        self.assertIn("Sommerfest", titles)
        self.assertIn("Medikamente bestellen", titles)
        self.assertFalse(any("Entscheidungsfrist" in title for title in titles))
        _, feed = self.call("/api/calendar-feed", "POST", {}, assistant)
        with urllib.request.urlopen(feed["url"]) as response:
            ical = response.read().decode()
        self.assertIn("SUMMARY:Sommerfest", ical)
        self.assertIn("SUMMARY:Medikamente bestellen", ical)
        self.assertNotIn("Entscheidungsfrist", ical)
        self.call("/api/users/" + calendar_user["id"], "PUT", {"displayName": "Kalenderhilfe", "role": "Assistenz", "permissions": {"calendar": False, "tasks": True, "cases": False}}, admin)
        with self.assertRaises(urllib.error.HTTPError) as revoked_permission:
            urllib.request.urlopen(feed["url"])
        self.assertEqual(revoked_permission.exception.code, 403)

    def test_important_contacts_are_shared_but_safely_editable(self):
        admin_cookie = self.login()
        self.call("/api/users", "POST", {
            "username": "contactowner", "displayName": "Kontaktpflege",
            "password": "sicheres-passwort", "role": "Assistenz", "permissions": {},
        }, admin_cookie)
        self.call("/api/users", "POST", {
            "username": "contactviewer", "displayName": "Kontaktansicht",
            "password": "sicheres-passwort", "role": "Assistenz", "permissions": {},
        }, admin_cookie)
        owner_cookie = self.login("contactowner", "sicheres-passwort")
        viewer_cookie = self.login("contactviewer", "sicheres-passwort")
        _, contact = self.call("/api/contacts", "POST", {
            "name": "Frau Beispiel", "category": "WfB / Arbeit",
            "organization": "Beispiel-WfB", "role": "Teamleitung",
            "phone": "+49 123 456", "email": "kontakt@example.de",
        }, owner_cookie)
        _, visible = self.call("/api/data", cookie=viewer_cookie)
        self.assertIn(contact["id"], [entry["id"] for entry in visible["importantContacts"]])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/contacts/" + contact["id"], "PUT", {"phone": "Nicht erlaubt"}, viewer_cookie)
        self.assertEqual(error.exception.code, 403)
        _, changed = self.call("/api/contacts/" + contact["id"], "PUT", {"phone": "+49 999 000"}, admin_cookie)
        self.assertEqual(changed["phone"], "+49 999 000")
        _, options = self.call("/api/contact-options", "PUT", {
            "categories": ["Familie", "Fahrdienst", "WfB", "Medizin"],
        }, admin_cookie)
        self.assertEqual(options["categories"], ["Familie", "Fahrdienst", "WfB", "Medizin"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/contact-options", "PUT", {"categories": ["Nicht erlaubt"]}, viewer_cookie)
        self.assertEqual(error.exception.code, 403)

    def test_document_scan_or_pdf_upload_is_protected(self):
        cookie = self.login()
        pdf = base64.b64encode(b"%PDF-1.4\nDokument").decode()
        _, document = self.call("/api/documents", "POST", {
            "title": "Pflegebescheid", "category": "Bescheid", "date": date.today().isoformat(),
            "documentFile": "data:application/pdf;base64," + pdf,
            "documentFileName": "Pflegebescheid.pdf",
        }, cookie)
        self.assertEqual(document["attachmentName"], "Pflegebescheid.pdf")
        request = urllib.request.Request("http://127.0.0.1:8765/api/document-files/" + document["attachmentFile"], headers={"Cookie": cookie})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.headers.get_content_type(), "application/pdf")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen("http://127.0.0.1:8765/api/document-files/" + document["attachmentFile"])
        self.assertEqual(error.exception.code, 401)

    def test_case_file_correspondence_and_supplementary_application(self):
        cookie = self.login()
        _, initial = self.call("/api/data", cookie=cookie)
        pdf = base64.b64encode(b"%PDF-1.4\nTestbrief").decode()
        _, application = self.call("/api/cases", "POST", {
            "title": "Antrag auf Assistenz", "authority": "Sozialamt",
            "assignee": initial["members"][0]["id"], "status": "submitted",
            "area": "Eingliederungshilfe", "submittedAt": date.today().isoformat(),
            "receivedAt": date.today().isoformat(),
            "caseFile": "data:application/pdf;base64," + pdf, "caseFileName": "Antrag.pdf",
        }, cookie)
        self.assertEqual(application["status"], "submitted")
        self.assertEqual(application["attachmentType"], "application/pdf")
        _, child = self.call("/api/cases", "POST", {
            "title": "Ergänzung Hilfsmittel", "parentCaseId": application["id"], "status": "draft",
        }, cookie)
        self.assertEqual(child["parentCaseId"], application["id"])
        _, letter = self.call("/api/correspondence", "POST", {
            "caseId": application["id"], "eventType": "correspondence", "direction": "incoming", "date": date.today().isoformat(),
            "subject": "Rückfrage der Behörde", "caseFile": "data:application/pdf;base64," + pdf,
            "caseFileName": "Rueckfrage.pdf",
        }, cookie)
        self.assertEqual(letter["caseId"], application["id"])
        deadline_date = (date.today() + timedelta(days=14)).isoformat()
        _, deadline = self.call("/api/correspondence", "POST", {
            "caseId": application["id"], "eventType": "deadline", "date": deadline_date,
            "subject": "Unterlagen nachreichen", "deadlineType": "authority",
            "deadlineStatus": "open", "reminderDays": "7", "source": "Schreiben vom Sozialamt",
        }, cookie)
        self.assertEqual(deadline["date"], deadline_date)
        self.assertEqual(deadline["deadlineType"], "authority")
        self.assertTrue(deadline["reminderTaskId"])
        _, with_reminder = self.call("/api/data", cookie=cookie)
        reminder = next(task for task in with_reminder["tasks"] if task.get("caseDeadlineId") == deadline["id"])
        self.assertEqual(reminder["due"], (date.today() + timedelta(days=7)).isoformat())
        _, completed_deadline = self.call("/api/correspondence/" + deadline["id"], "PUT", {
            "deadlineStatus": "met", "eventType": "deadline", "date": deadline_date,
            "subject": "Unterlagen nachreichen", "reminderDays": "7",
        }, cookie)
        self.assertNotIn("reminderTaskId", completed_deadline)
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

    def test_routine_plans_use_admin_approved_versions(self):
        admin_cookie = self.login()
        self.call("/api/users", "POST", {
            "username": "routinehelper", "displayName": "Ablaufhilfe",
            "password": "sicheres-passwort", "role": "Assistenz", "permissions": {"tasks": True},
        }, admin_cookie)
        helper_cookie = self.login("routinehelper", "sicheres-passwort")
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nroutine").decode()
        _, version1 = self.call("/api/routine-plans", "POST", {
            "title": "Typischer Montag", "planType": "day", "days": "Montag",
            "schedule": "08:00 | Frühstück | Medikamente\n08:30 | Abfahrt zur WfB",
            "planFile": "data:image/png;base64," + png, "planFileName": "montag.png",
        }, helper_cookie)
        self.assertEqual(version1["approvalStatus"], "pending")
        request = urllib.request.Request("http://127.0.0.1:8765/api/routine-files/" + version1["attachmentFile"], headers={"Cookie": helper_cookie})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.headers.get_content_type(), "image/png")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/routine-plans/" + version1["id"] + "/review", "PUT", {"approvalStatus": "approved"}, helper_cookie)
        self.assertEqual(error.exception.code, 403)
        _, approved1 = self.call("/api/routine-plans/" + version1["id"] + "/review", "PUT", {"approvalStatus": "approved"}, admin_cookie)
        self.assertEqual(approved1["approvalStatus"], "approved")
        _, version2 = self.call("/api/routine-plans/" + version1["id"], "PUT", {
            "title": "Typischer Montag", "planType": "day", "days": "Montag",
            "schedule": "08:00 | Frühstück\n08:45 | Abfahrt zur WfB",
        }, helper_cookie)
        self.assertEqual(version2["version"], 2)
        self.assertEqual(version2["approvalStatus"], "pending")
        _, before_review = self.call("/api/data", cookie=helper_cookie)
        old = next(plan for plan in before_review["routinePlans"] if plan["id"] == version1["id"])
        self.assertEqual(old["approvalStatus"], "approved")
        self.call("/api/routine-plans/" + version2["id"] + "/review", "PUT", {"approvalStatus": "approved"}, admin_cookie)
        _, after_review = self.call("/api/data", cookie=helper_cookie)
        old = next(plan for plan in after_review["routinePlans"] if plan["id"] == version1["id"])
        new = next(plan for plan in after_review["routinePlans"] if plan["id"] == version2["id"])
        self.assertEqual(old["approvalStatus"], "superseded")
        self.assertEqual(new["approvalStatus"], "approved")

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
        self.assertEqual(len(initial["accounts"]), 3)
        self.assertNotIn("Gemeinsame WG-Barkasse", [account["name"] for account in initial["accounts"]])
        self.assertNotIn("Familie", [member["name"] for member in initial["members"]])
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
        request = urllib.request.Request("http://127.0.0.1:8765/api/receipts/" + entry["receiptFile"], headers={"Cookie": cookie})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen("http://127.0.0.1:8765/api/receipts/" + entry["receiptFile"])
        self.assertEqual(error.exception.code, 401)

    def test_manual_csv_and_camt_import_preview_deduplication_and_review(self):
        cookie = self.login()
        _, initial = self.call("/api/data", cookie=cookie)
        account_id = initial["accounts"][2]["id"]
        csv_data = "Buchungstag;Verwendungszweck;Zahlungspartner;Betrag\n01.08.2026;Miete August;Vermietung;-650,00\n02.08.2026;Einkommen;Arbeitgeber;1250,50\n"
        payload = {"accountId": account_id, "fileName": "umsatz.csv", "fileData": "data:text/csv;base64," + base64.b64encode(csv_data.encode()).decode()}
        _, preview = self.call("/api/bank-import/preview", "POST", payload, cookie)
        self.assertEqual(preview["newCount"], 2)
        _, imported = self.call("/api/bank-import/commit", "POST", payload, cookie)
        self.assertEqual(imported["imported"], 2)
        _, duplicate = self.call("/api/bank-import/commit", "POST", payload, cookie)
        self.assertEqual(duplicate["imported"], 0)
        self.assertEqual(duplicate["duplicates"], 2)
        _, visible = self.call("/api/data", cookie=cookie)
        imported_entries = [entry for entry in visible["ledger"] if entry.get("importBatchId") == imported["batchId"]]
        self.assertTrue(all(entry["reviewStatus"] == "pending" for entry in imported_entries))
        _, reviewed = self.call("/api/ledger/" + imported_entries[0]["id"] + "/review", "PUT", {}, cookie)
        self.assertEqual(reviewed["reviewStatus"], "reviewed")
        camt = """<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt><Ntry><Amt Ccy="EUR">29.95</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-08-03</Dt></BookgDt><AddtlNtryInf>Telefonrechnung</AddtlNtryInf></Ntry></Stmt></BkToCstmrStmt></Document>"""
        camt_payload = {"accountId": account_id, "fileName": "kontoauszug.xml", "fileData": "data:application/xml;base64," + base64.b64encode(camt.encode()).decode()}
        _, camt_result = self.call("/api/bank-import/commit", "POST", camt_payload, cookie)
        self.assertEqual(camt_result["imported"], 1)
    def test_about_linea_profile_goals_rules_and_comments(self):
        admin_cookie = self.login()
        _, profile = self.call("/api/person-profile", "PUT", {
            "introduction": "Ich bin Linea.", "strengths": "Kreativ und direkt",
            "beiSummary": "Wichtige Inhalte aus dem BEI",
        }, admin_cookie)
        self.assertEqual(profile["introduction"], "Ich bin Linea.")
        _, goal = self.call("/api/about/goals", "POST", {
            "title": "Selbstständiger wohnen", "description": "Schrittweise mehr übernehmen", "status": "active",
        }, admin_cookie)
        self.call("/api/users", "POST", {
            "username": "abouthelper", "displayName": "Assistenz About",
            "password": "sicheres-passwort", "role": "Assistenz", "permissions": {},
        }, admin_cookie)
        helper_cookie = self.login("abouthelper", "sicheres-passwort")
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nprofile").decode()
        _, with_photo = self.call("/api/profile-photo", "PUT", {
            "photoData": "data:image/png;base64," + png, "photoName": "linea.png",
        }, admin_cookie)
        self.assertTrue(with_photo["photoFile"].endswith(".png"))
        pdf = base64.b64encode(b"%PDF-1.4\nBEI").decode()
        _, bei_wak = self.call("/api/about/beis", "POST", {
            "title": "BEI Wohnen", "area": "Wohnen", "date": date.today().isoformat(),
            "beiFile": "data:application/pdf;base64," + pdf, "beiFileName": "BEI-Wohnen.pdf",
        }, admin_cookie)
        _, bei_work = self.call("/api/about/beis", "POST", {
            "title": "BEI Arbeit", "area": "Arbeit",
            "beiFile": "data:application/pdf;base64," + pdf, "beiFileName": "BEI-Arbeit.pdf",
        }, admin_cookie)
        _, visible = self.call("/api/data", cookie=helper_cookie)
        self.assertEqual(visible["personProfile"]["beiSummary"], "Wichtige Inhalte aus dem BEI")
        self.assertEqual({bei["id"] for bei in visible["beis"]}, {bei_wak["id"], bei_work["id"]})
        request = urllib.request.Request("http://127.0.0.1:8765/api/about-files/" + bei_wak["attachmentFile"], headers={"Cookie": helper_cookie})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.headers.get_content_type(), "application/pdf")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/about/beis", "POST", {"title": "Nicht erlaubt"}, helper_cookie)
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/person-profile", "PUT", {"introduction": "Nicht erlaubt"}, helper_cookie)
        self.assertEqual(error.exception.code, 403)
        _, rule = self.call("/api/about/rules", "POST", {
            "title": "Absprachen dokumentieren", "text": "Änderungen werden gemeinsam festgehalten.",
        }, helper_cookie)
        self.assertEqual(rule["approvalStatus"], "pending")
        _, comment = self.call("/api/about/comments", "POST", {
            "targetType": "goal", "targetId": goal["id"], "text": "Der erste Schritt ist geschafft.",
        }, helper_cookie)
        self.assertEqual(comment["createdByName"], "Assistenz About")
        _, approved = self.call("/api/about/rules/" + rule["id"], "PUT", {"approvalStatus": "approved"}, admin_cookie)
        self.assertEqual(approved["approvalStatus"], "approved")

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

    def test_team_member_profile_and_photo_are_protected(self):
        cookie = self.login()
        image = "data:image/png;base64," + base64.b64encode(b"profile-image").decode()
        _, member = self.call("/api/members", "POST", {
            "name": "Mara", "role": "Assistenz", "email": "mara@example.de",
            "phone": "+49 123", "personalWords": "Ich mag Musik.",
            "photoData": image, "photoName": "mara.png",
        }, cookie)
        self.assertEqual(member["personalWords"], "Ich mag Musik.")
        self.assertTrue(member["photoFile"].endswith(".png"))
        request = urllib.request.Request("http://127.0.0.1:8765/api/member-files/" + member["photoFile"])
        request.add_header("Cookie", cookie)
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.read(), b"profile-image")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen("http://127.0.0.1:8765/api/member-files/" + member["photoFile"])
        self.assertEqual(error.exception.code, 401)

    def test_one_time_invitation_creates_profile_and_pause_blocks_access(self):
        admin_cookie = self.login()
        _, invitation = self.call("/api/invitations", "POST", {
            "displayName": "Neue Assistenz", "email": "neu@example.de", "role": "Assistenz",
            "validDays": 7, "permissions": {"tasks": True, "documents": True},
        }, admin_cookie)
        self.assertIn("token", invitation)
        response, accepted = self.call("/api/invitations/" + invitation["token"] + "/accept", "POST", {
            "displayName": "Mara Assistenz", "username": "mara-neu", "password": "sicheres-passwort",
            "personalWords": "Ich freue mich auf die Zusammenarbeit.",
        })
        user_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(accepted["user"]["accessStatus"], "active")
        _, visible = self.call("/api/data", cookie=user_cookie)
        self.assertTrue(visible["capabilities"]["tasks"])
        self.assertFalse(visible["currentUser"]["isAdmin"])
        self.assertIn("Mara Assistenz", [member["name"] for member in visible["members"]])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/invitations/" + invitation["token"] + "/accept", "POST", {
                "displayName": "Nochmal", "username": "nochmal", "password": "sicheres-passwort",
            })
        self.assertEqual(error.exception.code, 410)
        user_id = accepted["user"]["id"]
        _, paused = self.call("/api/users/" + user_id, "PUT", {"accessStatus": "paused"}, admin_cookie)
        self.assertEqual(paused["accessStatus"], "paused")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/data", cookie=user_cookie)
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.login("mara-neu", "sicheres-passwort")
        self.assertEqual(error.exception.code, 401)
        _, active = self.call("/api/users/" + user_id, "PUT", {"accessStatus": "active"}, admin_cookie)
        self.assertEqual(active["accessStatus"], "active")
        self.assertIsNotNone(self.login("mara-neu", "sicheres-passwort"))

    def test_close_relative_role_gets_explicit_admin_access(self):
        admin_cookie = self.login()
        _, invitation = self.call("/api/invitations", "POST", {
            "displayName": "Enger Angehöriger", "role": "Enge Angehörige", "validDays": 7,
        }, admin_cookie)
        _, accepted = self.call("/api/invitations/" + invitation["token"] + "/accept", "POST", {
            "displayName": "Enger Angehöriger", "username": "eng-angehoerig", "password": "sicheres-passwort",
        })
        self.assertTrue(accepted["user"]["isAdmin"])
        self.assertTrue(all(accepted["user"]["permissions"].values()))

    def test_infoboard_mentions_read_receipts_and_preferences(self):
        admin_cookie = self.login()
        _, helper = self.call("/api/users", "POST", {
            "username": "infobrett-hilfe", "displayName": "Infobrett Hilfe", "password": "sicheres-passwort",
            "role": "Assistenz", "permissions": {"tasks": True},
        }, admin_cookie)
        helper_cookie = self.login("infobrett-hilfe", "sicheres-passwort")
        _, preferences = self.call("/api/notification-preferences", "PUT", {
            "notificationEmail": "hilfe@example.de", "notificationPreference": "mentions",
        }, helper_cookie)
        self.assertEqual(preferences["notificationPreference"], "mentions")
        _, announcement = self.call("/api/announcements", "POST", {
            "title": "Heute zu Hause", "text": "Bitte @infobrett-hilfe beachten.",
            "importance": "important", "validUntil": (date.today() + timedelta(days=1)).isoformat(),
        }, admin_cookie)
        self.assertIn(helper["id"], announcement["mentionedUserIds"])
        self.assertNotIn(helper["id"], announcement["readByUserIds"])
        _, read = self.call("/api/announcements/" + announcement["id"] + "/read", "PUT", {}, helper_cookie)
        self.assertIn(helper["id"], read["readByUserIds"])
        _, visible = self.call("/api/data", cookie=helper_cookie)
        self.assertIn(announcement["id"], [item["id"] for item in visible["announcements"]])

    def test_topic_comments_follow_target_permissions(self):
        admin_cookie = self.login()
        _, application = self.call("/api/cases", "POST", {"title": "Kommunikationsantrag"}, admin_cookie)
        _, task = self.call("/api/tasks", "POST", {"title": "Kommunikationsaufgabe", "status": "open"}, admin_cookie)
        _, helper = self.call("/api/users", "POST", {
            "username": "themen-hilfe", "displayName": "Themen Hilfe", "password": "sicheres-passwort",
            "role": "Assistenz", "permissions": {"tasks": True, "cases": False},
        }, admin_cookie)
        helper_cookie = self.login("themen-hilfe", "sicheres-passwort")
        _, comment = self.call("/api/topic-comments", "POST", {
            "targetType": "task", "targetId": task["id"], "text": "Das hat heute gut geklappt.",
        }, helper_cookie)
        self.assertEqual(comment["createdByUserId"], helper["id"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/topic-comments", "POST", {
                "targetType": "case", "targetId": application["id"], "text": "Nicht sichtbar",
            }, helper_cookie)
        self.assertEqual(error.exception.code, 403)
        _, visible = self.call("/api/data", cookie=helper_cookie)
        self.assertIn(comment["id"], [item["id"] for item in visible["topicComments"]])
        self.assertNotIn("case", [item["targetType"] for item in visible["topicComments"]])

    def test_shared_workspace_separates_tasks_cash_and_memberships(self):
        admin_cookie = self.login()
        _, helper = self.call("/api/users", "POST", {
            "username": "wg-assistenz", "displayName": "WG Assistenz", "password": "sicheres-passwort",
            "role": "Assistenz", "permissions": {"tasks": True, "ledger": True},
        }, admin_cookie)
        _, private_before = self.call("/api/data", cookie=admin_cookie)
        _, shared = self.call("/api/workspaces", "POST", {
            "name": "WG Sonnenstraße", "memberUserIds": [helper["id"]],
        }, admin_cookie)
        _, shared_data = self.call("/api/data", cookie=admin_cookie, workspace_id=shared["id"])
        self.assertEqual(shared_data["workspace"]["type"], "shared")
        self.assertEqual(shared_data["cases"], [])
        self.assertEqual(shared_data["documents"], [])
        self.assertEqual(len(shared_data["accounts"]), 1)
        _, shared_task = self.call("/api/tasks", "POST", {
            "title": "Gemeinsamer Einkauf", "status": "open",
        }, admin_cookie, shared["id"])
        _, shared_entry = self.call("/api/ledger", "POST", {
            "description": "WG-Einkauf", "amount": "20.00", "type": "expense",
            "accountId": shared_data["accounts"][0]["id"], "receiptStatus": "none",
        }, admin_cookie, shared["id"])
        _, private_after = self.call("/api/data", cookie=admin_cookie, workspace_id=private_before["workspace"]["id"])
        self.assertNotIn(shared_task["id"], [item["id"] for item in private_after["tasks"]])
        self.assertNotIn(shared_entry["id"], [item["id"] for item in private_after["ledger"]])
        helper_cookie = self.login("wg-assistenz", "sicheres-passwort")
        _, helper_shared = self.call("/api/data", cookie=helper_cookie, workspace_id=shared["id"])
        self.assertIn(shared_task["id"], [item["id"] for item in helper_shared["tasks"]])
        self.assertIn(shared_entry["id"], [item["id"] for item in helper_shared["ledger"]])
        _, outsider = self.call("/api/users", "POST", {
            "username": "wg-aussen", "displayName": "Außenstehend", "password": "sicheres-passwort",
            "role": "Assistenz", "permissions": {},
        }, admin_cookie)
        outsider_cookie = self.login("wg-aussen", "sicheres-passwort")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/data", cookie=outsider_cookie, workspace_id=shared["id"])
        self.assertEqual(error.exception.code, 403)

    def test_one_account_can_keep_a_private_area_and_a_shared_wg_separate(self):
        admin_cookie = self.login()
        _, helper = self.call("/api/users", "POST", {
            "username": "maria", "displayName": "Maria", "password": "sicheres-passwort",
            "role": "Assistenz", "permissions": {"tasks": True, "ledger": True},
        }, admin_cookie)
        _, shared = self.call("/api/workspaces", "POST", {
            "name": "WG Zusammen", "memberUserIds": [helper["id"]],
        }, admin_cookie)
        maria_cookie = self.login("maria", "sicheres-passwort")
        _, private = self.call("/api/workspaces", "POST", {
            "type": "private", "name": "Marias Bereich", "personName": "Maria",
        }, maria_cookie, shared["id"])
        _, private_data = self.call("/api/data", cookie=maria_cookie, workspace_id=private["id"])
        self.assertEqual(private_data["family"]["person"], "Maria")
        self.assertEqual(len(private_data["accounts"]), 3)
        self.call("/api/person-profile", "PUT", {"introduction": "Das ist Marias Profil."}, maria_cookie, private["id"])
        _, linea_data = self.call("/api/data", cookie=admin_cookie)
        self.assertNotEqual(linea_data["personProfile"].get("introduction"), "Das ist Marias Profil.")
        _, maria_shared = self.call("/api/data", cookie=maria_cookie, workspace_id=shared["id"])
        self.assertEqual(maria_shared["workspace"]["name"], "WG Zusammen")
        self.assertIn(private["id"], [area["id"] for area in private_data["workspaces"]])
        self.assertIn(shared["id"], [area["id"] for area in private_data["workspaces"]])
        self.assertNotIn(private["id"], [area["id"] for area in linea_data["workspaces"]])

    def test_operator_can_invite_an_independent_person_without_seeing_content(self):
        admin = self.login()
        _, invitation = self.call("/api/operator/invitations", "POST", {
            "displayName": "Nora", "areaName": "Noras Zuhause", "validDays": 2,
        }, admin)
        _, accepted = self.call(f"/api/invitations/{invitation['token']}/accept", "POST", {
            "displayName": "Nora", "username": "nora-eigen", "password": "sicheres-passwort",
        })
        nora_cookie = accepted["user"] and self.login("nora-eigen", "sicheres-passwort")
        _, nora = self.call("/api/data", cookie=nora_cookie)
        self.assertEqual(nora["workspace"]["name"], "Noras Zuhause")
        self.assertEqual(nora["family"]["person"], "Nora")
        self.call("/api/person-profile", "PUT", {"introduction": "Nur Noras Inhalt"}, nora_cookie)
        _, private_document = self.call("/api/documents", "POST", {
            "title": "Noras privates Dokument", "date": date.today().isoformat(),
            "documentFile": "data:application/pdf;base64," + base64.b64encode(b"private pdf").decode(),
            "documentFileName": "nora.pdf",
        }, nora_cookie)
        _, operator = self.call("/api/operator", cookie=admin)
        summary = next(item for item in operator["workspaces"] if item["id"] == nora["workspace"]["id"])
        self.assertNotIn("introduction", summary)
        _, linea = self.call("/api/data", cookie=admin)
        self.assertNotIn(nora["workspace"]["id"], [item["id"] for item in linea["workspaces"]])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/document-files/" + private_document["attachmentFile"], cookie=admin)
        self.assertEqual(error.exception.code, 404)

    def test_existing_account_can_join_an_invited_shared_area(self):
        admin = self.login()
        _, existing = self.call("/api/users", "POST", {
            "username": "bestehend", "displayName": "Bestehend", "password": "sicheres-passwort",
            "role": "Assistenz", "permissions": {"tasks": True},
        }, admin)
        _, shared = self.call("/api/workspaces", "POST", {"name": "WG Beitritt"}, admin)
        _, invitation = self.call("/api/invitations", "POST", {
            "displayName": "Bestehend", "role": "Assistenz", "permissions": {"tasks": True},
        }, admin, shared["id"])
        existing_cookie = self.login("bestehend", "sicheres-passwort")
        _, joined = self.call(f"/api/invitations/{invitation['token']}/join", "POST", {}, existing_cookie)
        self.assertEqual(joined["workspaceId"], shared["id"])
        _, visible = self.call("/api/data", cookie=existing_cookie, workspace_id=shared["id"])
        self.assertEqual(visible["workspace"]["name"], "WG Beitritt")

    def test_totp_and_privacy_export(self):
        cookie = self.login()
        _, setup = self.call("/api/security/totp/setup", "POST", {}, cookie)
        padded = setup["secret"] + "=" * ((8 - len(setup["secret"]) % 8) % 8)
        key = base64.b32decode(padded); counter = int(time.time() // 30)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest(); offset = digest[-1] & 15
        code = f"{(struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff) % 1000000:06d}"
        self.call("/api/security/totp/confirm", "POST", {"code": code}, cookie)
        with self.assertRaises(urllib.error.HTTPError) as error: self.call("/api/login", "POST", {"username": "linea", "password": "test-passwort"})
        self.assertEqual(error.exception.code, 401)
        response, _ = self.call("/api/login", "POST", {"username": "linea", "password": "test-passwort", "otp": code})
        secured_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        _, exported = self.call("/api/privacy/export", cookie=secured_cookie)
        self.assertEqual(exported["workspace"]["id"], exported["profile"] is not None and exported["workspace"]["id"])
        self.call("/api/security/totp", "DELETE", {}, secured_cookie)


if __name__ == "__main__": unittest.main()
