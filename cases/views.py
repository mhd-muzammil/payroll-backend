import logging
import math
import re
from bisect import bisect_left, bisect_right
from datetime import datetime, time, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db import IntegrityError, connection, transaction
from django.db.models import OuterRef, Q, Subquery
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Case, CaseVisit, DutySession, EngineerAlias, EngineerScorecard, LocationPing
from .models import EngineerScorecardDay
from .models import PhoneStatus
from .serializer import CaseSerializer, LocationPingSerializer, LiveEngineerSerializer
from .pings import (
    MAX_BATCH,
    MAX_EVENTS,
    PingRejected,
    build_ping,
    coerce_number,
    ingest_batch,
    ingest_events,
)
from .tracks import snapped_trail
from . import places
from . import olamaps, route_km
from . import day_cases
from . import km_rule
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

    Put in km_rule.trail_order first, whatever order the caller had them in,
    so fixes sharing a second are walked the same way on every read.
    """
    clean = _usable_pings(sorted(pings, key=km_rule.trail_order))
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


def _trail_km(pings, context=None):
    """Total distance along a trail, in km.

    Low-accuracy fixes are dropped, and so is wander: standing still is 0.00,
    not the sum of however many metres the GPS drifted while nobody moved.

    Which straight lines between the fixes that are left count is decided in
    cases/km_rule.py: all of them, except the trip to a Login and a stretch
    with Location switched off -- and, for what had arrived before that rule
    began, the old rule of skipping the line into every flagged fix.

    Deliberately NOT inferred from the time gap. An engineer standing still
    produces no new rows either, so a 40-minute hole is either 40 minutes of
    untracked driving or 40 minutes of work at one customer, and guessing threw
    away the journey after every long stop.

    `context` is the km_rule.TrailContext the caller loaded for the request;
    without one it is read here. Shared by /live, /path, /day, the roster and
    the app's own duty so the number on an engineer's row is computed exactly
    the same way as the one in their detail view and on their phone.
    """
    pings = sorted(pings, key=km_rule.trail_order)
    moving = _moving_trail(pings)
    if len(moving) < 2:
        return 0.0
    if context is None:
        context = km_rule.context_for(pings)
    rule = km_rule.SegmentRule(pings, context)
    usable = _usable_pings(pings)
    total = 0.0
    for prev, cur in zip(moving, moving[1:]):
        start = rule.counted_from(prev, cur, usable)
        if start is None:
            continue
        total += haversine_km(start.latitude, start.longitude, cur.latitude, cur.longitude)
    return round(total, 2)


class _DayKm:
    """One local day's kilometres, for one engineer or a whole board.

    The one place an engineer-day's km is read: every fix each of them took
    that day, in trail order, and the Logins and phone events the km rule reads
    beside them, handed to _trail_km. The roster, the app's "Travelled today"
    and the engineer's history all go through here, so the office's KM report,
    the phone and the history cannot come to three figures for one day.

    Fixes are asked for between the day's two instants rather than by
    timestamp__date: the same rows, as a range the (engineer, timestamp) index
    answers directly instead of a date cast of every row's timestamp.

    Three queries whatever the number of engineers. `kinds` widens the events
    for a caller that reads them for something else as well; the km come out
    the same, since the rule reads only what RULE_EVENT_KINDS holds.
    """

    def __init__(self, engineer_ids, day, kinds=km_rule.RULE_EVENT_KINDS):
        ids = list(engineer_ids)
        start, end = km_rule.day_bounds(day)
        self.contexts = km_rule.load_contexts(ids, start, end, kinds=kinds)
        self.trails = {}
        for ping in LocationPing.objects.filter(
            engineer_id__in=ids, timestamp__gte=start, timestamp__lt=end
        ).order_by("timestamp", "id"):
            self.trails.setdefault(ping.engineer_id, []).append(ping)

    def km(self, engineer_id):
        return _trail_km(self.trails.get(engineer_id, []), self.contexts[engineer_id])


def _js_round2(value):
    """Rounded to two decimals as the office's KM report rounds a total:
    JavaScript's Math.round(x * 100) / 100, halves up. Python's round() takes
    a half to even and lands a cent lower on 2.125."""
    scaled = value * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    return whole / 100


def _counted_across(rule, moving, usable, start, end):
    """Whether the kilometres take the straight line across start..end, the
    span of an entry on the day rail: read off the kept pair around it --
    the line _trail_km adds -- by the same rule, so the board says "counted
    as a straight line" only where the number counted it. False while no
    line runs across it yet."""
    index = bisect_right([ping.timestamp for ping in moving], start)
    if index == 0 or index == len(moving) or moving[index].timestamp < end:
        return False
    origin = rule.counted_from(moving[index - 1], moving[index], usable)
    return origin is not None and origin.timestamp <= start


# Below this a gap is the ordinary jitter of a phone handing the GPS back and
# forth, not a stretch anybody lost.
MIN_DARK_MINUTES = 5

# How far apart the two fixes either side of a silence have to be before that
# silence is worth an entry.
#
# A quiet phone is not news by itself: an engineer standing at a customer for
# two hours produces no fixes either, because the phone only reports every ten
# metres of movement -- and the stop detector already draws that as "Waiting".
# What is worth saying is a silence they came out of somewhere else: they
# travelled and nothing watched it. Half a kilometre is comfortably past any
# GPS wander while parked.
UNTRACKED_MOVE_METERS = 500

# How far from an entry a fix may be and still be called its place. A quarter of
# an hour of driving is a different town, and "view on map" has to open where
# the thing happened or not at all.
NEAREST_FIX_SECONDS = 15 * 60


def duration_words(minutes):
    """"4h 27m", "12 min" -- the way somebody would say it out loud."""
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest}m" if rest else f"{hours}h"


def _offline_event(first, last):
    """BOTH ends of a stretch the phone held its fixes before sending them.

    Measured from when the first held fix was TAKEN to when the last one was
    finally DELIVERED -- that is the window the office saw nothing in.

    Two entries, not one. A hole that opens has to be shown closing, or
    somebody reading the rail at 11:11 has to work out for themselves whether
    11:30 was still dark. The moment the fixes were sent is a fact we hold, so
    it is written down rather than left to be inferred.

    Named for what is known, not for the network. The phone may have had no
    network; it may equally have been in a pocket with the app asleep, its
    fixes waiting for the app to wake -- and calling that "No network" sent the
    office after a signal that was fine. When the phone reports the network
    itself, the day view leaves this out for its Net entries.

    The second end carries no position on purpose. Where the phone WAS when
    the delivery went through is not where the last held fix was taken, so the
    nearest-fix pass in the day view answers it instead, and leaves it blank
    rather than guessing when nothing is close enough in time.

    Returns a list so the caller can extend() and get nothing for a stretch too
    short to be worth a line.
    """
    if last is None or last.received_at is None:
        return []
    minutes = int((last.received_at - first.timestamp).total_seconds() // 60)
    if minutes < MIN_DARK_MINUTES:
        return []
    return [
        {
            "at": first.timestamp,
            "type": "no_network",
            "label": f"Kept on the phone \u00b7 {duration_words(minutes)}",
            "minutes": minutes,
            "latitude": first.latitude,
            "longitude": first.longitude,
        },
        {
            "at": last.received_at,
            "type": "network_back",
            "label": "Sent from the phone",
            "minutes": minutes,
            "latitude": None,
            "longitude": None,
        },
    ]


# The phone's own events that explain a silence in the fixes: with one of
# these in it, the rail lets the phone say what happened instead of guessing.
# app_opened is not one of them -- it only says the app had been closed.
EXPLAINING_KINDS = (
    "gps_off",
    "gps_on",
    "net_off",
    "net_on",
    "tracker_stopped",
    "tracker_started",
)

# How far either side of a gap the phone's word about it may fall and still be
# about it. Events carry the phone's own clock, and the fixes either side of a
# gap are seconds, not minutes, from when it opened and closed.
EVENT_SLACK = timedelta(minutes=1)
# How close to the first fix after a gap the app must have been opened for the
# gap to have been the app being closed.
APP_OPENED_SLACK = timedelta(minutes=2)
# A network that dropped and came back within this was a blip, not an outage,
# and forty blips nobody reads are the same as none.
NET_BLIP = timedelta(minutes=2)

# Each "off" the phone reports, with the "on" that ends it.
PAIRED_KINDS = (
    ("gps_off", "gps_on"),
    ("net_off", "net_on"),
    ("tracker_stopped", "tracker_started"),
)

PHONE_EVENT_LABELS = {
    "gps_off": "GPS off",
    "gps_on": "GPS on",
    "net_off": "Net off",
    "net_on": "Net on",
    "tracker_stopped": "Phone stopped the app",
    "tracker_started": "App tracking again",
}


def _any_between(moments, start, end):
    """Whether any of the sorted `moments` falls within [start, end]."""
    index = bisect_left(moments, start)
    return index < len(moments) and moments[index] <= end


def _phone_event_entries(events, sessions, pings, day_start, day_end):
    """The rail entries for what the phone said about itself on the day.

    GPS off and on, Net off and on, the phone stopping the app and the app
    tracking again -- each where the phone put it, and how long it lasted
    when both ends are known. Only what happened on the day and on duty, like
    every other entry (Location from just before a Login: _phone_on_duty).
    app_opened has no entry of its own; it names a hole.

    `found` marks GPS the phone found already off (or on) when it looked,
    rather than saw being switched: only a switch takes kilometres away.

    A net_off and net_on under NET_BLIP apart are left out together. The
    phone stopping the app is measured to the app tracking again, or failing
    that to the first fix after it, or to the end of the duty it fell in.
    """
    ordered = sorted(events, key=lambda event: (event.at, event.pk or 0))
    lasted = {}
    for off_kind, on_kind in PAIRED_KINDS:
        opened = None
        for event in ordered:
            if event.kind == off_kind and opened is None:
                opened = event
            elif event.kind == on_kind and opened is not None:
                lasted[id(opened)] = lasted[id(event)] = event.at - opened.at
                opened = None

    entries = []
    for event in ordered:
        label = PHONE_EVENT_LABELS.get(event.kind)
        if label is None:
            continue
        if not (day_start <= event.at < day_end) or not _phone_on_duty(sessions, event):
            continue
        span = lasted.get(id(event))
        if event.kind in ("net_off", "net_on") and span is not None and span < NET_BLIP:
            continue
        if event.kind == "tracker_stopped" and span is None:
            span = _until_tracking_again(event.at, sessions, pings, day_end) - event.at
        minutes = max(0, int(span.total_seconds() // 60)) if span is not None else None
        if event.kind == "tracker_stopped":
            label = f"{label} \u00b7 {duration_words(minutes)}"
        entry = {
            "at": event.at,
            "type": event.kind,
            "label": label,
            "latitude": event.latitude,
            "longitude": event.longitude,
        }
        if minutes is not None:
            entry["minutes"] = minutes
        if event.kind in ("gps_off", "gps_on"):
            entry["found"] = (event.detail or "").strip() in ("found_off", "found_on")
        entries.append(entry)
    return entries


def _net_outages(entries):
    """The network outages the rail itself shows, as (off, on) -- `on` None
    for one still off. Paired from the rail's own Net entries, so a blip left
    off the rail is no outage here either."""
    outages = []
    opened = None
    for entry in sorted(entries, key=lambda e: e["at"]):
        if entry["type"] == "net_off" and opened is None:
            opened = entry["at"]
        elif entry["type"] == "net_on" and opened is not None:
            outages.append((opened, entry["at"]))
            opened = None
    if opened is not None:
        outages.append((opened, None))
    return outages


def _until_tracking_again(moment, sessions, pings, day_end):
    """When the trail picked up again after the phone stopped the app: the
    first fix after it, or else the end of the duty it fell in (now, for one
    still open). With no duty on record at all, the end of the day."""
    for ping in pings:
        if ping.timestamp > moment:
            return ping.timestamp
    now = timezone.now()
    for session in sessions:
        if session.started_at <= moment <= (session.ended_at or now):
            return session.ended_at or now
    return min(now, day_end)


def _on_duty_at(sessions, moment):
    """Was the engineer on duty at that instant?

    True when there are no sessions at all: a day with no session is a fault in
    the record, and hiding every entry would bury it instead of showing it.

    An open session counts up to now, which is what "still on duty" means.
    """
    if not sessions:
        return True
    for session in sessions:
        if session.started_at <= moment <= (session.ended_at or timezone.now()):
            return True
    return False


def _phone_on_duty(sessions, event):
    """On duty at the event's own time -- or, for what the phone says about
    Location, up to km_rule.CLOCK_SKEW before a Login. The phone looks at
    Location when the duty starts and stamps it by its own clock, and the
    board's "GPS off since" (_gps_off_since) reads the event from there: the
    rail has to say what the board does."""
    if _on_duty_at(sessions, event.at):
        return True
    return event.kind in ("gps_off", "gps_on") and any(
        session.started_at - km_rule.CLOCK_SKEW <= event.at < session.started_at
        for session in sessions
    )


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
                # The WO number beside it, because that is what everyone outside
                # Payroll calls this job.
                "case_ref": next(
                    (p.case.external_ref or None for p in reversed(cluster) if p.case_id and p.case),
                    None,
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


def _day_moments(case, visit):
    """The four moments of one call, for one day.

    From the day's own visit row when there is one: that is the record a later
    dispatch cannot overwrite, so it still answers for a day that has already
    happened after the same call has been sent out again. A call dispatched
    before visits existed answers from its own columns.

    Each entry is (field, label, when, punch lat, punch lon). The labels are
    the engineer's own words for the two buttons, and the coordinates are where
    they were standing when they pressed them.
    """
    if visit is not None:
        # Per tap, not all-or-nothing. A row can exist for a day with no punch
        # on it yet -- the sync writes it when the call is pushed, hours before
        # the engineer arrives -- and the case's own column is then the only
        # answer. It is safe to prefer it: the caller keeps only the stamps
        # whose date IS the day being read, so a column a later trip has
        # overwritten simply does not appear.
        checked_in = visit.checked_in_at or case.reached_at
        checked_out = visit.checked_out_at or case.completed_at
        return (
            ("assigned_at", "Case assigned", visit.assigned_at or case.assigned_at, None, None),
            ("started_at", "Left for the call", case.started_at, None, None),
            (
                "reached_at",
                "Check in",
                checked_in,
                visit.punch_in_lat if visit.checked_in_at else case.punch_in_lat,
                visit.punch_in_lon if visit.checked_in_at else case.punch_in_lon,
            ),
            (
                "completed_at",
                "Check out",
                checked_out,
                visit.punch_out_lat if visit.checked_out_at else case.punch_out_lat,
                visit.punch_out_lon if visit.checked_out_at else case.punch_out_lon,
            ),
        )
    return (
        ("assigned_at", "Case assigned", case.assigned_at, None, None),
        ("started_at", "Left for the call", case.started_at, None, None),
        ("reached_at", "Check in", case.reached_at, case.punch_in_lat, case.punch_in_lon),
        ("completed_at", "Check out", case.completed_at, case.punch_out_lat, case.punch_out_lon),
    )


def _trip_day(case, visits=None):
    """The plan day the case's CURRENT trip belongs to.

    Read from the visits when there are any -- that is the only record a later
    dispatch cannot overwrite. Falling back, in order: the plan day the sync
    last stamped, then the day of whatever the engineer last did to the call.
    Returns None when nothing on the case says which day it belongs to, and the
    caller must treat that as "unknown", never as "an earlier day".
    """
    if visits is None:
        visits = list(case.visits.all()) if case.pk else []
    if visits:
        return max(visit.plan_date for visit in visits)
    if case.plan_date:
        return case.plan_date
    for stamp in (case.completed_at, case.reached_at, case.started_at, case.assigned_at):
        if stamp:
            return timezone.localtime(stamp).date()
    return None


def _visit_for(case, day, engineer=None):
    """This call's row for that day, made if it is not there yet."""
    visit, _ = CaseVisit.objects.get_or_create(
        case=case,
        plan_date=day,
        defaults={
            "engineer": engineer or case.assigned_to,
            "assigned_at": case.assigned_at,
        },
    )
    return visit


