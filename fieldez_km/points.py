"""Where each check-in was, by FieldEZ: the FIELDEZ table's points.

ROUTE KM routes an engineer's day through the places they marked on the phone:
Login, every Check in, Logout. FIELDEZ is the same day with the one difference
the office asked for (14 Oct 2026): a check-in stands where FieldEZ recorded
its work order's "Problem Resolution", not where the phone was when Check In
was pressed. Login and Logout are ROUTE KM's own, taken from
route_km.key_points as they are, so the two tables cannot place them apart.

A check-in FieldEZ has no place for is left out -- never put back at the
phone's fix -- and listed in `missing` with the reason. One whose WO the worker
has not read yet makes the day wait: measured without it, the day would come
out short and look finished.

What FieldEZ said is read from FieldezWorkOrder, which the worker fills over
the API. Every Problem Resolution is held to clean_resolution here as well as
when it is posted, so a row that reached the table some other way places
nothing it should not.
"""

from __future__ import annotations

import datetime
import itertools
import math
import re

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from cases import route_km
from cases.models import Case, CaseVisit

from .models import FieldezWorkOrder

# A WO as people and systems write it: "WO-036165368", "wo036165368",
# "WO 036165368". "-1" on the end is a revisit, a different WO. The worker
# (fieldez_km_worker/src/resolve.js, woKey) keeps the same rule, so the two
# sides agree on which WO is which. [0-9], not \d: Python's \d also takes other
# scripts' digits, and JavaScript's does not.
_WHITESPACE = re.compile(r"\s+")
_WO = re.compile(r"WO-?([0-9]{6,})(-[0-9]+)?")
_WO_KEY_LENGTH = FieldezWorkOrder._meta.get_field("wo_key").max_length

# What a Problem Resolution may say about itself, and how much of it is kept.
MAX_RESOLUTIONS = 50
USER_LENGTH = 120
ADDRESS_LENGTH = 300

# What PostgreSQL cannot keep in a text column or in jsonb: NUL, and half of a
# surrogate pair (json.loads joins a whole pair into one character, so only a
# lone half is left). One of them would make the save a 500 and hold up every
# result posted after it.
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")

# How often a WO is read again from FieldEZ. One with no Problem Resolution yet
# may get one later in the day, and one FieldEZ did not have may appear, so
# both are asked about again every half hour while their last check-in is
# recent -- the place usually comes within hours -- and once a day after
# STALE_AFTER, for as long as a check-in on them is in the window; a read that
# failed, hourly, up to route_km.MAX_FAILED_ATTEMPTS times in a row; one
# already found only when a check-in has been made on it since it was read.
FIELDEZ_REFETCH_MINUTES = 30
FAILED_REFETCH_MINUTES = 60
STALE_AFTER = datetime.timedelta(hours=48)
STALE_REFETCH_MINUTES = 24 * 60
# Half the worker's 30-minute pass (see _read_before).
READ_SLACK = datetime.timedelta(minutes=15)

# The most work orders one answer asks the worker to read.
MAX_WANTED = 200

# The furthest back, in IST days counting today, the worker can be asked to
# read (/wanted/'s ceiling). A check-in older than that will never be read, so
# it is left out as "not_read" rather than kept waiting for a read that cannot
# come -- a day waiting for ever would never settle.
MAX_WANTED_DAYS = 31


def storable(text):
    """`text` without what the database cannot store (_UNSTORABLE). Only ever
    asked of text that is shown -- a name, an address, an error -- which is
    worth keeping without it, rather than turning the whole read away."""
    return _UNSTORABLE.sub("", text)


def wo_key(text):
    """The WO `text` names, as "WO-<digits>" with any revisit suffix, or None.

    Upper-cased and every space removed; what is left must be WO, an optional
    hyphen, at least six digits and an optional "-<digits>". Anything else -- a
    blank, a serial number -- is not a WO. A key longer than the table's column
    is not one either: it could never be stored, so it could never be read.
    """
    if not isinstance(text, str):
        return None
    match = _WO.fullmatch(_WHITESPACE.sub("", text).upper())
    if not match:
        return None
    key = "WO-%s%s" % (match.group(1), match.group(2) or "")
    return key if len(key) <= _WO_KEY_LENGTH else None


# -- a Problem Resolution -----------------------------------------------------


def _moment(text):
    """An ISO-8601 time that says its own offset, in UTC; None for anything
    else. A time with no offset could be IST or UTC, five and a half hours
    apart, so it is not guessed at. Nor is one outside 2000-2099, the worker's
    own bounds (resolve.js): near the year's end, IST is past 9999, and the
    day's every look would fail."""
    if not isinstance(text, str):
        return None
    try:
        moment = parse_datetime(text.strip())
    except ValueError:
        return None
    if moment is None or moment.utcoffset() is None:
        return None
    try:
        moment = moment.astimezone(datetime.timezone.utc)
    except (OverflowError, ValueError):
        return None
    return moment if 2000 <= moment.year < 2100 else None


