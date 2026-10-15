"""FIELDEZ's road kilometres: ROUTE KM's measuring, on a table of its own.

Each cell is measured the way cases/route_km.py measures its own -- once, by
route_km.measure, and again only when its points move; a few cells a request,
the rest left pending for the next poll -- through the points
points.fieldez_points gives: ROUTE KM's Login and Logout, and each check-in
where FieldEZ recorded its Problem Resolution.

What is different, and why:

  It waits for FieldEZ. A day with a check-in whose WO the worker has not read
  yet is stored "waiting" and not measured: without that check-in the road
  would come out short, and a short figure looks like an answer.

  It gives way. Ola's per-minute limit is shared with ROUTE KM and the live
  board, and this is the table that can wait. It asks Ola nothing while ROUTE
  KM has read or measured anything in the last minute, nothing while a 429
  written on either table is fresh, and at most FIELDEZ_KM_PER_MINUTE a minute
  of its own. Giving way stops only the measuring: the cells that need no Ola
  are still read.

  It borrows. A day whose points are ROUTE KM's own -- one with no check-ins,
  or FieldEZ placing each where the phone was -- takes ROUTE KM's km and road
  as they are, and spends no request.

ROUTE KM's table is read here and never written: nothing in this module can
change a figure ROUTE KM shows.
"""

from __future__ import annotations

import datetime
import logging
import os
import time

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from cases import olamaps, route_km
from cases.models import RouteKmDay
from employees.models import Employee

from .models import FieldezKmDay, FieldezWorkOrder
from .points import fieldez_points, readable

logger = logging.getLogger(__name__)

# How many measurements every worker together may start in a minute, unless
# FIELDEZ_KM_PER_MINUTE says otherwise. A third of ROUTE KM's: this table comes
# after it, and after the live board.
DEFAULT_PER_MINUTE = 2

# Why a check-in can be left out today and placed tomorrow: the worker reads
# such a WO again (points.needs_read) for as long as its day is readable, and
# FieldEZ may have it, or its Problem Resolution, by then.
REREAD_REASONS = ("not_in_fieldez", "no_problem_resolution")

# How far back a FieldEZ read can matter to _read_since: no cell is held back
# by its recheck window for longer than this.
READ_SINCE_MINUTES = max(route_km.RECHECK_MINUTES, route_km.EARLIER_RECHECK_MINUTES)

# A read and a look are each stamped with their request's start, so a WO saved
# while a look was reading its day can carry a time just before that look's:
# reads this close before a look still count as after it.
BEHIND_SLACK = datetime.timedelta(seconds=30)

# Monotonic time until which this process asks Ola nothing for FIELDEZ, after a
# 429 of its own. route_km keeps its own clock the same way, and that one is
# honoured here too -- read, never set.
_cooldown_until = 0.0


def per_minute() -> int:
    """How many FIELDEZ measurements may start in any minute, across every
    worker. Read from the environment on each call; never below one."""
    try:
        value = int(os.environ.get("FIELDEZ_KM_PER_MINUTE") or DEFAULT_PER_MINUTE)
    except ValueError:
        value = DEFAULT_PER_MINUTE
    return max(1, value)


# -- the stored cells ---------------------------------------------------------


def rows(engineer_ids, days) -> dict:
    """The stored rows for a grid, by (engineer id, day), without their road
    lines -- route_km.rows, on this table."""
    return {
        (row.engineer_id, row.day): row
        for row in FieldezKmDay.objects.filter(
            engineer_id__in=list(engineer_ids), day__in=list(days)
        ).defer("path")
    }


def is_pending(row, now) -> bool:
    """Whether a cell is waiting on Ola, by route_km.is_pending's own rule --
    except a settled day Ola has failed MAX_FAILED_ATTEMPTS times: refresh
    asks Ola about it again only once its places move, so no retry is coming,
    and counting it would keep the report polling for nothing.

    A "waiting" cell is not pending either: it waits on the worker reading
    FieldEZ, not on Ola, and is counted apart (is_waiting).
    """
    if (
        row is not None
        and row.status == "failed"
        and row.attempts >= route_km.MAX_FAILED_ATTEMPTS
        and route_km.settled(row.day, now)
    ):
        return False
    return route_km.is_pending(row, now)


