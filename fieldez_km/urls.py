from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import FieldezKmViewSet

# SimpleRouter, not DefaultRouter: no API root view of its own, so nothing here
# can answer for /api/ in place of the apps included before it.
router = SimpleRouter()
router.register(r"fieldez_km", FieldezKmViewSet, basename="fieldez-km")

urlpatterns = [
    path("", include(router.urls)),
]