def _coordinate(value, limit):
    """A latitude (limit 90) or longitude (limit 180) as a float, or None.

    A number, finite, within the limit and not 0: FieldEZ writes 0 for "no
    location", and a fix that really is on the equator or the prime meridian
    is not in Tamil Nadu.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    if not math.isfinite(value) or value == 0 or not -limit <= value <= limit:
        return None
    return value


def clean_resolution(entry):
    """One Problem Resolution as it is kept, or None if it cannot place anything.

    {"at": ISO-8601 in UTC, "user", "latitude", "longitude", "address"}. The
    time must carry its offset and the place must be a real one (see
    _coordinate); the name and the address are optional text, without what
    the database cannot store (storable), cut to USER_LENGTH and
    ADDRESS_LENGTH.
    """
    if not isinstance(entry, dict):
        return None
    moment = _moment(entry.get("at"))
    latitude = _coordinate(entry.get("latitude"), 90)
    longitude = _coordinate(entry.get("longitude"), 180)
    if moment is None or latitude is None or longitude is None:
        return None
    user = entry.get("user")
    address = entry.get("address")
    user = "" if user is None else user
    address = "" if address is None else address
    if not isinstance(user, str) or not isinstance(address, str):
        return None
    return {
        "at": moment.isoformat(),
        "user": storable(user).strip()[:USER_LENGTH],
        "latitude": latitude,
        "longitude": longitude,
        "address": storable(address).strip()[:ADDRESS_LENGTH],
    }


def choose_resolution(resolutions, checked_in_at):
    """The Problem Resolution that places a check-in made at `checked_in_at`,
    or None when the WO has none with a place.

    A WO visited on two days can be resolved on both. One on the check-in's own
    IST day is preferred; among those -- or among all, if none is -- the one
    nearest in time to the check-in, before or after it, the earlier winning a
    tie. Returns the entry as clean_resolution gives it.
    """
    usable = []
    for entry in resolutions if isinstance(resolutions, list) else []:
        clean = clean_resolution(entry)
        if clean is not None:
            usable.append((parse_datetime(clean["at"]), clean))
    if not usable:
        return None
    day = timezone.localtime(checked_in_at).date()
    same_day = [item for item in usable if timezone.localtime(item[0]).date() == day]
    _, chosen = min(
        same_day or usable,
        key=lambda item: (abs((item[0] - checked_in_at).total_seconds()), item[0]),
    )
    return chosen


# -- the points ---------------------------------------------------------------


def _missing_reason(order, in_window):
    """Why a check-in is left out, or None while it waits on the worker.

    `order` is its WO's row (None if never read); `in_window` whether its day
    is still inside the days the worker can be asked about (readable). Outside
    them nothing more will be read: a WO never read is "not_read", and a failed
    read is final however few times it failed.
    """
    if order is not None:
        if order.status == "not_found":
            return "not_in_fieldez"
        if order.status == "no_resolution":
            return "no_problem_resolution"
        if order.status == "failed" and (
            order.attempts >= route_km.MAX_FAILED_ATTEMPTS or not in_window
        ):
            return "fieldez_failed"
    if not in_window:
        return "not_read"
    return None


def readable(day, now) -> bool:
    """Whether `day` is still inside the MAX_WANTED_DAYS the worker can be asked
    to read: its WOs can still be read, and read again."""
    return day >= timezone.localdate(now) - datetime.timedelta(days=MAX_WANTED_DAYS - 1)


def _when(value):
    """A point's time: a datetime while the list is built, an ISO string after."""
    return value if isinstance(value, datetime.datetime) else parse_datetime(value)


