import hashlib
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from core.models import ManagedFile, Member, Membership, Record, Workspace, WorkspaceOptions, WorkspaceProfile
from core.services import KIND_BY_COLLECTION, allowed, audit, create_record, import_fingerprint, member_dict, membership_context, next_recurring_date, parse_json, parse_statement, permissions, profile_dict, record_dict, recurring_dates, update_record, valid_email_or_empty, validate_and_store_file


PERMISSION_BY_COLLECTION = {
    "cases": "cases", "correspondence": "cases", "tasks": "tasks", "calendarEvents": "calendar",
    "documents": "documents", "ledger": "ledger", "accounts": "family", "members": "family",
}


def _record(workspace, kind, record_id):
    return Record.objects.select_related("attachment", "created_by").filter(pk=record_id, workspace=workspace, kind=kind).first()


def _options(workspace):
    WorkspaceOptions.objects.get_or_create(workspace=workspace)
    return WorkspaceOptions.objects.select_for_update().get(workspace=workspace)


def _sync_options(workspace, kind, payload):
    options = _options(workspace)
    value = options.value
    changed = False
    if kind == "ledger":
        for field, key in (("description", "descriptions"), ("category", "categories")):
            candidate = str(payload.get(field) or "").strip()
            choices = value.setdefault("ledger", {}).setdefault(key, [])
            if candidate and candidate not in choices:
                choices.append(candidate)
                changed = True
    if kind == "task":
        candidate = str(payload.get("category") or "").strip()
        choices = value.setdefault("task", {}).setdefault("categories", [])
        if candidate and candidate not in choices:
            choices.append(candidate)
            changed = True
    if changed:
        options.value = value
        options.save(update_fields=("value",))


def _attachment_from_payload(workspace, user, collection, payload):
    specification = {
        "ledger": ("receiptImage", "receiptFileName", "receipt", True, False),
        "cases": ("caseFile", "caseFileName", "case", False, False),
        "correspondence": ("caseFile", "caseFileName", "case", False, False),
        "documents": ("documentFile", "documentFileName", "document", False, False),
    }.get(collection)
    if not specification:
        return None
    data_key, name_key, category, images_only, pdf_only = specification
    data_url = payload.pop(data_key, None)
    original_name = payload.pop(name_key, "Dokument")
    if not data_url:
        return None
    return validate_and_store_file(workspace, user, str(data_url), str(original_name), category, images_only=images_only, pdf_only=pdf_only)


def _sync_deadline_reminder(workspace, correspondence: Record, user):
    payload = correspondence.payload
    task_id = payload.get("reminderTaskId")
    existing = Record.objects.filter(workspace=workspace, kind="task", pk=task_id).first() if task_id else None
    days = str(payload.get("reminderDays") or "none")
    if payload.get("eventType") != "deadline" or days == "none" or not correspondence.event_date or payload.get("deadlineStatus") in {"met", "cancelled"}:
        if existing:
            existing.delete()
        if task_id:
            payload.pop("reminderTaskId", None)
            correspondence.payload = payload
            correspondence.save(update_fields=("payload", "updated_at"))
        return
    application = Record.objects.filter(workspace=workspace, kind="case", pk=correspondence.parent_id).first()
    due = max(correspondence.event_date - timedelta(days=int(days)), timezone.localdate())
    values = {
        "title": f"Frist: {payload.get('subject') or 'Antrag prüfen'} – {(application.title if application else 'Antrag')}",
        "category": "Anträge", "assignee": (application.payload.get("assignee") if application else "") or "",
        "due": due.isoformat(), "status": "planned", "recurrence": "once",
        "notes": f"Fristdatum: {correspondence.event_date.isoformat()}\n{payload.get('source') or 'Frist aus der Antragsakte'}\n{payload.get('notes') or ''}".strip(),
        "caseDeadlineId": str(correspondence.id),
    }
    if existing:
        update_record(existing, values, user)
    else:
        task = create_record(workspace, "task", values, user)
        payload["reminderTaskId"] = str(task.id)
        correspondence.payload = payload
        correspondence.save(update_fields=("payload", "updated_at"))


