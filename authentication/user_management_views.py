from django.db.models import Q
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import User
from .permissions import IsAdmin
from .user_management_serializers import UserManagementSerializer


class UserManagementViewSet(viewsets.ModelViewSet):
    serializer_class = UserManagementSerializer
    permission_classes = [IsAuthenticated, IsAdmin]
    queryset = User.objects.all().order_by("-date_joined")

    def get_queryset(self):
        queryset = super().get_queryset()

        # Branch scoping: superadmin/superuser sees all; a branch-scoped admin
        # only sees users in their branch (plus unassigned). Prevents IDOR.
        user = self.request.user
        if not (user.is_superuser or getattr(user, "role", None) == "superadmin"):
            assigned = getattr(user, "assigned_branch", None)
            if assigned:
                queryset = queryset.filter(
                    Q(assigned_branch=assigned) | Q(assigned_branch__isnull=True)
                )

        q = self.request.query_params.get("q")
        role = self.request.query_params.get("role")
        is_active = self.request.query_params.get("is_active")

        if q:
            queryset = queryset.filter(
                Q(username__icontains=q)
                | Q(email__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            )
        if role:
            queryset = queryset.filter(role=role)
        if is_active in ["true", "false"]:
            queryset = queryset.filter(is_active=(is_active == "true"))
        return queryset