def fieldez_points(engineer, day, now=None):
    """The FIELDEZ points of `engineer`'s `day`: (points, missing, waiting, checkins).

      points    Login and Logout exactly as route_km.key_points gives them,
                office stand-ins included, and each check-in at its WO's
                Problem Resolution -- in the order the day happened, as
                key_points orders it, "at" as ISO strings. A check-in's point
                is {"kind", "at" (the check-in's own time), "latitude",
                "longitude", "case_number", "wo", "office": False,
                "resolved_at", "address", "user"}.
      missing   the check-ins left out, in time order:
                [{"case_number", "wo", "reason"}], reason one of no_wo,
                not_in_fieldez, no_problem_resolution, fieldez_failed, and
                not_read (a day older than MAX_WANTED_DAYS whose WO was never
                read: the worker can no longer be asked about it).
      waiting   how many check-ins are waiting on the worker: their WO has no
                row yet, a pending one, or one whose reads failed and will be
                tried again -- only while the day is within MAX_WANTED_DAYS.
      checkins  how many check-ins the day had, all of them.

    The check-ins are route_km._checkins_for_day's, so a check-in is credited
    to whoever made it exactly as ROUTE KM credits it.
    """
    now = now or timezone.now()
    in_window = readable(day, now)
    points = [
        point
        for point in route_km.key_points(engineer, day)
        if point["kind"] in ("login", "logout")
    ]
    checkins = route_km._checkins_for_day(engineer, day)

    numbers = {number for _, _, _, number in checkins if number}
    refs = (
        dict(Case.objects.filter(case_number__in=numbers).values_list("case_number", "external_ref"))
        if numbers
        else {}
    )
    keys = {number: wo_key(ref) for number, ref in refs.items()}
    known = {key for key in keys.values() if key}
    orders = (
        {order.wo_key: order for order in FieldezWorkOrder.objects.filter(wo_key__in=known)}
        if known
        else {}
    )

    missing = []
    waiting = 0
    for at, _, _, number in checkins:
        key = keys.get(number)
        if key is None:
            missing.append((at, {"case_number": number, "wo": None, "reason": "no_wo"}))
            continue
        order = orders.get(key)
        if order is not None and order.status == "found":
            chosen = choose_resolution(order.resolutions, at)
            if chosen is None:
                missing.append(
                    (at, {"case_number": number, "wo": key, "reason": "no_problem_resolution"})
                )
                continue
            points.append(
                {
                    "kind": "checkin",
                    "at": at,
                    "latitude": chosen["latitude"],
                    "longitude": chosen["longitude"],
                    "case_number": number,
                    "wo": key,
                    "office": False,
                    "resolved_at": chosen["at"],
                    "address": chosen["address"],
                    "user": chosen["user"],
                }
            )
            continue
        reason = _missing_reason(order, in_window)
        if reason is None:
            # Not read yet: no row, a pending one, a failed read that will be
            # tried again -- or a status this code does not know, which is
            # not taken as an answer either.
            waiting += 1
            continue
        missing.append((at, {"case_number": number, "wo": key, "reason": reason}))

    # route_km's own order: by time, and a Login, a call and a Logout that
    # share a moment in the order a day runs.
    points.sort(key=lambda point: (_when(point["at"]), route_km._ORDER[point["kind"]]))
    for point in points:
        if isinstance(point["at"], datetime.datetime):
            point["at"] = point["at"].isoformat()
    missing.sort(key=lambda item: item[0])
    return points, [entry for _, entry in missing], waiting, len(checkins)


# -- a Problem Resolution pressed away from the call --------------------------

# How far from where the engineer checked in a Problem Resolution may stand
# before the FIELDEZ table marks it (asked 16 Oct 2026). Pressed at the
# customer's it stands metres away. One pressed later from somewhere else --
# on 13 Oct, an Anna Nagar call's at 8:30 PM from the next call in Madha
# Nagar, 11.7 km off -- stands where that was, and the road drawn through it
# is not the one the engineer took.
AWAY_KM = 1.0


def away_from_checkin(fieldez, route):
    """The FIELDEZ check-ins placed more than AWAY_KM from where the engineer
    checked in, in the day's order: [{"case_number", "wo", "at",
    "resolved_at", "address", "away_km"}].

    `fieldez` is a FIELDEZ day's points and `route` the same day's ROUTE KM
    points. A check-in's own place is ROUTE KM's point for it -- the one with
    the same case number and moment, where the day timeline shows it. A
    check-in ROUTE KM has no point for is not judged.
    """
    # Imported here, as route_km imports from views: views is the heavy end.
    from cases.views import haversine_km

    checked_in = {}
    for point in route or []:
        if point.get("kind") == "checkin":
            checked_in[(point.get("case_number"), _when(point["at"]))] = point
    away = []
    for point in fieldez or []:
        if point.get("kind") != "checkin":
            continue
        own = checked_in.get((point.get("case_number"), _when(point["at"])))
        if own is None:
            continue
        km = haversine_km(own["latitude"], own["longitude"], point["latitude"], point["longitude"])
        if km > AWAY_KM:
            away.append(
                {
                    "case_number": point.get("case_number"),
                    "wo": point.get("wo"),
                    "at": point["at"],
                    "resolved_at": point.get("resolved_at"),
                    "address": point.get("address") or "",
                    "away_km": round(km, 1),
                }
            )
    return away


# -- what the worker should read ----------------------------------------------


