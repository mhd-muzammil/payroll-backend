from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CaseViewSet, TrackingViewSet

router = DefaultRouter()
router.register(r"cases", CaseViewSet, basename="case")
router.register(r"tracking", TrackingViewSet, basename="tracking")

urlpatterns = [
    path("", include(router.urls)),
]
