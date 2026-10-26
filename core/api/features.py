import re
import uuid
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from core.models import DeveloperFeedback, Record, Workspace
from core.services import allowed, audit, create_record, membership_context, parse_json, record_dict, update_record, valid_email_or_empty, validate_and_store_file


logger = logging.getLogger(__name__)


def _item(workspace, kind, item_id):
    return Record.objects.select_related("attachment", "created_by").filter(workspace=workspace, kind=kind, pk=item_id).first()


def _remove(record):
    attachment = record.attachment
    record.delete()
    if attachment and not attachment.records.exists():
        attachment.delete()


def about_collection(request, collection, item_id=None):
    workspace, membership = membership_context(request)
    if workspace.type != Workspace.Type.PRIVATE:
        return JsonResponse({"error": "Diese Inhalte gibt es nur im persönlichen Bereich."}, status=403)
    kind = {"goals": "goal", "rules": "rule", "comments": "about_comment", "beis": "bei"}.get(collection)
    if not kind:
        return JsonResponse({"error": "Nicht gefunden."}, status=404)
    admin_only = collection in {"goals", "beis"}
    with transaction.atomic():
        if request.method == "POST" and item_id is None:
            if admin_only and not membership.is_admin:
                return JsonResponse({"error": "Dieser Eintrag darf nur von Administrator*innen angelegt werden."}, status=403)
            payload = parse_json(request)
            if collection == "goals" and not str(payload.get("title") or "").strip():
                return JsonResponse({"error": "Bitte gib dem Ziel einen Titel."}, status=400)
            if collection == "rules" and (not str(payload.get("title") or "").strip() or not str(payload.get("text") or "").strip()):
                return JsonResponse({"error": "Bitte gib Titel und Vereinbarung ein."}, status=400)
            if collection == "comments" and not str(payload.get("text") or "").strip():
                return JsonResponse({"error": "Der Kommentar darf nicht leer sein."}, status=400)
            attachment = None
            if collection == "comments":
                target_kind = {"goal": "goal", "rule": "rule"}.get(payload.get("targetType"))
                if not target_kind or not Record.objects.filter(workspace=workspace, kind=target_kind, pk=payload.get("targetId")).exists():
                    return JsonResponse({"error": "Das Ziel oder die Regel wurde nicht gefunden."}, status=400)
            if collection == "rules":
                payload["approvalStatus"] = "pending"
            if collection == "beis":
                file_data = payload.pop("beiFile", "")
                if not file_data:
                    return JsonResponse({"error": "Bitte lade das BEI als PDF hoch."}, status=400)
                attachment = validate_and_store_file(workspace, request.user, file_data, payload.pop("beiFileName", "BEI.pdf"), "bei", pdf_only=True)
            record = create_record(workspace, kind, payload, request.user, attachment)
            audit(workspace, request.user, "created", collection, record.id, f"{collection}: Eintrag angelegt")
            return JsonResponse(record_dict(record), status=201)
        record = _item(workspace, kind, item_id)
        if not record:
            return JsonResponse({"error": "Eintrag nicht gefunden."}, status=404)
        own_comment = collection == "comments" and record.created_by_id == request.user.id
        if request.method == "PUT":
            if collection in {"goals", "rules", "beis"} and not membership.is_admin or collection == "comments" and not (membership.is_admin or own_comment):
                return JsonResponse({"error": "Keine Berechtigung für diese Änderung."}, status=403)
            payload = parse_json(request)
            attachment = None
            if collection == "beis" and payload.get("beiFile"):
                attachment = validate_and_store_file(workspace, request.user, payload.pop("beiFile"), payload.pop("beiFileName", "BEI.pdf"), "bei", pdf_only=True)
            if collection == "rules" and payload.get("approvalStatus") in {"approved", "rejected"}:
                payload.update({"reviewedAt": timezone.now().isoformat(), "reviewedByName": request.user.label})
            old = record.attachment
            update_record(record, payload, request.user, attachment)
            if old and old != record.attachment and not old.records.exists():
                old.delete()
            audit(workspace, request.user, "updated", collection, record.id, f"{collection}: Eintrag geändert")
            return JsonResponse(record_dict(record))
        if request.method == "DELETE":
            if collection in {"goals", "rules", "beis"} and not membership.is_admin or collection == "comments" and not (membership.is_admin or own_comment):
                return JsonResponse({"error": "Keine Berechtigung für diese Löschung."}, status=403)
            if collection in {"goals", "rules"}:
                Record.objects.filter(workspace=workspace, kind="about_comment", parent_id=record.id).delete()
                Record.objects.filter(workspace=workspace, kind="topic_comment", parent_id=record.id).delete()
            _remove(record)
            audit(workspace, request.user, "deleted", collection, item_id, f"{collection}: Eintrag gelöscht")
            return JsonResponse({"ok": True})
    return JsonResponse({"error": "Methode nicht erlaubt."}, status=405)