def _read_before(fetched_at, now, minutes) -> bool:
    """Whether a read made at `fetched_at` is at least `minutes` old, give or
    take READ_SLACK: fetched_at is when the worker posted, near the end of its
    pass, and the next pass asks a pass's length after the last one started.
    Without the slack a 30-minute read would come every other pass, and a
    daily one drift later each day until it missed one."""
    return fetched_at is None or fetched_at <= now - datetime.timedelta(minutes=minutes) + READ_SLACK


def needs_read(order, latest_checkin, now) -> bool:
    """Whether the worker should read a WO from FieldEZ now.

    Never read, or still pending; or read and found to have no Problem
    Resolution, or not to be in FieldEZ, FIELDEZ_REFETCH_MINUTES ago or more --
    STALE_REFETCH_MINUTES once its latest check-in is older than STALE_AFTER;
    or a read that failed, FAILED_REFETCH_MINUTES ago or more, while it has
    failed fewer than route_km.MAX_FAILED_ATTEMPTS times in a row; or found,
    but read before its latest check-in -- a later visit can bring a later
    Problem Resolution.
    """
    if order is None:
        return True
    if order.status == "found":
        return order.fetched_at is None or order.fetched_at < latest_checkin
    if order.status == "failed":
        return order.attempts < route_km.MAX_FAILED_ATTEMPTS and _read_before(
            order.fetched_at, now, FAILED_REFETCH_MINUTES
        )
    if order.status in ("not_found", "no_resolution") and latest_checkin < now - STALE_AFTER:
        return _read_before(order.fetched_at, now, STALE_REFETCH_MINUTES)
    return _read_before(order.fetched_at, now, FIELDEZ_REFETCH_MINUTES)


def _tier(order, first, since) -> int:
    """Where a WO due a read stands in /wanted/'s list: 0 never read (or still
    pending), 1 read before and due again, 2 its last read failed -- after the
    re-reads, so WOs that fail again together cannot stop the worker's pass
    (five failures in a row) before it gets to them. A failed read whose first
    check-in leaves the window within two days goes with the never-read ones:
    left behind them, its day would settle without it."""
    if order is None or order.status == "pending":
        return 0
    if order.status == "failed":
        return 0 if first < since + EDGE else 2
    return 1


# How close to leaving the window a failed read's check-in is let jump the queue.
EDGE = datetime.timedelta(days=2)


def wanted_work_orders(since, now, branches, limit=MAX_WANTED) -> list[str]:
    """The WOs the worker should read, at most `limit`, in this order:

      never read, oldest first check-in first -- each is read once before
      anything is read again, and none is left to age out of the window --
      with any failed read about to leave the window;
      then the ones read before and due again, newest check-in first --
      today's calls are the ones whose place is about to be entered;
      then the other failed reads, oldest first (see _tier).

    Oldest first throughout, a few hundred WOs FieldEZ never places would take
    every read, pass after pass, and a new check-in would never be read
    (found by review on 15 Oct 2026).

    Every WO checked into since `since` -- a visit's check-in, and a call
    checked into before visits were kept, from its own columns -- once, if
    needs_read says so. `branches` is get_allowed_branches' answer: a login
    that does not see every branch is asked only about the calls of the
    engineers it can see.
    """
    visits = CaseVisit.objects.filter(checked_in_at__gte=since)
    legacy = Case.objects.filter(reached_at__gte=since).exclude(visits__checked_in_at__gte=since)
    if "All" not in branches:
        visits = visits.filter(
            Q(engineer__branch__in=branches) | Q(case__assigned_to__branch__in=branches)
        )
        legacy = legacy.filter(assigned_to__branch__in=branches)

    spans = {}
    for moment, ref in itertools.chain(
        visits.values_list("checked_in_at", "case__external_ref"),
        legacy.values_list("reached_at", "external_ref"),
    ):
        key = wo_key(ref)
        if key is None:
            continue
        first, last = spans.get(key, (moment, moment))
        spans[key] = (min(first, moment), max(last, moment))

    # Asked in slices: a month of a large team can be more WOs than SQLite
    # takes variables in one statement.
    keys = sorted(spans)
    orders = {}
    for start in range(0, len(keys), 500):
        for order in FieldezWorkOrder.objects.filter(wo_key__in=keys[start : start + 500]):
            orders[order.wo_key] = order

    due = []
    for key, (first, last) in spans.items():
        order = orders.get(key)
        if not needs_read(order, last, now):
            continue
        tier = _tier(order, first, since)
        due.append((tier, -last.timestamp() if tier == 1 else first.timestamp(), key))
    due.sort()
    return [key for _, _, key in due[:limit]]
