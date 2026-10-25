from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    AppointmentOrderViewSet,
    CandidateViewSet,
    OfferLetterViewSet,
    OnboardingLinkViewSet,
    OnboardingViewSet,
    PublicAppointmentOrderCodeView,
    PublicAppointmentOrderPdfView,
    PublicAppointmentOrderSignView,
    PublicAppointmentOrderView,
    PublicOfferRespondView,
    PublicOfferView,
    PublicOnboardingView,
)

router = DefaultRouter()
router.register(r'onboarding', OnboardingViewSet, basename='onboarding')
router.register(r'onboarding-links', OnboardingLinkViewSet, basename='onboarding-link')
router.register(r'candidates', CandidateViewSet, basename='candidate')
router.register(r'offer-letters', OfferLetterViewSet, basename='offer-letter')
router.register(r'appointment-orders', AppointmentOrderViewSet, basename='appointment-order')

urlpatterns = [
    # The one public route in this app. Kept out of the router and spelled out
    # here so it is visible, rather than one basename among several.
    path('onboard/<str:token>/', PublicOnboardingView.as_view(), name='public-onboard'),
    # The employee signing their appointment order: the same kind of door, a
    # token in place of a login (see AppointmentOrderViewSet).
    path('appointment/<str:token>/', PublicAppointmentOrderView.as_view(), name='public-appointment'),
    path('appointment/<str:token>/pdf/', PublicAppointmentOrderPdfView.as_view(), name='public-appointment-pdf'),
    path('appointment/<str:token>/code/', PublicAppointmentOrderCodeView.as_view(), name='public-appointment-code'),
    path('appointment/<str:token>/sign/', PublicAppointmentOrderSignView.as_view(), name='public-appointment-sign'),
    # The candidate answering an offer mail (Accept / Decline).
    path('offer/<str:token>/', PublicOfferView.as_view(), name='public-offer'),
    path('offer/<str:token>/respond/', PublicOfferRespondView.as_view(), name='public-offer-respond'),
    path('', include(router.urls)),
]