def _validate_payload(workspace, collection, payload):
    required = {
        "cases": ("title",), "correspondence": ("caseId", "subject", "date"),
        "tasks": ("title", "due"), "calendarEvents": ("title", "date"),
        "documents": ("title",), "ledger": ("description", "accountId", "amount", "date", "type"),
        "accounts": ("name",),
    }.get(collection, ())
    missing = [key for key in required if str(payload.get(key) or "").strip() == ""]
    if missing:
        raise ValueError("Bitte fülle alle Pflichtfelder aus.")
    for key in ("date", "due", "endDate", "recurrenceUntil"):
        if payload.get(key):
            try:
                date.fromisoformat(str(payload[key]))
            except ValueError as error:
                raise ValueError("Mindestens ein Datum ist ungültig.") from error
    if collection == "calendarEvents":
        for key in ("startTime", "endTime"):
            if payload.get(key):
                try:
                    datetime.strptime(str(payload[key]), "%H:%M")
                except ValueError as error:
                    raise ValueError("Mindestens eine Uhrzeit ist ungültig.") from error
        if payload.get("startTime") and payload.get("endTime") and not payload.get("endDate") and str(payload["endTime"]) < str(payload["startTime"]):
            raise ValueError("Das Terminende darf nicht vor dem Beginn liegen.")
    if collection == "ledger":
        if payload.get("type") not in {"income", "expense"}:
            raise ValueError("Die Buchungsart ist ungültig.")
        if payload.get("receiptStatus") not in {None, "", "available", "none"}:
            raise ValueError("Der Belegstatus ist ungültig.")
        try:
            amount = Decimal(str(payload.get("amount")).replace(",", "."))
            if not amount.is_finite() or amount < 0 or amount >= Decimal("1000000000000"):
                raise ValueError
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Der Betrag ist ungültig.") from error
        if not Record.objects.filter(workspace=workspace, kind="account", pk=payload.get("accountId")).exists():
            raise ValueError("Bitte wähle ein gültiges Konto aus.")
    allowed_statuses = {
        "cases": {"draft", "collecting", "submitted", "question", "approved", "done"},
        "tasks": {"open", "planned", "done", "refused"},
    }
    if collection in allowed_statuses and payload.get("status") not in {None, "", *allowed_statuses[collection]}:
        raise ValueError("Der Status ist ungültig.")
    if collection in {"tasks", "calendarEvents"} and payload.get("recurrence") not in {None, "", "once", "daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"}:
        raise ValueError("Die Wiederholung ist ungültig.")
    if collection == "correspondence":
        if payload.get("eventType") not in {None, "", "correspondence", "letter", "deadline", "status", "note"}:
            raise ValueError("Die Art des Verlaufseintrags ist ungültig.")
        if payload.get("deadlineStatus") not in {None, "", "open", "met", "cancelled"}:
            raise ValueError("Der Friststatus ist ungültig.")
        if str(payload.get("reminderDays") or "none") not in {"none", "2", "7", "14", "30"}:
            raise ValueError("Die Erinnerung ist ungültig.")
    if collection == "accounts" and payload.get("type") not in {None, "", "Bargeld", "Bankkonto"}:
        raise ValueError("Die Kontoart ist ungültig.")


