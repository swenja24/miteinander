import json
import logging
from functools import wraps

from django.core.exceptions import PermissionDenied, RequestDataTooBig, ValidationError
from django.core.cache import cache
from django.http import FileResponse, JsonResponse
from django.conf import settings
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


logger = logging.getLogger(__name__)


@ensure_csrf_cookie
def index(request):
    response = render(request, "index.html")
    response["Cache-Control"] = "no-cache"
    return response


def health(request):
    from django.db import connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        cache.set("health-check", "ok", 10)
        if cache.get("health-check") != "ok":
            raise RuntimeError("Cache health check failed")
    except Exception:
        logger.exception("Database health check failed")
        return JsonResponse({"status": "unhealthy"}, status=503)
    return JsonResponse({"status": "ok"})


def csrf_failure(request, reason=""):
    if request.path.startswith("/api/"):
        return JsonResponse({"error": "Die Sicherheitsprüfung ist abgelaufen. Lade die Seite neu und versuche es noch einmal."}, status=403)
    return JsonResponse({"error": "Sicherheitsprüfung fehlgeschlagen."}, status=403)


def service_worker(request):
    response = FileResponse((settings.BASE_DIR / "public" / "service-worker.js").open("rb"), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def api_endpoint(view, *, methods=None, public=False):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if methods and request.method not in methods:
            return JsonResponse({"error": "Methode nicht erlaubt."}, status=405)
        if not public and not request.user.is_authenticated:
            return JsonResponse({"error": "Bitte melde dich an."}, status=401)
        if not public and (not request.user.is_active or request.user.access_status != "active"):
            from django.contrib.auth import logout
            logout(request)
            return JsonResponse({"error": "Dieser Zugang ist pausiert oder deaktiviert."}, status=401)
        try:
            return view(request, *args, **kwargs)
        except RequestDataTooBig:
            return JsonResponse({"error": "Die Anfrage ist zu groß."}, status=413)
        except (json.JSONDecodeError, ValueError, ValidationError) as error:
            message = getattr(error, "message", None) or str(error) or "Ungültige Eingabe."
            return JsonResponse({"error": message}, status=400)
        except PermissionError as error:
            from core.services import WorkspaceLockedError
            if isinstance(error, WorkspaceLockedError):
                return JsonResponse({"error": str(error)}, status=423)
            return JsonResponse({"error": str(error) or "Keine Berechtigung."}, status=403)
        except PermissionDenied as error:
            return JsonResponse({"error": str(error) or "Keine Berechtigung."}, status=403)
        except Exception:
            logger.exception("Unhandled API error")
            return JsonResponse({"error": "Die Anfrage konnte nicht verarbeitet werden."}, status=500)
    return wrapped
