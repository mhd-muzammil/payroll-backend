"""Ola Maps, for putting an engineer's GPS trail onto actual roads.

A trail is a list of fixes taken every 30 seconds. Joining them with straight
lines draws a route that cuts corners and crosses buildings — it is legible as
"roughly there", not as "this is where they went". Ola's snapToRoad moves each
fix onto the nearest routable road segment, which is what makes the drawn route
match the road the engineer was actually on.

Two rules this module keeps:

  Nothing here is allowed to break tracking. Every failure — no key, a timeout,
  a 500, a malformed body — raises SnapUnavailable, and the caller falls back to
  the raw fixes. A map with a rough route beats a map that will not load.

  The key never leaves the server. It is read from the environment and put in
  the query string of a server-side request; it is never serialised into a
  response, logged, or sent to a browser.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

SNAP_URL = "https://api.olamaps.io/routing/v1/snapToRoad"

# "It supports up to 50 latitude/longitude pairs per request" — Ola's own docs.
# Batching to this is what keeps the request count at trail_length / 50 rather
# than one call per fix.
MAX_POINTS_PER_REQUEST = 50

TIMEOUT_SECONDS = 20

# Ola rate-limits per minute as well as per month, which its pricing page does
# not mention: 15 quick requests in a row earned a 429 while the monthly count
# was still in the twenties. A trail long enough to need several batches has to
# pace itself, so batches are spaced and a 429 is retried once.
BATCH_PAUSE_SECONDS = 0.4
RETRY_PAUSE_SECONDS = 2.0


class SnapUnavailable(Exception):
    """Snapping could not be done. The caller should use the raw points."""


class RateLimited(SnapUnavailable):
    """Ola is throttling us. Worth trying again later, not worth trying harder."""


def api_key() -> str:
    """The key, or "" when it has not been configured.

    Read at call time rather than import time so a key added to the environment
    takes effect on the next request instead of the next restart.
    """
    try:
        from django.conf import settings

        key = getattr(settings, "OLA_MAPS_API_KEY", "") or ""
    except Exception:
        key = ""
    return (key or os.environ.get("OLA_MAPS_API_KEY", "") or "").strip()


def is_configured() -> bool:
    return bool(api_key())


def _fetch(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # The body often says which parameter Ola disliked, and that is the only
        # way to tell a bad key from a bad coordinate. The URL is NOT logged:
        # the key is in it.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code == 429:
            raise RateLimited("rate limited by Ola: %s" % detail) from exc
        raise SnapUnavailable("HTTP %s from Ola: %s" % (exc.code, detail)) from exc
    except Exception as exc:
        raise SnapUnavailable("%s: %s" % (type(exc).__name__, exc)) from exc

    try:
        return json.loads(body)
    except ValueError as exc:
        raise SnapUnavailable("Ola returned a body that is not JSON") from exc


def _snap_batch(points: list[tuple[float, float]], enhance_path: bool) -> list[tuple[float, float]]:
    """One request. Returns the snapped coordinates for `points`, in order."""
    key = api_key()
    if not key:
        raise SnapUnavailable("OLA_MAPS_API_KEY is not set")

    query = urllib.parse.urlencode(
        {
            "points": "|".join("%s,%s" % (lat, lon) for lat, lon in points),
            "enhancePath": "true" if enhance_path else "false",
            "api_key": key,
        },
        safe="|,",
    )
    payload = _fetch(SNAP_URL + "?" + query)

    snapped_raw = payload.get("snapped_points")
    if not isinstance(snapped_raw, list):
        raise SnapUnavailable("no snapped_points in Ola's response")

    out: list[tuple[float, float]] = []
    for item in snapped_raw:
        if not isinstance(item, dict):
            continue
        location = item.get("location") or {}
        lat, lon = location.get("lat"), location.get("lng")
        if lat is None or lon is None:
            continue

        # NoSegment means Ola found no road near this fix — a village track, a
        # site with no mapped access road. Snapping it would drag the route to
        # whatever road happened to be nearest, which is worse than leaving the
        # fix where the phone put it, so the original coordinate is kept.
        if item.get("snapped_type") == "NoSegment":
            index = item.get("original_index")
            if isinstance(index, int) and 0 <= index < len(points):
                out.append(points[index])
                continue

        out.append((float(lat), float(lon)))

    if not out:
        raise SnapUnavailable("Ola returned no usable points")
    return out


def snap_to_road(
    points: list[tuple[float, float]], enhance_path: bool = False
) -> list[tuple[float, float]]:
    """Put a trail onto roads. Raises SnapUnavailable rather than returning junk.

    `enhance_path` is Ola's documented option to add intermediate points so the
    line follows a road's curves instead of cutting between fixes. Measured
    against the live API in August 2026 it changed nothing: two fixes 3 km apart
    came back as two points with it on and two with it off. So it is off by
    default and no caller should rely on it. If Ola starts honouring it, turning
    it on would make the result LONGER than the input, and callers must not
    assume the two line up one-to-one.

    Our fixes are 30 seconds apart — around 250 m at city speed — so what makes
    the route look right is that each point is on the road, not that the line
    between them is curved.
    """
    if not points:
        return []

    snapped: list[tuple[float, float]] = []
    for start in range(0, len(points), MAX_POINTS_PER_REQUEST):
        if start:
            # Pace the batches: the per-minute limit is easy to trip on a long
            # trail, and a 429 halfway through wastes the batches already spent.
            time.sleep(BATCH_PAUSE_SECONDS)
        batch = points[start : start + MAX_POINTS_PER_REQUEST]
        try:
            snapped.extend(_snap_batch(batch, enhance_path))
        except RateLimited:
            # One retry, then leave it. The caller falls back to the raw trail
            # and the next read will try again — nothing is lost but a little
            # road-following on this one view.
            time.sleep(RETRY_PAUSE_SECONDS)
            snapped.extend(_snap_batch(batch, enhance_path))
    return snapped
