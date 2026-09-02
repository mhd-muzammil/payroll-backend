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

from .models import Case, DutySession, EngineerAlias, EngineerScorecard, LocationPing
from .serializer import CaseSerializer, LocationPingSerializer, LiveEngineerSerializer
from .pings import MAX_BATCH, PingRejected, build_ping, coerce_number, ingest_batch
from .tracks import snapped_trail
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
# A fix that reached us this much later than it was taken spent time queued on
# the phone: the phone was offline and has since caught up. Two minutes is
# comfortably past the 30-second cadence plus ordinary latency, so it does not
# fire for a fix that was merely slow.
QUEUED_THRESHOLD_MINUTES = 2

STOP_RADIUS_METERS = 120
STOP_MIN_MINUTES = 8

# The shortest gap between two fixes that counts as having gone somewhere.
#
# A parked phone does not report the same position twice: it wanders a few metres
# every ping. Summing every gap therefore turned standing still into distance —
# four minutes at one spot came out as 0.05 km, and a two-hour customer visit
# would have read as 1.4 km travelled without the engineer moving at all.
#
# 20 m over a 30-second ping is 2.4 km/h, slower than walking, so nothing an
# engineer actually does falls under it. The floor is raised by the two fixes'
# own reported accuracy, because a pair of +/-15 m fixes can differ by 30 m
# while sitting in one place.
#
# The trade: a genuine crawl of under 20 m per ping — a long traffic jam — is not
# counted. Under-reporting a jam is a smaller lie than inventing a kilometre of
# travel for someone who never left the building.
MIN_STEP_METERS = 20.0


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


def _moving_trail(pings):
    """The trail with GPS wander taken out: every point is a real move from the
    one before it.

    Walks forward keeping a point only when it is far enough from the last KEPT
    point — not from its immediate predecessor, which would let a parked phone
    creep across a car park in 6 m steps that each looked like nothing.

    Used for distance and for the road-snapped line. NOT used for the points
    list itself: stop detection needs the stationary fixes, since a cluster of
    them sitting in one place is exactly what a stop IS.
    """
    clean = _usable_pings(pings)
    if not clean:
        return []

    kept = [clean[0]]
    for ping in clean[1:]:
        last = kept[-1]
        metres = haversine_km(last.latitude, last.longitude, ping.latitude, ping.longitude) * 1000
        floor = max(MIN_STEP_METERS, (last.accuracy or 0) + (ping.accuracy or 0))
        if metres >= floor:
            kept.append(ping)
    return kept


def _trail_km(pings):
    """Total distance along an ordered trail, in km.

    Three things are deliberately NOT counted.

    Low-accuracy fixes are dropped, and so is wander: standing still is 0.00,
    not the sum of however many metres the GPS drifted while nobody moved.

    And travel nobody measured. A fix carrying `after_gap` is the first one
    after the phone stopped tracking -- its location switched off, its
    permission withdrawn -- so whatever happened between the fix before it and
    this one was never seen. Joining them would charge a straight line across
    it: shorter than the road they drove, invented either way. That one segment
    is skipped; everything on both sides of it still counts in full.

    Deliberately NOT inferred from the time gap. An engineer standing still
    produces no new rows either, so a 40-minute hole is either 40 minutes of
    untracked driving or 40 minutes of work at one customer, and guessing threw
    away the journey after every long stop. Only the phone knows, so the phone
    says.

    Shared by /live, /path and /day so the number on an engineer's row is
    computed exactly the same way as the one in their detail view.
    """
    # The times tracking came back, taken from ALL the fixes rather than from
    # the ones that survive filtering. The first fix after the GPS wakes up is
    # routinely the noisiest of the day, so it is exactly the one the accuracy
    # filter drops -- and reading the flag only off the kept points would lose
    # it there and quietly count the straight line anyway.
    resumed_at = [
        ping.timestamp for ping in pings if getattr(ping, "after_gap", False)
    ]

    moving = _moving_trail(pings)
    total = 0.0
    for prev, cur in zip(moving, moving[1:]):
        if any(prev.timestamp < when <= cur.timestamp for when in resumed_at):
            continue
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