def is_rechecking(row, now) -> bool:
    """Whether a measured cell of today is due to be read again, by
    route_km.is_rechecking's own rule."""
    return route_km.is_rechecking(row, now)


def is_waiting(row) -> bool:
    """Whether a cell waits on the worker reading a check-in's WO from FieldEZ."""
    return row is not None and row.status == "waiting"


def _work_orders(row) -> set:
    """The WOs a stored cell's check-ins were placed by or left out over.

    A waiting cell does not keep the WOs it waits on -- only the ones placed
    or left out so far -- so for it this is not the whole list.
    """
    keys = set()
    for entry in list(row.points or []) + list(row.missing or []):
        if isinstance(entry, dict) and isinstance(entry.get("wo"), str) and entry["wo"]:
            keys.add(entry["wo"])
    return keys


def _read_since(now):
    """Which stored cells FieldEZ has been read for since they were last read
    themselves: a function of a row, True for those -- to be asked only of a
    row inside its recheck window, as refresh does. A row outside it is due
    anyway, so the reads that can matter are the last READ_SINCE_MINUTES', and
    one query finds them.

    That read is what such a cell may have been waiting for -- a WO found, or
    found to have no place -- so it is read again now, whatever its recheck
    window says; left to the window, a day before today would go on showing
    "waiting" for an hour over an answer that is already in. A waiting cell
    is due after any read at all, as it does not keep the WOs it waits on, and
    once its day has left the worker's window, after which none will come:
    its unread check-ins are then "not_read". Any other cell is due when one
    of its own WOs was read after it.
    """
    since = now - datetime.timedelta(minutes=READ_SINCE_MINUTES)
    read_at = dict(
        FieldezWorkOrder.objects.filter(fetched_at__gt=since).values_list("wo_key", "fetched_at")
    )
    last_read = max(read_at.values(), default=None)

    def behind(row) -> bool:
        if row.status == "waiting" and not readable(row.day, now):
            return True
        if not row.checked_at or last_read is None:
            return False
        looked = row.checked_at - BEHIND_SLACK
        if looked >= last_read:
            return False
        if row.status == "waiting":
            return True
        return any(key in read_at and read_at[key] > looked for key in _work_orders(row))

    return behind


def _store(engineer_id, day, **fields):
    """Write one cell, as its own statement -- route_km._store, on this table."""
    if FieldezKmDay.objects.filter(engineer_id=engineer_id, day=day).update(**fields):
        return
    try:
        with transaction.atomic():
            FieldezKmDay.objects.create(engineer_id=engineer_id, day=day, **fields)
    except IntegrityError:
        FieldezKmDay.objects.filter(engineer_id=engineer_id, day=day).update(**fields)


# -- giving way ---------------------------------------------------------------


def _cooling_down(now) -> bool:
    """Whether Ola has asked anyone to wait in the last COOLDOWN_SECONDS.

    This process's clocks first, FIELDEZ's and ROUTE KM's; then the database,
    where a refused request is written on its cell -- on either table, so a
    429 that ROUTE KM was given holds FIELDEZ back as well.
    """
    if time.monotonic() < max(_cooldown_until, route_km._cooldown_until):
        return True
    since = now - datetime.timedelta(seconds=route_km.COOLDOWN_SECONDS)
    return (
        RouteKmDay.objects.filter(error=route_km.RATE_LIMITED, measured_at__gte=since).exists()
        or FieldezKmDay.objects.filter(
            error=route_km.RATE_LIMITED, measured_at__gte=since
        ).exists()
    )


def _giving_way(minute_ago, claimed=False) -> bool:
    """Whether FIELDEZ must ask Ola nothing now: ROUTE KM has read or measured
    a cell in the last minute, or FIELDEZ's own allowance for the minute is
    spent. With `claimed`, the count already includes this worker's own claim.

    Read, not only measured: the KM report asks both tables at once, and ROUTE
    KM writes checked_at on every cell it reads, before it claims one to
    measure -- so FIELDEZ sees it at work from its first cell, not only once
    it has asked Ola (found by review on 15 Oct 2026). It ends a minute after
    ROUTE KM goes quiet, so it cannot hold FIELDEZ back for long.

    Two questions, not one with an OR: checked_at has no index, and an OR
    would read the whole table, which grows every day. Reads are asked about
    within the days a report can show, which the day index bounds."""
    if RouteKmDay.objects.filter(measured_at__gte=minute_ago).exists():
        return True
    if RouteKmDay.objects.filter(
        day__gte=timezone.localdate(minute_ago) - datetime.timedelta(days=route_km.MAX_DAYS),
        checked_at__gte=minute_ago,
    ).exists():
        return True
    count = FieldezKmDay.objects.filter(measured_at__gte=minute_ago).count()
    return count > per_minute() if claimed else count >= per_minute()


