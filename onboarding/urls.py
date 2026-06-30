from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import OnboardingViewSet, CandidateViewSet

router = DefaultRouter()
router.register(r'onboarding', OnboardingViewSet, basename='onboarding')
router.register(r'candidates', CandidateViewSet, basename='candidate')

urlpatterns = [
    path('', include(router.urls)),
]