def generic_collection(request, collection: str, item_id=None):
    workspace, membership = membership_context(request)
    if workspace.status in {"paused", "deletion-pending"}:
        return JsonResponse({"error": "Dieser Bereich ist vorübergehend gesperrt."}, status=423)
    if collection not in PERMISSION_BY_COLLECTION:
        return JsonResponse({"error": "Nicht gefunden."}, status=404)
    if workspace.type == Workspace.Type.SHARED and collection in {"cases", "correspondence", "documents"}:
        return JsonResponse({"error": "Dieser Inhalt gehört in einen persönlichen Bereich."}, status=403)
    editing_own_member = collection == "members" and item_id and membership.member_id and str(membership.member_id) == str(item_id) and request.method == "PUT"
    if not allowed(membership, PERMISSION_BY_COLLECTION[collection]) and not editing_own_member:
        return JsonResponse({"error": "Keine Berechtigung für diesen Bereich."}, status=403)
    if collection == "accounts" and request.method in {"POST", "PUT", "DELETE"} and not membership.is_admin:
        return JsonResponse({"error": "Konten dürfen nur von Administrator*innen geändert werden."}, status=403)
    if collection == "members":
        return member_collection(request, workspace, membership, item_id, editing_own_member)
    kind = KIND_BY_COLLECTION[collection]
    with transaction.atomic():
        if request.method == "POST" and item_id is None:
            payload = parse_json(request)
            _validate_payload(workspace, collection, payload)
            if collection == "correspondence" and not Record.objects.filter(workspace=workspace, kind="case", pk=payload.get("caseId")).exists():
                return JsonResponse({"error": "Der zugehörige Antrag wurde nicht gefunden."}, status=400)
            if collection == "cases" and payload.get("parentCaseId") and not Record.objects.filter(workspace=workspace, kind="case", pk=payload.get("parentCaseId")).exists():
                return JsonResponse({"error": "Der Hauptantrag wurde nicht gefunden."}, status=400)
            if collection in {"tasks", "calendarEvents"}:
                return _create_recurring(request, workspace, membership, collection, payload)
            attachment = None if collection == "ledger" and payload.get("receiptStatus") == "none" else _attachment_from_payload(workspace, request.user, collection, payload)
            record = create_record(workspace, kind, payload, request.user, attachment)
            if collection == "correspondence":
                _sync_deadline_reminder(workspace, record, request.user)
            _sync_options(workspace, kind, payload)
            audit(workspace, request.user, "created", collection, record.id, f"{collection}: {record.title or 'Eintrag'}")
            return JsonResponse(record_dict(record), status=201)
        record = _record(workspace, kind, item_id)
        if not record:
            return JsonResponse({"error": "Eintrag nicht gefunden."}, status=404)
        if request.method == "PUT":
            payload = parse_json(request)
            _validate_payload(workspace, collection, {**record.payload, **payload})
            series_scope = str(payload.pop("_seriesScope", "single"))
            if collection in {"tasks", "calendarEvents"} and record.series_id and series_scope in {"future", "all"}:
                if collection == "tasks" and payload.get("due") and str(payload["due"]) < timezone.localdate().isoformat() and not membership.is_admin:
                    return JsonResponse({"error": "Vergangene Aufgaben dürfen nur von Administrator*innen ergänzt werden."}, status=403)
                return _update_series(request, workspace, collection, record, payload, series_scope)
            if collection == "tasks":
                if record.payload.get("deletedAt"):
                    return JsonResponse({"error": "Eine gelöschte Aufgabe kann nicht bearbeitet werden."}, status=409)
                new_due = str(payload.get("due") or (record.event_date.isoformat() if record.event_date else ""))
                old_due = record.event_date.isoformat() if record.event_date else ""
                if new_due and new_due < timezone.localdate().isoformat() and not membership.is_admin and new_due != old_due:
                    return JsonResponse({"error": "Vergangene Aufgaben dürfen nur von Administrator*innen ergänzt werden."}, status=403)
                if new_due != old_due:
                    history = list(record.payload.get("history", []))
                    history.append({"type": "rescheduled", "from": old_due, "to": new_due, "at": timezone.now().isoformat(), "byUserId": str(request.user.id), "byName": request.user.label})
                    payload["history"] = history
            old_attachment = record.attachment
            attachment = None if collection == "ledger" and payload.get("receiptStatus") == "none" else _attachment_from_payload(workspace, request.user, collection, payload)
            if collection == "ledger" and payload.get("receiptStatus") == "none":
                record.attachment = None
            update_record(record, payload, request.user, attachment)
            if old_attachment and old_attachment != record.attachment and not old_attachment.records.exclude(pk=record.pk).exists():
                old_attachment.delete()
            if collection == "correspondence":
                _sync_deadline_reminder(workspace, record, request.user)
            _sync_options(workspace, kind, record.payload)
            audit(workspace, request.user, "updated", collection, record.id, f"{collection}: Eintrag geändert")
            return JsonResponse(record_dict(record))
        if request.method == "DELETE":
            payload = parse_json(request)
            series_scope = str(payload.get("_seriesScope") or "single")
            if collection in {"tasks", "calendarEvents"} and record.series_id and series_scope in {"future", "all"}:
                date_filter = {} if series_scope == "all" else {"event_date__gte": record.event_date}
                targets = Record.objects.filter(workspace=workspace, kind=kind, series_id=record.series_id, **date_filter)
                target_ids = list(targets.values_list("id", flat=True))
                count = targets.count()
                if collection == "tasks" and not membership.is_admin:
                    stamp = timezone.now().isoformat()
                    for target in targets:
                        target.payload.update({"deletedAt": stamp, "deletedByUserId": str(request.user.id), "deletedByName": request.user.label})
                        target.save(update_fields=("payload", "updated_at"))
                    return JsonResponse({"ok": True, "pendingAdminConfirmation": True, "affected": count})
                Record.objects.filter(workspace=workspace, kind="topic_comment", parent_id__in=target_ids).delete()
                targets.delete()
                audit(workspace, request.user, "deleted-series", collection, record.series_id, f"{count} Vorkommnisse einer Serie gelöscht")
                return JsonResponse({"ok": True, "affected": count})
            if collection == "tasks" and not membership.is_admin:
                record.payload.update({"deletedAt": timezone.now().isoformat(), "deletedByUserId": str(request.user.id), "deletedByName": request.user.label})
                record.save(update_fields=("payload", "updated_at"))
                audit(workspace, request.user, "delete-requested", "tasks", record.id, f"Löschung von „{record.title}“ vorgemerkt")
                return JsonResponse({"ok": True, "pendingAdminConfirmation": True})
            if collection == "cases" and (
                Record.objects.filter(workspace=workspace, kind="case", parent_id=record.id).exists()
                or Record.objects.filter(workspace=workspace, kind="correspondence", parent_id=record.id).exists()
            ):
                return JsonResponse(
                    {"error": "Dieser Antrag enthält noch Korrespondenz oder Ergänzungsanträge. Bitte lösche diese zuerst."},
                    status=409,
                )
            if collection == "accounts" and Record.objects.filter(workspace=workspace, kind="ledger", account_id=record.id).exists():
                return JsonResponse(
                    {"error": "Dieses Konto enthält noch Buchungen. Lösche oder verschiebe diese zuerst."},
                    status=409,
                )
            if collection == "correspondence" and record.payload.get("reminderTaskId"):
                Record.objects.filter(
                    workspace=workspace,
                    kind="task",
                    pk=record.payload["reminderTaskId"],
                ).delete()
            attachment = record.attachment
            if collection in {"cases", "tasks"}:
                Record.objects.filter(workspace=workspace, kind="topic_comment", parent_id=record.id).delete()
            record.delete()
            if attachment and not attachment.records.exists():
                attachment.delete()
            audit(workspace, request.user, "deleted", collection, item_id, f"{collection}: Eintrag gelöscht")
            return JsonResponse({"ok": True})
    return JsonResponse({"error": "Methode nicht erlaubt."}, status=405)


