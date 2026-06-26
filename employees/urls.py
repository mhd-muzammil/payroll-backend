from django.urls import path,include
from .views import EmployeeViewSet, TaskViewSet, PerformanceViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r'employees', EmployeeViewSet)
router.register(r'tasks', TaskViewSet)
router.register(r'performance', PerformanceViewSet)

urlpatterns = [
    path('', include(router.urls)),
]

