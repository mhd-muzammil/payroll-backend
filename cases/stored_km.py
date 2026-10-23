"""ROUTE KM as it is stored, for readers that must not measure anything.

/api/tracking/route_km/ is the KM report's: every call reads a few cells,
measures a few with Ola and writes what it found. That is right for a page the
office is watching fill in, and wrong for anything that only wants the figures
already there -- a report run from Claude through OpenCall's MCP, or a nightly
job that measures first and reads afterwards. This module is that read: the
stored rows, nothing asked of Ola, nothing written.

A cell says how far it has got, so the reader can tell a gap that will close
from one that will not:

  not_measured  no row: nobody has asked for this day yet
  pending       a row waiting to be measured
  ready         measured; `km` is the figure (0.0 is an answer, not a gap)
  no_points     fewer than two places marked that day: nothing to measure
  failed        Ola could not route it; `final` says whether it will be tried again

Counts only, never places: `points` is how many points the km runs through,
and nothing of a point -- where, when, which case -- leaves this module.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils.dateparse import parse_date

from authentication.models import get_allowed_branches
from employees.models import Employee

from . import route_km


def parse_grid(params, today):
    """The engineers and days a stored-km GET asks for, checked exactly as
    route_km's POST checks its body: at most route_km.MAX_ENGINEERS ids and
    route_km.MAX_DAYS days, from not after to, nothing after today.

    Query: ?engineers=1,2,3&from=YYYY-MM-DD&to=YYYY-MM-DD
    Returns (ids, days, None), or (None, None, message) for a 400.
    """
    raw = params.get("engineers", "")
    parts = [part.strip() for part in raw.split(",")] if raw.strip() else []
    if len(parts) > route_km.MAX_ENGINEERS:
        return None, None, f"engineers must list at most {route_km.MAX_ENGINEERS} ids."
    ids = []
    for text in parts:
        # As route_km: digits only, and short enough to be a database integer.
        if not (text.isascii() and text.isdigit()) or len(text) > 19 or int(text) >= 2**63:
            return None, None, "engineers must be a comma-separated list of numeric ids."
        if int(text) not in ids:
            ids.append(int(text))
    bounds = []
    for name in ("from", "to"):
        value = params.get(name)
        try:
            parsed = parse_date(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if not parsed:
            return None, None, f"{name} must be YYYY-MM-DD."
        bounds.append(parsed)
    start, end = bounds
    if start > end:
        return None, None, "from must not be after to."
    if (end - start).days + 1 > route_km.MAX_DAYS:
        return None, None, f"from and to may cover at most {route_km.MAX_DAYS} days."
    end = min(end, today)
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    return ids, days, None


def visible(user, ids):
    """Only the engineers this user may see, as on route_km's POST. An id that
    matches nobody is simply not in the answer."""
    branches = get_allowed_branches(user, "attendance")
    found = Employee.objects.filter(pk__in=ids)
    if "All" not in branches:
        found = found.filter(branch__in=branches)
    found = set(found.values_list("pk", flat=True))
    return [engineer_id for engineer_id in ids if engineer_id in found]


def route_cell(row):
    """One stored ROUTE KM cell, counts and figures only."""
    if row is None:
        return {"km": None, "status": "not_measured", "points": 0, "office": False, "final": False, "measured_at": None}
    return {
        "km": row.km if row.status == "ready" else None,
        "status": row.status,
        "points": len(row.points or []),
        # A Login or Logout in it was put at the office, for want of a fix.
        "office": any(point.get("office") for point in row.points or []),
        "final": row.final,
        "measured_at": row.measured_at.isoformat() if row.measured_at else None,
    }


def route_grid(ids, days):
    """Every engineer's stored ROUTE KM cells, by YYYY-MM-DD. One query."""
    stored = route_km.rows(ids, days)
    return [
        {"engineer_id": engineer_id, "days": {day.isoformat(): route_cell(stored.get((engineer_id, day))) for day in days}}
        for engineer_id in ids
    ]
