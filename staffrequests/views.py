from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from authentication.models import get_allowed_branches

from .models import EmployeeRequest, RequestMessage
from .serializers import EmployeeRequestSerializer, RequestMessageSerializer

STAFF_ROLES = ("superadmin", "admin", "hr")


def _role(user):
    return "superadmin" if user.is_superuser else getattr(user, "role", "employee")


def _is_staff_role(user):
    return _role(user) in STAFF_ROLES


def _employee(user):
    return getattr(user, "employee_profile", None)


class EmployeeRequestViewSet(viewsets.ModelViewSet):
    """Employees raise advance / report requests here; the office answers them.

    An employee sees only their own requests. Staff see their branches', with
    the same branch scoping the payroll sections use.
    """

    serializer_class = EmployeeRequestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = EmployeeRequest.objects.select_related("employee", "reviewed_by").prefetch_related(
            Prefetch("messages", queryset=RequestMessage.objects.select_related("sender"))
        )

        if _role(user) == "employee":
            employee = _employee(user)
            if not employee:
                return qs.none()
            return qs.filter(employee=employee)

        branches = get_allowed_branches(user, "payroll")
        if "All" not in branches:
            qs = qs.filter(employee__branch__in=branches)

        params = self.request.query_params
        status_param = params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        type_param = params.get("request_type")
        if type_param:
            qs = qs.filter(request_type=type_param)
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["viewer_is_employee"] = _role(self.request.user) == "employee"
        return context

    def perform_create(self, serializer):
        employee = _employee(self.request.user)
        if not employee:
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {"detail": "Your login is not linked to an employee record. Contact HR."}
            )
        serializer.save(employee=employee)

    def update(self, request, *args, **kwargs):
        return self._guard_edit(request, super().update, request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        return self._guard_edit(request, super().partial_update, request, *args, **kwargs)

    def _guard_edit(self, request, handler, *args, **kwargs):
        """An employee may fix their own request only while it is still Pending;
        once the office has decided, the record of what was decided stands."""
        instance = self.get_object()
        if _role(request.user) == "employee":
            if instance.employee != _employee(request.user):
                return Response({"detail": "Not your request."}, status=403)
            if instance.status != "Pending":
                return Response(
                    {"detail": f"This request was already {instance.status.lower()}."}, status=400
                )
        return handler(*args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if _role(request.user) == "employee":
            if instance.employee != _employee(request.user):
                return Response({"detail": "Not your request."}, status=403)
            if instance.status != "Pending":
                return Response(
                    {"detail": "You can only withdraw a request that is still pending."},
                    status=400,
                )
        return super().destroy(request, *args, **kwargs)

    # -- decisions ----------------------------------------------------------

    def _decide(self, request, new_status):
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        obj = self.get_object()
        if obj.status != "Pending":
            # Two people opening the queue must not both decide it.
            return Response(
                {"detail": f"This request was already {obj.status.lower()}."}, status=400
            )

        obj.status = new_status
        obj.reviewed_by = request.user
        obj.reviewed_at = timezone.now()
        obj.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

        # The decision lands in the thread so the employee sees the reason
        # alongside the outcome instead of a bare status flip.
        note = (request.data.get("note") or "").strip()
        RequestMessage.objects.create(
            request=obj,
            sender=request.user,
            from_employee=False,
            is_decision=True,
            body=note or f"{new_status}.",
            read_by_staff=True,
        )
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Records the decision ONLY. Payroll is untouched by design — HR still
        enters the deduction on the employee record, so approving here can never
        silently change someone's pay."""
        return self._decide(request, "Approved")

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._decide(request, "Rejected")

    # -- conversation -------------------------------------------------------

    @action(detail=True, methods=["get", "post"])
    def messages(self, request, pk=None):
        obj = self.get_object()
        viewer_is_employee = _role(request.user) == "employee"

        if viewer_is_employee and obj.employee != _employee(request.user):
            return Response({"detail": "Not your request."}, status=403)

        if request.method == "POST":
            body = (request.data.get("body") or "").strip()
            if not body:
                return Response({"detail": "Message cannot be empty."}, status=400)
            message = RequestMessage.objects.create(
                request=obj,
                sender=request.user,
                from_employee=viewer_is_employee,
                body=body,
                # Sending counts as having read your own side.
                read_by_employee=viewer_is_employee,
                read_by_staff=not viewer_is_employee,
            )
            return Response(RequestMessageSerializer(message).data, status=201)

        # Reading the thread clears the unread flag for the side reading it.
        field = "read_by_employee" if viewer_is_employee else "read_by_staff"
        obj.messages.filter(**{field: False}).update(**{field: True})
        return Response(RequestMessageSerializer(obj.messages.all(), many=True).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts for the badge on the nav item and the header tiles."""
        qs = self.get_queryset()
        viewer_is_employee = _role(request.user) == "employee"
        field = "read_by_employee" if viewer_is_employee else "read_by_staff"

        unread = RequestMessage.objects.filter(
            request__in=qs, from_employee=not viewer_is_employee, **{field: False}
        ).count()

        return Response(
            {
                "total": qs.count(),
                "pending": qs.filter(status="Pending").count(),
                "approved": qs.filter(status="Approved").count(),
                "rejected": qs.filter(status="Rejected").count(),
                "unread_messages": unread,
            }
        )
