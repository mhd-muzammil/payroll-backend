"""The Day Report: what the Live Tracking day panel says about each
engineer-day, for many engineers and days at once, to read on screen and to
download.

Per engineer and day, from the helpers the day panel
(views.TrackingViewSet.day) builds its rail with, so the two cannot disagree:

  Login / Logout   the duty sessions that began on the day, each placed at the
                   fix nearest in time, within 15 minutes, as the rail places
                   them;
  Check in / out   the day's punches (views._punches_for_day) -- the board's
                   CHECK-INS -- where the engineer stood when they tapped, or
                   the customer's pin when that was not recorded;
  Assigned / Attended / Closed   OpenCall's own numbers for the day, as
                   Engineer Productivity shows them (EngineerScorecardDay);
  Waiting          the stops on duty (views._detect_stops), the rail's
                   "Waiting N min";
  GPS / Net        the phone's own GPS off / on and Net off / on entries
                   (views._phone_event_entries), and how long each was off
                   while the engineer was on duty;
  Total km         the day's km as the KM report has it (views._DayKm).

Read only, and it never calls Ola: an address is the customer's, for anything
tied to a call, or one the place cache already holds (cases/places.py, never
looked up from here). Anything else is given by its coordinates alone.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import timedelta

from django.utils import timezone

from . import km_rule, places
from .models import Case, EngineerScorecardDay

# The most days one request may cover; the report asks a day at a time.
MAX_DAYS = 31


def _nearest(times, located, moment, within):
    """The fix nearest in time to `moment`, if one is within `within` seconds."""
    if not located:
        return None
    index = bisect_left(times, moment)
    best = None
    for candidate in located[max(0, index - 1): index + 1]:
        apart = abs((candidate.timestamp - moment).total_seconds())
        if best is None or apart < best[0]:
            best = (apart, candidate)
    if best is None or best[0] > within:
        return None
    return best[1]


def _off_minutes(entries, off_kind, on_kind, on_duty, day_start, day_end, now):
    """How long the phone said `off_kind` was off, counted only while the
    engineer was on duty. An off never turned back on that day runs to the
    end of the duty it fell in."""
    spans = []
    opened = None
    for entry in sorted(entries, key=lambda e: e["at"]):
        if entry["type"] == off_kind and opened is None:
            opened = entry["at"]
        elif entry["type"] == on_kind and opened is not None:
            spans.append((opened, entry["at"]))
            opened = None
    if opened is not None:
        spans.append((opened, min(now, day_end)))
    # A day with no Login of its own counts the whole day, as the rail shows
    # every entry of such a day (views._on_duty_at).
    windows = [
        (max(session.started_at, day_start), min(session.ended_at or now, day_end))
        for session in on_duty
    ] or [(day_start, min(now, day_end))]
    total = timedelta()
    for start, end in spans:
        for window_start, window_end in windows:
            overlap = min(end, window_end) - max(start, window_start)
            if overlap > timedelta():
                total += overlap
    return int(total.total_seconds() // 60)


def build(engineers, days):
    """{(engineer id, day): {...}} for every engineer and day asked about.

    Each value holds lists of moments -- {"at", "address", "latitude",
    "longitude", ...} -- and the day's figures; see the module docstring.
    """
    from . import views  # views imports this module

    ids = [engineer.id for engineer in engineers]
    now = timezone.now()
    cells = {}
    wanted_places = []

    for day in days:
        day_start, day_end = km_rule.day_bounds(day)
        # Every event kind, as the day panel reads them: the rail needs GPS,
        # Net and the app's own entries. The km come out as the KM report's.
        day_km = views._DayKm(ids, day, kinds=None)
        # The calls the fixes were tagged with, in one query rather than one
        # per stop, with the customer's address and pin.
        tagged = {ping.case_id for trail in day_km.trails.values() for ping in trail if ping.case_id}
        tagged_cases = Case.objects.only(
            "case_number", "external_ref", "address", "latitude", "longitude"
        ).in_bulk(tagged)
        for trail in day_km.trails.values():
            for ping in trail:
                if ping.case_id in tagged_cases:
                    ping.case = tagged_cases[ping.case_id]
        scores = {
            row.engineer_id: row
            for row in EngineerScorecardDay.objects.filter(engineer_id__in=ids, day=day)
        }

        for engineer in engineers:
            pings = day_km.trails.get(engineer.id, [])
            context = day_km.contexts[engineer.id]
            located = [ping for ping in pings if ping.latitude is not None]
            times = [ping.timestamp for ping in located]

            def nearest_place(moment, latitude=None, longitude=None):
                if latitude is None or longitude is None:
                    fix = _nearest(times, located, moment, views.NEAREST_FIX_SECONDS)
                    if fix is not None:
                        latitude, longitude = fix.latitude, fix.longitude
                return {"latitude": latitude, "longitude": longitude}

            sessions = [
                session
                for session in context.sessions
                if timezone.localtime(session.started_at).date() == day
            ]
            on_duty = (
                [
                    session
                    for session in context.sessions
                    if session.started_at < day_end
                    and (session.ended_at is None or session.ended_at >= day_start)
                ]
                if sessions
                else []
            )

            logins = []
            logouts = []
            still_on_duty = False
            for session in sorted(sessions, key=lambda s: s.started_at):
                logins.append({"at": session.started_at, **nearest_place(session.started_at)})
                if session.ended_at is None:
                    still_on_duty = True
                    continue
                logouts.append(
                    {
                        "at": session.ended_at,
                        "auto": bool(session.auto_closed),
                        **nearest_place(session.ended_at),
                    }
                )

            punches = views._punches_for_day(engineer, day)
            punched_cases = Case.objects.only(
                "external_ref", "address", "latitude", "longitude"
            ).in_bulk({punch["case_id"] for punch in punches})
            check_ins = []
            check_outs = []
            for punch in punches:
                case = punched_cases.get(punch["case_id"])
                latitude, longitude = punch["latitude"], punch["longitude"]
                if (latitude is None or longitude is None) and case is not None:
                    latitude, longitude = case.latitude, case.longitude
                entry = {
                    "at": punch["at"],
                    "case_number": punch["case_number"],
                    "case_ref": (case.external_ref or None) if case else None,
                    "address": ((case.address or "").strip() or None) if case else None,
                    "latitude": latitude,
                    "longitude": longitude,
                }
                (check_ins if punch["kind"] == "in" else check_outs).append(entry)

            waits = []
            for stop in views._detect_stops(pings):
                if not views._on_duty_at(on_duty, stop["arrived_at"]):
                    continue
                case = tagged_cases.get(stop["case_id"]) if stop["case_id"] else None
                waits.append(
                    {
                        "at": stop["arrived_at"],
                        "minutes": stop["minutes"],
                        "case_number": stop["case_number"],
                        "address": ((case.address or "").strip() or None) if case else None,
                        "latitude": stop["latitude"],
                        "longitude": stop["longitude"],
                    }
                )

            phone = views._phone_event_entries(context.events, on_duty, pings, day_start, day_end)

            def moments(kind):
                return [
                    {"at": entry["at"], **nearest_place(entry["at"], entry["latitude"], entry["longitude"])}
                    for entry in phone
                    if entry["type"] == kind
                ]

            score = scores.get(engineer.id)
            cell = {
                "logins": logins,
                "logouts": logouts,
                "on_duty": still_on_duty,
                "check_ins": check_ins,
                "check_outs": check_outs,
                "assigned": score.assigned if score else None,
                "attended": score.attended if score else None,
                "closed": score.closed if score else None,
                "waiting_minutes": sum(wait["minutes"] for wait in waits),
                "waits": waits,
                "gps_off_minutes": _off_minutes(
                    phone, "gps_off", "gps_on", on_duty, day_start, day_end, now
                ),
                "gps_offs": moments("gps_off"),
                "gps_ons": moments("gps_on"),
                "net_off_minutes": _off_minutes(
                    phone, "net_off", "net_on", on_duty, day_start, day_end, now
                ),
                "net_offs": moments("net_off"),
                "net_ons": moments("net_on"),
                "km": day_km.km(engineer.id),
            }
            cells[(engineer.id, day)] = cell
            for key in (
                "logins", "logouts", "check_ins", "check_outs", "waits",
                "gps_offs", "gps_ons", "net_offs", "net_ons",
            ):
                wanted_places.extend(cell[key])

    # Addresses for what is not tied to a call: the cache only, never Ola.
    unnamed = [
        (moment["latitude"], moment["longitude"])
        for moment in wanted_places
        if not moment.get("address") and moment["latitude"] is not None
    ]
    known = places.describe_many(unnamed, budget=0) if unnamed else {}
    for moment in wanted_places:
        if not moment.get("address"):
            moment["address"] = places.address_of(known, moment["latitude"], moment["longitude"])
    return cells
