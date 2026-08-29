from django.db.models import Q
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from .models import Employee, Task, Performance, Asset
from .serializer import EmployeeSerializer, SimpleEmployeeSerializer
from .task_serializer import TaskSerializer
from .performance_serializer import PerformanceSerializer
from .asset_serializer import AssetSerializer
from authentication.models import get_allowed_branches

class EmployeeViewSet(viewsets.ModelViewSet):
    queryset = Employee.objects.all()
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee" and self.action == 'list':
            return SimpleEmployeeSerializer
        return EmployeeSerializer

    def get_queryset(self):
        user = self.request.user
        queryset = Employee.objects.all().order_by("-id")
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            # Employees may only ever see their own record (no cross-branch
            # PII exposure in the list action).
            return queryset.filter(user=user)

        branches = get_allowed_branches(user, "employees")
        if "All" not in branches:
            queryset = queryset.filter(branch__in=branches)
        # Someone marked Relieved in onboarding has left, so they drop off the
        # working list. Their record and everything hanging off it is still
        # here — ?include_relieved=1 shows them, and setting them back to
        # Active in onboarding brings them back for good.
        if self.request.query_params.get("include_relieved") not in ("1", "true", "True"):
            queryset = queryset.exclude(status="relieved")
        return queryset

    def _guard_write(self):
        """Employees must not create/update/delete employee records (this is
        where salary/EPF/branch/user live — mass-assignment risk)."""
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            raise PermissionDenied("Employees cannot modify employee records.")

    def _guard_branch(self, request):
        """A branch-scoped admin/HR may only create or move an employee within
        their allowed branch(es) — otherwise they could plant/move records into
        branches they don't manage."""
        branches = get_allowed_branches(self.request.user, "employees")
        if "All" in branches:
            return
        target_branch = request.data.get("branch")
        if target_branch and target_branch not in branches:
            raise PermissionDenied(
                f"You can only manage employees in your branch(es): {', '.join(branches) or 'none'}."
            )

    def create(self, request, *args, **kwargs):
        self._guard_write()
        self._guard_branch(request)
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self._guard_write()
        self._guard_branch(request)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        self._guard_write()
        return super().destroy(request, *args, **kwargs)


class TaskViewSet(viewsets.ModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Task.objects.all().order_by("-id")
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            return queryset.filter(Q(assigned_to__user=user) | Q(assigned_by=user))
        
        branches = get_allowed_branches(user, "tasks")
        if "Own" in branches:
            queryset = queryset.filter(Q(assigned_to__user=user) | Q(assigned_by=user))
        elif "All" not in branches:
            queryset = queryset.filter(assigned_to__branch__in=branches)
        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(assigned_by=user)

    def update(self, request, *args, **kwargs):
        user = self.request.user
        instance = self.get_object()
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            if instance.assigned_by != user:
                # activity_log is a server-managed audit trail — not client-writable.
                allowed_keys = {'status', 'employee_notes', 'checklist'}
                for key in request.data.keys():
                    if key not in allowed_keys:
                        raise PermissionDenied("Employees can only update task status, checklists, progress notes, and comments for tasks assigned to them by others.")
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        user = self.request.user
        instance = self.get_object()
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee" and instance.assigned_by != user:
            raise PermissionDenied("Employees cannot delete tasks assigned to them by others.")
        return super().destroy(request, *args, **kwargs)



class PerformanceViewSet(viewsets.ModelViewSet):
    queryset = Performance.objects.all()
    serializer_class = PerformanceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Performance.objects.all().order_by("-id")
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            return queryset.filter(employee__user=user)
        
        branches = get_allowed_branches(user, "performance")
        if "All" not in branches:
            queryset = queryset.filter(employee__branch__in=branches)
        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            raise PermissionDenied("Employees cannot write performance reviews.")
        serializer.save(reviewer=user)

    def update(self, request, *args, **kwargs):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            raise PermissionDenied("Employees cannot edit performance reviews.")
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            raise PermissionDenied("Employees cannot delete performance reviews.")
        return super().destroy(request, *args, **kwargs)


class AssetViewSet(viewsets.ModelViewSet):
    queryset = Asset.objects.all()
    serializer_class = AssetSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Asset.objects.all().order_by("-id")
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            return queryset.filter(assigned_to__user=user)
        
        branches = get_allowed_branches(user, "assets")
        if "All" not in branches:
            # Include unassigned assets (assigned_to is null) so scoped HR can
            # still see/manage the available equipment pool.
            queryset = queryset.filter(
                Q(assigned_to__branch__in=branches) | Q(assigned_to__isnull=True)
            )
        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            raise PermissionDenied("Employees cannot create assets.")
        serializer.save()

    def update(self, request, *args, **kwargs):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            raise PermissionDenied("Employees cannot edit assets.")
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            raise PermissionDenied("Employees cannot delete assets.")
        return super().destroy(request, *args, **kwargs)

