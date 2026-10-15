"""Road kilometres through the places an engineer marked.

The KM report's first table is the GPS trail: every fix the phone sent, joined
up. This is the second, ROUTE KM, and it answers a different question -- how
far is it by road from where they logged in, through every call they checked
in at, to where they logged out. Those are points the engineer marked
themselves, so the figure does not depend on how well the phone tracked in
between. Check-outs are not points: a call's place is where they checked in.

Measured by Ola's Directions, and checkable by hand -- the report links the
same points, in the same order, to Google Maps. On one real day in Hosur five
points came to 12.74 km by Ola and 12.3 km by Google on a two-wheeler.

Three rules this module keeps:

  A cell is measured once. The km is stored with a signature of the points it
  was measured through, and Ola is asked again only when those points change.

  A request does a bounded amount of work. A bill cycle is hundreds of cells,
  so each call reads a few, measures fewer and leaves the rest pending; the
  report polls until nothing is.

  Ola is paced. Its per-minute limit is shared with the live board's snapping,
  so measurements are counted across every worker, and a 429 stops this
  process asking for two minutes.

And olamaps' rule about the key holds here too: it goes in the query string of
a server-side request and nowhere else -- not in a stored error, not in a log
line, not in an exception message.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from employees.models import Employee

from . import olamaps
from .models import Case, CaseVisit, DutySession, LocationPing, RouteKmDay
from .pings import MAX_BACKLOG_HOURS

logger = logging.getLogger(__name__)

DIRECTIONS_URL = "https://api.olamaps.io/routing/v1/directions"

# Points per request, both ends included. 26 is what road_km proved against the
# live API; 51 is refused.
POINTS_PER_CALL = 26

# The same Ola as the snapper, so the same spacing between requests. Less
# patience than the snapper with each one: these run inside a request the
# report is polling, and a worker held for a minute by a slow Ola is a worker
# the rest of Payroll does not have.
BATCH_PAUSE_SECONDS = olamaps.BATCH_PAUSE_SECONDS
TIMEOUT_SECONDS = 10

# Today is read again at most this often. Its points change only when the
# engineer taps something, and the report polls far more often.
RECHECK_MINUTES = 5

# An open day before today is read again at most this often: only a late fix
# or a late tap can still move its points. Read as often as today, the two or
# three earlier open days of a large team would take every poll's reads, and
# today's -- what the report waits on -- would never be reached.
EARLIER_RECHECK_MINUTES = 60

# How long a day Ola could not route waits before it is tried again. Whatever
# failed -- a point no road reaches, an outage -- will not have changed by the
# next poll, and every retry is a request.
FAILED_RETRY_MINUTES = 60

# After a 429, how long every worker asks Ola nothing at all.
COOLDOWN_SECONDS = 120

# What a cell's error says while Ola is making us wait. Written on the cell
# whose request was refused, so every worker can see the wait, not only the
# one that was told.
RATE_LIMITED = "rate limited by Ola"

# How long past midnight a day is still open. A phone that was offline sends
# its queued fixes when it is back, and Payroll takes them up to
# MAX_BACKLOG_HOURS late (cases/pings.py): until then a Login placed at the
# office for want of a fix may still get its real one. An hour more for the
# taps that come with them; after that the day is final and its points are not
# read again.
SETTLE_HOURS = MAX_BACKLOG_HOURS + 1

# A day Ola could not route is tried this many times in all. Once the day is
# settled and this is spent, it stays failed instead of being asked about
# every hour for ever.
MAX_FAILED_ATTEMPTS = 3

# How many measurements every worker together may start in a minute, unless
# ROUTE_KM_PER_MINUTE says otherwise. Fifteen quick requests in a row earned a
# 429 (see olamaps), and the live board's snapping draws on the same minute.
DEFAULT_PER_MINUTE = 6

# The most the report may ask about in one call.
MAX_ENGINEERS = 300
MAX_DAYS = 62

# Points that share a moment are ordered the way a day runs: on duty, at the
# call, off duty.
_ORDER = {"login": 0, "checkin": 1, "logout": 2}

# Each region's office, by the branch an engineer belongs to. The office asked
# (14 Oct 2026) that a Login or Logout the phone gave no position for be put at
# the engineer's own office -- where those days start and end -- instead of
# being left out. A tap that has a fix keeps its fix. Taken from the offices'
# own Google Maps links; Vellore's link carried only the Street View point at
# the door, a few metres from it.
OFFICES = {
    "chennai": (13.0544285, 80.177887),
    "hosur": (12.7242996, 77.826677),
    "kanchipuram": (12.8377362, 79.7045409),
    "salem": (11.6700898, 78.1422186),
    "vellore": (12.9491539, 79.1368408),
}


def office_of(engineer):
    """(lat, lon) of the office an engineer's branch names, or None."""
    return OFFICES.get((getattr(engineer, "branch", "") or "").strip().lower())

