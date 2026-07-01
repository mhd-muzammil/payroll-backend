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
            if self.action == 'list':
                return queryset
            return queryset.filter(user=user)
        
        branches = get_allowed_branches(user, "employees")
        if "All" not in branches:
            queryset = queryset.filter(branch__in=branches)
        return queryset


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
                allowed_keys = {'status', 'employee_notes', 'checklist', 'activity_log'}
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
            queryset = queryset.filter(assigned_to__branch__in=branches)
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

