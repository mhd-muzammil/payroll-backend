"""Taking in location fixes, one at a time or as a backlog.

An engineer's phone loses signal — a basement, a lift, a village stretch — and
the fixes it takes while offline are kept on the phone and posted when the
signal returns. So a fix carries two times: when it HAPPENED, which the phone
knows, and when we RECEIVED it, which only we know.

The phone's time is taken but not trusted. A wrong clock or a tampered request
would otherwise rewrite a day's route, so a timestamp in the future or older
than the backlog we are willing to accept is refused, and received_at is always
stamped here.

Replaying a batch must not double the trail. A phone that posts a backlog, times
out waiting for the answer, and posts it again has sent the same fixes twice —
so each fix carries the phone's own id for it and a repeat is skipped rather
than stored.
"""

from __future__ import annotations

import datetime
import logging

from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

# A phone's clock is often a little off. Anything further ahead than this is not
# skew, it is wrong, and a fix stamped in the future would sort to the end of
# every route it appears in.
MAX_CLOCK_SKEW_MINUTES = 5

# How much backlog is worth accepting. A phone offline for two days has a route
# nobody is going to act on, and accepting older would let a stale queue rewrite
# a week of history.
MAX_BACKLOG_HOURS = 48

# Most fixes a single batch may carry. A day of duty is about 1,080, so this is
# roughly half a day — enough for a real outage, small enough that one request
# cannot tie up the database.
MAX_BATCH = 500


class PingRejected(ValueError):
    """This fix cannot be stored. The message says why, for the client's log."""


def _coord(value, name):
    if value is None:
        raise PingRejected("%s is required." % name)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise PingRejected("Invalid %s." % name)


def coerce_number(value):
    """A float, or None when the value is missing or not a number.

    Public because the case punch endpoints need the same leniency: a phone with
    no fix sends nothing, and that must read as "no position", not as an error.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def battery_percent(value):
    """0-100, or None. Accepts the 0-1 fraction Capacitor reports as well.

    @capacitor/device gives batteryLevel as 0.0-1.0. Sending it straight through
    would store 1 for a full battery and 0 for an empty one, which reads as 1%
    and is indistinguishable from a dying phone.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if 0.0 < number <= 1.0:
        number *= 100
    percent = int(round(number))
    if percent < 0 or percent > 100:
        return None
    return percent


def _flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no"):
        return False
    return None


def resolve_timestamp(raw, now=None) -> datetime.datetime:
    """When this fix was taken, as far as we are willing to believe.

    Falls back to now when the phone sends nothing — a live ping, which is the
    ordinary case. Refuses a time that is impossible rather than storing it:
    accepting the future breaks every route's ordering, and accepting the distant
    past lets a forgotten queue overwrite settled history.
    """
    now = now or timezone.now()
    if raw in (None, ""):
        return now

    when = parse_datetime(raw) if isinstance(raw, str) else raw
    if not isinstance(when, datetime.datetime):
        raise PingRejected("timestamp must be an ISO 8601 datetime.")
    if timezone.is_naive(when):
        when = timezone.make_aware(when)

    if when > now + datetime.timedelta(minutes=MAX_CLOCK_SKEW_MINUTES):
        raise PingRejected("timestamp is in the future.")
    if when < now - datetime.timedelta(hours=MAX_BACKLOG_HOURS):
        raise PingRejected("timestamp is older than %d hours." % MAX_BACKLOG_HOURS)
    return when


def build_ping(employee, payload, case_lookup, now=None):
    """An unsaved LocationPing from one posted fix. Raises PingRejected."""
    from .models import LocationPing

    now = now or timezone.now()
    latitude = _coord(payload.get("latitude"), "latitude")
    longitude = _coord(payload.get("longitude"), "longitude")

    key = payload.get("client_key")
    key = str(key)[:64] if key not in (None, "") else None

    return LocationPing(
        engineer=employee,
        case=case_lookup(payload.get("case_id")),
        latitude=latitude,
        longitude=longitude,
        accuracy=coerce_number(payload.get("accuracy")),
        speed=coerce_number(payload.get("speed")),
        status=str(payload.get("status") or "")[:20],
        battery_level=battery_percent(payload.get("battery_level")),
        is_charging=_flag(payload.get("is_charging")),
        timestamp=resolve_timestamp(payload.get("timestamp"), now),
        received_at=now,
        client_key=key,
        # The phone telling us it had stopped tracking before this fix. Trusted
        # because it is the only thing that can know, and because getting it
        # wrong costs the engineer distance rather than earning them any.
        # bool(), not _flag() alone: _flag returns None for anything it does not
        # recognise, and this column is NOT NULL. Every ping from an app that
        # predates the flag omits it entirely, so None is the ORDINARY case and
        # storing it would 500 the whole tracking endpoint.
        after_gap=bool(_flag(payload.get("after_gap"))),
    )


def ingest_batch(employee, payloads, case_lookup):
    """Store a backlog. Returns what happened to each fix, by count.

    Duplicates are filtered before writing rather than caught afterwards: a
    single IntegrityError inside a bulk insert takes the whole batch down with
    it, and a phone retrying a batch it already delivered is the expected case,
    not an error.
    """
    from .models import LocationPing

    if not isinstance(payloads, list):
        raise PingRejected("pings must be a list.")
    if not payloads:
        return {"stored": 0, "duplicates": 0, "rejected": []}
    if len(payloads) > MAX_BATCH:
        raise PingRejected("a batch may carry at most %d fixes." % MAX_BATCH)

    now = timezone.now()
    candidates = []
    rejected = []
    for index, payload in enumerate(payloads):
        if not isinstance(payload, dict):
            rejected.append({"index": index, "reason": "each fix must be an object."})
            continue
        try:
            candidates.append(build_ping(employee, payload, case_lookup, now))
        except PingRejected as exc:
            rejected.append({"index": index, "reason": str(exc)})

    # Skip what we already hold, and what the batch repeats within itself.
    keys = [p.client_key for p in candidates if p.client_key]
    already = set(
        LocationPing.objects.filter(engineer=employee, client_key__in=keys).values_list(
            "client_key", flat=True
        )
    )
    fresh = []
    duplicates = 0
    seen_here = set()
    for candidate in candidates:
        key = candidate.client_key
        if key and (key in already or key in seen_here):
            duplicates += 1
            continue
        if key:
            seen_here.add(key)
        fresh.append(candidate)

    LocationPing.objects.bulk_create(fresh, batch_size=200)
    return {"stored": len(fresh), "duplicates": duplicates, "rejected": rejected}