# Monotonic time until which this process asks Ola nothing. Per process on
# purpose: the per-minute count in the database is what paces the workers
# together; this only holds back the one that has just been told to stop.
_cooldown_until = 0.0


def per_minute() -> int:
    """How many measurements may start in any minute, across every worker.

    Read from the environment on each call, like the Ola key. Never below one:
    a zero would stop the report for good rather than slow it down.
    """
    try:
        value = int(os.environ.get("ROUTE_KM_PER_MINUTE") or DEFAULT_PER_MINUTE)
    except ValueError:
        value = DEFAULT_PER_MINUTE
    return max(1, value)


# -- the points ---------------------------------------------------------------


def _nearest_fix(engineer, day, moment, seconds):
    """The fix the day timeline places an entry at, or None.

    The timeline gives Login and Logout the position of the day's fix nearest
    in time to them, if one is within `seconds`, the earliest winning a tie.
    Only the window around the moment is asked for, which comes to the same
    answer: every fix outside it is too far away to be used.
    """
    window = datetime.timedelta(seconds=seconds)
    nearest = None
    best = None
    for ping in (
        LocationPing.objects.filter(
            engineer=engineer,
            timestamp__date=day,
            latitude__isnull=False,
            timestamp__gte=moment - window,
            timestamp__lte=moment + window,
        )
        .only("timestamp", "latitude", "longitude")
        .order_by("timestamp", "id")
    ):
        apart = abs((ping.timestamp - moment).total_seconds())
        if best is None or apart < best:
            nearest, best = ping, apart
    return nearest


def _checkins_for_day(engineer, day):
    """Every call `engineer` checked into on `day`: (moment, lat, lon, case number).

    Who made the visit is the visit's own engineer -- whoever checked in, which
    the check-in writes on it (views._record_trip) -- not whoever the case is
    assigned to now. A call sent on to someone else keeps yesterday's check-in
    with the engineer who made it; read off the case, it would move into the
    new engineer's route and out of the old one's. A visit with no engineer
    recorded falls back to the case's, as the day timeline reads every visit.

    A call checked into with no visit row at all -- from before visits were
    kept -- answers from its own columns, as on the timeline. One that has a
    visit checked in that day is never read from its columns as well: those
    columns would put the same check-in in whoever holds the call now. Asked
    of that day only, in the database, rather than of the engineer's whole
    case history.
    """
    out = []
    recorded = set()
    for visit in (
        CaseVisit.objects.filter(Q(engineer=engineer) | Q(case__assigned_to=engineer))
        .filter(Q(checked_in_at__date=day) | Q(checked_out_at__date=day))
        .select_related("case")
    ):
        case, at = visit.case, visit.checked_in_at
        # Who pressed Check In. Only the call's engineer can, and every hand-over
        # (assign, bulk_dispatch) re-stamps assigned_at: a call held since before
        # the check-in was checked into by whoever holds it now -- whatever an
        # older row says, for rows written before the check-in named its maker.
        # A call handed on since then: the visit's own engineer.
        if at and case.assigned_to_id and case.assigned_at and case.assigned_at <= at:
            made_by = case.assigned_to_id
        else:
            made_by = visit.engineer_id or case.assigned_to_id
        if made_by != engineer.id:
            continue
        recorded.add(visit.case_id)
        if not at or timezone.localtime(at).date() != day:
            continue
        out.append((at, visit.punch_in_lat, visit.punch_in_lon, case.case_number))

    for case in (
        Case.objects.filter(assigned_to=engineer, reached_at__date=day)
        .exclude(id__in=recorded)
        .exclude(visits__checked_in_at__date=day)
        .only("case_number", "reached_at", "punch_in_lat", "punch_in_lon")
    ):
        out.append((case.reached_at, case.punch_in_lat, case.punch_in_lon, case.case_number))
    return out


