from django.db import connections
from django.db.utils import OperationalError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods


@require_http_methods(["GET", "HEAD"])
def app_head(request):
    return HttpResponse("ok\n", content_type="text/plain")


def health_check(request):
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
    except OperationalError:
        return JsonResponse({"status": "unhealthy", "database": "unavailable"}, status=503)

    return JsonResponse({"status": "ok"})