def contacts(request, item_id=None):
    workspace, membership = membership_context(request)
    if workspace.type != Workspace.Type.PRIVATE:
        return JsonResponse({"error": "Kontakte gehören in einen persönlichen Bereich."}, status=403)
    if request.method == "POST" and item_id is None:
        payload = parse_json(request)
        if not str(payload.get("name") or "").strip():
            return JsonResponse({"error": "Bitte gib einen Namen oder eine Organisation an."}, status=400)
        if "email" in payload:
            payload["email"] = valid_email_or_empty(payload.get("email"))
        record = create_record(workspace, "contact", payload, request.user)
        return JsonResponse(record_dict(record), status=201)
    record = _item(workspace, "contact", item_id)
    if not record:
        return JsonResponse({"error": "Kontakt nicht gefunden."}, status=404)
    if not (membership.is_admin or record.created_by_id == request.user.id):
        return JsonResponse({"error": "Du kannst nur eigene Kontakte bearbeiten oder löschen."}, status=403)
    if request.method == "PUT":
        payload = parse_json(request)
        if "email" in payload:
            payload["email"] = valid_email_or_empty(payload.get("email"))
        update_record(record, payload, request.user)
        return JsonResponse(record_dict(record))
    if request.method == "DELETE":
        record.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "Methode nicht erlaubt."}, status=405)


def routine_plans(request, item_id=None, review=False):
    workspace, membership = membership_context(request)
    if not allowed(membership, "tasks"):
        return JsonResponse({"error": "Keine Berechtigung für Ablaufpläne."}, status=403)
    plan = _item(workspace, "routine_plan", item_id) if item_id else None
    if review and request.method == "PUT":
        if not membership.is_admin:
            return JsonResponse({"error": "Ablaufpläne dürfen nur von Administrator*innen freigegeben werden."}, status=403)
        if not plan:
            return JsonResponse({"error": "Ablaufplan nicht gefunden."}, status=404)
        decision = parse_json(request).get("approvalStatus")
        if decision not in {"approved", "rejected"}:
            return JsonResponse({"error": "Ungültige Entscheidung."}, status=400)
        if decision == "approved":
            for current in Record.objects.filter(workspace=workspace, kind="routine_plan", series_id=plan.series_id, status="approved"):
                update_record(current, {"approvalStatus": "superseded"}, request.user)
        update_record(plan, {"approvalStatus": decision, "reviewedAt": timezone.now().isoformat(), "reviewedByName": request.user.label}, request.user)
        audit(workspace, request.user, "reviewed", "routine-plan", plan.id, f"Ablaufplan {decision}")
        return JsonResponse(record_dict(plan))
    if request.method == "POST" and item_id is None:
        payload = parse_json(request)
        if not str(payload.get("title") or "").strip():
            return JsonResponse({"error": "Bitte gib dem Ablaufplan einen Titel."}, status=400)
        attachment = None
        if payload.get("planFile"):
            attachment = validate_and_store_file(workspace, request.user, payload.pop("planFile"), payload.pop("planFileName", "Ablaufplan"), "routine")
        payload.update({"seriesId": str(uuid.uuid4()), "version": 1, "approvalStatus": "pending"})
        plan = create_record(workspace, "routine_plan", payload, request.user, attachment)
        return JsonResponse(record_dict(plan), status=201)
    if not plan:
        return JsonResponse({"error": "Ablaufplan nicht gefunden."}, status=404)
    if request.method == "PUT":
        payload = parse_json(request)
        versions = Record.objects.filter(workspace=workspace, kind="routine_plan", series_id=plan.series_id)
        version = max([int(item.payload.get("version", 1)) for item in versions] or [0]) + 1
        attachment = plan.attachment
        if payload.get("planFile"):
            attachment = validate_and_store_file(workspace, request.user, payload.pop("planFile"), payload.pop("planFileName", "Ablaufplan"), "routine")
        payload.update({"seriesId": str(plan.series_id), "version": version, "approvalStatus": "pending", "basedOnId": str(plan.id)})
        revision = create_record(workspace, "routine_plan", payload, request.user, attachment)
        return JsonResponse(record_dict(revision), status=201)
    if request.method == "DELETE":
        if not (membership.is_admin or plan.created_by_id == request.user.id and plan.status == "pending"):
            return JsonResponse({"error": "Du kannst nur eigene, noch nicht freigegebene Entwürfe löschen."}, status=403)
        _remove(plan)
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "Methode nicht erlaubt."}, status=405)