def key_points(engineer, day, *, as_before_15_oct=False) -> list[dict]:
    """Where the engineer marked their day, in the order they marked it.

    Login, every Check in, Logout, each placed exactly where the day timeline
    places it, so the markers on the report's map and the entries on that rail
    cannot disagree:

      Login and Logout carry no coordinate of their own. They take the day's
      nearest fix within NEAREST_FIX_SECONDS, as the timeline does. A tap with
      no fix that close is put at the engineer's office (OFFICES, by branch),
      marked "office" so the map can say so; with no office for the branch it
      is left out rather than guessed at.

      A Logout the system wrote -- the auto-close after a forgotten one,
      stamped at the session's last fix -- is on the rail as "Auto-closed (no
      Logout)" at its nearest fix, and is a point there too: the office asked
      (15 Oct 2026) for every Login, Check in and Logout on the rail to be on
      the route. Never at the office: nobody saw the engineer there, and a
      trip to it would be km nobody travelled.

      A Check in is where the phone was when they pressed it, credited to the
      engineer who made the visit (see _checkins_for_day). One the phone gave
      no position for is placed exactly as the timeline places it: at the
      customer's pin on the call or, with no pin, at the day's nearest fix
      within NEAREST_FIX_SECONDS. A pin with a latitude and no longitude
      places it nowhere, there as here. One placed nowhere cannot be routed
      through and is left out.

    as_before_15_oct gives the points as they were made before those two were
    asked for: no auto-closed Logout and no Check in without a position.
    route_km_reread uses it to tell a settled cell the new rule changes from
    one that moved for some other reason.

    Returns [{"kind", "at", "latitude", "longitude", "case_number", "office"},
    ...] with "at" as an ISO string, ready to be stored and served as it is.
    """
    # Imported here: views imports this module, the same way it imports tracks.
    from .views import NEAREST_FIX_SECONDS

    points = []
    office = office_of(engineer)
    sessions = DutySession.objects.filter(engineer=engineer, started_at__date=day).order_by(
        "started_at"
    )
    for session in sessions:
        # (kind, moment, whether the office may stand in for a missing fix)
        taps = [("login", session.started_at, True)]
        if session.ended_at and not (as_before_15_oct and session.auto_closed):
            taps.append(("logout", session.ended_at, not session.auto_closed))
        for kind, moment, office_stands_in in taps:
            fix = _nearest_fix(engineer, day, moment, NEAREST_FIX_SECONDS)
            if fix is not None:
                (latitude, longitude), at_office = (fix.latitude, fix.longitude), False
            elif office is not None and office_stands_in:
                (latitude, longitude), at_office = office, True
            else:
                continue
            points.append(
                {
                    "kind": kind,
                    "at": moment,
                    "latitude": latitude,
                    "longitude": longitude,
                    "case_number": None,
                    "office": at_office,
                }
            )

    checkins = _checkins_for_day(engineer, day)
    # The customer's pin of each call checked into with no position.
    unplaced = {
        number
        for _, latitude, longitude, number in checkins
        if number and (latitude is None or longitude is None)
    }
    pins = {}
    if unplaced and not as_before_15_oct:
        for number, latitude, longitude in Case.objects.filter(
            case_number__in=unplaced
        ).values_list("case_number", "latitude", "longitude"):
            pins[number] = (latitude, longitude)
    for moment, latitude, longitude, case_number in checkins:
        if latitude is None or longitude is None:
            if as_before_15_oct:
                continue
            # As TrackingViewSet.day places it: the customer's pin, and the
            # nearest fix only when the pin has no latitude.
            latitude, longitude = pins.get(case_number, (None, None))
            if latitude is None:
                fix = _nearest_fix(engineer, day, moment, NEAREST_FIX_SECONDS)
                if fix is None:
                    continue
                latitude, longitude = fix.latitude, fix.longitude
            elif longitude is None:
                continue
        points.append(
            {
                "kind": "checkin",
                "at": moment,
                "latitude": latitude,
                "longitude": longitude,
                "case_number": case_number,
                "office": False,
            }
        )

    points.sort(key=lambda point: (point["at"], _ORDER[point["kind"]]))
    for point in points:
        point["at"] = point["at"].isoformat()
    return points


