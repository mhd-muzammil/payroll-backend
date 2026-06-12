from django.db import connections
from django.db.utils import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods


@require_http_methods(["GET", "HEAD"])
def app_head(request):
    return HttpResponse("ok\n", content_type="text/plain")


@require_http_methods(["GET", "HEAD"])
def live_check(request):
    return JsonResponse({"status": "ok"})


@require_http_methods(["GET", "HEAD"])
def health_check(request):
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "unhealthy", "database": "unavailable"}, status=503)

    return JsonResponse({"status": "ok", "database": "available"})
