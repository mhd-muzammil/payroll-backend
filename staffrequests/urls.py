from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import EmployeeRequestViewSet

router = DefaultRouter()
router.register(r"requests", EmployeeRequestViewSet, basename="staff-request")

urlpatterns = [
    path("", include(router.urls)),
]
