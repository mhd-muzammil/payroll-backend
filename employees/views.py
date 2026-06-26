from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from .models import Employee, Task, Performance
from .serializer import EmployeeSerializer
from .task_serializer import TaskSerializer
from .performance_serializer import PerformanceSerializer

class EmployeeViewSet(viewsets.ModelViewSet):
    queryset = Employee.objects.all()
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Employee.objects.all().order_by("-id")
        role = "superadmin" if user.is_superuser else user.role
        if role == "employee":
            return queryset.filter(user=user)
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
            return queryset.filter(assigned_to__user=user)
        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            raise PermissionDenied("Employees cannot assign tasks.")
        serializer.save(assigned_by=user)

    def update(self, request, *args, **kwargs):
        user = self.request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            allowed_keys = {'status', 'employee_notes', 'checklist', 'activity_log'}
            for key in request.data.keys():
                if key not in allowed_keys:
                    raise PermissionDenied("Employees can only update task status, checklists, progress notes, and comments.")
        return super().update(request, *args, **kwargs)


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