def _claim(engineer_id, day, row, now) -> str:
    """Take a cell for measuring: "ok", "taken" by another worker, or "full"
    when FIELDEZ may not ask Ola now (see _giving_way).

    route_km._claim's pattern on this table: the claim is the cell's
    measured_at set to now, which only one worker gets from a row not measured
    in the last minute; the minute is checked before the claim and again after
    it, and a claim that lands past it is given back.
    """
    minute_ago = now - datetime.timedelta(seconds=60)
    if _giving_way(minute_ago):
        return "full"
    if row is None:
        # Made pending, and only if it is still absent: another worker may
        # have measured it since this one looked.
        try:
            with transaction.atomic():
                FieldezKmDay.objects.get_or_create(engineer_id=engineer_id, day=day)
        except IntegrityError:
            pass
    cell = FieldezKmDay.objects.filter(engineer_id=engineer_id, day=day)
    before = cell.values_list("measured_at", flat=True).first()
    took = cell.filter(Q(measured_at__isnull=True) | Q(measured_at__lt=minute_ago)).update(
        measured_at=now
    )
    if not took:
        return "taken"
    if _giving_way(minute_ago, claimed=True):
        cell.filter(measured_at=now).update(measured_at=before)
        return "full"
    return "ok"


def _waits_on_ola(engineer_id, day, row, points, missing, checkins, **fields):
    """A cell read now that only Ola stands between it and a km.

    Never stored, stored "waiting" -- which would go on saying the worker has
    not read its WOs when it has --, or ready / no_points through places that
    have since moved, it is stored pending now, with the points it will be
    measured through: the next look knows it waits only on Ola and passes it
    by while Ola is closed, and the report counts it and keeps asking. A km it
    had is kept with its signature, for the unchanged branch. Never final:
    another request may have finished the cell from an older look, and a
    final pending cell would never be read again. A failed row keeps what it
    had, as in route_km. `fields` are written either way.
    """
    if row is None or row.status in ("waiting", "ready", "no_points"):
        fields.update(
            status="pending", points=points, missing=missing, checkins=checkins, final=False
        )
    if fields:
        _store(engineer_id, day, **fields)


# -- refresh ------------------------------------------------------------------


