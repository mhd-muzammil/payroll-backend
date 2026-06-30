from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PayslipViewSet, BranchFinancialViewSet

router = DefaultRouter()
router.register(r'payslips', PayslipViewSet, basename='payslip')
router.register(r'branch-financials', BranchFinancialViewSet, basename='branch-financial')

urlpatterns = [
    path('', include(router.urls)),
]
