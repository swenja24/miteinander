import json
import logging
from functools import wraps

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, JsonResponse
from django.conf import settings
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


logger = logging.getLogger(__name__)


@ensure_csrf_cookie
def index(request):
    return render(request, "index.html")


def health(request):
    from django.db import connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        logger.exception("Database health check failed")
        return JsonResponse({"status": "unhealthy"}, status=503)
    return JsonResponse({"status": "ok"})


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
        except (json.JSONDecodeError, ValueError, ValidationError) as error:
            message = getattr(error, "message", None) or str(error) or "Ungültige Eingabe."
            return JsonResponse({"error": message}, status=400)
        except (PermissionError, PermissionDenied) as error:
            return JsonResponse({"error": str(error) or "Keine Berechtigung."}, status=403)
        except Exception:
            logger.exception("Unhandled API error on %s", request.path)
            return JsonResponse({"error": "Die Anfrage konnte nicht verarbeitet werden."}, status=500)
    return wrapped