def _punches_for_day(engineer, day):
    """Every call this engineer punched in or out of on `day`, with where.

    Read from the cases rather than derived from the trail: these are the
    moments the engineer themselves marked, and the coordinates are the ones
    their phone reported at the instant they pressed the button.

    A punch whose position was never captured — no fix at the time — is still
    returned, so the timeline is complete; it simply cannot be drawn.
    """
    cases = Case.objects.filter(assigned_to=engineer).only(
        "case_number", "title", "reached_at", "completed_at",
        "punch_in_lat", "punch_in_lon", "punch_in_accuracy",
        "punch_out_lat", "punch_out_lon", "punch_out_accuracy",
    )

    out = []
    for case in cases:
        for kind, at, lat, lon, accuracy in (
            ("in", case.reached_at, case.punch_in_lat, case.punch_in_lon, case.punch_in_accuracy),
            ("out", case.completed_at, case.punch_out_lat, case.punch_out_lon, case.punch_out_accuracy),
        ):
            if not at or timezone.localtime(at).date() != day:
                continue
            out.append(
                {
                    "kind": kind,
                    "at": at,
                    "case_id": case.id,
                    "case_number": case.case_number,
                    "title": case.title,
                    "latitude": lat,
                    "longitude": lon,
                    "accuracy": accuracy,
                }
            )
    out.sort(key=lambda p: p["at"])
    return out


def _role(user):
    return "superadmin" if user.is_superuser else getattr(user, "role", "employee")


def _is_staff_role(user):
    return _role(user) in ("superadmin", "admin", "hr")


def _queued_minutes(ping):
    """How long this fix waited on the phone before it could be sent.

    None when we cannot tell — a row from before received_at existed, or no fix
    at all. 0 for one that arrived when it was taken, the ordinary case. The two
    mean different things and the row should not claim to know what it does not.
    """
    if ping is None or ping.received_at is None:
        return None
    delay = (ping.received_at - ping.timestamp).total_seconds() / 60
    return 0 if delay < QUEUED_THRESHOLD_MINUTES else int(delay)


# What the phone had left, and whether it had been offline. Together these make
# "no signal" answerable: a last fix at 4% says the phone died, one at 80% says
# the signal went, and a non-zero queued_minutes says it has since caught up.
def _phone_state(ping):
    return {
        "battery_level": ping.battery_level if ping else None,
        "is_charging": ping.is_charging if ping else None,
        "queued_minutes": _queued_minutes(ping),
    }


def _case_for(case_id):
    """The case a fix is tagged with, or None.

    A non-numeric id is tolerated rather than fatal: losing which case a fix
    belonged to is a small loss, losing the fix itself is not.
    """
    if not case_id:
        return None
    try:
        return Case.objects.filter(pk=case_id).first()
    except (ValueError, TypeError):
        return None


