"""The FIELDEZ table's endpoints.

  POST /api/fieldez_km/range/        the KM report's grid                staff
  GET  /api/fieldez_km/day/          one cell, for the map beside it     staff
  GET  /api/fieldez_km/wanted/       the WOs the worker should read      staff who see every branch
  POST /api/fieldez_km/work_orders/  what the worker read from FieldEZ   staff who see every branch

range and day answer as /api/tracking/route_km/ and /api/tracking/route_km_day/
do, request for request, with FIELDEZ's two facts beside ROUTE KM's: `waiting`
(check-ins whose WO the worker has not read) and `missing` (check-ins left out,
and why). Which cells are measured, and when, is decided in km.py; where each
point stands, in points.py.
"""

import logging
from datetime import datetime, time, timedelta

from django.db import IntegrityError, transaction
from django.db.models import F, Value
from django.db.models.functions import Least
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from authentication.models import get_allowed_branches
from cases import olamaps, route_km
from cases.views import _is_staff_role
from employees.models import Employee

from . import km
from .models import FieldezWorkOrder
from .points import (
    MAX_RESOLUTIONS,
    MAX_WANTED_DAYS,
    clean_resolution,
    fieldez_points,
    storable,
    wanted_work_orders,
    wo_key,
)

logger = logging.getLogger(__name__)

# What /wanted/ looks back over, in IST days counting today, when not told:
# every day a check-in can still be read for (points.MAX_WANTED_DAYS).
DEFAULT_WANTED_DAYS = MAX_WANTED_DAYS

# The most results one /work_orders/ post may carry.
MAX_RESULTS = 200

# FieldEZ's id for a ticket, as kept.
TICKET_ID_LENGTH = FieldezWorkOrder._meta.get_field("fieldez_ticket_id").max_length
ERROR_LENGTH = FieldezWorkOrder._meta.get_field("error").max_length
# The most failed reads in a row a WO can count: the column's own ceiling.
MAX_ATTEMPTS_COUNTED = 32767


def _sees_every_branch(user):
    """An office login that may write what every branch's FIELDEZ km is read
    from -- the test mark_absent makes before marking every branch."""
    return _is_staff_role(user) and "All" in get_allowed_branches(user, "attendance")


def _clean_result(item):
    """One result from the worker as it is kept: (wo key, fields), or None if
    anything in it is not what the worker is meant to send.

    The status has to agree with the resolutions: "found" carries at least
    one, every other status none. A result that disagrees with itself is
    refused whole rather than half believed.
    """
    if not isinstance(item, dict):
        return None
    key = wo_key(item.get("wo"))
    status = item.get("status")
    if key is None or status not in FieldezWorkOrder.STATUSES:
        return None

    ticket = item.get("fieldez_ticket_id")
    if ticket is None:
        ticket = ""
    elif isinstance(ticket, int) and not isinstance(ticket, bool):
        ticket = str(ticket)
    elif isinstance(ticket, str):
        ticket = ticket.strip()
    else:
        return None
    # FieldEZ's ticket ids are numbers, and the worker sends them as digits.
    if len(ticket) > TICKET_ID_LENGTH or (ticket and not (ticket.isascii() and ticket.isdigit())):
        return None

    error = item.get("error")
    if error is None:
        error = ""
    elif not isinstance(error, str):
        return None
    error = storable(error)

    raw = item.get("resolutions")
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > MAX_RESOLUTIONS:
        return None
    resolutions = []
    for entry in raw:
        clean = clean_resolution(entry)
        if clean is None:
            return None
        resolutions.append(clean)
    if (status == "found") != bool(resolutions):
        return None

    return key, {
        "status": status,
        "fieldez_ticket_id": ticket,
        "error": error.strip()[:ERROR_LENGTH],
        "resolutions": resolutions,
    }


def _save_result(key, fields, now):
    """Write one WO's read, as its own statement, the way route_km._store
    writes a cell: an update, a create if there was nothing to update, and an
    update after all if another post created it in between."""
    failed = fields["status"] == "failed"
    counted = Least(F("attempts") + 1, Value(MAX_ATTEMPTS_COUNTED)) if failed else 0
    if FieldezWorkOrder.objects.filter(wo_key=key).update(
        attempts=counted, fetched_at=now, **fields
    ):
        return
    try:
        with transaction.atomic():
            FieldezWorkOrder.objects.create(
                wo_key=key, attempts=1 if failed else 0, fetched_at=now, **fields
            )
    except IntegrityError:
        FieldezWorkOrder.objects.filter(wo_key=key).update(
            attempts=counted, fetched_at=now, **fields
        )


