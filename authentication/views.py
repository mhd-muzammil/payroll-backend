import logging

from django.utils import timezone
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated, AllowAny
from django_filters.rest_framework import DjangoFilterBackend
from .serializers import RoleTokenObtainPairSerializer, UserSerializer
from .models import User
from .permissions import IsAdmin

logger = logging.getLogger(__name__)


# The header the phone app sets on every request. Only the app sends it, so a
# login carrying it is a login from the app -- which is the one thing the
# browser cannot fake by accident and the only signal we have that somebody has
# actually installed the thing.
APP_CLIENT_HEADER = "HTTP_X_PAYROLL_CLIENT"
APP_CLIENT_VALUE = "app"


class RoleTokenObtainPairView(TokenObtainPairView):
    serializer_class = RoleTokenObtainPairSerializer
    permission_classes = [AllowAny]  # login must stay public

    def post(self, request, *args, **kwargs):
        """Sign in, and note it if it came from the app.

        SIMPLE_JWT's UPDATE_LAST_LOGIN already stamps `last_login` for any
        client. This adds the two app-specific stamps on top, so HR can tell
        "has an account and has never opened the app" from "uses the site on a
        laptop" -- the first needs chasing, the second does not.

        Stamped after the parent has answered, and only on success: a failed
        password attempt must not look like somebody using the app. Wrapped so
        that a write failure here can never cost anybody their login.
        """
        response = super().post(request, *args, **kwargs)

        if response.status_code == 200 and request.META.get(APP_CLIENT_HEADER) == APP_CLIENT_VALUE:
            username = (request.data or {}).get("username")
            if username:
                try:
                    now = timezone.now()
                    fields = {"last_app_login_at": now}
                    user = User.objects.filter(username=username).first()
                    if user is not None:
                        if user.first_app_login_at is None:
                            fields["first_app_login_at"] = now
                        User.objects.filter(pk=user.pk).update(**fields)
                except Exception:  # noqa: BLE001 - never break a login over this
                    logger.exception("could not record an app login for %s", username)

        return response


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by("-date_joined")
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, IsAdmin]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["role", "is_active"]
    search_fields = ["username", "email", "first_name", "last_name"]
