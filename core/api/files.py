from django.http import FileResponse, JsonResponse

from core.models import ManagedFile, Membership
from core.services import allowed, membership_context


def secure_file(request, file_id):
    workspace, membership = membership_context(request)
    managed = ManagedFile.objects.filter(pk=file_id).first()
    if not managed:
        return JsonResponse({"error": "Datei nicht gefunden."}, status=404)
    category_permission = {"receipt": "ledger", "case": "cases", "document": "documents", "routine": "tasks"}
    permission = category_permission.get(managed.category)
    permitted = managed.workspace_id == workspace.id and (not permission or allowed(membership, permission))
    if managed.category == "member":
        permitted = Membership.objects.filter(workspace=workspace, member__photo_id=managed.id).exists() or managed.workspace_id == workspace.id
    if not permitted:
        return JsonResponse({"error": "Datei nicht gefunden."}, status=404)
    try:
        inline = managed.content_type.startswith("image/") or managed.content_type == "application/pdf"
        response = FileResponse(managed.file.open("rb"), as_attachment=not inline, filename=managed.original_name, content_type=managed.content_type)
    except FileNotFoundError:
        return JsonResponse({"error": "Datei nicht gefunden."}, status=404)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["X-Frame-Options"] = "DENY"
    return response