def announcements(request, item_id=None, read=False):
    workspace, membership = membership_context(request)
    record = _item(workspace, "announcement", item_id) if item_id else None
    if read and request.method == "PUT":
        if not record:
            return JsonResponse({"error": "Information nicht gefunden."}, status=404)
        readers = list(record.payload.get("readByUserIds", []))
        if str(request.user.id) not in readers:
            readers.append(str(request.user.id))
            update_record(record, {"readByUserIds": readers}, request.user)
        return JsonResponse(record_dict(record))
    if request.method == "POST" and item_id is None:
        payload = parse_json(request)
        if not payload.get("title") or not payload.get("text"):
            return JsonResponse({"error": "Überschrift und Information werden benötigt."}, status=400)
        payload["readByUserIds"] = [str(request.user.id)]
        record = create_record(workspace, "announcement", payload, request.user)
        _notify(workspace, request.user, payload.get("text", ""), "Neue Information in Miteinander")
        return JsonResponse(record_dict(record), status=201)
    if not record:
        return JsonResponse({"error": "Information nicht gefunden."}, status=404)
    if request.method == "DELETE" and (membership.is_admin or record.created_by_id == request.user.id):
        record.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "Keine Berechtigung."}, status=403)


def topic_comments(request, item_id=None):
    workspace, membership = membership_context(request)
    if request.method == "POST" and item_id is None:
        payload = parse_json(request)
        if not str(payload.get("text") or "").strip():
            return JsonResponse({"error": "Der Beitrag darf nicht leer sein."}, status=400)
        target_kind = {"goal": "goal", "case": "case", "task": "task"}.get(payload.get("targetType"))
        required = {"case": "cases", "task": "tasks"}.get(payload.get("targetType"))
        if required and not allowed(membership, required):
            return JsonResponse({"error": "Keine Berechtigung für dieses Thema."}, status=403)
        if not target_kind or not Record.objects.filter(workspace=workspace, kind=target_kind, pk=payload.get("targetId")).exists():
            return JsonResponse({"error": "Das Thema wurde nicht gefunden."}, status=400)
        record = create_record(workspace, "topic_comment", payload, request.user)
        _notify(workspace, request.user, payload.get("text", ""), "Neuer Beitrag in Miteinander", required or "")
        return JsonResponse(record_dict(record), status=201)
    record = _item(workspace, "topic_comment", item_id)
    if not record:
        return JsonResponse({"error": "Beitrag nicht gefunden."}, status=404)
    if request.method == "DELETE" and (membership.is_admin or record.created_by_id == request.user.id):
        record.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"error": "Keine Berechtigung."}, status=403)


def developer_feedback(request):
    workspace, _ = membership_context(request)
    rate_key = f"developer-feedback:{request.user.id}"
    current_count = int(cache.get(rate_key, 0))
    if current_count >= 10:
        return JsonResponse({"error": "Du hast bereits mehrere Vorschläge gesendet. Bitte versuche es später erneut."}, status=429)
    payload = parse_json(request)
    message = str(payload.get("message") or "").strip()
    category = str(payload.get("category") or "other")[:100]
    if len(message) < 10 or len(message) > 4000:
        return JsonResponse({"error": "Der Vorschlag muss zwischen 10 und 4.000 Zeichen lang sein."}, status=400)
    item = DeveloperFeedback.objects.create(category=category, message=message, created_by=request.user, created_by_name=request.user.label, workspace_type=workspace.type)
    cache.set(rate_key, current_count + 1, 3600)
    if settings.DEVELOPER_CONTACT_EMAIL and settings.EMAIL_HOST:
        from core.tasks import send_feedback_email
        transaction.on_commit(lambda: _enqueue(send_feedback_email, str(item.id)))
    return JsonResponse({"ok": True, "id": str(item.id)}, status=201)


def _notify(workspace, author, text, subject, required_permission=""):
    usernames = set(re.findall(r"(?<!\w)@([\w.@+-]{3,150})", str(text)))
    if not settings.EMAIL_HOST:
        return
    from core.tasks import send_workspace_notification
    transaction.on_commit(lambda: _enqueue(send_workspace_notification, str(workspace.id), str(author.id), sorted(usernames), subject, required_permission))


def _enqueue(task, *args):
    try:
        task.delay(*args)
    except Exception:
        logger.exception("Background notification could not be queued")