def signature(points) -> str:
    """A fingerprint of the points' kinds and places, in order.

    Five decimals is about a metre: a point that moved less than that is the
    same place, and measuring it again would cost a request to say so. Times
    are left out on purpose -- the road between two places does not depend on
    when somebody stood at them.
    """
    text = "|".join(
        "%s:%.5f,%.5f" % (point["kind"], point["latitude"], point["longitude"])
        for point in points
    )
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# -- Ola ----------------------------------------------------------------------


def decode_polyline(encoded: str, precision: int = 5) -> list[list[float]]:
    """Google's encoded polyline, which is how Ola sends a route's line.

    Ola encodes at five decimals, Google's own default. A string that is not a
    polyline raises ValueError, so a garbled answer fails where it can be seen
    instead of drawing a road somewhere else.
    """
    factor = 10**precision
    coordinates = []
    index = lat = lon = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            result = shift = 0
            while True:
                if index >= len(encoded):
                    raise ValueError("polyline ends in the middle of a coordinate")
                byte = ord(encoded[index]) - 63
                index += 1
                if not 0 <= byte < 64:
                    raise ValueError("polyline has a character outside its alphabet")
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coordinates.append([lat / factor, lon / factor])
    return coordinates


def _quotable(text, key) -> str:
    """`text`, if it is safe to store and log; otherwise a stand-in.

    Ola's error bodies and Python's network errors are worth keeping -- they
    are how a bad key is told from a bad coordinate -- but either can quote the
    request back, and the request carries the key. Anything that names the key
    or Ola's host is withheld whole rather than half-redacted.
    """
    text = str(text)
    if (key and key in text) or "olamaps.io" in text:
        return "(withheld: it quoted the request)"
    return text


