from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CandidateViewSet,
    OnboardingLinkViewSet,
    OnboardingViewSet,
    PublicOnboardingView,
)

router = DefaultRouter()
router.register(r'onboarding', OnboardingViewSet, basename='onboarding')
router.register(r'onboarding-links', OnboardingLinkViewSet, basename='onboarding-link')
router.register(r'candidates', CandidateViewSet, basename='candidate')

urlpatterns = [
    # The one public route in this app. Kept out of the router and spelled out
    # here so it is visible, rather than one basename among several.
    path('onboard/<str:token>/', PublicOnboardingView.as_view(), name='public-onboard'),
    path('', include(router.urls)),
]