def _punches_for_day(engineer, day):
    """Every call this engineer punched in or out of on `day`, with where.

    Read from the cases rather than derived from the trail: these are the
    moments the engineer themselves marked, and the coordinates are the ones
    their phone reported at the instant they pressed the button.

    A punch whose position was never captured — no fix at the time — is still
    returned, so the timeline is complete; it simply cannot be drawn.
    """
    out = []

    # The day's visits first. A call sent out again overwrites the columns on
    # the case, so for any call that has been punched since visits existed this
    # is the only place the day's own punches survive.
    recorded = set()
    for visit in (
        CaseVisit.objects.filter(case__assigned_to=engineer)
        .filter(Q(checked_in_at__date=day) | Q(checked_out_at__date=day))
        .select_related("case")
    ):
        recorded.add(visit.case_id)
        for kind, at, lat, lon, accuracy in (
            ("in", visit.checked_in_at, visit.punch_in_lat, visit.punch_in_lon, visit.punch_in_accuracy),
            ("out", visit.checked_out_at, visit.punch_out_lat, visit.punch_out_lon, visit.punch_out_accuracy),
        ):
            if not at or timezone.localtime(at).date() != day:
                continue
            out.append(
                {
                    "kind": kind,
                    "at": at,
                    "case_id": visit.case_id,
                    "case_number": visit.case.case_number,
                    "title": visit.case.title,
                    "latitude": lat,
                    "longitude": lon,
                    "accuracy": accuracy,
                }
            )

    # Anything with no visit row at all -- dispatched before this table existed
    # and never punched since -- still answers from its own columns.
    cases = (
        Case.objects.filter(assigned_to=engineer)
        .exclude(id__in=recorded)
        .only(
            "case_number", "title", "reached_at", "completed_at",
            "punch_in_lat", "punch_in_lon", "punch_in_accuracy",
            "punch_out_lat", "punch_out_lon", "punch_out_accuracy",
        )
    )
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


# Every phone on duty sends a fix every thirty seconds and a heartbeat every
# minute, and the board only asks whether it heard from one in the last ten
# minutes. Writing each of them to the session would be a write per request for
# nothing; one every half minute answers the same question.
CONTACT_EVERY_SECONDS = 30


def _note_contact(employee):
    """Write down that the engineer's phone was just heard from.

    Called only for what proves something is recording: a fix we had not got
    before, and a post to events/ (an empty one is the heartbeat). Not for a
    duty read, a Login or a re-sent fix -- a page asks for its duty every
    minute even when the tracker behind it has stopped, and counting that kept
    a phone that recorded nothing green on the board all day.

    One UPDATE on the open session, skipped while the last one is under
    CONTACT_EVERY_SECONDS old. Never fails the request: this is bookkeeping
    about the phone, and losing a fix over it would be the wrong way round.
    """
    now = timezone.now()
    try:
        with transaction.atomic():
            DutySession.objects.filter(engineer=employee, ended_at__isnull=True).filter(
                Q(last_contact_at__isnull=True)
                | Q(last_contact_at__lt=now - timedelta(seconds=CONTACT_EVERY_SECONDS))
            ).update(last_contact_at=now)
    except Exception:  # noqa: BLE001 - never cost anybody a fix
        logger.exception("could not note contact for engineer %s", employee.pk)


