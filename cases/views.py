import math
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models import Max, Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Case, LocationPing
from .serializer import CaseSerializer, LocationPingSerializer, LiveEngineerSerializer
from employees.models import Employee
from authentication.models import get_allowed_branches

# An engineer is considered "live" if their last ping is within this window.
LIVE_WINDOW_MINUTES = 10
# Pings less accurate than this (meters) are ignored for distance / path drawing.
MAX_ACCURACY_METERS = 100


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in kilometers."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _role(user):
    return "superadmin" if user.is_superuser else getattr(user, "role", "employee")


def _is_staff_role(user):
    return _role(user) in ("superadmin", "admin", "hr")


def _get_employee(user):
    return getattr(user, "employee_profile", None)


class CaseViewSet(viewsets.ModelViewSet):
    """Case management + dispatch. Admin/HR create and assign cases; the assigned
    engineer sees only their own cases and drives the status forward in the field."""

    serializer_class = CaseSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Case.objects.select_related("assigned_to", "assigned_by").all()

        if _role(user) == "employee":
            employee = _get_employee(user)
            if not employee:
                return qs.none()
            return qs.filter(assigned_to=employee)

        # Staff: branch-scoped by the assigned engineer's branch. Unassigned
        # cases have no branch yet, so include them too — otherwise a branch
        # admin could never see (and assign) a freshly created open case.
        branches = get_allowed_branches(user, "attendance")
        if "All" not in branches:
            qs = qs.filter(Q(assigned_to__branch__in=branches) | Q(assigned_to__isnull=True))

        params = self.request.query_params
        status_param = params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        engineer_id = params.get("engineer")
        if engineer_id and engineer_id.isdigit():
            qs = qs.filter(assigned_to_id=int(engineer_id))
        return qs

    def create(self, request, *args, **kwargs):
        if not _is_staff_role(request.user):
            return Response({"detail": "Only admin/HR can create cases."}, status=403)
        # Idempotent dispatch: if a case with this external_ref already exists,
        # update it in place instead of creating a duplicate (OpenCall may
        # re-send the same ticket when a row is re-scheduled).
        ext = request.data.get("external_ref")
        if ext:
            existing = Case.objects.filter(external_ref=ext).first()
            if existing:
                serializer = self.get_serializer(existing, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(serializer.data, status=200)
        return super().create(request, *args, **kwargs)

    def _guard_staff(self, request):
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)
        return None

    def update(self, request, *args, **kwargs):
        denied = self._guard_staff(request)
        if denied:
            return denied
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        denied = self._guard_staff(request)
        if denied:
            return denied
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        denied = self._guard_staff(request)
        if denied:
            return denied
        return super().destroy(request, *args, **kwargs)

    @staticmethod
    def _resolve_engineer(data):
        """Look up an Employee by id, then email, then (case-insensitive) name."""
        engineer_id = data.get("engineer_id")
        if engineer_id:
            # A non-numeric engineer_id would raise ValueError on a pk lookup;
            # treat it as "not found" and fall through to email/name instead.
            try:
                emp = Employee.objects.filter(pk=engineer_id).first()
            except (ValueError, TypeError):
                emp = None
            if emp:
                return emp
        email = data.get("engineer_email")
        if email:
            emp = Employee.objects.filter(email__iexact=email).first()
            if emp:
                return emp
        name = data.get("engineer_name")
        if name:
            return Employee.objects.filter(employee_name__iexact=name).first()
        return None

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        """Admin/HR assigns (or reassigns) this case to an engineer.

        Accepts any one of engineer_id / engineer_email / engineer_name so an
        external system (OpenCall) that only knows the engineer by name/email
        can assign without knowing Payroll's internal employee id."""
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        case = self.get_object()
        engineer = self._resolve_engineer(request.data)
        if engineer is None:
            return Response(
                {"detail": "Engineer not found. Provide engineer_id, engineer_email or engineer_name."},
                status=404,
            )

        case.assigned_to = engineer
        case.assigned_by = request.user
        case.assigned_at = timezone.now()
        case.status = "assigned"
        case.save()
        return Response(self.get_serializer(case).data)

    def _engineer_transition(self, request, pk, allowed_from, new_status, stamp_field=None, extra=None):
        """Shared helper for the field-driven status transitions. Only the
        engineer the case is assigned to (or staff) may move it."""
        case = self.get_object()
        user = request.user

        if _role(user) == "employee":
            employee = _get_employee(user)
            if not employee or case.assigned_to_id != employee.id:
                return Response({"detail": "This case is not assigned to you."}, status=403)
        elif not _is_staff_role(user):
            return Response({"detail": "Permission denied."}, status=403)

        if allowed_from and case.status not in allowed_from:
            return Response(
                {"detail": f"Cannot move case from '{case.status}' to '{new_status}'."},
                status=400,
            )

        case.status = new_status
        if stamp_field:
            setattr(case, stamp_field, timezone.now())
        if extra:
            for k, v in extra.items():
                setattr(case, k, v)
        case.save()
        return Response(self.get_serializer(case).data)

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        return self._engineer_transition(request, pk, ["assigned"], "accepted")

    @action(detail=True, methods=["post"])
    def start_travel(self, request, pk=None):
        return self._engineer_transition(
            request, pk, ["assigned", "accepted"], "on_the_way", stamp_field="started_at"
        )

    @action(detail=True, methods=["post"])
    def reached(self, request, pk=None):
        return self._engineer_transition(
            request, pk, ["on_the_way"], "reached", stamp_field="reached_at"
        )

    @action(detail=True, methods=["post"])
    def start_work(self, request, pk=None):
        return self._engineer_transition(request, pk, ["reached", "on_the_way"], "working")

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        notes = request.data.get("resolution_notes", "")
        return self._engineer_transition(
            request,
            pk,
            ["working", "reached"],
            "completed",
            stamp_field="completed_at",
            extra={"resolution_notes": notes} if notes else None,
        )