def _create_recurring(request, workspace, membership, collection, payload):
    kind = KIND_BY_COLLECTION[collection]
    key = "due" if collection == "tasks" else "date"
    start = str(payload.get(key) or "")
    recurrence = str(payload.get("recurrence") or "once")
    if payload.get("recurrenceUntil") and start and str(payload["recurrenceUntil"]) < start:
        return JsonResponse({"error": "Das Ende der Wiederholung darf nicht vor dem ersten Datum liegen."}, status=400)
    if collection == "tasks":
        if start and start < timezone.localdate().isoformat() and not membership.is_admin:
            return JsonResponse({"error": "Vergangene Aufgaben dürfen nur von Administrator*innen ergänzt werden."}, status=403)
        if payload.get("status") not in {None, "open", "planned", "done", "refused"}:
            return JsonResponse({"error": "Ungültiger Aufgabenstatus."}, status=400)
    if collection == "calendarEvents" and payload.get("endDate") and start and str(payload["endDate"]) < start:
        return JsonResponse({"error": "Das Terminende darf nicht vor dem Beginn liegen."}, status=400)
    try:
        dates = recurring_dates(start, recurrence, str(payload.get("recurrenceUntil") or "") or None)
    except ValueError:
        return JsonResponse({"error": "Das Datum der Wiederholung ist ungültig."}, status=400)
    series_id = uuid.uuid4() if recurrence != "once" else None
    duration = (date.fromisoformat(str(payload.get("endDate") or start)) - date.fromisoformat(start)).days if collection == "calendarEvents" and start else 0
    created = []
    for occurrence_date in dates or [start]:
        values = dict(payload)
        values[key] = occurrence_date
        if series_id:
            values["recurrenceSeriesId"] = str(series_id)
        if collection == "tasks":
            values.setdefault("history", [])
        if collection == "calendarEvents" and payload.get("endDate"):
            values["endDate"] = (date.fromisoformat(occurrence_date) + timedelta(days=duration)).isoformat()
        created.append(create_record(workspace, kind, values, request.user))
    _sync_options(workspace, kind, payload)
    audit(workspace, request.user, "created", collection, created[0].id, f"{collection}: {created[0].title}")
    return JsonResponse(record_dict(created[0]), status=201)