def _post(url: str) -> dict:
    """One POST to Ola, decoded.

    olamaps._fetch's error handling, for the method Directions wants. A 429 is
    RateLimited so the caller can back off; every other failure -- an HTTP
    error, no network, a body that is not JSON -- is SnapUnavailable with a
    short reason. The URL never goes in a message, because the key is in it,
    and nothing is chained `from exc`: a chained exception prints its own
    message in any traceback that logs this one.
    """
    key = olamaps.api_key()
    request = urllib.request.Request(url, method="POST", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        detail = _quotable(detail, key)
        if exc.code == 429:
            raise olamaps.RateLimited("rate limited by Ola: %s" % detail) from None
        raise olamaps.SnapUnavailable("HTTP %s from Ola: %s" % (exc.code, detail)) from None
    except Exception as exc:
        raise olamaps.SnapUnavailable(
            "%s: %s" % (type(exc).__name__, _quotable(exc, key))
        ) from None

    try:
        return json.loads(body)
    except ValueError:
        raise olamaps.SnapUnavailable("Ola returned a body that is not JSON") from None


def _batches(points):
    """Runs of at most POINTS_PER_CALL that share their end points, so no leg is
    lost at a seam."""
    step = POINTS_PER_CALL - 1
    for start in range(0, len(points) - 1, step):
        batch = points[start : start + POINTS_PER_CALL]
        if len(batch) >= 2:
            yield batch


def _directions(batch):
    """Metres along the road through one batch, in order, and the road drawn.

    Every leg has to be there with a distance. A route that comes back with a
    leg missing is refused rather than added up: it would be short by a stretch
    nobody can see.
    """
    key = olamaps.api_key()
    if not key:
        raise olamaps.SnapUnavailable("no Ola key configured")
    params = {
        "origin": "%s,%s" % batch[0],
        "destination": "%s,%s" % batch[-1],
    }
    middle = batch[1:-1]
    if middle:
        params["waypoints"] = "|".join("%s,%s" % point for point in middle)
    params["api_key"] = key
    payload = _post(DIRECTIONS_URL + "?" + urllib.parse.urlencode(params, safe="|,"))

    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        status = payload.get("status") if isinstance(payload, dict) else None
        raise olamaps.SnapUnavailable("Ola returned no route (%s)" % _quotable(status, key)[:60])
    route = routes[0]

    legs = route.get("legs")
    if not isinstance(legs, list) or len(legs) != len(batch) - 1:
        raise olamaps.SnapUnavailable(
            "Ola returned %s legs for %d points"
            % (len(legs) if isinstance(legs, list) else "no", len(batch))
        )
    metres = 0.0
    for leg in legs:
        distance = leg.get("distance") if isinstance(leg, dict) else None
        if isinstance(distance, bool):
            distance = None
        try:
            distance = float(distance)
        except (TypeError, ValueError):
            raise olamaps.SnapUnavailable("Ola returned a leg with no distance") from None
        if not math.isfinite(distance) or distance < 0:
            raise olamaps.SnapUnavailable("Ola returned a leg with no distance")
        metres += distance

    try:
        path = decode_polyline(route.get("overview_polyline"))
    except (TypeError, ValueError):
        raise olamaps.SnapUnavailable("Ola returned a route line that does not decode") from None
    return metres, path


def one_place(points) -> bool:
    """Whether every point stands in one place -- typically a day logged in and
    out at the office with no call between. There is no road from a place to
    itself: that is 0 km, and no request is spent to be told so."""
    return len({(round(p["latitude"], 5), round(p["longitude"], 5)) for p in points}) == 1


def measure(points) -> tuple[float, list[list[float]]]:
    """The road km through `points`, in their order, and the road drawn.

    `points` is what key_points returns. Sent POINTS_PER_CALL at a time with
    the seams shared and the requests spaced like the snapper's; the km is
    every leg added up and rounded to ten metres. Raises RateLimited when Ola
    is throttling and SnapUnavailable for anything else it could not do --
    never a half answer.
    """
    coordinates = [(point["latitude"], point["longitude"]) for point in points]
    if len(coordinates) < 2:
        raise olamaps.SnapUnavailable("fewer than two points to route through")
    if one_place(points):
        return 0.0, [list(coordinates[0])]

    metres = 0.0
    path = []
    for number, batch in enumerate(_batches(coordinates)):
        if number:
            time.sleep(BATCH_PAUSE_SECONDS)
        batch_metres, batch_path = _directions(batch)
        metres += batch_metres
        # Each batch starts where the one before it ended, so its first point
        # is the previous last one: drawn once, not twice.
        if path and batch_path and batch_path[0] == path[-1]:
            batch_path = batch_path[1:]
        path.extend(batch_path)
    return round(metres / 1000.0, 2), path


# -- the stored cells ---------------------------------------------------------


def settled(day, now) -> bool:
    """Whether `day` is over for good: SETTLE_HOURS past the midnight that ends
    it. Today never is."""
    ends = timezone.make_aware(
        datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time.min),
        timezone.get_current_timezone(),
    )
    return now >= ends + datetime.timedelta(hours=SETTLE_HOURS)


def rows(engineer_ids, days) -> dict:
    """The stored rows for a grid, by (engineer id, day) -- without their road
    lines, which only the map of one day needs and a cycle's worth of which
    would be read for nothing on every poll."""
    return {
        (row.engineer_id, row.day): row
        for row in RouteKmDay.objects.filter(
            engineer_id__in=list(engineer_ids), day__in=list(days)
        ).defer("path")
    }


def is_pending(row, now) -> bool:
    """Whether a cell is waiting on Ola -- what the report's `pending` counts.

    A cell never read, or read and not measured, is. So is a failed one whose
    retry is due; one that failed in the last FAILED_RETRY_MINUTES is left
    alone, so a failure does not keep the report polling.
    """
    if row is None or row.status == "pending":
        return True
    if row.final or row.status != "failed":
        return False
    return not (
        row.measured_at
        and row.measured_at > now - datetime.timedelta(minutes=FAILED_RETRY_MINUTES)
    )


