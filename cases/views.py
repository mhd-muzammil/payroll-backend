import logging
import math
import re
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models import Max, Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Case, DutySession, EngineerAlias, LocationPing
from .serializer import CaseSerializer, LocationPingSerializer, LiveEngineerSerializer
from employees.models import Employee
from authentication.models import get_allowed_branches

logger = logging.getLogger(__name__)

# Statuses that still need the engineer's attention — what their case list shows.
ACTIVE_STATUSES = ["open", "assigned", "accepted", "on_the_way", "reached", "working"]

# Once the engineer has moved a case past "assigned" in the field, THEY own its
# status. An incoming sync that merely repeats "still assigned upstream" must
# not drag them back to the start (or resurrect work they already finished).
ENGINEER_OWNED_STATUSES = ("accepted", "on_the_way", "reached", "working", "completed")

# An engineer is considered "live" if their last ping is within this window.
LIVE_WINDOW_MINUTES = 10
# Pings less accurate than this (meters) are ignored for distance / path drawing.
MAX_ACCURACY_METERS = 100

# A stop is the engineer staying inside this circle for at least this long. The
# radius has to comfortably exceed ordinary GPS wander while parked (a phone
# indoors drifts tens of metres), and the duration has to exceed a traffic light
# or a slow junction — otherwise every red signal on the ride would read as a
# visit to a customer.
STOP_RADIUS_METERS = 120
STOP_MIN_MINUTES = 8


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


class NoEmployeeProfile(APIException):
    """The user logs in fine but no Employee record is linked to their account.

    Reported explicitly instead of returning an empty list: an engineer handed a
    login that was never linked (or whose employee row was deleted) would
    otherwise see the exact same "No cases assigned to you." as a genuinely
    quiet day, and nobody could tell the two apart. TrackingViewSet.ping already
    reports this condition; the case list now does too.
    """

    status_code = 409
    default_detail = "Your login is not linked to an employee record. Contact HR."
    default_code = "no_employee_profile"


def _trail_km(pings):
    """Total distance along an ordered trail, in km.

    Low-accuracy fixes are dropped first so one stray jump across town does not
    inflate the day's kilometres. Shared by /live and /path so the number an
    engineer's row shows is computed exactly the same way as their detail view.
    """
    clean = [p for p in pings if p.accuracy is None or p.accuracy <= MAX_ACCURACY_METERS]
    total = 0.0
    for prev, cur in zip(clean, clean[1:]):
        total += haversine_km(prev.latitude, prev.longitude, cur.latitude, cur.longitude)
    return round(total, 2)


def _usable_pings(pings):
    """Ordered pings with the low-accuracy noise dropped.

    Everything that reasons about where somebody WAS uses this, so a stray fix
    from a cold GPS cannot invent a journey or a visit.
    """
    return [p for p in pings if p.accuracy is None or p.accuracy <= MAX_ACCURACY_METERS]