def _update_series(request, workspace, collection, selected, payload, scope):
    kind = KIND_BY_COLLECTION[collection]
    date_key = "due" if collection == "tasks" else "date"
    old_date = selected.event_date
    new_date = date.fromisoformat(str(payload.get(date_key))) if payload.get(date_key) else old_date
    if payload.get("recurrenceUntil") and new_date and date.fromisoformat(str(payload["recurrenceUntil"])) < new_date:
        return JsonResponse({"error": "Das Ende der Wiederholung darf nicht vor dem ersten Datum liegen."}, status=400)
    queryset = Record.objects.filter(workspace=workspace, kind=kind, series_id=selected.series_id)
    if scope == "future":
        queryset = queryset.filter(event_date__gte=old_date)
    targets = list(queryset)
    shift = (new_date - old_date).days if new_date and old_date else 0
    common = {key: value for key, value in payload.items() if key not in {"due", "date", "endDate"}}
    for target in targets:
        values = dict(common)
        if shift and target.event_date:
            values[date_key] = (target.event_date + timedelta(days=shift)).isoformat()
        if collection == "calendarEvents" and shift and target.payload.get("endDate"):
            values["endDate"] = (date.fromisoformat(target.payload["endDate"]) + timedelta(days=shift)).isoformat()
        update_record(target, values, request.user)
    audit(workspace, request.user, "updated-series", collection, selected.series_id, f"{len(targets)} Vorkommnisse einer Serie geändert")
    selected.refresh_from_db()
    return JsonResponse(record_dict(selected))


def member_collection(request, workspace, membership, item_id, editing_own):
    if request.method == "POST" and not item_id:
        payload = parse_json(request)
        if not str(payload.get("name") or "").strip():
            return JsonResponse({"error": "Bitte gib einen Namen an."}, status=400)
        email = valid_email_or_empty(payload.get("email"))
        photo_data = payload.pop("photoData", None)
        photo = validate_and_store_file(workspace, request.user, str(photo_data), str(payload.pop("photoName", "Profilfoto")), "member", images_only=True) if photo_data else None
        member = Member.objects.create(home_workspace=workspace, name=str(payload.get("name") or "")[:250], role=str(payload.get("role") or "")[:100], email=email, phone=str(payload.get("phone") or "")[:100], address=str(payload.get("address") or "")[:5000], personal_words=str(payload.get("personalWords") or "")[:5000], color=str(payload.get("color") or "#285c4d")[:30], photo=photo)
        return JsonResponse(member_dict(member), status=201)
    member = Member.objects.select_related("photo").filter(pk=item_id).first()
    if not member or not (member.home_workspace_id == workspace.id or editing_own):
        return JsonResponse({"error": "Eintrag nicht gefunden."}, status=404)
    if request.method == "PUT":
        payload = parse_json(request)
        if editing_own and not membership.is_admin:
            payload = {key: value for key, value in payload.items() if key in {"email", "phone", "address", "personalWords", "photoData", "photoName", "color"}}
        mapping = {"name": "name", "role": "role", "email": "email", "phone": "phone", "address": "address", "personalWords": "personal_words", "color": "color"}
        for source, target in mapping.items():
            if source in payload:
                value = valid_email_or_empty(payload[source]) if target == "email" else str(payload[source])[:5000 if target in {"address", "personal_words"} else 250]
                setattr(member, target, value)
        if payload.get("photoData"):
            old = member.photo
            member.photo = validate_and_store_file(workspace, request.user, str(payload["photoData"]), str(payload.get("photoName") or "Profilfoto"), "member", images_only=True)
            if old:
                old.delete()
        member.save()
        return JsonResponse(member_dict(member))
    if request.method == "DELETE" and membership.is_admin:
        if member.user_id or member.memberships.exists():
            return JsonResponse(
                {"error": "Dieses Profil ist mit einem Zugang verknüpft. Entferne zuerst die betreffende Mitgliedschaft."},
                status=409,
            )
        photo = member.photo
        member.delete()
        if photo and not photo.member_photos.exists():
            photo.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "Methode nicht erlaubt."}, status=405)


def profile_photo(request):
    workspace, membership = membership_context(request)
    if workspace.type != Workspace.Type.PRIVATE or not membership.is_admin:
        return JsonResponse({"error": "Das Profilfoto darf nur von Administrator*innen geändert werden."}, status=403)
    payload = parse_json(request)
    photo = validate_and_store_file(workspace, request.user, str(payload.get("photoData") or ""), str(payload.get("photoName") or "Profilfoto"), "profile", images_only=True)
    profile, _ = WorkspaceProfile.objects.get_or_create(workspace=workspace)
    old = profile.photo
    profile.photo = photo
    profile.updated_by = request.user
    profile.save()
    if old:
        old.delete()
    return JsonResponse(profile_dict(profile))