def _contact(session, latest):
    """When this phone was last heard from: its session's own record of that,
    or when its newest fix arrived, whichever is later. None if never."""
    moments = []
    if session is not None and session.last_contact_at is not None:
        moments.append(session.last_contact_at)
    if latest is not None:
        moments.append(km_rule.arrival(latest))
    return max(moments) if moments else None


def _minutes_since(moment, now):
    if moment is None:
        return None
    return max(0, int((now - moment).total_seconds() // 60))


def _gps_off_since(session, events, latest):
    """When Location went off on this duty's phone, if nothing since says it is
    back: no gps_on after it, and no fix taken after it.

    Only for an open duty, and only from its own events -- an old switch-off
    from an earlier duty, never followed by a gps_on because nobody was
    watching by then, must not paint today red. A few minutes before the Login
    still counts: the phone says what it found at Login on its own clock.
    """
    if session is None or session.ended_at is not None:
        return None
    since = session.started_at - km_rule.CLOCK_SKEW
    off = None
    for event in events:
        if event.kind == "gps_off" and event.at >= since:
            off = event
    if off is None:
        return None
    if any(event.kind == "gps_on" and event.at > off.at for event in events):
        return None
    if latest is not None and latest.timestamp > off.at:
        return None
    return off.at


def _no_signal(session, latest, contact, gps_off_since, now):
    """`stale` for an on-duty row: the board's "No signal".

    Nothing heard from the phone for LIVE_WINDOW_MINUTES; or heard from, but
    not one fix since the Login and the Login is that old -- a page can keep
    talking while the tracker behind it records nothing; or Location is off,
    which the board names in its own words, and an older board reads as No
    signal rather than as live.
    """
    window = timedelta(minutes=LIVE_WINDOW_MINUTES)
    if contact is None or now - contact > window:
        return True
    fixed_since_login = latest is not None and km_rule.arrival(latest) >= session.started_at
    if not fixed_since_login and now - session.started_at > window:
        return True
    return gps_off_since is not None


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


# The header the phone app puts on every request. A browser sends nothing.
APP_CLIENT_HEADER = "HTTP_X_PAYROLL_CLIENT"


def _from_the_app(request):
    """Whether this request came from an engineer's own phone app.

    Only the app may report an engineer's position. Anyone signed in as an
    engineer used to be tracked as that engineer: the office opened one
    engineer's account in a desktop browser two hundred and fifty kilometres
    away, to see what he sees, and the page posted the OFFICE LAPTOP'S position
    as his. His day drew a straight line to the coast and read 519 km for a man
    who never left Hosur -- and kilometres feed allowances.

    The app is blocked from tracking outside itself as well, in
    useLiveTracking; this is the half that cannot be bypassed by a page.
    """
    return (request.META.get(APP_CLIENT_HEADER) or "").strip().lower() == "app"


# The phone-status report's advisory lock: this namespace in the high half of
# the key, the employee id in the low half.
PHONE_STATUS_LOCK = 7401

_PHONE_STATUS_KEYS = (
    "app_version",
    "android_version",
    "battery_exempt",
    "location_permission",
    "notification_permission",
    "gps_on",
)


def _phone_status_values(data):
    """A phone-status report's fields, cleaned. A value of the wrong kind or an
    unknown permission word reads as "the phone did not say", never as an error:
    one odd field must not cost the office the rest of the report."""

    def text(key, limit):
        value = data.get(key)
        if not isinstance(value, str):
            return ""
        # Control characters and lone surrogates: Postgres refuses a NUL in
        # text and a surrogate cannot be encoded, so either would be a 500.
        return re.sub(r"[\x00-\x1f\x7f\ud800-\udfff]", "", value).strip()[:limit]

    def flag(key):
        value = data.get(key)
        return value if isinstance(value, bool) else None

    def permission(key):
        value = data.get(key)
        return value if value in PhoneStatus.PERMISSIONS else ""

    return {
        "app_version": text("app_version", 40),
        "android_version": text("android_version", 20),
        "battery_exempt": flag("battery_exempt"),
        "location_permission": permission("location_permission"),
        "notification_permission": permission("notification_permission"),
        "gps_on": flag("gps_on"),
    }


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


# Most days one history push may carry: a week of the whole register several
# times over, small enough that one request cannot tie up the database.
MAX_SCORECARD_DAYS = 2000

# How far back a history push may write. The engineer sees three months; this
# is room for a backfill of more without letting a stray date rewrite years.
SCORECARD_DAYS_BACK = 400

# The longest span my_history answers at once: two bill cycles, the same limit
# the office's KM report puts on a range.
MAX_HISTORY_DAYS = 62


def _query_day(raw):
    """A YYYY-MM-DD value as a date, or None when it is missing or is not one."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return parse_date(raw)
    except ValueError:
        return None


def _store_history_day(employee, day, values, today):
    """Write one day's numbers from the history push: "stored" for a new row,
    "updated" for one rewritten, "kept" for today's live row, left as it is.

    Today belongs to the live push. The check and the write happen under the
    row's lock, and an insert that loses a race to the live push's own insert
    reads the row again and applies the same rule, so a history push never
    overwrites a live row for today, whichever of the two gets there first.
    """
    for attempt in range(2):
        try:
            with transaction.atomic():
                row = (
                    EngineerScorecardDay.objects.select_for_update()
                    .filter(engineer=employee, day=day)
                    .first()
                )
                if row is None:
                    EngineerScorecardDay.objects.create(
                        engineer=employee,
                        day=day,
                        source=EngineerScorecardDay.SOURCE_HISTORY,
                        **values,
                    )
                    return "stored"
                if day == today and row.source == EngineerScorecardDay.SOURCE_LIVE:
                    return "kept"
                for field, value in values.items():
                    setattr(row, field, value)
                row.source = EngineerScorecardDay.SOURCE_HISTORY
                row.save()
                return "updated"
        except IntegrityError:
            if attempt:
                raise


class CaseViewSet(viewsets.ModelViewSet):
    """Case management + dispatch. Admin/HR create and assign cases; the assigned
    engineer sees only their own cases and drives the status forward in the field."""

    serializer_class = CaseSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # prefetch_related("visits"): the serializer reads this trip's two taps
        # for every card, and without this that is one query per case.
        qs = (
            Case.objects.select_related("assigned_to", "assigned_by")
            .prefetch_related("visits")
            .all()
        )

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

            card, _ = EngineerScorecard.objects.update_or_create(
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
            # The same numbers filed under their day, for the engineer's
            # history. The zeroing below is the live card's alone: it never
            # touches these rows, and nothing here ever deletes one.
            EngineerScorecardDay.objects.update_or_create(
                engineer=employee,
                day=as_of,
                defaults={
                    "assigned": card.assigned,
                    "attended": card.attended,
                    "closed": card.closed,
                    "daily_target": daily_target,
                    "monthly_target": monthly_target,
                    "source": EngineerScorecardDay.SOURCE_LIVE,
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

    @action(detail=False, methods=["post"], url_path="scorecard_days")
    def scorecard_days(self, request):
        """Receive past days' per-engineer counts from OpenCall, for each
        engineer's history.

        Body: {"days": [ {engineer_name?/engineer_email?/engineer_phone?/engineer_id?,
                          day: "YYYY-MM-DD", assigned, attended, closed,
                          daily_target, monthly_target}, ... ]}

        One row per (engineer, day), rewritten in place and marked "history".
        The live card is never touched, and neither is today's row the live
        push wrote: today belongs to that push. Engineers are matched exactly
        as the scorecards push matches them. An unmatched one is named in
        `unmatched`, and an item that cannot be stored is listed in `rejected`
        by its index, rather than failing everybody else's days.

        Optional "complete_days": ["YYYY-MM-DD", ...] -- days this request
        carries ALL of, every engineer that day's productivity lists. For such
        a day before today, anybody else's row is set to zero (never deleted):
        an engineer whose calls went to somebody else later in the day kept the
        morning's count otherwise, and History read more than Productivity --
        an overcount, which is the worse way to be wrong. Nothing is zeroed for
        a day none of whose engineers matched, because that is a lookup that
        failed, not a day nobody worked.
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Only admin/HR can push scorecards."}, status=403)

        items = request.data.get("days") if hasattr(request.data, "get") else None
        if not isinstance(items, list):
            return Response({"detail": 'Body must be {"days": [ ... ]}.'}, status=400)
        if len(items) > MAX_SCORECARD_DAYS:
            return Response(
                {
                    "detail": "a push may carry at most %d days." % MAX_SCORECARD_DAYS,
                    "max_days": MAX_SCORECARD_DAYS,
                },
                status=400,
            )

        today = timezone.localdate()
        oldest = today - timedelta(days=SCORECARD_DAYS_BACK)
        identity_keys = ("engineer_name", "engineer_email", "engineer_phone", "engineer_id")
        stored = updated = 0
        unmatched = []
        rejected = []
        # A week of the register names each engineer seven times; each name is
        # looked up once.
        matches = {}
        # Who this request matched, per day, for the complete-day rule.
        matched_on = {}

        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                rejected.append({"index": index, "reason": "each day must be an object."})
                continue
            day = _query_day(raw.get("day"))
            if day is None:
                rejected.append({"index": index, "reason": "day must be YYYY-MM-DD."})
                continue
            if day > today:
                rejected.append({"index": index, "reason": "day is in the future."})
                continue
            if day < oldest:
                rejected.append(
                    {"index": index, "reason": "day is older than %d days." % SCORECARD_DAYS_BACK}
                )
                continue
            # How the push named the engineer: the name, else whichever key it
            # gave, which is what the unmatched list reports.
            named = [str(raw.get(key) or "").strip() for key in identity_keys]
            if not any(named):
                rejected.append({"index": index, "reason": "no engineer given."})
                continue

            identity = tuple(repr(raw.get(key)) for key in identity_keys)
            if identity not in matches:
                matches[identity] = resolve_engineer(raw)
            employee = matches[identity]
            if employee is None:
                name = next(value for value in named if value)
                if name not in unmatched:
                    unmatched.append(name)
                continue

            matched_on.setdefault(day, set()).add(employee.id)
            outcome = _store_history_day(
                employee,
                day,
                {
                    "assigned": _as_count(raw.get("assigned")),
                    "attended": _as_count(raw.get("attended")),
                    "closed": _as_count(raw.get("closed")),
                    "daily_target": _as_count(raw.get("daily_target")),
                    "monthly_target": _as_count(raw.get("monthly_target")),
                },
                today,
            )
            if outcome == "stored":
                stored += 1
            elif outcome == "updated":
                updated += 1
            else:
                rejected.append(
                    {"index": index, "reason": "today's numbers come from the live push."}
                )

        zeroed = 0
        complete = request.data.get("complete_days") if hasattr(request.data, "get") else None
        for raw_day in complete if isinstance(complete, list) else []:
            day = _query_day(raw_day)
            # Only a finished day, and only one this request matched somebody on.
            if day is None or day >= today or day < oldest or not matched_on.get(day):
                continue
            zeroed += (
                EngineerScorecardDay.objects.filter(day=day)
                .exclude(engineer_id__in=matched_on[day])
                .filter(Q(assigned__gt=0) | Q(attended__gt=0) | Q(closed__gt=0))
                .update(
                    assigned=0,
                    attended=0,
                    closed=0,
                    source=EngineerScorecardDay.SOURCE_HISTORY,
                    updated_at=timezone.now(),
                )
            )

        return Response(
            {
                "stored": stored,
                "updated": updated,
                "unmatched": unmatched,
                "rejected": rejected,
                "zeroed": zeroed,
            }
        )

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

    @action(detail=False, methods=["get"], url_path="my_history")
    def my_history(self, request):
        """The caller's own past days, for the History view on their Cases
        screen: km, Assigned / Attended / Closed and the targets, per day.

        Query: ?from=YYYY-MM-DD&to=YYYY-MM-DD. Like my_scorecard there is no
        engineer to pass: it only ever answers about you. `to` stops at today,
        and a range is at most MAX_HISTORY_DAYS days.

        Every calendar day in the range, oldest first. A day's km is read by
        _DayKm, so it is the roster's distance_km for that day -- the office
        KM report's figure -- and today's is "Travelled today". The total is
        added up and rounded the way the KM report adds up a row.
        """
        employee = _get_employee(request.user)
        if not employee:
            raise NoEmployeeProfile()

        first = _query_day(request.query_params.get("from"))
        last = _query_day(request.query_params.get("to"))
        if first is None or last is None:
            return Response({"detail": "from and to must be YYYY-MM-DD."}, status=400)
        if first > last:
            return Response({"detail": "from must not be after to."}, status=400)
        # A day that has not happened yet has nothing to show; a range wholly
        # after today is answered with no days.
        last = min(last, timezone.localdate())
        span = (last - first).days + 1
        if span > MAX_HISTORY_DAYS:
            return Response(
                {
                    "detail": "at most %d days at a time." % MAX_HISTORY_DAYS,
                    "max_days": MAX_HISTORY_DAYS,
                },
                status=400,
            )

        # A duty nobody stopped is closed first, as the roster closes it before
        # it reads a day.
        TrackingViewSet._close_forgotten_sessions()

        scores = {
            row.day: row
            for row in EngineerScorecardDay.objects.filter(
                engineer=employee, day__gte=first, day__lte=last
            )
        }
        days = []
        km_total = 0.0
        for offset in range(max(span, 0)):
            day = first + timedelta(days=offset)
            km = _DayKm([employee.id], day).km(employee.id)
            score = scores.get(day)
            days.append({
                "day": day.isoformat(),
                "km": km,
                "assigned": score.assigned if score else 0,
                "attended": score.attended if score else 0,
                "closed": score.closed if score else 0,
                "daily_target": score.daily_target if score else 0,
                "has_score": score is not None,
            })
            # Oldest first, as the KM report adds a row's days.
            km_total += km

        newest = max(scores.values(), key=lambda row: row.day, default=None)
        return Response({
            "from": first.isoformat(),
            "to": last.isoformat(),
            "days": days,
            "totals": {
                "km": _js_round2(km_total),
                "days_tracked": sum(1 for entry in days if entry["km"] > 0),
                "assigned": sum(entry["assigned"] for entry in days),
                "attended": sum(entry["attended"] for entry in days),
                "closed": sum(entry["closed"] for entry in days),
                "monthly_target": newest.monthly_target if newest else 0,
            },
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
            # THE DAY THIS CALL IS ALREADY ON -- read before the plan date below
            # overwrites it.
            #
            # A push for a LATER day is a second trip to the same customer: the
            # part did not arrive, nobody was in, the job needs another visit.
            # The engineer's side has to start again or their phone shows the
            # call with no Check In on it -- the case still says completed from
            # the first trip, and the branch below leaves an engineer's own
            # status alone on purpose.
            #
            # A push for the SAME day is the sync repeating itself. It runs
            # every couple of minutes and always says "assigned", including
            # about calls the engineer closed an hour ago, so it must change
            # nothing. Unknown on either side counts as the same day for the
            # same reason: a guess here re-opens finished work every tick.
            previous_trip_day = None if is_new else _trip_day(case)
            second_trip = (
                plan_date is not None
                and previous_trip_day is not None
                and plan_date > previous_trip_day
            )
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
            # Re-stamped for a second trip too: the call was handed to them
            # again today, and the board should say so rather than "on the list
            # from" a week ago. The first trip's own date is kept on its visit.
            if case.assigned_at is None or reassigned or second_trip:
                case.assigned_at = timezone.now()

            desired = self._EXTERNAL_STATUS_MAP.get((raw.get("status") or "").strip().lower())
            if desired in ("completed", "cancelled"):
                # Terminal upstream: the originating system says this call is
                # finished, which always wins over the field status.
                case.status = desired
                # Stamped on a second trip as well, so a close reported today
                # is dated today instead of inheriting the first trip's time
                # and being filed on the wrong day.
                if desired == "completed" and (case.completed_at is None or second_trip):
                    case.completed_at = timezone.now()
            elif reassigned or second_trip:
                # A different engineer, or a different day: either way the
                # engineer's side of this call starts from the beginning.
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
            if plan_date:
                # WHICH DAY'S LIST THIS CALL WAS ON, written once and never
                # rewritten. The plan date on the case is renewed every couple
                # of minutes and so can only ever answer for today; this is the
                # record a second dispatch cannot overwrite, and it is what
                # lets a day that has already happened keep its own answer.
                _visit_for(case, plan_date, engineer)
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
        moment = timezone.now()
        if stamp_field:
            setattr(case, stamp_field, moment)
        if extra:
            for k, v in extra.items():
                setattr(case, k, v)
        case.save()
        # The case's columns are the LATEST trip; this is the trip itself. A
        # call sent out again next week overwrites the columns, and without
        # this row that day's arrival would be gone from the board that showed
        # it.
        if stamp_field in ("reached_at", "completed_at"):
            self._record_trip(case, stamp_field, moment, extra or {})
            # The serializer reads this trip off the case, and the prefetch
            # that loaded it ran before the row was written. Left stale, the
            # reply to a Check In would say the engineer had not checked in.
            if hasattr(case, "_prefetched_objects_cache"):
                case._prefetched_objects_cache.pop("visits", None)
        return Response(self.get_serializer(case).data)

    def _record_trip(self, case, stamp_field, moment, extra):
        """Write the punch onto the day's visit as well as onto the case.

        Which day: the one the engineer is standing in. A check-out is filed
        against the trip it belongs to even when the clock has passed midnight
        -- an engineer finishing at 00:20 checked in yesterday, and splitting
        that into two trips would leave both halves unreadable.
        """
        today = timezone.localdate()
        if stamp_field == "completed_at":
            open_trip = (
                case.visits.filter(checked_out_at__isnull=True, checked_in_at__isnull=False)
                .order_by("-plan_date")
                .first()
            )
            visit = open_trip or _visit_for(case, today)
        else:
            visit = _visit_for(case, today)

        if stamp_field == "reached_at":
            visit.checked_in_at = moment
            visit.punch_in_lat = extra.get("punch_in_lat", visit.punch_in_lat)
            visit.punch_in_lon = extra.get("punch_in_lon", visit.punch_in_lon)
            visit.punch_in_accuracy = extra.get("punch_in_accuracy", visit.punch_in_accuracy)
        else:
            visit.checked_out_at = moment
            visit.punch_out_lat = extra.get("punch_out_lat", visit.punch_out_lat)
            visit.punch_out_lon = extra.get("punch_out_lon", visit.punch_out_lon)
            visit.punch_out_accuracy = extra.get("punch_out_accuracy", visit.punch_out_accuracy)
            if extra.get("resolution_notes"):
                visit.resolution_notes = extra["resolution_notes"]
        if stamp_field == "reached_at" and case.assigned_to_id:
            # Only the call's engineer can check in, so whoever holds it now is
            # who made this visit -- also when it was booked to someone else
            # earlier the same day and the day's row still names them. ROUTE KM
            # credits the check-in by this; nothing else reads it.
            visit.engineer = case.assigned_to
        elif visit.engineer_id is None:
            visit.engineer = case.assigned_to
        if visit.assigned_at is None:
            visit.assigned_at = case.assigned_at
        visit.save()

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
        any read or write of duty state.

        Closed at the session's own last fix, not at the clock time the sweep
        happens to run. Stamping it now() recorded every hour between the
        engineer's last position and whenever somebody next opened the board as
        duty they had worked -- a night of it, most mornings. Capped at the
        maximum duration, because a session cannot honestly be longer than the
        rule that closed it.

        The per-session query costs nothing in the ordinary case: this filter
        matches no rows at all unless somebody actually forgot.
        """
        cutoff = timezone.now() - timedelta(hours=DutySession.MAX_DURATION_HOURS)
        forgotten = list(
            DutySession.objects.filter(ended_at__isnull=True, started_at__lt=cutoff)
        )
        if not forgotten:
            return
        for session in forgotten:
            window_end = session.started_at + timedelta(hours=DutySession.MAX_DURATION_HOURS)
            last = (
                LocationPing.objects.filter(
                    engineer_id=session.engineer_id,
                    timestamp__gte=session.started_at,
                    timestamp__lte=window_end,
                )
                .order_by("-timestamp")
                .first()
            )
            # No fix at all means the duty never reported anything, so it has no
            # honest length: it starts and ends where it started.
            session.ended_at = last.timestamp if last else session.started_at
            session.auto_closed = True
        DutySession.objects.bulk_update(forgotten, ["ended_at", "auto_closed"])

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
        # The day's fixes, Logins and phone events, read by the one helper the
        # roster and the engineer's history read a day with, and the way the
        # day view reads them, so all of them come to the same figure.
        today_km = _DayKm([employee.id], today).km(employee.id)
        return {
            "on_duty": session is not None,
            "session_id": session.id if session else None,
            "started_at": session.started_at if session else None,
            "duration_minutes": session.duration_minutes() if session else 0,
            "today_km": today_km,
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
        session = self._open_session(employee)
        # A session that began on an earlier day was never stopped -- the app was
        # closed, or the phone closed it for them. Today's Login is the signal
        # that yesterday is over.
        #
        # It has to be said here because the two halves disagreed about dates:
        # this lookup is date-blind, while the office's board asks for sessions
        # STARTED on the day it is showing. An engineer who forgot to Logout last
        # night logged in this morning, was quietly told "already on duty", no row
        # was written for today, and the board read "Not on duty" all day beside
        # his live position and a distance that kept climbing.
        if session and timezone.localtime(session.started_at).date() < timezone.localdate():
            # Closed at its own last fix rather than now: the engineer stopped
            # reporting when the phone did, and stamping it with this morning's
            # clock would invent a night's duty that nobody worked.
            last = (
                LocationPing.objects.filter(
                    engineer=employee, timestamp__gte=session.started_at
                )
                .order_by("-timestamp")
                .first()
            )
            session.ended_at = last.timestamp if last else session.started_at
            session.auto_closed = True
            session.save(update_fields=["ended_at", "auto_closed"])
            session = None
        # Idempotent within the day: tapping Login twice (or a reconnect) must not
        # open a second overlapping session and double-count the day.
        if not session:
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
        if not _from_the_app(request):
            return Response(
                {
                    "detail": "Only the RTPL app can record a position.",
                    "app_only": True,
                },
                status=403,
            )

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
        _note_contact(employee)
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
        if not _from_the_app(request):
            return Response(
                {
                    "detail": "Only the RTPL app can record a position.",
                    "app_only": True,
                },
                status=403,
            )

        try:
            result = ingest_batch(employee, request.data.get("pings"), _case_for)
        except PingRejected as exc:
            return Response({"detail": str(exc), "max_batch": MAX_BATCH}, status=400)

        if result["stored"]:
            _note_contact(employee)
        return Response(result, status=201)

    @action(detail=False, methods=["post"], url_path="events")
    def events(self, request):
        """What the phone noticed about itself: Location off and on, the
        network gone and back, the app stopped and running again, the app
        opened. Body: {"events": [{kind, at, client_key, latitude?,
        longitude?, detail?}, ...]}.

        The same gates as a fix, because two of these change the kilometres.
        An empty list is the phone's heartbeat: nothing new to say, but alive
        and recording, which is what keeps a parked engineer off "No signal".
        """
        employee = _get_employee(request.user)
        if not employee:
            return Response({"detail": "No employee profile linked to this user."}, status=400)
        if not _from_the_app(request):
            return Response(
                {
                    "detail": "Only the RTPL app can record a position.",
                    "app_only": True,
                },
                status=403,
            )

        posted = request.data.get("events") if hasattr(request.data, "get") else None
        try:
            result = ingest_events(employee, posted)
        except PingRejected as exc:
            return Response({"detail": str(exc), "max_batch": MAX_EVENTS}, status=400)

        _note_contact(employee)
        return Response(result, status=201)

    @action(detail=False, methods=["post"], url_path="phone_status")
    def phone_status(self, request):
        """How this phone is set up to record, posted by the app when it opens:
        {app_version, android_version, battery_exempt, location_permission,
        notification_permission, gps_on}.

        From the app only, like a fix: a browser signed in as an engineer is not
        that engineer's phone. A report that changes nothing moves last_seen_at;
        one that changes anything starts a new row, so the rows are the history
        App Usage shows. It touches nothing else: not the duty, not the contact
        the board reads, not the kilometres.
        """
        employee = _get_employee(request.user)
        if not employee:
            return Response({"detail": "No employee profile linked to this user."}, status=400)
        if not _from_the_app(request):
            return Response(
                {"detail": "Only the RTPL app can report the phone's settings.", "app_only": True},
                status=403,
            )

        # A report says something. An empty body, or anything not an object,
        # would otherwise be stored as "the phone said nothing" and replace
        # what the office sees.
        data = request.data
        if not isinstance(data, dict) or not any(key in data for key in _PHONE_STATUS_KEYS):
            return Response({"detail": "Nothing to report."}, status=400)
        values = _phone_status_values(data)
        # One report at a time per employee. Two arriving together -- a retry
        # behind a token refresh, say -- would otherwise both read the same
        # latest row and both add one, and the history would show the same
        # set-up twice. An advisory lock, not the employee's row: locking the
        # row would make a report wait behind anything else holding it, and
        # could deadlock with deleting the employee. Nothing else takes this
        # lock, so nothing else ever waits on it. SQLite (the tests) has none.
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [(PHONE_STATUS_LOCK << 32) + employee.pk])
            now = timezone.now()
            latest = PhoneStatus.objects.filter(employee=employee).order_by("-first_seen_at", "-id").first()
            if latest is not None and all(getattr(latest, field) == value for field, value in values.items()):
                PhoneStatus.objects.filter(pk=latest.pk).update(last_seen_at=now)
                return Response({"changed": False})
            PhoneStatus.objects.create(employee=employee, first_seen_at=now, last_seen_at=now, **values)
        return Response({"changed": True}, status=201)

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

        # Latest fix per on-duty engineer: the newest by when it was TAKEN,
        # the id breaking a tie. Not the highest id -- a late batch is stored
        # after the live fixes it was taken before, and reading it as the
        # newest moved the marker back down the road and called a live phone
        # stale.
        newest = (
            LocationPing.objects.filter(engineer_id=OuterRef("pk"))
            .order_by("-timestamp", "-id")
            .values("id")[:1]
        )
        latest_ids = (
            Employee.objects.filter(pk__in=engineer_ids)
            .annotate(last_id=Subquery(newest))
            .values_list("last_id", flat=True)
        )
        latest_by_engineer = {
            p.engineer_id: p
            for p in LocationPing.objects.select_related("case").filter(
                id__in=[last_id for last_id in latest_ids if last_id is not None]
            )
        }

        # Distance covered since each duty started, from that session's own
        # pings — so the number resets with the duty instead of carrying over
        # yesterday's, and matches what "this shift" means to the operator.
        now = timezone.now()
        contexts = km_rule.load_contexts(
            engineer_ids,
            min(s.started_at for s in sessions),
            now,
            kinds=km_rule.STATE_EVENT_KINDS,
        )
        rows = []
        for session in sessions:
            ping = latest_by_engineer.get(session.engineer_id)
            context = contexts[session.engineer_id]
            trail = list(
                LocationPing.objects.filter(
                    engineer_id=session.engineer_id, timestamp__gte=session.started_at
                ).order_by("timestamp", "id")
            )
            contact = _contact(session, ping)
            gps_off_since = _gps_off_since(session, context.events, ping)
            rows.append(
                {
                    "engineer_id": session.engineer_id,
                    "engineer_name": session.engineer.employee_name,
                    "branch": session.engineer.branch,
                    "on_duty": True,
                    "duty_started_at": session.started_at,
                    "duty_minutes": session.duration_minutes(),
                    # Still on duty, just not currently reporting -- see
                    # _no_signal for what that means.
                    "stale": _no_signal(session, ping, contact, gps_off_since, now),
                    # Since the phone was last heard from; last_fix_minutes is
                    # since the position on the map was taken.
                    "last_seen_minutes": _minutes_since(contact, now),
                    "last_fix_minutes": _minutes_since(ping.timestamp if ping else None, now),
                    "gps_off_since": gps_off_since,
                    "last_contact_at": contact,
                    "distance_km": _trail_km(trail, context),
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

        pings = list(qs.order_by("timestamp", "id"))

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

        # Every session of theirs that overlaps the day, and what their phones
        # said about Location and the app: two queries for the whole board.
        # The km rule needs EVERY Login of the day -- holding only one session
        # per engineer, the board missed a second Login and read more km than
        # the day view and the engineer's own phone. Read with the day's fixes
        # by _DayKm, which the phone's own km and its history read a day with.
        day_km = _DayKm(ids, target_date, kinds=km_rule.STATE_EVENT_KINDS)
        contexts = day_km.contexts

        # That day's duty, per engineer: the open one if there is one, otherwise
        # the last that ended.
        sessions_by_engineer = {}
        for engineer_id, context in contexts.items():
            for session in context.sessions:
                if timezone.localtime(session.started_at).date() != target_date:
                    continue
                current = sessions_by_engineer.get(engineer_id)
                # An open session always wins; otherwise keep the latest.
                if current is None or session.ended_at is None or current.ended_at is not None:
                    sessions_by_engineer[engineer_id] = session

        # One pass over the day's fixes, loaded with the contexts above, so
        # distance costs a single query rather than one per engineer.
        trails = day_km.trails

        # Their last fix of that day, for the marker and the "last seen" age:
        # the newest by when it was taken, not the highest id, which a late
        # batch would win.
        latest_by_engineer = {
            p.engineer_id: p
            for p in LocationPing.objects.select_related("case").filter(
                id__in=[trail[-1].id for trail in trails.values()]
            )
        }

        now = timezone.now()
        rows = []
        for engineer, asked_as in resolved:
            session = sessions_by_engineer.get(engineer.id)
            ping = latest_by_engineer.get(engineer.id)
            context = contexts[engineer.id]

            if session and session.ended_at is None:
                state = "on_duty"
            elif session:
                state = "checked_out"
            else:
                state = "absent"

            contact = _contact(session, ping)
            gps_off_since = _gps_off_since(session, context.events, ping)
            rows.append(
                {
                    "engineer_id": engineer.id,
                    # The caller's spelling, so their board reads the way their
                    # users know the person; payroll_name is the record it matched.
                    "engineer_name": asked_as,
                    "payroll_name": engineer.employee_name,
                    "matched": True,
                    "branch": engineer.branch,
                    # So a board showing office staff beside the engineers can
                    # say who is who, and filter by department. Added alongside;
                    # nothing reads them otherwise.
                    "role": engineer.role,
                    "department": engineer.department,
                    "state": state,
                    "on_duty": state == "on_duty",
                    "duty_started_at": session.started_at if session else None,
                    "duty_ended_at": session.ended_at if session else None,
                    "duty_minutes": session.duration_minutes() if session else 0,
                    "auto_closed": bool(session.auto_closed) if session else False,
                    "distance_km": day_km.km(engineer.id),
                    # Only meaningful while on duty; a checked-out engineer is
                    # not expected to be reporting. The name is kept for the
                    # boards already reading it.
                    "stale": state == "on_duty"
                    and _no_signal(session, ping, contact, gps_off_since, now),
                    # Since the phone was last heard from, and since the fix on
                    # the map was taken: an engineer standing still is heard
                    # from every minute and has not moved for an hour.
                    "last_seen_minutes": _minutes_since(contact, now),
                    "last_fix_minutes": _minutes_since(ping.timestamp if ping else None, now),
                    "gps_off_since": gps_off_since,
                    "last_contact_at": contact,
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
                    "role": None,
                    "department": None,
                    "state": "unmatched",
                    "on_duty": False,
                    "duty_started_at": None,
                    "duty_ended_at": None,
                    "duty_minutes": 0,
                    "auto_closed": False,
                    "distance_km": 0.0,
                    "stale": False,
                    "last_seen_minutes": None,
                    "last_fix_minutes": None,
                    "gps_off_since": None,
                    "last_contact_at": None,
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
            .order_by("timestamp", "id")
        )
        day_start, day_end = km_rule.day_bounds(target_date)
        # Every session that overlaps the day, and everything the phone said
        # about itself around it: two queries, read by the km rule, the rail
        # and the on-duty test alike.
        context = km_rule.load_contexts([engineer.id], day_start, day_end, kinds=None)[
            engineer.id
        ]
        rule = km_rule.SegmentRule(pings, context)
        # The day's own Logins and Logouts are the sessions that began on it --
        # the same ones ROUTE KM routes through.
        sessions = [
            s for s in context.sessions if timezone.localtime(s.started_at).date() == target_date
        ]
        # Whether they were on duty at a moment is asked of every session that
        # overlaps the day, so a duty that ran past midnight still covers the
        # small hours it ran into. A day with no Login of its own is left
        # alone, as it always was (see _on_duty_at): a duty from the night
        # before ending in the small hours must not hide the rest of a day the
        # phone went on recording.
        on_duty = (
            [
                s
                for s in context.sessions
                if s.started_at < day_end and (s.ended_at is None or s.ended_at >= day_start)
            ]
            if sessions
            else []
        )
        # STANDING STILL OFF DUTY IS THEIR OWN TIME.
        #
        # Filtered here rather than where the entries are built, so the count in
        # the header, the markers on the map and the rail cannot disagree: one
        # list, one rule. A day with no session at all is left alone -- see
        # _on_duty_at.
        stops = [
            stop
            for stop in _detect_stops(pings)
            if _on_duty_at(on_duty, stop["arrived_at"])
        ]
        punches = _punches_for_day(engineer, target_date)

        # A timeline the office can read top to bottom, the way the day happened.
        events = []
        for session in sessions:
            events.append(
                # The engineer's own word for this tap. There is no button
                # called Start Duty any more; Login is what they press.
                {"at": session.started_at, "type": "duty_start", "label": "Login"}
            )
            if session.ended_at:
                events.append(
                    {
                        "at": session.ended_at,
                        "type": "duty_end",
                        "label": "Auto-closed (no Logout)"
                        if session.auto_closed
                        else "Logout",
                        "minutes": session.duration_minutes(),
                    }
                )
        for stop in stops:
            events.append(
                {
                    "at": stop["arrived_at"],
                    "type": "stop",
                    # "Waiting", the office's own word for it. Their read is
                    # that the engineer is not moving; whether that is work at a
                    # customer or a wait on the road, the case number beside it
                    # answers -- and this entry only ever claimed the first part.
                    "label": f"Waiting {stop['minutes']} min",
                    "minutes": stop["minutes"],
                    "latitude": stop["latitude"],
                    "longitude": stop["longitude"],
                    "case_number": stop["case_number"],
                    "case_ref": stop.get("case_ref"),
                }
            )
        # WHAT THE PHONE SAID ABOUT ITSELF: Location off and on, the network
        # gone and back, the phone stopping the app and the app tracking again.
        # Each one is a fact the phone reported, so it goes on the rail in its
        # own words -- and the guesses below give way to it.
        phone_entries = _phone_event_entries(context.events, on_duty, pings, day_start, day_end)
        events.extend(phone_entries)

        # What the rail itself says, and nothing else: an event left off it --
        # a Net blip, something off duty -- cannot be what explains a gap.
        def said(*kinds):
            return sorted(entry["at"] for entry in phone_entries if entry["type"] in kinds)

        gps_said = said("gps_off", "gps_on")
        phone_said = said(*EXPLAINING_KINDS)
        net_down = _net_outages(phone_entries)
        opened_at = sorted(event.at for event in context.events if event.kind == "app_opened")

        # Whether the kilometres take the straight line across a stretch, for
        # the entries that would otherwise leave the board to guess.
        moving = _moving_trail(pings)
        steady = _usable_pings(pings)

        def counted_across(start, end):
            return _counted_across(rule, moving, steady, start, end)

        for entry in phone_entries:
            if entry["type"] == "gps_off":
                entry["counted"] = counted_across(entry["at"], entry["at"])

        # WHERE THE DAY WENT DARK, and which kind of dark it was.
        #
        # Two facts already in the database, which is why the office could see
        # short kilometres and not why:
        #
        #   Tracking stopped -- the phone flagged the first fix after tracking
        #     stopped and started again. Older apps flagged every reload and
        #     every restart, so this no longer means Location was off: a Login
        #     is said by the Login entry, Location by the phone's own GPS
        #     entries, and what is left is the app having stopped. `counted`
        #     says whether the kilometres took the straight line across it
        #     (km_rule) -- they do since the new rule, and did not before it.
        #   Kept on the phone -- a fix taken at two and delivered at four sat
        #     on the phone. The kilometres are honest; only the board was empty
        #     at the time. Nothing is missing and nobody needs chasing.
        #
        # Five minutes is the floor. Below it these are the ordinary jitter of a
        # phone handing the GPS back and forth, and forty entries nobody reads
        # is the same as none.
        for previous, current in zip(pings, pings[1:]):
            if not getattr(current, "after_gap", False):
                continue
            dark = int((current.timestamp - previous.timestamp).total_seconds() // 60)
            if dark < MIN_DARK_MINUTES:
                continue
            # Off duty, nobody was asking the phone for anything. Judged on
            # where the stretch STARTS, so both ends of it are kept or dropped
            # together.
            if not _on_duty_at(on_duty, previous.timestamp):
                continue
            if rule.crosses_login(previous, current):
                continue  # the trip to work; the Login entry says it
            if _any_between(
                gps_said, previous.timestamp - EVENT_SLACK, current.timestamp + EVENT_SLACK
            ):
                continue  # the phone's GPS entries say it, in better words
            counted = not rule.skips(previous, current)
            events.append(
                {
                    # Stamped where the trail STOPS, not where it resumes: that
                    # is the moment the office is looking for when they ask what
                    # happened after four o'clock.
                    "at": previous.timestamp,
                    "type": "location_off",
                    "label": f"Tracking stopped \u00b7 {duration_words(dark)}",
                    "minutes": dark,
                    "latitude": previous.latitude,
                    "longitude": previous.longitude,
                    "counted": counted,
                }
            )
            # And the moment it came back. Without this the rail says a hole
            # opened and never says it closed, so a stretch that ended at 3:40
            # cannot be told from one that ran to the logout -- and that
            # difference is whether anybody needs chasing.
            #
            # Honest about what it knows: the phone started reporting again at
            # this moment, and the label says that and not why.
            events.append(
                {
                    "at": current.timestamp,
                    "type": "location_back",
                    "label": "Tracking back",
                    "minutes": dark,
                    "latitude": current.latitude,
                    "longitude": current.longitude,
                    "counted": counted,
                }
            )

        # AND THE SILENCES NOTHING FLAGGED.
        #
        # An app that is killed -- swiped off the recents list, or stopped by
        # the phone to save battery -- flags nothing: there is no error to
        # catch, the fixes simply stop. Hours could pass with nothing on this
        # rail at all, which is exactly the case the office cannot explain.
        #
        # Only when they came out of it somewhere ELSE. A quiet phone on its own
        # is an engineer standing still, and the stop detector has already drawn
        # that as "Waiting"; this is for a stretch they travelled with nothing
        # watching. Read off the fixes that survive the accuracy filter, so one
        # wild reading from a cold GPS cannot invent a journey.
        #
        # Never "Not tracked": the kilometres count this straight line, so it
        # is not missing, and the office read the words as a fault in the app.
        # `counted` says whether they did -- before the cutover a flag the
        # accuracy filter dropped still took the line, and the board must not
        # claim it. When the app was opened just as the fixes resumed, the app
        # had been closed and says so; otherwise only the silence is known.
        flagged = {id(ping) for ping in pings if getattr(ping, "after_gap", False)}
        for previous, current in zip(steady, steady[1:]):
            if id(current) in flagged:
                continue  # already said, and better said, as Tracking stopped
            dark = int((current.timestamp - previous.timestamp).total_seconds() // 60)
            if dark < MIN_DARK_MINUTES:
                continue
            metres = 1000 * haversine_km(
                previous.latitude, previous.longitude, current.latitude, current.longitude
            )
            if metres < UNTRACKED_MOVE_METERS:
                continue  # they stood still; that is a stop, not a hole
            # AND ONLY WHILE THEY WERE ON DUTY.
            #
            # An engineer who logs out at half past eleven and comes back at
            # seven is not being tracked in between, and should not be told off
            # for it. Without this the board read "Not tracked - 7h 40m" across
            # somebody's own afternoon. A stretch with a Login in it is the same
            # afternoon, ended: not counted, and said by the Login entry.
            if not _on_duty_at(on_duty, previous.timestamp):
                continue
            if rule.crosses_login(previous, current):
                continue
            if _any_between(
                phone_said, previous.timestamp - EVENT_SLACK, current.timestamp + EVENT_SLACK
            ):
                continue  # the phone said what happened, on its own entries
            closed = _any_between(
                opened_at, current.timestamp - APP_OPENED_SLACK, current.timestamp + APP_OPENED_SLACK
            )
            what = "App was closed" if closed else "No positions"
            events.append(
                {
                    # Where the trail stops, like Tracking stopped: that is the
                    # moment somebody is looking for.
                    "at": previous.timestamp,
                    "type": "untracked",
                    "label": f"{what} \u00b7 {duration_words(dark)}",
                    "minutes": dark,
                    # How far they had moved by the time it came back. The
                    # kilometres count this straight line -- the least they can
                    # have travelled -- so this is the size of what is missing.
                    "moved_km": round(metres / 1000, 1),
                    "latitude": previous.latitude,
                    "longitude": previous.longitude,
                    "counted": counted_across(previous.timestamp, current.timestamp),
                }
            )

        # One entry per OUTAGE, not per delayed fix: a phone offline for an hour
        # delivers a hundred of them at once and each one is the same fact.
        # Left to the phone's own Net entries when they cover it -- the network
        # off by the time the first held fix was taken and back by when the
        # last was delivered: one outage, said once. Net entries inside it only
        # say part of it, and the rest is still worth saying.
        def outage(first, last):
            for off, on in net_down:
                if off <= first.timestamp + EVENT_SLACK and (
                    on is None or on >= last.received_at - EVENT_SLACK
                ):
                    return []
            return _offline_event(first, last)

        outage_start = None
        outage_end = None
        for ping in pings:
            late = (
                ping.received_at is not None
                and (ping.received_at - ping.timestamp).total_seconds() / 60
                >= QUEUED_THRESHOLD_MINUTES
            )
            if late:
                # Only what happened on duty. A phone catching up hours after
                # the engineer logged out is not an outage anybody worked
                # through.
                if outage_start is None and not _on_duty_at(on_duty, ping.timestamp):
                    continue
                if outage_start is None:
                    outage_start = ping
                outage_end = ping
                continue
            if outage_start is not None:
                events.extend(outage(outage_start, outage_end))
                outage_start = outage_end = None
        if outage_start is not None:
            events.extend(outage(outage_start, outage_end))

        # What the engineer did to their cases that day, so a stop can be read
        # against the job it belongs to. The four moments and their labels live
        # in _day_moments, which reads them from the day's visit row when there
        # is one -- see there.

        # THE DAY'S WORKLOAD, not just what was stamped on the day.
        #
        # This used to be every case the engineer has ever had, kept only where
        # one of the four moments landed on the date. That answers "what did they
        # touch today" but not "what were they given today", and the office reads
        # this screen for the second one. assigned_at is stamped ONCE, on first
        # dispatch, so a call pushed yesterday evening and worked this morning
        # produced no assignment entry at all -- five cases on the engineer's own
        # list showed up here as one.
        #
        # WITHDRAWN, and why it is worth writing down.
        #
        # The plan cannot answer for a finished day: plan_date marks the plan the
        # sync LAST pushed and the sync renews it every two minutes for every
        # still-open call, so yesterday loses every call that is still open now.
        # Productivity said five cases and this listed three.
        #
        # Reconstructing from "assigned by then and not finished before the day"
        # replaced that with something worse: every call an engineer has ever had
        # open appeared on EVERY past day. One engineer read 20 against
        # Productivity's 5. Undercounting looks like loss; overcounting looks
        # like the truth, so it is the more expensive mistake and it is out.
        #
        # This is back to the plan, which is honest about what it knows: a past
        # day shows the calls whose LAST plan day was that day, plus anything
        # actually worked on it. Calls carried forward are missing, and they
        # cannot be recovered -- the renewal overwrote the only record that they
        # were ever on that day. The fix for that is to stop overwriting it:
        # record (case, plan_date) append-only as the sync pushes, and read a
        # past day from those rows. Until that ships, this under-reports rather
        # than inventing.
        planned = Case.objects.filter(assigned_to=engineer, in_current_plan=True).exclude(
            status="cancelled"
        )
        if target_date == timezone.localdate():
            planned = planned.filter(Q(plan_date=target_date) | Q(plan_date__isnull=True))
        else:
            planned = planned.filter(plan_date=target_date)

        touched = Case.objects.filter(assigned_to=engineer).filter(
            Q(assigned_at__date=target_date)
            | Q(started_at__date=target_date)
            | Q(reached_at__date=target_date)
            | Q(completed_at__date=target_date)
        )

        # Merged in Python rather than with union(): a combined queryset cannot
        # then be narrowed with only(), and a case can legitimately be in both
        # halves -- given today and worked today is the ordinary case.
        fields = (
            "case_number",
            "external_ref",
            "title",
            "status",
            # Where the customer is. Loaded here so the timeline can say where
            # each call happened without a query per entry.
            "address",
            # Where the customer IS, and where the engineer was STANDING when
            # they tapped each button. Both, because a punch is the precise
            # answer and the customer's pin is the fallback for a call moved by
            # the older actions that recorded no punch.
            "latitude",
            "longitude",
            "punch_in_lat",
            "punch_in_lon",
            "punch_out_lat",
            "punch_out_lon",
            "assigned_at",
            "started_at",
            "reached_at",
            "completed_at",
        )
        # THE DAY'S OWN RECORD, which is neither the plan nor the stamps.
        #
        # A visit row is written when the sync pushes a call for a day, and
        # nothing later rewrites it. That makes it the only source that can
        # still answer for a day after the same call has been sent out again --
        # both for whether it was on that day's list, and for the arrival and
        # departure punched on it. It is a record, not a reconstruction: a row
        # exists only for a day the call was actually somebody's to do.
        visits_by_case = {}
        for visit in CaseVisit.objects.filter(case__assigned_to=engineer).filter(
            Q(plan_date=target_date)
            | Q(checked_in_at__date=target_date)
            | Q(checked_out_at__date=target_date)
        ):
            visits_by_case.setdefault(visit.case_id, visit)
        visited = (
            Case.objects.filter(assigned_to=engineer, id__in=list(visits_by_case)).exclude(
                status="cancelled"
            )
            if visits_by_case
            else Case.objects.none()
        )

        day_cases = {}
        for queryset in (planned, touched, visited):
            for case in queryset.only(*fields):
                day_cases[case.pk] = case

        # Sorted so several carried-over calls land in the order they were given
        # rather than in whatever order the database returned them.
        for case in sorted(
            day_cases.values(), key=lambda c: c.assigned_at or timezone.now()
        ):
            visit = visits_by_case.get(case.pk)
            stamped_today = False
            for field, label, moment, lat, lon in _day_moments(case, visit):
                if moment and timezone.localtime(moment).date() == target_date:
                    if field == "assigned_at":
                        stamped_today = True
                    # Where they stood when they tapped, if that was recorded;
                    # otherwise where the customer is.
                    if lat is None or lon is None:
                        lat, lon = case.latitude, case.longitude
                    events.append(
                        {
                            "at": moment,
                            "type": field.replace("_at", ""),
                            "label": label,
                            "case_number": case.case_number,
                            "case_ref": case.external_ref or None,
                            "case_status": case.status,
                            "latitude": lat,
                            "longitude": lon,
                        }
                    )
            if stamped_today:
                continue
            # Given to them before today and still on the list: it opens the day
            # rather than appearing at a clock time that belongs to another one.
            # The date it was actually given is in the label, because "assigned"
            # with no date next to it reads as "assigned this morning".
            # The day it was given, from the day's own row when there is one:
            # a second dispatch re-stamps the case, and this line is what tells
            # an earlier day it was NOT given that morning.
            given_at = (visit.assigned_at if visit and visit.assigned_at else case.assigned_at)
            since = f" {timezone.localtime(given_at):%d %b}" if given_at else ""
            events.append(
                {
                    "at": timezone.make_aware(
                        datetime.combine(target_date, time.min), timezone.get_current_timezone()
                    ),
                    # Its own type, not "assigned": this is not a moment in the
                    # day, it is what the day started with, and the screen shows
                    # no clock time against it for exactly that reason.
                    "type": "carried",
                    "label": f"On the list from{since}" if since else "On the list",
                    "case_number": case.case_number,
                    "case_ref": case.external_ref or None,
                    # Where the case actually stands, which is NOT what this day
                    # shows: four of an engineer's five calls can be completed
                    # and still carry no event today, because they were closed
                    # on an earlier one. Without this the board would read
                    # "not started" beside an app that says DONE.
                    "case_status": case.status,
                }
            )
        # A PLACE FOR EVERY ENTRY, so every entry can be opened on the map.
        #
        # Login, Logout and "Left for the call" store no coordinate of their
        # own. The trail does: the fix nearest in time says where the engineer
        # was at that minute. Capped at fifteen minutes -- beyond that it is
        # somewhere else and a link would be a small lie, so those keep none.
        located = [ping for ping in pings if ping.latitude is not None]
        for event in events:
            if event.get("latitude") is not None:
                continue
            nearest = None
            best = None
            for ping in located:
                apart = abs((ping.timestamp - event["at"]).total_seconds())
                if best is None or apart < best:
                    nearest, best = ping, apart
            if nearest is not None and best <= NEAREST_FIX_SECONDS:
                event["latitude"] = nearest.latitude
                event["longitude"] = nearest.longitude

        events.sort(key=lambda e: e["at"])

        # WHERE each entry happened, in words rather than coordinates.
        #
        # Free for anything tied to a call: the customer's address is already on
        # the case. Only a stop away from any customer and the place the trail
        # went dark need a coordinate resolved, and those come from the cache in
        # cases/places.py -- looked up once per place, ever, and budgeted so a
        # first look at a day costs about a second instead of five.
        by_number = {
            case.case_number: (case.address or "").strip()
            for case in day_cases.values()
            if case.case_number
        }
        for event in events:
            address = by_number.get(event.get("case_number") or "")
            if address:
                event["address"] = address

        unresolved = [
            (event["latitude"], event["longitude"])
            for event in events
            if not event.get("address")
            and event.get("latitude") is not None
            and event.get("longitude") is not None
        ]
        if unresolved:
            found = places.describe_many(unresolved)
            for event in events:
                if event.get("address"):
                    continue
                address = places.address_of(
                    found, event.get("latitude"), event.get("longitude")
                )
                if address:
                    event["address"] = address

        clean = _usable_pings(pings)
        return Response(
            {
                "engineer_id": engineer.id,
                "engineer_name": engineer.employee_name,
                "branch": engineer.branch,
                "date": target_date,
                "total_km": _trail_km(pings, context),
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

    # -- ROUTE KM -------------------------------------------------------------

    @action(detail=False, methods=["post"], url_path="route_km")
    def route_km(self, request):
        """ROUTE KM for the KM report: every engineer's road distance, day by
        day, through the places they marked -- Login, every Check in, Logout.

        Body: {"engineers": [<employee id>, ...], "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}

        Measuring is a metered call to Ola, paced per minute, so one request
        measures a few cells and says how many are still `pending`; the report
        polls until that is 0. Which cells are measured, and when one is
        measured again, is decided in cases/route_km.py.
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        data = request.data if isinstance(request.data, dict) else {}

        raw_ids = data.get("engineers")
        if not isinstance(raw_ids, list) or len(raw_ids) > route_km.MAX_ENGINEERS:
            return Response(
                {"detail": f"engineers must be a list of at most {route_km.MAX_ENGINEERS} ids."},
                status=400,
            )
        ids = []
        for raw in raw_ids:
            # An id as JSON carries it, or as a string of digits. A bool is an
            # int to Python, and a number past the database's integer would
            # overflow the query instead of matching nobody, so neither is one.
            # Its length is checked before int() is asked: Python refuses to
            # read a string of more than 4,300 digits, and that would be a 500.
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
                    "measured_now": 0,
                }
            )

        # Only the engineers this user may see, as on the day view. An id that
        # matches nobody is simply not in the answer.
        branches = get_allowed_branches(request.user, "attendance")
        visible = Employee.objects.filter(pk__in=ids)
        if "All" not in branches:
            visible = visible.filter(branch__in=branches)
        visible = set(visible.values_list("pk", flat=True))
        ids = [engineer_id for engineer_id in ids if engineer_id in visible]

        # One clock for the work and the count, so a cell read in this request
        # is never counted as still to be read.
        now = timezone.now()
        measured_now = route_km.refresh(ids, days, now=now)

        stored = route_km.rows(ids, days)
        # Two counts, so the report knows what it is waiting for: `pending`
        # cells wait on Ola; `rechecking` ones are today's, measured, whose
        # places may have moved since they were read. The report keeps asking
        # while either is above 0, and shows the km it has meanwhile.
        pending = 0
        rechecking = 0
        engineers = []
        for engineer_id in ids:
            cells = {}
            for day in days:
                row = stored.get((engineer_id, day))
                if route_km.is_pending(row, now):
                    pending += 1
                elif route_km.is_rechecking(row, now):
                    rechecking += 1
                if row is None or row.status == "pending":
                    cells[day.isoformat()] = {
                        "km": None, "points": 0, "status": "pending", "office": False, "final": False
                    }
                    continue
                cells[day.isoformat()] = {
                    "km": row.km if row.status == "ready" else None,
                    "points": len(row.points or []),
                    "status": row.status,
                    # A Login or Logout in it was put at the office, for want
                    # of a fix -- so the table can say so beside the km.
                    "office": any(point.get("office") for point in row.points or []),
                    # Settled for good: a failed day that is will not be tried
                    # again, and the table must not promise that it will.
                    "final": row.final,
                }
            engineers.append({"engineer_id": engineer_id, "days": cells})

        return Response(
            {
                "configured": True,
                "days": day_keys,
                "engineers": engineers,
                "pending": pending,
                "rechecking": rechecking,
                "measured_now": measured_now,
            }
        )

    @action(detail=False, methods=["post"], url_path="cases_by_day")
    def cases_by_day(self, request):
        """The KM report's case counts: for every engineer and day, how many
        calls the day timeline lists, how many were closed that day, and how
        many check-ins the day had -- the Live Tracking board's "CASES",
        "Closed" and "CHECK-INS", for a whole cycle at once (cases/day_cases.py).

        Body: {"engineers": [<employee id>, ...], "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}
        Answer: {"configured": true, "days": [...], "engineers": [{"engineer_id",
        "days": {"YYYY-MM-DD": {"cases": n, "closed": m, "checkins": k}}}]}
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        data = request.data if isinstance(request.data, dict) else {}

        # The engineers and the days, checked exactly as route_km checks them --
        # copied rather than shared, so ROUTE KM's own code is left as it is.
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

        # Nothing after today: a day that has not happened has no calls worked.
        today = timezone.localdate()
        end = min(end, today)
        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]

        # Only the engineers this user may see, as on the day view.
        branches = get_allowed_branches(request.user, "attendance")
        visible = Employee.objects.filter(pk__in=ids)
        if "All" not in branches:
            visible = visible.filter(branch__in=branches)
        visible = set(visible.values_list("pk", flat=True))
        ids = [engineer_id for engineer_id in ids if engineer_id in visible]

        counts = day_cases.cases_by_day(ids, days, today=today)
        return Response(
            {
                "configured": True,
                "days": [day.isoformat() for day in days],
                "engineers": [
                    {
                        "engineer_id": engineer_id,
                        "days": {day.isoformat(): counts[(engineer_id, day)] for day in days},
                    }
                    for engineer_id in ids
                ],
            }
        )

    @action(detail=False, methods=["get"], url_path="route_km_day")
    def route_km_day(self, request):
        """One ROUTE KM cell, for the map beside the table: the points in the
        order they were marked, Ola's road route through them, and the km.

        Query: ?engineer=<id>&date=YYYY-MM-DD

        Asking measures this one cell if it is due and the minute's allowance
        lasts. If it does not, the answer is "pending" with the points read now,
        so the map can show where they were while the km is waited for.
        """
        if not _is_staff_role(request.user):
            return Response({"detail": "Permission denied."}, status=403)

        raw_engineer = request.query_params.get("engineer") or ""
        if not (raw_engineer.isascii() and raw_engineer.isdigit()) or int(raw_engineer) >= 2**63:
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
        }
        if not olamaps.is_configured():
            answer["configured"] = False
            return Response(answer)

        route_km.refresh(
            [engineer.id], [target_date], work_limit=1, measure_limit=1, time_budget=15.0
        )
        row = route_km.rows([engineer.id], [target_date]).get((engineer.id, target_date))
        if row is None or row.status == "pending":
            # Not measured yet: the minute's allowance is spent, or Ola has
            # asked us to wait. The points cost nothing, so the map has them.
            answer["points"] = route_km.key_points(engineer, target_date)
            return Response(answer)

        ready = row.status == "ready"
        answer.update(
            status=row.status,
            km=row.km if ready else None,
            points=row.points or [],
            path=(row.path or []) if ready else [],
            error=row.error if row.status == "failed" else "",
            final=row.final,
        )
        return Response(answer)
