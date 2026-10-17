"""The cache in front of Ola's snapToRoad.

The board asks for an engineer's trail every 30 seconds. Snapping on each of
those asks would spend a request on a route that has barely changed, so the
snapped positions are stored and only fixes they do not already cover are sent.

Three rules keep the route in the order it was travelled and the requests few:

  The stored route follows the trail, fix for fix. `SnappedTrack.ping_ids`
  names the fix behind each stored point, and a read keeps the stored points
  only as far as they still match the trail it was handed -- the same fixes in
  the same order. Everything after that is snapped as new. A fix that arrives
  late (held on the phone while it was offline or the app was closed) has a new
  id but an old time, so it belongs in the middle of the trail: the stored route
  is cut where it lands and snapped again from there. Adding fixes by id put it
  at the end, and the line jumped back across the map to draw it.

  A partial batch is not sent while the day can still grow. Today's newest fixes
  wait until there are 50 of them; until then they are drawn raw, which is a few
  minutes of slightly rough line at the head of the trail and nobody can tell.
  A day that is over is finalised, partial batch and all, because it will never
  fill.

  A read sends at most two requests. Ola limits requests per minute and the
  board reads every 30 seconds, so a long backlog -- a day first opened after it
  ended, a route cut early by a late fix -- is worked through over a few reads,
  drawn raw past the snapped part until it is done.

Nothing here can break the trail. Every failure path returns the raw fixes.
"""

from __future__ import annotations

import datetime
import logging

from django.db import transaction
from django.utils import timezone

from . import olamaps
from .models import SnappedTrack

logger = logging.getLogger(__name__)

# One request's worth. Sending fewer than this wastes a request on a trail that
# is about to grow, so today's tail waits until it fills.
BATCH = olamaps.MAX_POINTS_PER_REQUEST

# Requests one read may send. Ola limits requests per minute as well as per
# month (see olamaps), and the board reads every 30 seconds. What is past this
# waits, drawn raw, for the next read.
MAX_REQUESTS_PER_READ = 2

# How far a snapped fix may sit from where the phone put it before we stop
# believing the snap.
#
# Ola does not move each fix to its own nearest road: it path-matches, fitting
# the whole sequence to a route. That is what makes a real trail follow a real
# street, and it is also how a trail it cannot match cleanly — a gap in the
# fixes, a walk across a campus, a road it does not know — comes back projected
# onto some other road entirely. Measured on synthetic trails it moved points an
# average of 585 m and as much as 2.3 km.
#
# Fixes worse than 100 m accuracy are already dropped upstream, so a genuine
# snap moves a fix by road-width plus GPS error. An average shift past this is
# not a correction, it is a different journey, and the raw trail is the honest
# thing to draw.
MAX_MEAN_SHIFT_METERS = 200.0


def _mean_shift_metres(original, snapped) -> float:
    """Average distance between each fix and where it was snapped to."""
    from .views import haversine_km

    pairs = list(zip(original, snapped))
    if not pairs:
        return 0.0
    total = sum(
        haversine_km(lat, lon, slat, slon) * 1000 for (lat, lon), (slat, slon) in pairs
    )
    return total / len(pairs)


def _coords(pings) -> list[list[float]]:
    return [[p.latitude, p.longitude] for p in pings]


def _legacy_prefix(pings, last_ping_id: int, stored: int) -> int:
    """How many points of a track stored before `ping_ids` are pings[:n], snapped.

    Such a track has no record of which fix each point came from. Its points
    were appended a batch at a time -- the fixes with ids past `last_ping_id`,
    in the order the trail was then read -- so stored point i is pings[i] only
    while the fixes arrived in the order they were taken. The trusted run ends
    at the first fix that:

      came after the track was last snapped (an id past `last_ping_id`);

      is late: a higher id than a fix taken after it. The old track appended it
      behind fixes it belongs in front of. Its id can sit within
      `last_ping_id`, so the fixes up to `last_ping_id` being the right ones is
      not enough; they also have to be in the right order;

      shares its second with the next fix. Those were read by time alone, in no
      fixed order, so which stored point is which cannot be told.

    Never more than `stored`, the number of points the track holds.
    """
    # The smallest id after each position: a fix is late when one taken after
    # it has a smaller id.
    smallest_after = [None] * len(pings)
    smallest = None
    for i in range(len(pings) - 1, -1, -1):
        smallest_after[i] = smallest
        if smallest is None or pings[i].id < smallest:
            smallest = pings[i].id

    limit = min(stored, len(pings))
    for i in range(limit):
        ping = pings[i]
        if ping.id > last_ping_id:
            return i
        if smallest_after[i] is not None and smallest_after[i] < ping.id:
            return i
        if i + 1 < len(pings) and pings[i + 1].timestamp == ping.timestamp:
            return i
    return limit