def _as_count(value):
    """A non-negative whole number, or 0. Counts arrive over HTTP as anything."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _get_employee(user):
    return getattr(user, "employee_profile", None)


# Shared by case dispatch AND the tracking roster, so an engineer who can be
# sent a case can always be tracked, and vice versa. When the two had separate
# matching, a name only the alias table could resolve came back unmatched on the
# roster and that engineer's duty state was silently lost.
def resolve_engineer(data):
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
            # EXACTLY the engineer's Assigned column for TODAY, one case per ticket.
            #
            # Driven by the plan, not by status: filtering on status made the count
            # drift from what OpenCall shows — a call the engineer had completed
            # dropped out of their list while OpenCall still counted it as
            # assigned, so 5 upstream showed as 4 here.
            #
            # plan_date is what keeps yesterday out. The mirror pass also clears
            # in_current_plan, but that only happens when a sync runs; the date
            # means a stale ticket cannot outlive the day even if the sync stops.
            # A case created by hand in Payroll has no plan and is always shown.
            return (
                qs.filter(assigned_to=employee, in_current_plan=True)
                .filter(Q(plan_date=timezone.localdate()) | Q(plan_date__isnull=True))
                .exclude(status="cancelled")
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
        """Kept as the dispatch entry point; the logic lives at module level
        because the tracking roster resolves names the same way."""
        return resolve_engineer(data)

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

    # ------------------------------------------------- productivity numbers
    # OpenCall's Engineer Productivity page decides Assigned / Attended / Closed
    # in one function, and the case sync already reuses that same function to
    # pick the tickets it pushes here. These two endpoints carry the resulting
    # counts across the same bridge rather than deriving a second set, because
    # an engineer reading their phone and a manager reading the dashboard
    # showing different figures for the same day is worse than showing none.

    @action(detail=False, methods=["post"], url_path="scorecards")
    def scorecards(self, request):
        """Receive today's per-engineer counts from OpenCall.

        Body: {"as_of": "YYYY-MM-DD", "daily_target": 7, "monthly_target": 175,
               "rows": [ {engineer_name?/engineer_email?/engineer_phone?/engineer_id?,
                          assigned, attended, closed, month_closed}, ... ]}

        One row per engineer, replaced in place. An unmatched engineer is
        reported and skipped rather than failing the batch — the same rule
        bulk_dispatch uses, and for the same reason: one unrecognised name must
        not cost every other engineer their numbers.
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Only admin/HR can push scorecards."}, status=403)

        rows = request.data.get("rows")
        if not isinstance(rows, list):
            return Response({"detail": 'Body must be {"rows": [ ... ]}.'}, status=400)

        as_of = parse_date(str(request.data.get("as_of") or "")) or timezone.localdate()
        daily_target = _as_count(request.data.get("daily_target"))
        monthly_target = _as_count(request.data.get("monthly_target"))

        saved = 0
        skipped = []
        seen = set()

        for raw in rows:
            if not isinstance(raw, dict):
                skipped.append({"engineer_name": None, "reason": "not an object"})
                continue

            employee = resolve_engineer(raw)
            if employee is None:
                skipped.append({
                    "engineer_name": (raw.get("engineer_name") or "").strip() or None,
                    "reason": "engineer not matched",
                })
                continue

            EngineerScorecard.objects.update_or_create(
                engineer=employee,
                defaults={
                    "as_of": as_of,
                    "assigned": _as_count(raw.get("assigned")),
                    "attended": _as_count(raw.get("attended")),
                    "closed": _as_count(raw.get("closed")),
                    "month_closed": _as_count(raw.get("month_closed")),
                    "daily_target": daily_target,
                    "monthly_target": monthly_target,
                },
            )
            seen.add(employee.id)
            saved += 1

        # An engineer who dropped off today's plan keeps a card, zeroed rather
        # than left showing yesterday's figures. Only ever touches rows already
        # stamped with an older day, so a partial push cannot blank a colleague
        # who simply was not in this batch.
        stale = EngineerScorecard.objects.filter(as_of__lt=as_of).exclude(engineer_id__in=seen)
        zeroed = stale.update(
            as_of=as_of, assigned=0, attended=0, closed=0, month_closed=0,
            daily_target=daily_target, monthly_target=monthly_target,
        )

        return Response({"saved": saved, "zeroed": zeroed, "skipped": skipped})

    @action(detail=False, methods=["get"], url_path="my_scorecard")
    def my_scorecard(self, request):
        """The caller's own numbers, for the card on their Cases screen."""
        employee = _get_employee(request.user)
        if not employee:
            raise NoEmployeeProfile()

        card = EngineerScorecard.objects.filter(engineer=employee).first()
        today = timezone.localdate()

        # Yesterday's numbers wearing today's label would be a lie the engineer
        # cannot detect. If the sync has stopped, say so by showing nothing.
        fresh = card is not None and card.as_of == today

        return Response({
            "as_of": card.as_of if card else None,
            "stale": bool(card) and not fresh,
            "assigned": card.assigned if fresh else 0,
            "attended": card.attended if fresh else 0,
            "closed": card.closed if fresh else 0,
            "month_closed": card.month_closed if fresh else 0,
            "daily_target": card.daily_target if card else 0,
            "monthly_target": card.monthly_target if card else 0,
            "updated_at": card.updated_at if card else None,
        })

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

        # The plan day this batch speaks for, as the originating system counts
        # days. Stamped on every case pushed, so the engineer's list can show
        # today's plan and let yesterday's fall away on its own. A malformed value
        # is ignored rather than fatal — losing the date costs a stale row for a
        # day, refusing the batch costs every engineer their whole list.
        try:
            plan_date = parse_date((request.data.get("plan_date") or "").strip())
        except (ValueError, TypeError):
            plan_date = None

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
            if plan_date:
                case.plan_date = plan_date
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
        # A batch only speaks for its own day. The auto-sync always sends today,
        # but the manual sync endpoint takes the date from its caller — so a
        # re-sync of last Tuesday used to sweep EVERY synced case in the table,
        # clear in_current_plan on everything pushed today, and empty every
        # engineer's list at once while OpenCall still showed their assignments.
        # Nothing in the sweep was scoped to a day.
        today = timezone.localdate()
        speaks_for_today = plan_date is None or plan_date == today
        if (
            mirror
            and speaks_for_today
            and assigned_refs
            and "All" in get_allowed_branches(request.user, "attendance")
        ):
            # Out of the plan: drop the flag on every synced case that is no
            # longer pushed, completed ones included — that is what keeps the
            # engineer's count equal to the Assigned column. The status is left
            # alone here, so a completed call keeps saying completed.
            #
            # Scoped to TODAY's plan (and to cases that never had a plan date, so
            # rows dispatched before plan_date existed still age out). An earlier
            # day's rows are already invisible to the engineer through the date
            # filter on their queryset; sweeping them as well only risked
            # rewriting history this batch knows nothing about.
            left_plan = (
                Case.objects.exclude(external_ref="")
                .exclude(external_ref__in=assigned_refs)
                .filter(in_current_plan=True)
                .filter(Q(plan_date=today) | Q(plan_date__isnull=True))
            )
            cancelled_refs = sorted(left_plan.values_list("external_ref", flat=True))
            cancelled = len(cancelled_refs)
            left_plan.update(in_current_plan=False)

            # A call that never reached a terminal state and has left the plan is
            # cancelled, as before — a real outcome (completed) is never rewritten.
            # Limited to the tickets THIS sweep just dropped: it used to re-run
            # over every out-of-plan case in the table, so any push could cancel
            # calls from days it had no business touching.
            if cancelled_refs:
                Case.objects.filter(
                    external_ref__in=cancelled_refs, status__in=ACTIVE_STATUSES
                ).update(status="cancelled")
        elif mirror and not speaks_for_today:
            # Worth a line: this is the call that used to wipe the day.
            logger.info(
                "bulk_dispatch: batch is for %s, not today (%s) - plan sweep skipped",
                plan_date,
                today,
            )

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

    def _punch_coords(self, request, prefix):
        """The position the phone reported at the moment of the punch.

        Silently absent when the phone had no fix — a punch must never fail for
        want of GPS, because the alternative is an engineer standing at a
        customer unable to record that they are there. A punch with no
        coordinates still records the time; it just cannot be placed.
        """
        lat = coerce_number(request.data.get("latitude"))
        lon = coerce_number(request.data.get("longitude"))
        if lat is None or lon is None:
            return {}
        return {
            f"{prefix}_lat": lat,
            f"{prefix}_lon": lon,
            f"{prefix}_accuracy": coerce_number(request.data.get("accuracy")),
        }

    @action(detail=True, methods=["post"])
    def punch_in(self, request, pk=None):
        """The engineer is at the customer and starting work.

        One button in place of Accept, Start Travel, Reached and Start Work: an
        engineer with gloves on outside a customer's premises was being asked to
        drive a four-step workflow, and the office only ever needed to know two
        things — that they got there, and that they finished.

        Lands on `working` because that is the status the rest of the system,
        OpenCall included, already understands as "in progress". The older
        transitions still exist; this is a shorter road to the same place.
        """
        return self._engineer_transition(
            request,
            pk,
            ["assigned", "accepted", "on_the_way", "reached"],
            "working",
            stamp_field="reached_at",
            extra=self._punch_coords(request, "punch_in") or None,
        )

    @action(detail=True, methods=["post"])
    def punch_out(self, request, pk=None):
        """The work is done and the engineer is leaving."""
        notes = request.data.get("resolution_notes", "")
        extra = self._punch_coords(request, "punch_out")
        if notes:
            extra["resolution_notes"] = notes
        return self._engineer_transition(
            request,
            pk,
            ["working", "reached", "on_the_way"],
            "completed",
            stamp_field="completed_at",
            extra=extra or None,
        )

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
        # Today's distance, carried on the state the app already polls rather
        # than a request of its own. Computed by _trail_km, the same helper the
        # office's board uses — so an engineer asking "how far have I gone" and
        # a manager asking the same about them cannot get two answers.
        today = timezone.localdate()
        pings = list(
            LocationPing.objects.filter(engineer=employee, timestamp__date=today)
            .order_by("timestamp")
        )
        return {
            "on_duty": session is not None,
            "session_id": session.id if session else None,
            "started_at": session.started_at if session else None,
            "duration_minutes": session.duration_minutes() if session else 0,
            "today_km": _trail_km(pings),
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

        try:
            ping = build_ping(employee, request.data, _case_for)
        except PingRejected as exc:
            return Response({"detail": str(exc)}, status=400)

        # A phone re-sending a fix it already delivered is ordinary, not an
        # error: it timed out waiting for an answer it never saw. Hand back the
        # one we hold rather than storing the trail twice.
        if ping.client_key:
            existing = LocationPing.objects.filter(
                engineer=employee, client_key=ping.client_key
            ).first()
            if existing:
                return Response(LocationPingSerializer(existing).data, status=200)

        ping.save()
        return Response(LocationPingSerializer(ping).data, status=201)

    @action(detail=False, methods=["post"], url_path="ping/batch")
    def ping_batch(self, request):
        """A backlog of fixes the phone could not send when it took them.

        The phone keeps going while it is offline — the GPS does not need a
        network — and posts what it collected once the signal is back. Each fix
        keeps the time it was TAKEN, so the route is drawn in the order the
        engineer travelled rather than the order the network delivered.
        """
        employee = _get_employee(request.user)
        if not employee:
            return Response({"detail": "No employee profile linked to this user."}, status=400)

        try:
            result = ingest_batch(employee, request.data.get("pings"), _case_for)
        except PingRejected as exc:
            return Response({"detail": str(exc), "max_batch": MAX_BATCH}, status=400)

        return Response(result, status=201)

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
                    **_phone_state(ping),
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

        target_date = None
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

        payload = {
            "count": len(pings),
            "total_km": _trail_km(pings),
            "points": LocationPingSerializer(pings, many=True).data,
        }

        # The same trail, put onto roads. Added alongside `points` rather than
        # replacing it: every existing caller keeps the raw fixes it already
        # reads, and a map can draw the road version when it is there.
        #
        # Only for one engineer on one dated day — that is the unit the snapped
        # trail is cached by. A case's pings can span engineers and days, so
        # there is nothing coherent to cache them under.
        effective_engineer = engineer_id
        if not effective_engineer and not case_id:
            own = _get_employee(user)
            effective_engineer = own.id if own else None
        if effective_engineer and target_date:
            # _usable_pings, not the raw list: /day snaps the same filtered
            # trail, and both write to the one cached track per engineer-day.
            # Feeding them different point sets would interleave two versions of
            # the same route in one stored polyline.
            payload["road_path"] = snapped_trail(
                int(effective_engineer), target_date, _moving_trail(pings)
            )

        return Response(payload)

    @action(detail=False, methods=["get", "post"])
    def roster(self, request):
        """EVERY engineer and where they stand on a given day.

        /live answers "who is out right now", which means an engineer vanishes
        from it the moment they tap Stop Duty — and then nobody can open their
        day. This is the board you pick from: on duty, checked out, or never
        started, all clickable.

        GET  ?date=YYYY-MM-DD           -> every active employee
        POST {"names": [...], "date": ...} -> one row per NAME asked for

        The POST form exists so the caller's own engineer register can drive the
        board while the NAME MATCHING stays here, in the one place that owns it:
        the same _resolve_engineer the case dispatch uses, aliases included. When
        this was matched on the caller's side instead, a name the alias table
        would have resolved came back unmatched and the engineer's duty state was
        silently lost — they read as off duty while standing in a customer's shop.
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        date_str = request.data.get("date") if request.method == "POST" else None
        date_str = date_str or request.query_params.get("date")
        if date_str:
            try:
                target_date = parse_date(date_str)
            except ValueError:
                target_date = None
            if not target_date:
                return Response({"detail": "date must be YYYY-MM-DD."}, status=400)
        else:
            target_date = timezone.localdate()

        self._close_forgotten_sessions()

        branches = get_allowed_branches(request.user, "attendance")

        # Asked for by the caller's engineer register, or every active employee
        # when nothing is named.
        #
        # Each entry may be a bare name or {name, email, phone}. The keys matter:
        # email and phone are unique here and are what actually resolve a person,
        # and the case dispatch always sends them. Asking by name alone made the
        # board disagree with the cases — a ticket reached Praveen because his
        # email matched, while the roster could not choose between the Praveens
        # and reported nobody on duty while he was out on a call.
        requested = request.data.get("engineers") if request.method == "POST" else None
        if requested is None and request.method == "POST":
            # Older callers send a plain list of names.
            requested = request.data.get("names")

        # (employee, the name the caller asked under) — the caller's spelling is
        # echoed back so their board can label the row the way their users know it.
        resolved = []
        unmatched_names = []
        if isinstance(requested, list):
            seen = set()
            for raw in requested:
                if isinstance(raw, dict):
                    name = str(raw.get("name") or "").strip()
                    lookup = {
                        "engineer_name": name,
                        "engineer_email": raw.get("email"),
                        "engineer_phone": raw.get("phone"),
                    }
                else:
                    name = str(raw or "").strip()
                    lookup = {"engineer_name": name}
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                # The SAME resolution the case dispatch uses, with the same keys,
                # so an engineer who can receive a case can always be tracked.
                employee = resolve_engineer(lookup)
                if employee and ("All" in branches or employee.branch in branches):
                    resolved.append((employee, name))
                else:
                    unmatched_names.append(name)
        else:
            qs = Employee.objects.filter(status__in=["active", "onleave"])
            if "All" not in branches:
                qs = qs.filter(branch__in=branches)
            resolved = [(e, e.employee_name) for e in qs.order_by("employee_name")]

        if not resolved and not unmatched_names:
            return Response([])

        ids = [e.id for e, _ in resolved]

        # Every open duty session for the day, whether or not anybody asked about
        # its engineer. When the board reads "0 on duty" and an engineer is
        # plainly on duty in the app, this is the line that says which of the two
        # possible reasons it is: no session exists at all, or a session exists
        # against an employee that none of the requested names resolves to.
        open_today = list(
            DutySession.objects.filter(
                ended_at__isnull=True, started_at__date=target_date
            ).select_related("engineer")
        )
        if open_today:
            asked_for = {e.id for e, _ in resolved}
            logger.info(
                "roster %s: open duty -> %s | asked about %d name(s), %d resolved",
                target_date,
                "; ".join(
                    f"{s.engineer.employee_name} (id={s.engineer_id}, "
                    f"{'asked about' if s.engineer_id in asked_for else 'NOT ASKED ABOUT'})"
                    for s in open_today
                ),
                len(resolved) + len(unmatched_names),
                len(resolved),
            )
        else:
            logger.info(
                "roster %s: no open duty session at all for this date (%d resolved, %d unmatched)",
                target_date,
                len(resolved),
                len(unmatched_names),
            )

        # That day's duty, per engineer: the open one if there is one, otherwise
        # the last that ended.
        sessions_by_engineer = {}
        for session in DutySession.objects.filter(
            engineer_id__in=ids, started_at__date=target_date
        ).order_by("started_at"):
            current = sessions_by_engineer.get(session.engineer_id)
            # An open session always wins; otherwise keep the latest.
            if current is None or session.ended_at is None or current.ended_at is not None:
                sessions_by_engineer[session.engineer_id] = session

        # Their last fix of that day, for the marker and the "last seen" age.
        latest_ids = (
            LocationPing.objects.filter(engineer_id__in=ids, timestamp__date=target_date)
            .order_by()
            .values("engineer")
            .annotate(last_id=Max("id"))
            .values_list("last_id", flat=True)
        )
        latest_by_engineer = {
            p.engineer_id: p
            for p in LocationPing.objects.select_related("case").filter(id__in=list(latest_ids))
        }

        # One pass over the day's fixes so distance costs a single query rather
        # than one per engineer.
        trails = {}
        for ping in LocationPing.objects.filter(
            engineer_id__in=ids, timestamp__date=target_date
        ).order_by("timestamp"):
            trails.setdefault(ping.engineer_id, []).append(ping)

        now = timezone.now()
        rows = []
        for engineer, asked_as in resolved:
            session = sessions_by_engineer.get(engineer.id)
            ping = latest_by_engineer.get(engineer.id)
            trail = trails.get(engineer.id, [])

            if session and session.ended_at is None:
                state = "on_duty"
            elif session:
                state = "checked_out"
            else:
                state = "absent"

            last_seen_minutes = (
                int((now - ping.timestamp).total_seconds() // 60) if ping else None
            )
            rows.append(
                {
                    "engineer_id": engineer.id,
                    # The caller's spelling, so their board reads the way their
                    # users know the person; payroll_name is the record it matched.
                    "engineer_name": asked_as,
                    "payroll_name": engineer.employee_name,
                    "matched": True,
                    "branch": engineer.branch,
                    "state": state,
                    "on_duty": state == "on_duty",
                    "duty_started_at": session.started_at if session else None,
                    "duty_ended_at": session.ended_at if session else None,
                    "duty_minutes": session.duration_minutes() if session else 0,
                    "auto_closed": bool(session.auto_closed) if session else False,
                    "distance_km": _trail_km(trail),
                    # Only meaningful while on duty; a checked-out engineer is
                    # not expected to be reporting.
                    "stale": state == "on_duty"
                    and (ping is None or (now - ping.timestamp) > timedelta(minutes=LIVE_WINDOW_MINUTES)),
                    "last_seen_minutes": last_seen_minutes,
                    "latitude": ping.latitude if ping else None,
                    "longitude": ping.longitude if ping else None,
                    "accuracy": ping.accuracy if ping else None,
                    "status": ping.status if ping else "",
                    "timestamp": ping.timestamp if ping else None,
                    "active_case_id": ping.case_id if ping else None,
                    "active_case_number": ping.case.case_number if (ping and ping.case) else None,
                    **_phone_state(ping),
                }
            )

        # Names the caller asked for that no employee answers to. Returned as
        # rows, not dropped: this is the same gap that makes their cases get
        # skipped, and a board that quietly omits them hides it.
        for name in unmatched_names:
            rows.append(
                {
                    "engineer_id": None,
                    "engineer_name": name,
                    "payroll_name": None,
                    "matched": False,
                    "branch": None,
                    "state": "unmatched",
                    "on_duty": False,
                    "duty_started_at": None,
                    "duty_ended_at": None,
                    "duty_minutes": 0,
                    "auto_closed": False,
                    "distance_km": 0.0,
                    "stale": False,
                    "last_seen_minutes": None,
                    "latitude": None,
                    "longitude": None,
                    "accuracy": None,
                    "status": "",
                    "timestamp": None,
                    "active_case_id": None,
                    "active_case_number": None,
                    **_phone_state(None),
                }
            )
        rows.sort(key=lambda r: r["engineer_name"].lower())
        return Response(rows)

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
        punches = _punches_for_day(engineer, target_date)

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
                # Where the engineer SAID they arrived and left, as against the
                # stops above, which are inferred from the trail standing still.
                # A stop is our guess; a punch is their word, and the two are
                # worth being able to compare on the same map — a punch with no
                # stop under it is somebody who marked a call done from the road.
                "punches": punches,
                "events": events,
                # The same route put onto roads, so the line follows the street
                # the engineer was on instead of cutting between fixes. Sits
                # beside `points`, which is unchanged.
                "road_path": snapped_trail(engineer.id, target_date, _moving_trail(pings)),
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
