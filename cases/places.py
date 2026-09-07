"""Coordinates as places, so a day reads as somewhere rather than as numbers.

The office asked for the address on every timeline entry, the way the tracking
product they compared us against shows it. Most of them cost nothing: a call
the engineer reached already carries the customer's address in Payroll. What is
left is where they stood still away from a customer, and where the trail went
dark -- and for those a coordinate has to be turned into a street.

Three things this is careful about, because Ola is metered and rate-limited on
a per-minute window its pricing page does not mention:

  Cached forever, by rounded coordinate. An engineer who visits the same
  industrial estate every week costs one lookup, once.
  Budgeted per request. A day with fifteen unknown coordinates does not hold
  the panel open for four seconds; it fills in what it can and the rest are
  simply left without an address.
  Silent on failure. No address is a missing line, never an error page. The
  timeline is about the day, not about our geocoder.
"""

import logging
import time

from django.db import IntegrityError
from django.utils import timezone

from cases import olamaps
from cases.models import PlaceName

logger = logging.getLogger(__name__)

# ~11 metres. Fine enough to tell two customers on one street apart, coarse
# enough that standing in the same yard twice is one cache entry.
PRECISION = 4

# Ola throttles per minute. The trail snapper already learned this the hard way,
# so lookups are spaced the same way.
PAUSE_SECONDS = 0.4

# How many unknown coordinates one page load may look up. The rest wait for the
# next look at that day, by which time these are cached.
DEFAULT_BUDGET = 6

REVERSE_URL = "https://api.olamaps.io/places/v1/reverse-geocode"


def _key(value):
    return round(float(value), PRECISION)


def _fetch_address(lat, lon):
    """One reverse lookup, or None. Never raises."""
    try:
        payload = olamaps.fetch_json(REVERSE_URL, {"latlng": f"{lat},{lon}"})
    except Exception as exc:  # noqa: BLE001 -- an address is never worth a 500
        logger.info("reverse geocode failed for %s,%s: %s", lat, lon, exc)
        return None
    results = payload.get("results") or []
    if not results:
        return None
    address = (results[0].get("formatted_address") or "").strip()
    return address or None


def describe_many(coordinates, budget=DEFAULT_BUDGET):
    """{(lat, lon) rounded: address} for as many as are known or affordable.

    Reads the cache in one query, then spends at most `budget` lookups on what
    is missing. A coordinate that cannot be resolved is absent from the result
    rather than present and empty, so the caller cannot mistake "we do not
    know" for "there is nothing there".
    """
    wanted = {(_key(lat), _key(lon)) for lat, lon in coordinates if lat is not None and lon is not None}
    if not wanted:
        return {}

    known = {
        (float(row.lat_key), float(row.lon_key)): row.address
        for row in PlaceName.objects.filter(
            lat_key__in=[lat for lat, _ in wanted], lon_key__in=[lon for _, lon in wanted]
        )
        if (float(row.lat_key), float(row.lon_key)) in wanted
    }

    missing = [point for point in sorted(wanted) if point not in known]
    if not missing or not olamaps.is_configured():
        return known

    for index, (lat, lon) in enumerate(missing[:budget]):
        if index:
            time.sleep(PAUSE_SECONDS)
        address = _fetch_address(lat, lon)
        if not address:
            continue
        known[(lat, lon)] = address
        try:
            PlaceName.objects.create(
                lat_key=lat, lon_key=lon, address=address, fetched_at=timezone.now()
            )
        except IntegrityError:
            # Another request cached the same place first. Nothing to do.
            pass

    if len(missing) > budget:
        logger.info(
            "reverse geocode budget spent: %d of %d looked up", budget, len(missing)
        )
    return known


def address_of(lookup, lat, lon):
    """The address for one coordinate out of a describe_many() result."""
    if lat is None or lon is None:
        return None
    return lookup.get((_key(lat), _key(lon)))
