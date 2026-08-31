"""The cache in front of Ola's snapToRoad.

The board asks for an engineer's trail every 30 seconds. Snapping on each of
those asks would spend a request on a route that has barely changed, so the
snapped positions are stored and only genuinely new fixes are ever sent.

Two rules keep the request count at fixes / 50:

  A fix is snapped once, ever. `SnappedTrack.last_ping_id` marks how far the
  stored trail goes; only ids past it are sent, and what comes back is appended.

  A partial batch is not sent while the day can still grow. Today's newest fixes
  wait until there are 50 of them; until then they are drawn raw, which is a few
  minutes of slightly rough line at the head of the trail and nobody can tell.
  A day that is over is finalised, partial batch and all, because it will never
  fill.

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


def snapped_trail(engineer_id: int, day: datetime.date, pings: list) -> dict:
    """The trail for one engineer-day, on roads where we have been able to put it.

    `pings` is the already-filtered, already-ordered list the caller is about to
    serve, so this never re-queries and never disagrees with the raw points in
    the same response.

    Returns:
        points  — [[lat, lon], ...] in travel order, snapped where possible
        snapped — how many of those came from Ola
        raw     — how many are the phone's own coordinates
        source  — "ola", "partial" (snapped body, raw tail), or "raw"
    """
    if not pings:
        return {"points": [], "snapped": 0, "raw": 0, "source": "raw"}

    if not olamaps.is_configured():
        return {"points": _coords(pings), "snapped": 0, "raw": len(pings), "source": "raw"}

    is_closed_day = day < timezone.localdate()

    try:
        with transaction.atomic():
            track, _ = SnappedTrack.objects.get_or_create(
                engineer_id=engineer_id, day=day
            )

            pending = [p for p in pings if p.id > track.last_ping_id]

            # A closed day is final, so even a half batch is worth sending. An
            # open one waits: those fixes will still be there in ten minutes,
            # and by then there will be a full request's worth of them.
            if is_closed_day:
                to_send = pending
            else:
                whole_batches = (len(pending) // BATCH) * BATCH
                to_send = pending[:whole_batches]

            if to_send:
                original = [(p.latitude, p.longitude) for p in to_send]
                snapped = olamaps.snap_to_road(original)

                # Only comparable one-to-one because enhance_path is off. If it
                # is ever turned on the lengths diverge and this check has to
                # change with it.
                if len(snapped) == len(original):
                    shift = _mean_shift_metres(original, snapped)
                    if shift > MAX_MEAN_SHIFT_METERS:
                        raise olamaps.SnapUnavailable(
                            "snap moved the trail %.0f m on average, past the %.0f m we trust"
                            % (shift, MAX_MEAN_SHIFT_METERS)
                        )

                track.points = list(track.points or []) + [[lat, lon] for lat, lon in snapped]
                track.last_ping_id = to_send[-1].id
                track.save(update_fields=["points", "last_ping_id", "updated_at"])

            stored = list(track.points or [])
            tail = [p for p in pings if p.id > track.last_ping_id]

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