def is_rechecking(row, now) -> bool:
    """Whether a measured cell of today is due to be read again -- what the
    report's `rechecking` counts, so it keeps asking until today is read.

    Today's places move as the day goes on: a new check-in, the Logout. The km
    it has stays on screen meanwhile. Only today: an earlier open day changes
    only by a late fix or tap, and is read again hourly whenever the report
    asks (EARLIER_RECHECK_MINUTES); counting those too would keep a large
    team's report polling for ever.
    """
    if row is None or row.final or row.status not in ("ready", "no_points"):
        return False
    if row.day != timezone.localdate(now):
        return False
    return not (
        row.checked_at and row.checked_at > now - datetime.timedelta(minutes=RECHECK_MINUTES)
    )


def _store(engineer_id, day, **fields):
    """Write one cell, as its own statement, so a poll that is cut off keeps
    every cell it finished.

    Two polls can reach a new cell at once. Neither finds a row to update, both
    create, and the unique constraint refuses the second -- which then has a
    row to update after all.
    """
    if RouteKmDay.objects.filter(engineer_id=engineer_id, day=day).update(**fields):
        return
    try:
        with transaction.atomic():
            RouteKmDay.objects.create(engineer_id=engineer_id, day=day, **fields)
    except IntegrityError:
        RouteKmDay.objects.filter(engineer_id=engineer_id, day=day).update(**fields)


def _cooling_down(now) -> bool:
    """Whether Ola has asked any worker to wait in the last COOLDOWN_SECONDS.

    This process's own clock first; then the database, where a refused request
    is written on its cell, so the other workers wait too instead of each
    finding out with a request of its own.
    """
    if time.monotonic() < _cooldown_until:
        return True
    return RouteKmDay.objects.filter(
        error=RATE_LIMITED,
        measured_at__gte=now - datetime.timedelta(seconds=COOLDOWN_SECONDS),
    ).exists()


def _claim(engineer_id, day, row, now) -> str:
    """Take a cell for measuring: "ok", "taken" by another worker, or "full"
    when the minute's allowance is spent.

    The claim is the cell's measured_at set to now, and only one worker gets it
    from a row that was not measured in the last minute. The allowance is
    counted with every claim in the minute -- the ones still waiting on Ola
    included -- before and again after taking it, so two workers polling at
    once cannot each spend the whole of it; a claim that lands past it is
    given back.
    """
    minute_ago = now - datetime.timedelta(seconds=60)
    if RouteKmDay.objects.filter(measured_at__gte=minute_ago).count() >= per_minute():
        return "full"
    if row is None:
        # A cell never read has no row yet to claim: made, pending, only if it
        # is still absent. Not through _store, which would overwrite the row
        # another worker may have measured since this one looked.
        try:
            with transaction.atomic():
                RouteKmDay.objects.get_or_create(engineer_id=engineer_id, day=day)
        except IntegrityError:
            pass
    cell = RouteKmDay.objects.filter(engineer_id=engineer_id, day=day)
    before = cell.values_list("measured_at", flat=True).first()
    took = cell.filter(Q(measured_at__isnull=True) | Q(measured_at__lt=minute_ago)).update(
        measured_at=now
    )
    if not took:
        return "taken"
    if RouteKmDay.objects.filter(measured_at__gte=minute_ago).count() > per_minute():
        cell.filter(measured_at=now).update(measured_at=before)
        return "full"
    return "ok"