class TrackingViewSet(viewsets.ViewSet):
    """Live GPS tracking of field engineers. Engineers POST their position to
    /ping while on duty; staff read /live (everyone's latest position) and
    /path (one engineer's or one case's full trail + distance travelled)."""

    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=["post"])
    def ping(self, request):
        employee = _get_employee(request.user)
        if not employee:
            return Response({"detail": "No employee profile linked to this user."}, status=400)

        lat = request.data.get("latitude")
        lon = request.data.get("longitude")
        if lat is None or lon is None:
            return Response({"detail": "latitude and longitude are required."}, status=400)

        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid coordinates."}, status=400)

        case = None
        case_id = request.data.get("case_id")
        if case_id:
            # Tolerate a bad case_id (non-numeric) — just record the ping with no
            # case rather than 500-ing on the pk lookup.
            try:
                case = Case.objects.filter(pk=case_id).first()
            except (ValueError, TypeError):
                case = None

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        ping = LocationPing.objects.create(
            engineer=employee,
            case=case,
            latitude=lat,
            longitude=lon,
            accuracy=_num(request.data.get("accuracy")),
            speed=_num(request.data.get("speed")),
            status=request.data.get("status", ""),
        )
        return Response(LocationPingSerializer(ping).data, status=201)

    @action(detail=False, methods=["get"])
    def live(self, request):
        """Latest position of every engineer active within LIVE_WINDOW_MINUTES."""
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        since = timezone.now() - timedelta(minutes=LIVE_WINDOW_MINUTES)
        # Latest ping id per engineer within the window. NOTE: .order_by() is
        # required — LocationPing has Meta.ordering = ["-timestamp"], which Django
        # would otherwise fold into the GROUP BY, breaking the per-engineer
        # aggregation and returning one row per ping instead of per engineer.
        latest_ids = (
            LocationPing.objects.filter(timestamp__gte=since)
            .order_by()
            .values("engineer")
            .annotate(last_id=Max("id"))
            .values_list("last_id", flat=True)
        )
        pings = LocationPing.objects.select_related("engineer", "case").filter(id__in=list(latest_ids))

        branches = get_allowed_branches(request.user, "attendance")
        rows = []
        for p in pings:
            if "All" not in branches and p.engineer.branch not in branches:
                continue
            rows.append(
                {
                    "engineer_id": p.engineer_id,
                    "engineer_name": p.engineer.employee_name,
                    "branch": p.engineer.branch,
                    "latitude": p.latitude,
                    "longitude": p.longitude,
                    "accuracy": p.accuracy,
                    "speed": p.speed,
                    "status": p.status,
                    "timestamp": p.timestamp,
                    "active_case_id": p.case_id,
                    "active_case_number": p.case.case_number if p.case else None,
                }
            )
        return Response(LiveEngineerSerializer(rows, many=True).data)

    @action(detail=False, methods=["get"])
    def path(self, request):
        """Ordered trail + total distance (km) for one engineer's day, or one case.
        Query: ?engineer=<id>&date=YYYY-MM-DD  OR  ?case=<id>"""
        user = request.user
        qs = LocationPing.objects.select_related("engineer").all()

        # Only accept numeric ids; a non-numeric value would 500 on the filter.
        raw_case = request.query_params.get("case")
        raw_engineer = request.query_params.get("engineer")
        case_id = raw_case if (raw_case and raw_case.isdigit()) else None
        engineer_id = raw_engineer if (raw_engineer and raw_engineer.isdigit()) else None

        if (raw_case or raw_engineer) and not (case_id or engineer_id):
            return Response({"detail": "case and engineer must be numeric ids."}, status=400)

        if case_id:
            qs = qs.filter(case_id=case_id)
        elif engineer_id:
            qs = qs.filter(engineer_id=engineer_id)
        else:
            # Engineers may fetch their own trail without extra params.
            employee = _get_employee(user)
            if employee:
                qs = qs.filter(engineer=employee)
            else:
                return Response({"detail": "engineer or case is required."}, status=400)

        # Non-staff can only ever see their own trail.
        if not _is_staff_role(user):
            employee = _get_employee(user)
            if not employee:
                return Response({"detail": "Permission denied."}, status=403)
            qs = qs.filter(engineer=employee)

        date_str = request.query_params.get("date")
        target_date = parse_date(date_str) if date_str else None
        if target_date:
            qs = qs.filter(timestamp__date=target_date)

        pings = list(qs.order_by("timestamp"))

        # Total distance, skipping low-accuracy noise so a stray jump doesn't
        # inflate the kilometers.
        total_km = 0.0
        clean = [
            p for p in pings
            if p.accuracy is None or p.accuracy <= MAX_ACCURACY_METERS
        ]
        for prev, cur in zip(clean, clean[1:]):
            total_km += haversine_km(prev.latitude, prev.longitude, cur.latitude, cur.longitude)

        return Response(
            {
                "count": len(pings),
                "total_km": round(total_km, 2),
                "points": LocationPingSerializer(pings, many=True).data,
            }
        )