def _detect_stops(pings):
    """Where the engineer stood still, and for how long.

    Walks the day's fixes and grows a cluster while they stay within
    STOP_RADIUS_METERS of where the cluster began. A cluster that lasted at
    least STOP_MIN_MINUTES is a stop — a customer visit, a parts pickup, lunch.
    Anything shorter is traffic, so junctions and signals do not show up as
    visits.

    Distance is measured from the cluster's ANCHOR rather than from the previous
    fix: comparing neighbours would let a slow walk drift across a whole street
    while every individual step stayed under the radius.

    Returns dicts, not model objects — nothing is written to the database. The
    stops are derived from the trail every time it is read, so changing the
    thresholds re-reads history correctly instead of leaving stale rows behind.
    """
    clean = _usable_pings(pings)
    stops = []
    if not clean:
        return stops

    def close_run(cluster):
        minutes = (cluster[-1].timestamp - cluster[0].timestamp).total_seconds() / 60
        if minutes < STOP_MIN_MINUTES:
            return
        # Centre of the cluster, so the marker sits where they actually were
        # rather than on whichever fix happened to arrive first.
        stops.append(
            {
                "latitude": sum(p.latitude for p in cluster) / len(cluster),
                "longitude": sum(p.longitude for p in cluster) / len(cluster),
                "arrived_at": cluster[0].timestamp,
                "left_at": cluster[-1].timestamp,
                "minutes": int(minutes),
                "fixes": len(cluster),
                # The case they were attending, if the app was tagging pings.
                "case_id": next((p.case_id for p in reversed(cluster) if p.case_id), None),
                "case_number": next(
                    (p.case.case_number for p in reversed(cluster) if p.case_id and p.case), None
                ),
            }
        )

    cluster = [clean[0]]
    for ping in clean[1:]:
        anchor = cluster[0]
        metres = haversine_km(anchor.latitude, anchor.longitude, ping.latitude, ping.longitude) * 1000
        if metres <= STOP_RADIUS_METERS:
            cluster.append(ping)
        else:
            close_run(cluster)
            cluster = [ping]
    close_run(cluster)
    return stops


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
                raise NoEmployeeProfile()
            # EXACTLY the engineer's Assigned column, one case per ticket.
            #
            # Driven by in_current_plan, not by status: filtering on status made
            # the count drift from what OpenCall shows — a call the engineer had
            # completed dropped out of their list while OpenCall still counted it
            # as assigned, so 5 upstream showed as 4 here. A ticket leaves this
            # list only when it leaves the plan, which the mirror pass records.
            return qs.filter(assigned_to=employee, in_current_plan=True).exclude(
                status="cancelled"
            )

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
                # Enforce branch scope on the matched case so a branch-scoped
                # admin can't reach across branches via a known external_ref.
                branches = get_allowed_branches(request.user, "attendance")
                if (
                    "All" not in branches
                    and existing.assigned_to
                    and existing.assigned_to.branch not in branches
                ):
                    return Response({"detail": "Not allowed to modify this case."}, status=403)
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
        """Look up an Employee by id, then email, then phone, then name.

        Email and phone are the reliable keys (both unique on the Employee),
        so an engineer whose email OR mobile number matches the OpenCall record
        links correctly even if the name is spelled differently. Phone is
        compared on the last 10 digits so "+91 98765 43210" matches "9876543210".
        """
        engineer_id = data.get("engineer_id")
        if engineer_id:
            # A non-numeric engineer_id would raise ValueError on a pk lookup;
            # treat it as "not found" and fall through to email/phone/name.
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
        phone = data.get("engineer_phone")
        if phone:
            digits = re.sub(r"\D", "", str(phone))
            if len(digits) >= 10:
                target = digits[-10:]
                # Compare on digits-only, last 10, on BOTH sides — stored phones
                # carry spaces/"+91" so a raw SQL endswith would miss. Employee
                # tables are small, so a scan of phone-bearing rows is fine.
                matches = [
                    emp for emp in Employee.objects.exclude(phone__isnull=True).exclude(phone="")
                    if re.sub(r"\D", "", emp.phone)[-10:] == target
                ]
                if len(matches) == 1:
                    return matches[0]
                # 0 or ambiguous (placeholder phones collide) -> fall through to name
        name = data.get("engineer_name")
        if name:
            nm = name.strip()
            # An explicit alias always wins: it is the operator stating outright
            # who this name is, which is the only safe way to resolve a name the
            # automatic rules refuse (a namesake, or a spelling with nothing in
            # common like "Lava" -> "LAVAKUMAR").
            alias = EngineerAlias.objects.filter(
                external_name=nm.lower()
            ).select_related("employee").first()
            if alias:
                return alias.employee
            # Name matching only ever considers employees who HAVE a login. A
            # case pinned to a login-less row is readable by nobody, yet the
            # dispatch reports success — so the ticket looks delivered and the
            # real engineer sees nothing. Duplicate/stale rows for the same
            # person are common (e.g. an old "Vijaya kumar (ARK)" alongside the
            # live "VIJAYAKUMAR (ark)"), and dropping the login-less ones also
            # disambiguates several of those pairs down to a single match.
            # email/phone are unique and explicit, so those branches stay open.
            reachable = Employee.objects.exclude(user__isnull=True)
            # employee_name is NOT unique; refuse to guess between namesakes.
            matches = list(reachable.filter(employee_name__iexact=nm)[:2])
            if len(matches) == 1:
                return matches[0]
            if not matches and nm:
                # Tolerate a trailing surname/initial the other system omits
                # ("Praveen" in OpenCall vs "Praveen S" in Payroll) — but ONLY
                # when it resolves to exactly ONE employee, so real namesakes
                # (e.g. several "Vijayakumar") are never guessed.
                prefix = list(reachable.filter(employee_name__istartswith=nm + " ")[:2])
                if len(prefix) == 1:
                    return prefix[0]
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

    # Normalised external status hint -> Payroll status. Lets a backfill mark a
    # ticket that is already finished in the originating system (OpenCall) as
    # completed/cancelled in Payroll, so historical calls don't clutter the
    # engineer's ACTIVE list while still appearing in their case history.
    _EXTERNAL_STATUS_MAP = {
        "completed": "completed",
        "closed": "completed",
        "done": "completed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "assigned": "assigned",
        "active": "assigned",
        "open": "assigned",
        "scheduled": "assigned",
    }

    @action(detail=False, methods=["post"])
    def bulk_dispatch(self, request):
        """Dispatch MANY cases in one call (OpenCall "Sync to Payroll" backfill).

        Body: {"cases": [ { external_ref, customer_name, customer_phone, title,
        description, address, priority, status?, engineer_id?/engineer_email?/
        engineer_phone?/engineer_name? }, ... ] }

        Each item is idempotent on external_ref (re-syncing updates the one case,
        never duplicates), resolves the engineer the same way /assign does, and
        optionally maps an external status so already-finished calls land as
        completed. An item whose engineer can't be matched is saved UNASSIGNED
        and reported in `skipped` rather than failing the whole batch."""
        if not _is_staff_role(request.user):
            return Response({"detail": "Only admin/HR can dispatch cases."}, status=403)

        items = request.data.get("cases")
        if not isinstance(items, list):
            return Response({"detail": 'Body must be {"cases": [ ... ]}.'}, status=400)

        # Mirror mode (default on): after upserting, any OTHER previously-synced
        # case whose ticket was NOT successfully assigned in THIS call is marked
        # CANCELLED (NOT deleted) so each engineer's list mirrors exactly the
        # current "Assigned" set. We track the refs actually ASSIGNED (not merely
        # received) so a ticket that moved to an engineer we can't match in
        # Payroll doesn't linger on its old engineer. Send {"mirror": false} to
        # disable. Non-destructive — cancelled cases stay in the DB, just hidden.
        mirror = request.data.get("mirror", True)
        assigned_refs = set()

        valid_priorities = dict(Case.PRIORITY_CHOICES)
        created = updated = assigned = skipped = 0
        details = []
        # Engineer names the caller sent that no Payroll employee answers to, and
        # matched employees who have no login. Both mean "these tickets reach
        # nobody" — reported back (and logged) so whoever runs the sync can SEE
        # which people need onboarding instead of guessing at an empty list.
        unmatched_engineers = set()
        unreachable_engineers = set()

        for raw in items:
            if not isinstance(raw, dict):
                skipped += 1
                details.append({"external_ref": None, "result": "skipped", "reason": "not an object"})
                continue

            ext = (raw.get("external_ref") or "").strip()
            existing = Case.objects.filter(external_ref=ext).first() if ext else None
            case = existing or Case()

            case.external_ref = ext
            case.customer_name = (raw.get("customer_name") or case.customer_name or "Unknown")[:150]
            case.customer_phone = (raw.get("customer_phone") or case.customer_phone or "")[:20]
            case.title = (raw.get("title") or case.title or "Service call")[:200]
            if raw.get("description") is not None:
                case.description = raw.get("description") or ""
            if raw.get("address") is not None:
                case.address = raw.get("address") or ""
            pr = raw.get("priority")
            if pr in valid_priorities:
                case.priority = pr
            # Ticket detail for the engineer's screen: customer, contact,
            # product, address. Replaced wholesale rather than merged — the
            # originating system is the authority, and a merge would keep a
            # value it has since cleared. Non-dict payloads are ignored so a bad
            # item cannot poison the record.
            incoming_details = raw.get("details")
            if isinstance(incoming_details, dict):
                case.details = {
                    str(k): ("" if v is None else str(v))
                    for k, v in incoming_details.items()
                }

            engineer = self._resolve_engineer(raw)
            if engineer is None:
                # Do NOT create an orphan case for an engineer we can't match —
                # an unassigned case just clutters Payroll and shows to nobody.
                # Skip it; a later sync picks it up once that engineer exists in
                # Payroll (e.g. after onboarding).
                who = (raw.get("engineer_name") or "").strip()
                if who:
                    unmatched_engineers.add(who)
                skipped += 1
                details.append({
                    "external_ref": ext,
                    "result": "skipped",
                    "reason": "engineer not matched",
                    "engineer_name": who or None,
                })
                continue

            if engineer.user_id is None:
                # Matched by email/phone onto a row with no login. The case is
                # still saved (the data is real), but flag it — nobody can open it.
                unreachable_engineers.add(engineer.employee_name)

            is_new = case.pk is None
            reassigned = case.assigned_to_id not in (None, engineer.id)
            # Pushed in this batch, so it IS in the plan — including a ticket
            # coming back after having dropped out.
            case.in_current_plan = True
            case.assigned_to = engineer
            case.assigned_by = request.user
            # Stamp on first assignment, and re-stamp when the ticket actually
            # moves to a different engineer — for the new engineer it IS a fresh
            # assignment. An unchanged engineer keeps the original timestamp.
            if case.assigned_at is None or reassigned:
                case.assigned_at = timezone.now()

            desired = self._EXTERNAL_STATUS_MAP.get((raw.get("status") or "").strip().lower())
            if desired in ("completed", "cancelled"):
                # Terminal upstream: the originating system says this call is
                # finished, which always wins over the field status.
                case.status = desired
                if desired == "completed" and case.completed_at is None:
                    case.completed_at = timezone.now()
            elif reassigned:
                # New engineer — restart their side of the lifecycle, whatever
                # the previous engineer had already done.
                case.status = desired or "assigned"
            elif case.status in ENGINEER_OWNED_STATUSES:
                # Sync runs every few minutes and keeps repeating "assigned";
                # leave an engineer who is already on the way / working / done
                # exactly where they are.
                pass
            elif desired:
                case.status = desired
            elif case.status == "open":
                case.status = "assigned"

            case.save()
            if ext:
                assigned_refs.add(ext)
            assigned += 1
            if is_new:
                created += 1
            else:
                updated += 1
            details.append({
                "external_ref": ext,
                "result": "assigned",
                "case_number": case.case_number,
                "engineer": engineer.employee_name,
                "status": case.status,
            })

        cancelled = 0
        # Mirror: a synced case whose ticket was NOT assigned in THIS call is
        # stale — mark it cancelled (kept in the DB, just hidden from the
        # engineer's active list). Keyed on assigned_refs (tickets actually
        # assigned now), so a ticket that moved to an unmatched engineer stops
        # lingering on its old one. NON-destructive. Only synced cases
        # (external_ref set) are ever touched, never manually-created ones.
        # Guarded: full-access (All-branch) caller + at least one assignment, so
        # an empty/failed sync never cancels anything.
        if mirror and assigned_refs and "All" in get_allowed_branches(request.user, "attendance"):
            # Out of the plan: drop the flag on EVERY synced case that is no
            # longer pushed, completed ones included — that is what keeps the
            # engineer's count equal to the Assigned column. The status is left
            # alone here, so a completed call keeps saying completed.
            left_plan = (
                Case.objects.exclude(external_ref="")
                .exclude(external_ref__in=assigned_refs)
                .filter(in_current_plan=True)
            )
            cancelled_refs = sorted(left_plan.values_list("external_ref", flat=True))
            cancelled = len(cancelled_refs)
            left_plan.update(in_current_plan=False)

            # A call that never reached a terminal state and has left the plan is
            # cancelled, as before — a real outcome (completed) is never rewritten.
            Case.objects.exclude(external_ref="").filter(
                in_current_plan=False, status__in=ACTIVE_STATUSES
            ).update(status="cancelled")

        # ASCII only: these lines get read in a Docker/Dokploy log tail.
        if unmatched_engineers:
            logger.warning(
                "bulk_dispatch: skipped tickets, no Payroll employee matches: %s",
                ", ".join(sorted(unmatched_engineers)),
            )
        if unreachable_engineers:
            logger.warning(
                "bulk_dispatch: assigned cases nobody can open (employee has no login): %s",
                ", ".join(sorted(unreachable_engineers)),
            )

        return Response({
            "created": created,
            "updated": updated,
            "assigned": assigned,
            "skipped": skipped,
            "cancelled": cancelled,
            "total": len(items),
            # The two lists that explain a short sync at a glance.
            "unmatched_engineers": sorted(unmatched_engineers),
            "unreachable_engineers": sorted(unreachable_engineers),
            "details": details,
        })

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
    """Live GPS tracking of field engineers. Engineers declare duty via
    /start_duty and /end_duty and POST their position to /ping while on it;
    staff read /live (everyone on duty, with their last known position) and
    /path (one engineer's or one case's full trail + distance travelled)."""

    permission_classes = [IsAuthenticated]

    # -- duty state ---------------------------------------------------------

    @staticmethod
    def _close_forgotten_sessions():
        """Auto-close sessions nobody ever stopped, so a missed Stop Duty does
        not leave someone 'on duty' for days. Cheap and idempotent; run before
        any read or write of duty state."""
        cutoff = timezone.now() - timedelta(hours=DutySession.MAX_DURATION_HOURS)
        DutySession.objects.filter(ended_at__isnull=True, started_at__lt=cutoff).update(
            ended_at=timezone.now(), auto_closed=True
        )

    @classmethod
    def _open_session(cls, employee):
        cls._close_forgotten_sessions()
        return DutySession.objects.filter(engineer=employee, ended_at__isnull=True).first()

    def _duty_payload(self, employee):
        session = self._open_session(employee)
        return {
            "on_duty": session is not None,
            "session_id": session.id if session else None,
            "started_at": session.started_at if session else None,
            "duration_minutes": session.duration_minutes() if session else 0,
        }

    @action(detail=False, methods=["get"])
    def duty(self, request):
        """The caller's own duty state. The engineer's app reads this on load so
        a refresh (or a reopened tab) resumes tracking instead of silently
        going off duty while the engineer believes they are on."""
        employee = _get_employee(request.user)
        if not employee:
            raise NoEmployeeProfile()
        return Response(self._duty_payload(employee))

    @action(detail=False, methods=["post"], url_path="start_duty")
    def start_duty(self, request):
        employee = _get_employee(request.user)
        if not employee:
            raise NoEmployeeProfile()
        # Idempotent: tapping Start twice (or a reconnect) must not open a second
        # overlapping session and double-count the day.
        if not self._open_session(employee):
            DutySession.objects.create(engineer=employee)
        return Response(self._duty_payload(employee), status=201)

    @action(detail=False, methods=["post"], url_path="end_duty")
    def end_duty(self, request):
        employee = _get_employee(request.user)
        if not employee:
            raise NoEmployeeProfile()
        session = self._open_session(employee)
        if session:
            session.ended_at = timezone.now()
            session.save(update_fields=["ended_at"])
        # Always report the resulting state, so "stop when already stopped" is a
        # success rather than an error the app has to special-case.
        return Response(self._duty_payload(employee))

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
        """Everyone currently ON DUTY, with their last known position and the
        distance they have covered on this duty.

        Membership of this list is the engineer's DECLARED duty, not their GPS.
        A locked phone or a dead signal stops the pings but does not end the
        duty, so such an engineer stays here with stale=True and a growing
        last_seen_minutes — "on duty, no signal for 15m" — instead of vanishing
        as though they had gone home. An engineer who is on duty but has not
        sent a single fix yet has latitude/longitude of None; the map skips
        them, the table still lists them.
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        self._close_forgotten_sessions()
        sessions = (
            DutySession.objects.filter(ended_at__isnull=True)
            .select_related("engineer")
            .order_by("engineer__employee_name")
        )

        branches = get_allowed_branches(request.user, "attendance")
        if "All" not in branches:
            sessions = sessions.filter(engineer__branch__in=branches)
        sessions = list(sessions)
        if not sessions:
            return Response([])

        engineer_ids = [s.engineer_id for s in sessions]

        # Latest ping per on-duty engineer. NOTE: .order_by() is required —
        # LocationPing has Meta.ordering = ["-timestamp"], which Django would
        # otherwise fold into the GROUP BY, breaking the per-engineer
        # aggregation and returning one row per ping instead of per engineer.
        latest_ids = (
            LocationPing.objects.filter(engineer_id__in=engineer_ids)
            .order_by()
            .values("engineer")
            .annotate(last_id=Max("id"))
            .values_list("last_id", flat=True)
        )
        latest_by_engineer = {
            p.engineer_id: p
            for p in LocationPing.objects.select_related("case").filter(id__in=list(latest_ids))
        }

        # Distance covered since each duty started, from that session's own
        # pings — so the number resets with the duty instead of carrying over
        # yesterday's, and matches what "this shift" means to the operator.
        now = timezone.now()
        rows = []
        for session in sessions:
            ping = latest_by_engineer.get(session.engineer_id)
            trail = LocationPing.objects.filter(
                engineer_id=session.engineer_id, timestamp__gte=session.started_at
            ).order_by("timestamp")
            last_seen_minutes = (
                int((now - ping.timestamp).total_seconds() // 60) if ping else None
            )
            rows.append(
                {
                    "engineer_id": session.engineer_id,
                    "engineer_name": session.engineer.employee_name,
                    "branch": session.engineer.branch,
                    "on_duty": True,
                    "duty_started_at": session.started_at,
                    "duty_minutes": session.duration_minutes(),
                    # No fix yet, or the last one is older than the live window:
                    # still on duty, just not currently reporting.
                    "stale": ping is None or (now - ping.timestamp) > timedelta(minutes=LIVE_WINDOW_MINUTES),
                    "last_seen_minutes": last_seen_minutes,
                    "distance_km": _trail_km(trail),
                    "latitude": ping.latitude if ping else None,
                    "longitude": ping.longitude if ping else None,
                    "accuracy": ping.accuracy if ping else None,
                    "speed": ping.speed if ping else None,
                    "status": ping.status if ping else "",
                    "timestamp": ping.timestamp if ping else None,
                    "active_case_id": ping.case_id if ping else None,
                    "active_case_number": ping.case.case_number if (ping and ping.case) else None,
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
        if date_str:
            # parse_date raises ValueError on a format-valid but impossible date
            # (e.g. 2026-13-01); treat that as a 400 rather than a 500.
            try:
                target_date = parse_date(date_str)
            except ValueError:
                return Response({"detail": "Invalid date."}, status=400)
            if target_date:
                qs = qs.filter(timestamp__date=target_date)

        pings = list(qs.order_by("timestamp"))

        return Response(
            {
                "count": len(pings),
                "total_km": _trail_km(pings),
                "points": LocationPingSerializer(pings, many=True).data,
            }
        )

    @action(detail=False, methods=["get"])
    def day(self, request):
        """One engineer's whole day: the route, the distance, the time on duty,
        where they stood still and for how long, and a timeline of what happened.

        Query: ?engineer=<id>&date=YYYY-MM-DD (date defaults to today)

        This is the "what did they actually do" view. Everything is derived from
        the trail and the duty sessions on read, so nothing here can go stale.
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        raw_engineer = request.query_params.get("engineer")
        if not (raw_engineer and raw_engineer.isdigit()):
            return Response({"detail": "engineer must be a numeric id."}, status=400)
        engineer = Employee.objects.filter(pk=int(raw_engineer)).first()
        if not engineer:
            return Response({"detail": "Engineer not found."}, status=404)

        branches = get_allowed_branches(request.user, "attendance")
        if "All" not in branches and engineer.branch not in branches:
            return Response({"detail": "Permission denied."}, status=403)

        date_str = request.query_params.get("date")
        if date_str:
            try:
                target_date = parse_date(date_str)
            except ValueError:
                target_date = None
            if not target_date:
                return Response({"detail": "date must be YYYY-MM-DD."}, status=400)
        else:
            target_date = timezone.localdate()

        pings = list(
            LocationPing.objects.select_related("case")
            .filter(engineer=engineer, timestamp__date=target_date)
            .order_by("timestamp")
        )
        sessions = list(
            DutySession.objects.filter(engineer=engineer, started_at__date=target_date).order_by(
                "started_at"
            )
        )
        stops = _detect_stops(pings)

        # A timeline the office can read top to bottom, the way the day happened.
        events = []
        for session in sessions:
            events.append(
                {"at": session.started_at, "type": "duty_start", "label": "Started duty"}
            )
            if session.ended_at:
                events.append(
                    {
                        "at": session.ended_at,
                        "type": "duty_end",
                        "label": "Auto-closed (no Stop Duty)"
                        if session.auto_closed
                        else "Stopped duty",
                        "minutes": session.duration_minutes(),
                    }
                )
        for stop in stops:
            events.append(
                {
                    "at": stop["arrived_at"],
                    "type": "stop",
                    "label": f"Stopped {stop['minutes']} min",
                    "minutes": stop["minutes"],
                    "latitude": stop["latitude"],
                    "longitude": stop["longitude"],
                    "case_number": stop["case_number"],
                }
            )
        # What the engineer did to their cases that day, so a stop can be read
        # against the job it belongs to.
        case_moments = (
            ("assigned_at", "Case assigned"),
            ("started_at", "Left for the call"),
            ("reached_at", "Reached the site"),
            ("completed_at", "Completed the call"),
        )
        for case in Case.objects.filter(assigned_to=engineer).only(
            "case_number", "title", "assigned_at", "started_at", "reached_at", "completed_at"
        ):
            for field, label in case_moments:
                moment = getattr(case, field)
                if moment and timezone.localtime(moment).date() == target_date:
                    events.append(
                        {
                            "at": moment,
                            "type": field.replace("_at", ""),
                            "label": label,
                            "case_number": case.case_number,
                        }
                    )
        events.sort(key=lambda e: e["at"])

        clean = _usable_pings(pings)
        return Response(
            {
                "engineer_id": engineer.id,
                "engineer_name": engineer.employee_name,
                "branch": engineer.branch,
                "date": target_date,
                "total_km": _trail_km(pings),
                "duty_minutes": sum(s.duration_minutes() for s in sessions),
                "first_seen": clean[0].timestamp if clean else None,
                "last_seen": clean[-1].timestamp if clean else None,
                "stop_count": len(stops),
                "stops": stops,
                "events": events,
                # The route, thinned of noise so the line drawn matches the km.
                "points": [
                    {
                        "latitude": p.latitude,
                        "longitude": p.longitude,
                        "timestamp": p.timestamp,
                        "accuracy": p.accuracy,
                        "status": p.status,
                    }
                    for p in clean
                ],
            }
        )
