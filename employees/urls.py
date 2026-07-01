from django.urls import path,include
from .views import EmployeeViewSet, TaskViewSet, PerformanceViewSet, AssetViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r'employees', EmployeeViewSet)
router.register(r'tasks', TaskViewSet)
router.register(r'performance', PerformanceViewSet)
router.register(r'assets', AssetViewSet)

urlpatterns = [
    path('', include(router.urls)),
]