@transaction.atomic
def options_view(request, kind):
    workspace, membership = membership_context(request)
    permission = "ledger" if kind == "ledger" else "tasks" if kind == "task" else "family"
    if not allowed(membership, permission) or kind == "contact" and not membership.is_admin:
        return JsonResponse({"error": "Keine Berechtigung."}, status=403)
    payload = parse_json(request)
    options = _options(workspace)
    value = options.value
    if kind == "contact":
        categories = payload.get("categories")
        if not isinstance(categories, list):
            return JsonResponse({"error": "Kategorien müssen als Liste übermittelt werden."}, status=400)
        value["contact"] = {"categories": list(dict.fromkeys(str(item).strip()[:200] for item in categories if str(item).strip()))[:200]}
    elif kind == "task":
        categories = payload.get("categories")
        if not isinstance(categories, list):
            return JsonResponse({"error": "Kategorien müssen als Liste übermittelt werden."}, status=400)
        value["task"] = {"categories": list(dict.fromkeys(str(item).strip()[:200] for item in categories if str(item).strip()))[:200]}
    else:
        current = value.setdefault("ledger", {"descriptions": [], "categories": []})
        for key in ("descriptions", "categories"):
            if isinstance(payload.get(key), list):
                current[key] = list(dict.fromkeys(str(item).strip()[:200] for item in payload[key] if str(item).strip()))[:200]
    options.value = value
    options.save(update_fields=("value",))
    return JsonResponse(value[kind])


@transaction.atomic
def bank_import(request, commit=False):
    workspace, membership = membership_context(request)
    if not allowed(membership, "ledger"):
        return JsonResponse({"error": "Keine Berechtigung für das Kassenbuch."}, status=403)
    payload = parse_json(request)
    if commit:
        Workspace.objects.select_for_update().filter(pk=workspace.pk).exists()
    account_id = payload.get("accountId")
    if not Record.objects.filter(workspace=workspace, kind="account", pk=account_id).exists():
        return JsonResponse({"error": "Bitte wähle ein gültiges Konto aus."}, status=400)
    rows = parse_statement(str(payload.get("fileData") or ""), str(payload.get("fileName") or "kontoauszug.csv"))
    existing = set(Record.objects.filter(workspace=workspace, kind="ledger").values_list("payload__importFingerprint", flat=True))
    preview = [{**row, "amount": f"{row['amount']:.2f}", "duplicate": import_fingerprint(workspace.id, account_id, row) in existing} for row in rows]
    if not commit:
        return JsonResponse({"rows": preview[:500], "total": len(preview), "newCount": sum(not row["duplicate"] for row in preview), "duplicateCount": sum(row["duplicate"] for row in preview)})
    batch_id = str(uuid.uuid4())
    created = []
    for row in rows:
        fingerprint = import_fingerprint(workspace.id, account_id, row)
        if fingerprint in existing:
            continue
        values = {"accountId": account_id, "type": "income" if row["amount"] >= 0 else "expense", "amount": f"{abs(row['amount']):.2f}", "date": row["date"], "description": row["description"], "payee": row.get("payee", ""), "category": "", "receiptStatus": "none", "notes": "Manuell aus einem Kontoauszug importiert.", "reviewStatus": "pending", "importFingerprint": fingerprint, "importBatchId": batch_id, "importFileName": str(payload.get("fileName") or "Kontoauszug")[:255], "importedAt": timezone.now().isoformat()}
        created.append(create_record(workspace, "ledger", values, request.user))
        existing.add(fingerprint)
    audit(workspace, request.user, "imported", "ledger", batch_id, f"{len(created)} Bankumsätze importiert")
    return JsonResponse({"imported": len(created), "duplicates": len(rows) - len(created), "batchId": batch_id}, status=201)


def ledger_review(request, record_id):
    workspace, membership = membership_context(request)
    if not allowed(membership, "ledger"):
        return JsonResponse({"error": "Keine Berechtigung für das Kassenbuch."}, status=403)
    record = _record(workspace, "ledger", record_id)
    if not record:
        return JsonResponse({"error": "Buchung nicht gefunden."}, status=404)
    update_record(record, {"reviewStatus": "reviewed", "reviewedAt": timezone.now().isoformat(), "reviewedByName": request.user.label}, request.user)
    return JsonResponse(record_dict(record))