def refresh(
    engineer_ids, days, now=None, work_limit=30, measure_limit=2, time_budget=8.0
) -> int:
    """Bring a grid of FIELDEZ cells up to date, as far as one request should,
    and say how many it asked Ola to measure, the failed ones included.

    route_km.refresh's rules: never measured first, oldest day first, as a
    cycle fills; then cells whose points may have moved, then failed days due
    a retry -- these newest day first, so today's places, which move as the day
    goes, are not held up behind earlier days read again for a late FieldEZ
    answer; then by engineer. Final cells are passed over, as are cells read in
    the last RECHECK_MINUTES (a day before today: EARLIER_RECHECK_MINUTES)
    unless FieldEZ has been read for them since (_read_since), and failed ones
    inside FAILED_RETRY_MINUTES. A pending cell is always due. At most
    `work_limit` cells are read, within `time_budget` seconds.

    A day with a check-in waiting on FieldEZ is stored "waiting", never
    measured and never final; nor is a day final while a check-in left out for
    one of REREAD_REASONS can still be read again. A day of fewer than two
    points, or all of them in one place, needs no Ola; one whose points are the
    ones its km was measured through keeps it -- a waiting day keeps a km it
    had, so the same places coming back cost no request; one whose points are
    ROUTE KM's, measured, takes ROUTE KM's km; a settled day Ola has failed
    MAX_FAILED_ATTEMPTS times is not asked about again until its points move.
    The rest are measured -- at most `measure_limit`, and only while _claim
    lets FIELDEZ ask. Once Ola is closed to this request the cells that need
    it are left pending, and not read at all if already known to need it, but
    the reading goes on: a day that needs no Ola is never held up by one that
    does.
    """
    global _cooldown_until

    now = now or timezone.now()
    started = time.monotonic()
    engineers = Employee.objects.in_bulk(list(set(engineer_ids)))
    ids = sorted(engineers)
    days = sorted(set(days))
    stored = rows(ids, days)

    today = timezone.localdate(now)
    recheck_after = now - datetime.timedelta(minutes=route_km.RECHECK_MINUTES)
    earlier_recheck_after = now - datetime.timedelta(minutes=route_km.EARLIER_RECHECK_MINUTES)
    retry_after = now - datetime.timedelta(minutes=route_km.FAILED_RETRY_MINUTES)

    def rank(row):
        if row is None or row.status == "pending":
            return 0
        return 2 if row.status == "failed" else 1

    def order(item):
        place, day, engineer_id, _ = item
        return (place, day.toordinal() if place == 0 else -day.toordinal(), engineer_id)

    behind = _read_since(now)
    due = []
    for day in days:
        for engineer_id in ids:
            row = stored.get((engineer_id, day))
            if row is not None:
                if row.final:
                    continue
                read_after = earlier_recheck_after if day < today else recheck_after
                if (
                    row.status != "pending"
                    and row.checked_at
                    and row.checked_at > read_after
                    and not behind(row)
                ):
                    continue
                if row.status == "failed" and row.measured_at and row.measured_at > retry_after:
                    continue
            due.append((rank(row), day, engineer_id, row))
    due.sort(key=order)

    measured = 0
    read = 0
    ola_open = True
    for _, day, engineer_id, row in due:
        if read >= work_limit or time.monotonic() - started >= time_budget:
            break
        if ola_open and (measured >= measure_limit or _cooling_down(now)):
            ola_open = False
        if not ola_open and row is not None and (
            row.status in ("pending", "failed") or row.error == route_km.RATE_LIMITED
        ):
            # Known to need Ola, and Ola is closed to this request: read again
            # once it is open, so the cells behind it are not held up. So is
            # the cell that carries a 429: written over, the other workers
            # would stop waiting it out.
            continue
        read += 1

        points, missing, waiting, checkins = fieldez_points(engineers[engineer_id], day, now)
        mark = route_km.signature(points)
        # Settled as ROUTE KM's day is, and FieldEZ done with it too: nothing
        # waiting, and nothing left out that the worker will read again.
        final = (
            route_km.settled(day, now)
            and not waiting
            and not (
                readable(day, now)
                and any(entry["reason"] in REREAD_REASONS for entry in missing)
            )
        )

        if waiting:
            # Measured without the check-ins still to be read, the road would
            # be short. Not final however old the day: the worker may yet
            # read them. A km the day already had is kept, with the signature
            # of the places it was measured through and its road: not shown
            # while waiting, it is the answer again if the read leaves the
            # places as they were -- a WO with no place, the usual case. So
            # are a failed day's signature and tries: a FieldEZ read that
            # failed for a moment must not buy Ola three more tries through
            # the same places.
            if row is not None and row.km is not None:
                kept = {"attempts": 0}
            elif row is not None and row.status in ("failed", "waiting", "pending") and row.attempts:
                kept = {"km": None, "path": []}
            else:
                kept = {"km": None, "path": [], "signature": mark, "attempts": 0}
            _store(
                engineer_id,
                day,
                status="waiting",
                points=points,
                missing=missing,
                checkins=checkins,
                error="",
                checked_at=now,
                final=False,
                **kept,
            )
            continue

        if len(points) < 2:
            _store(
                engineer_id,
                day,
                status="no_points",
                km=None,
                path=[],
                points=points,
                missing=missing,
                checkins=checkins,
                signature=mark,
                error="",
                attempts=0,
                checked_at=now,
                final=final,
            )
            continue

        if (
            row is not None
            and row.km is not None
            and row.status in ("ready", "waiting", "pending")
            and row.signature == mark
        ):
            # Nothing has moved since it was measured, so the km still holds.
            # A row keeps a km only with the signature of the places it was
            # measured through, so a waiting or pending one is ready again.
            # Its error is left alone: it may be a fresh 429 the other workers
            # are waiting out.
            _store(
                engineer_id,
                day,
                status="ready",
                points=points,
                missing=missing,
                checkins=checkins,
                checked_at=now,
                final=final,
            )
            continue

        if route_km.one_place(points):
            # No road to measure, and no request spent saying so.
            _store(
                engineer_id,
                day,
                status="ready",
                km=0.0,
                path=[[points[0]["latitude"], points[0]["longitude"]]],
                points=points,
                missing=missing,
                checkins=checkins,
                signature=mark,
                error="",
                attempts=0,
                checked_at=now,
                final=final,
            )
            continue

        borrowed = (
            RouteKmDay.objects.filter(
                engineer_id=engineer_id, day=day, status="ready", signature=mark, km__isnull=False
            )
            .values_list("km", "path")
            .first()
        )
        if borrowed is not None:
            # The same places in the same order as a day ROUTE KM has measured:
            # the same road. Its km and line, read, and no request spent.
            _store(
                engineer_id,
                day,
                status="ready",
                km=borrowed[0],
                path=borrowed[1] or [],
                points=points,
                missing=missing,
                checkins=checkins,
                signature=mark,
                error="",
                attempts=0,
                checked_at=now,
                final=final,
            )
            continue

        # A failed day's tries, carried through a wait on FieldEZ (see above).
        tried = (
            row is not None
            and row.km is None
            and row.status in ("failed", "waiting", "pending")
            and row.signature == mark
        )
        if (
            tried
            and row.attempts >= route_km.MAX_FAILED_ATTEMPTS
            and route_km.settled(day, now)
        ):
            # Ola has failed these very places every try, and the day is over:
            # ROUTE KM stops there too. Asked again once the places move --
            # held open by a check-in FieldEZ may yet place, the day would
            # otherwise be retried every hour for a month.
            _store(
                engineer_id,
                day,
                status="failed",
                points=points,
                missing=missing,
                checkins=checkins,
                checked_at=now,
                final=final,
            )
            continue

        if not ola_open:
            _waits_on_ola(engineer_id, day, row, points, missing, checkins)
            continue

        claim = _claim(engineer_id, day, row, now)
        if claim == "full":
            # FIELDEZ may not ask Ola now: nothing more is measured in this
            # request, and the cells left that need Ola wait for the next poll.
            ola_open = False
            _waits_on_ola(engineer_id, day, row, points, missing, checkins)
            continue
        if claim == "taken":
            # Another worker is measuring this one.
            continue

        try:
            km, path = route_km.measure(points)
        except olamaps.RateLimited as exc:
            _cooldown_until = time.monotonic() + route_km.COOLDOWN_SECONDS
            # Written on the cell, with when, so every other worker finds the
            # wait in _cooling_down instead of each asking Ola to be told.
            _waits_on_ola(
                engineer_id,
                day,
                row,
                points,
                missing,
                checkins,
                error=route_km.RATE_LIMITED,
                measured_at=now,
            )
            logger.warning(
                "fieldez km: Ola is rate limiting, asking nothing for %d s: %s",
                route_km.COOLDOWN_SECONDS,
                exc,
            )
            ola_open = False
            continue
        except olamaps.SnapUnavailable as exc:
            measured += 1
            # Failures in a row through the same places; new places start again.
            attempts = (row.attempts if tried else 0) + 1
            logger.warning(
                "fieldez km unavailable for engineer %s on %s: %s", engineer_id, day, exc
            )
            _store(
                engineer_id,
                day,
                status="failed",
                km=None,
                path=[],
                points=points,
                missing=missing,
                checkins=checkins,
                signature=mark,
                error=str(exc)[:200],
                attempts=attempts,
                checked_at=now,
                measured_at=now,
                # A settled day that has failed every try stays failed.
                final=final and attempts >= route_km.MAX_FAILED_ATTEMPTS,
            )
            continue

        measured += 1
        _store(
            engineer_id,
            day,
            status="ready",
            km=km,
            path=path,
            points=points,
            missing=missing,
            checkins=checkins,
            signature=mark,
            error="",
            attempts=0,
            checked_at=now,
            measured_at=now,
            final=final,
        )
    return measured