class FieldezKmViewSet(viewsets.ViewSet):
    """FIELDEZ: road km through Login, each check-in's FieldEZ Problem
    Resolution place, and Logout -- and the two calls the worker that reads
    FieldEZ makes."""

    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=["post"], url_path="range")
    def km_range(self, request):
        """FIELDEZ for the KM report: every engineer's road distance, day by
        day. The request and the answer are /api/tracking/route_km/'s, each
        cell with `missing` (how many check-ins were left out) and the answer
        with `waiting` (how many cells wait on the worker reading FieldEZ).

        Body: {"engineers": [<employee id>, ...], "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        data = request.data if isinstance(request.data, dict) else {}

        # The engineers and the days, checked exactly as
        # cases/views.py TrackingViewSet.route_km checks them. Copied: there
        # they are written in the view, not in a helper this could import.
        # Keep the two alike.
        raw_ids = data.get("engineers")
        if not isinstance(raw_ids, list) or len(raw_ids) > route_km.MAX_ENGINEERS:
            return Response(
                {"detail": f"engineers must be a list of at most {route_km.MAX_ENGINEERS} ids."},
                status=400,
            )
        ids = []
        for raw in raw_ids:
            text = "" if isinstance(raw, bool) or not isinstance(raw, (int, str)) else str(raw)
            if not (text.isascii() and text.isdigit()) or len(text) > 19 or int(text) >= 2**63:
                return Response({"detail": "engineers must be a list of numeric ids."}, status=400)
            if int(text) not in ids:
                ids.append(int(text))

        bounds = []
        for name in ("from", "to"):
            value = data.get(name)
            try:
                parsed = parse_date(value) if isinstance(value, str) else None
            except ValueError:
                parsed = None
            if not parsed:
                return Response({"detail": f"{name} must be YYYY-MM-DD."}, status=400)
            bounds.append(parsed)
        start, end = bounds
        if start > end:
            return Response({"detail": "from must not be after to."}, status=400)
        if (end - start).days + 1 > route_km.MAX_DAYS:
            return Response(
                {"detail": f"from and to may cover at most {route_km.MAX_DAYS} days."},
                status=400,
            )

        # Nothing after today: a day that has not happened has nothing marked.
        end = min(end, timezone.localdate())
        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        day_keys = [day.isoformat() for day in days]

        if not olamaps.is_configured():
            return Response(
                {
                    "configured": False,
                    "days": day_keys,
                    "engineers": [],
                    "pending": 0,
                    "rechecking": 0,
                    "waiting": 0,
                    "measured_now": 0,
                }
            )

        # Only the engineers this user may see, as on ROUTE KM.
        branches = get_allowed_branches(request.user, "attendance")
        visible = Employee.objects.filter(pk__in=ids)
        if "All" not in branches:
            visible = visible.filter(branch__in=branches)
        visible = set(visible.values_list("pk", flat=True))
        ids = [engineer_id for engineer_id in ids if engineer_id in visible]

        now = timezone.now()
        measured_now = km.refresh(ids, days, now=now)

        stored = km.rows(ids, days)
        # ROUTE KM's two counts, and a third: `waiting` cells wait on the
        # worker reading their WOs from FieldEZ, not on Ola.
        pending = 0
        rechecking = 0
        waiting = 0
        engineers = []
        for engineer_id in ids:
            cells = {}
            for day in days:
                row = stored.get((engineer_id, day))
                if km.is_pending(row, now):
                    pending += 1
                elif km.is_rechecking(row, now):
                    rechecking += 1
                elif km.is_waiting(row):
                    waiting += 1
                if row is None or row.status == "pending":
                    cells[day.isoformat()] = {
                        "km": None,
                        "points": 0,
                        "status": "pending",
                        "office": False,
                        "final": False,
                        "missing": 0,
                    }
                    continue
                cells[day.isoformat()] = {
                    "km": row.km if row.status == "ready" else None,
                    "points": len(row.points or []),
                    "status": row.status,
                    "office": any(point.get("office") for point in row.points or []),
                    "final": row.final,
                    # Check-ins left out of the km: FieldEZ had no place for them.
                    "missing": len(row.missing or []),
                }
            engineers.append({"engineer_id": engineer_id, "days": cells})

        return Response(
            {
                "configured": True,
                "days": day_keys,
                "engineers": engineers,
                "pending": pending,
                "rechecking": rechecking,
                "waiting": waiting,
                "measured_now": measured_now,
            }
        )

    @action(detail=False, methods=["get"], url_path="day")
    def km_day(self, request):
        """One FIELDEZ cell, for the map beside the table: the points in the
        order they were marked, Ola's road through them, the km, the check-ins
        left out (`missing`) and how many wait on FieldEZ (`waiting`).

        Query: ?engineer=<id>&date=YYYY-MM-DD

        As /api/tracking/route_km_day/: asking measures this one cell if it is
        due and FIELDEZ may ask Ola now. A cell not measured -- never read,
        pending, or waiting on FieldEZ -- answers with its points read now.
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        # route_km_day's checks, copied (cases/views.py). The length is checked
        # before int() is asked, as route_km's body ids are: Python refuses to
        # read more than 4,300 digits, and that would be a 500, not a 400.
        raw_engineer = request.query_params.get("engineer") or ""
        if (
            not (raw_engineer.isascii() and raw_engineer.isdigit())
            or len(raw_engineer) > 19
            or int(raw_engineer) >= 2**63
        ):
            return Response({"detail": "engineer must be a numeric id."}, status=400)
        engineer = Employee.objects.filter(pk=int(raw_engineer)).first()
        if not engineer:
            return Response({"detail": "Engineer not found."}, status=404)

        branches = get_allowed_branches(request.user, "attendance")
        if "All" not in branches and engineer.branch not in branches:
            return Response({"detail": "Permission denied."}, status=403)

        date_str = request.query_params.get("date")
        try:
            target_date = parse_date(date_str) if date_str else None
        except ValueError:
            target_date = None
        if not target_date or target_date > timezone.localdate():
            return Response({"detail": "date must be YYYY-MM-DD, today or earlier."}, status=400)

        answer = {
            "configured": True,
            "engineer_id": engineer.id,
            "date": target_date.isoformat(),
            "status": "pending",
            "km": None,
            "points": [],
            "path": [],
            "error": "",
            "final": False,
            "missing": [],
            "waiting": 0,
        }
        if not olamaps.is_configured():
            answer["configured"] = False
            return Response(answer)

        km.refresh([engineer.id], [target_date], work_limit=1, measure_limit=1, time_budget=15.0)
        row = km.rows([engineer.id], [target_date]).get((engineer.id, target_date))
        if row is None or row.status in ("pending", "waiting"):
            # Not measured: the worker has not read a check-in's WO, or Ola
            # cannot be asked now. The points cost nothing, so the map has
            # them -- and says which of the two it is waiting for.
            points, missing, waiting, _ = fieldez_points(engineer, target_date)
            answer.update(
                status="waiting" if waiting else "pending",
                points=points,
                missing=missing,
                waiting=waiting,
            )
            return Response(answer)

        ready = row.status == "ready"
        answer.update(
            status=row.status,
            km=row.km if ready else None,
            points=row.points or [],
            path=(row.path or []) if ready else [],
            error=row.error if row.status == "failed" else "",
            final=row.final,
            missing=row.missing or [],
        )
        return Response(answer)

    @action(detail=False, methods=["get"], url_path="wanted")
    def wanted(self, request):
        """The work orders the FieldEZ worker should read next.

        Query: ?days=<1..31>, default 31 -- how many IST days back, today
        included, to look for check-ins.

        Answer: {"days": n, "work_orders": ["WO-...", ...]}, at most 200: the
        WOs never read first, then re-reads newest check-in first, then failed
        reads (points.wanted_work_orders). Which WOs need a read is
        points.needs_read.

        The same login as /work_orders/: one that sees every branch. Asked
        with any other, it is refused here, before the worker spends a single
        FieldEZ request on WOs it would not be allowed to save.
        """
        if not _sees_every_branch(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        raw = request.query_params.get("days")
        if raw is None or raw == "":
            days = DEFAULT_WANTED_DAYS
        elif raw.isascii() and raw.isdigit() and len(raw) <= 2 and 1 <= int(raw) <= MAX_WANTED_DAYS:
            days = int(raw)
        else:
            return Response(
                {"detail": f"days must be a whole number from 1 to {MAX_WANTED_DAYS}."},
                status=400,
            )

        now = timezone.now()
        first = timezone.localdate(now) - timedelta(days=days - 1)
        since = timezone.make_aware(datetime.combine(first, time.min))
        branches = get_allowed_branches(request.user, "attendance")
        return Response({"days": days, "work_orders": wanted_work_orders(since, now, branches)})

    @action(detail=False, methods=["post"], url_path="work_orders")
    def work_orders(self, request):
        """What the worker read from FieldEZ, one result per WO.

        Body: {"results": [{"wo", "status", "fieldez_ticket_id", "error",
        "resolutions": [{"at", "user", "latitude", "longitude", "address"}]}]}

        Each result is checked on its own (_clean_result) and kept by its WO
        key, read now; a WO's failed reads are counted, any other answer sets
        the count back to 0. A result that is not right is refused and counted,
        never a 500, and the rest are kept. Answer: {"saved": n, "rejected": m}.

        Only an office login that sees every branch may post: one WO's place
        is what every branch's FIELDEZ km is measured through.
        """
        if not _sees_every_branch(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        data = request.data if isinstance(request.data, dict) else {}
        results = data.get("results")
        if not isinstance(results, list) or len(results) > MAX_RESULTS:
            return Response(
                {"detail": f"results must be a list of at most {MAX_RESULTS} results."},
                status=400,
            )

        now = timezone.now()
        saved = 0
        rejected = 0
        for item in results:
            clean = _clean_result(item)
            if clean is None:
                rejected += 1
                continue
            _save_result(*clean, now)
            saved += 1

        # Counts only: what was read is the customer's, and stays out of logs.
        logger.info("fieldez km: %d work orders saved, %d rejected", saved, rejected)
        return Response({"saved": saved, "rejected": rejected})