def refresh(
    engineer_ids, days, now=None, work_limit=30, measure_limit=3, time_budget=8.0
) -> int:
    """Bring a grid of cells up to date, as far as one request should, and say
    how many it asked Ola to measure -- the ones that failed included, since
    they spent a request and count against the minute all the same.

    Cells are taken in this order: never measured first, then ones whose
    points may have moved, then failed days due a retry -- each oldest day
    first, then by engineer -- passing over any that are final, any read in the
    last RECHECK_MINUTES (a day before today: EARLIER_RECHECK_MINUTES) and any
    that failed in the last FAILED_RETRY_MINUTES.
    At most `work_limit` are read. A cell with fewer than two points, or all of
    them in one place, needs no Ola at all, and one whose points are the ones
    it was measured through keeps its km. The rest are measured -- at most
    `measure_limit`, within `time_budget` seconds, and only while Ola is not
    making anyone wait and the minute's allowance lasts. Once nothing more can
    be measured the reading stops too: whatever is not reached is left exactly
    as it was, still pending, first in line for the next poll.
    """
    global _cooldown_until

    now = now or timezone.now()
    started = time.monotonic()
    engineers = Employee.objects.in_bulk(list(set(engineer_ids)))
    ids = sorted(engineers)
    days = sorted(set(days))
    stored = rows(ids, days)

    today = timezone.localdate(now)
    recheck_after = now - datetime.timedelta(minutes=RECHECK_MINUTES)
    earlier_recheck_after = now - datetime.timedelta(minutes=EARLIER_RECHECK_MINUTES)
    retry_after = now - datetime.timedelta(minutes=FAILED_RETRY_MINUTES)

    def rank(row):
        if row is None or row.status == "pending":
            return 0
        return 2 if row.status == "failed" else 1

    due = []
    for day in days:
        for engineer_id in ids:
            row = stored.get((engineer_id, day))
            if row is not None:
                if row.final:
                    continue
                read_after = earlier_recheck_after if day < today else recheck_after
                if row.checked_at and row.checked_at > read_after:
                    continue
                if row.status == "failed" and row.measured_at and row.measured_at > retry_after:
                    continue
            due.append((rank(row), day, engineer_id, row))
    due.sort(key=lambda item: item[:3])
    due = due[:work_limit]

    measured = 0
    for _, day, engineer_id, row in due:
        if (
            measured >= measure_limit
            or time.monotonic() - started >= time_budget
            or _cooling_down(now)
        ):
            # Nothing more will be measured in this request, and the cells left
            # are read again by the next poll anyway.
            break

        points = key_points(engineers[engineer_id], day)
        mark = signature(points)
        final = settled(day, now)

        if len(points) < 2:
            _store(
                engineer_id,
                day,
                status="no_points",
                km=None,
                path=[],
                points=points,
                signature=mark,
                error="",
                attempts=0,
                checked_at=now,
                final=final,
            )
            continue

        if row is not None and row.status == "ready" and row.signature == mark:
            # Nothing has moved since it was measured, so the km still holds.
            _store(engineer_id, day, points=points, checked_at=now, final=final)
            continue

        if one_place(points):
            # No road to measure, and no request spent saying so.
            _store(
                engineer_id,
                day,
                status="ready",
                km=0.0,
                path=[[points[0]["latitude"], points[0]["longitude"]]],
                points=points,
                signature=mark,
                error="",
                attempts=0,
                checked_at=now,
                final=final,
            )
            continue

        claim = _claim(engineer_id, day, row, now)
        if claim == "full":
            # The minute's allowance is spent: nothing more will be measured now.
            break
        if claim == "taken":
            # Another worker is measuring this one.
            continue

        try:
            km, path = measure(points)
        except olamaps.RateLimited as exc:
            _cooldown_until = time.monotonic() + COOLDOWN_SECONDS
            if row is not None and row.status == "failed":
                # A failed day keeps its own reason and its hour to the next
                # retry: the claim is given back rather than the wait written
                # over them. This process waits; the others find out from Ola.
                RouteKmDay.objects.filter(engineer_id=engineer_id, day=day, measured_at=now).update(
                    measured_at=row.measured_at
                )
            else:
                # Written down, so every worker waits; the cell keeps what it had.
                _store(engineer_id, day, error=RATE_LIMITED)
            logger.warning(
                "route km: Ola is rate limiting, asking nothing for %d s: %s",
                COOLDOWN_SECONDS,
                exc,
            )
            break
        except olamaps.SnapUnavailable as exc:
            measured += 1
            attempts = (row.attempts if row is not None and row.status == "failed" else 0) + 1
            logger.warning("route km unavailable for engineer %s on %s: %s", engineer_id, day, exc)
            _store(
                engineer_id,
                day,
                status="failed",
                km=None,
                path=[],
                points=points,
                signature=mark,
                error=str(exc)[:200],
                attempts=attempts,
                checked_at=now,
                measured_at=now,
                # A settled day that has failed every try stays failed.
                final=final and attempts >= MAX_FAILED_ATTEMPTS,
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
            signature=mark,
            error="",
            attempts=0,
            checked_at=now,
            measured_at=now,
            final=final,
        )
    return measured