def snapped_trail(engineer_id: int, day: datetime.date, pings: list) -> dict:
    """The trail for one engineer-day, on roads where we have been able to put it.

    `pings` is the already-filtered list the caller is about to serve, in the
    order it was travelled (time, then id), so this never re-queries and never
    disagrees with the raw points in the same response.

    Returns:
        points  — [[lat, lon], ...] in the order of `pings`, snapped where possible
        snapped — how many of those came from Ola (always the first ones)
        raw     — how many are the phone's own coordinates (the rest)
        source  — "ola", "partial" (snapped body, raw tail), or "raw"
    """
    if not pings:
        return {"points": [], "snapped": 0, "raw": 0, "source": "raw"}

    if not olamaps.is_configured():
        return {"points": _coords(pings), "snapped": 0, "raw": len(pings), "source": "raw"}

    is_closed_day = day < timezone.localdate()
    ids = [p.id for p in pings]

    try:
        with transaction.atomic():
            track, _ = SnappedTrack.objects.get_or_create(
                engineer_id=engineer_id, day=day
            )
            had_points = list(track.points or [])
            had_ids = list(track.ping_ids or [])

            # How far the stored route is still this trail. A late fix, or a fix
            # no longer in the trail, ends it; the rest is snapped again.
            if had_ids:
                keep = 0
                limit = min(len(had_ids), len(had_points), len(ids))
                while keep < limit and had_ids[keep] == ids[keep]:
                    keep += 1
            elif had_points:
                # Stored before ping_ids existed. Kept as far as it can be shown
                # to fit, which for a day with no late fix is all of it: no
                # request spent on a route that is already right.
                keep = _legacy_prefix(pings, track.last_ping_id, len(had_points))
            else:
                keep = 0

            stored = had_points[:keep]
            pending = pings[keep:]

            # A closed day is final, so even a half batch is worth sending. An
            # open one waits: those fixes will still be there in ten minutes,
            # and by then there will be a full request's worth of them.
            most = MAX_REQUESTS_PER_READ * BATCH
            if is_closed_day:
                to_send = pending[:most]
            else:
                whole_batches = (len(pending) // BATCH) * BATCH
                to_send = pending[: min(whole_batches, most)]

            if to_send:
                original = [(p.latitude, p.longitude) for p in to_send]
                snapped = olamaps.snap_to_road(original)

                # One point back for every fix sent, which is what lets each
                # stored point name its fix. enhance_path is off, so another
                # count is a reply that cannot be lined up with the fixes, and
                # the shift check below only compares one-to-one.
                if len(snapped) != len(original):
                    raise olamaps.SnapUnavailable(
                        "snap returned %d points for %d fixes" % (len(snapped), len(original))
                    )
                shift = _mean_shift_metres(original, snapped)
                if shift > MAX_MEAN_SHIFT_METERS:
                    raise olamaps.SnapUnavailable(
                        "snap moved the trail %.0f m on average, past the %.0f m we trust"
                        % (shift, MAX_MEAN_SHIFT_METERS)
                    )

                stored += [[lat, lon] for lat, lon in snapped]

            stored_ids = ids[: len(stored)]
            if to_send or keep != len(had_points) or stored_ids != had_ids:
                track.points = stored
                track.ping_ids = stored_ids
                # Still the highest id snapped, for anything that reads it.
                track.last_ping_id = max(stored_ids, default=0)
                track.save(update_fields=["points", "ping_ids", "last_ping_id", "updated_at"])

            tail = pings[len(stored):]

    except olamaps.SnapUnavailable as exc:
        # Ola is the optional half of this. Say so once, quietly, and hand back
        # the trail the phone reported.
        logger.warning("snapToRoad unavailable for engineer %s on %s: %s", engineer_id, day, exc)
        return {"points": _coords(pings), "snapped": 0, "raw": len(pings), "source": "raw"}
    except Exception:
        logger.exception("snapped trail failed for engineer %s on %s", engineer_id, day)
        return {"points": _coords(pings), "snapped": 0, "raw": len(pings), "source": "raw"}

    points = stored + _coords(tail)
    if not stored:
        source = "raw"
    elif tail:
        source = "partial"
    else:
        source = "ola"

    return {
        "points": points,
        "snapped": len(stored),
        "raw": len(tail),
        "source": source,
    }
