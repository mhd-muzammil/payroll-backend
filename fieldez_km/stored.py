"""FIELDEZ as it is stored: cases/stored_km.py's read, on this table.

Nothing is measured, nothing written, FieldEZ's worker is asked for nothing.
A cell carries ROUTE KM's stored fields and what FIELDEZ adds, as counts:

  checkins           every check-in of the day (null with no row: not known)
  placed             check-ins routed through a FieldEZ Problem Resolution place
  waiting            check-ins whose WO the worker has not read yet
  missing_by_reason  check-ins left out, counted by points' reasons: no_wo,
                     not_in_fieldez, no_problem_resolution, fieldez_failed,
                     not_read
  beyond_lookback    the day is older than points.MAX_WANTED_DAYS: the worker
                     can no longer be asked about its WOs, so a check-in not
                     read by now stays out for good

Status is ROUTE KM's, plus `waiting` (a check-in's WO not read yet, so the day
is not measured: without it the road would come out short).

Never a WO, a case number, a place or an address: only how many.
"""

from __future__ import annotations

from django.utils import timezone

from cases.stored_km import route_cell

from . import km
from .points import readable

MISSING_REASONS = ("no_wo", "not_in_fieldez", "no_problem_resolution", "fieldez_failed", "not_read")


def fieldez_cell(row, day, now):
    """One stored FIELDEZ cell, counts and figures only."""
    cell = route_cell(row)
    cell["beyond_lookback"] = not readable(day, now)
    if row is None:
        cell.update(checkins=None, placed=None, waiting=None, missing_by_reason={reason: 0 for reason in MISSING_REASONS})
        return cell
    missing = row.missing or []
    placed = sum(1 for point in row.points or [] if point.get("kind") == "checkin")
    counts = {reason: 0 for reason in MISSING_REASONS}
    for entry in missing:
        reason = entry.get("reason")
        if reason in counts:
            counts[reason] += 1
    cell.update(
        checkins=row.checkins,
        placed=placed,
        # Not stored on its own: what is neither placed nor left out is waiting.
        waiting=max(0, row.checkins - placed - len(missing)),
        missing_by_reason=counts,
    )
    return cell


def fieldez_grid(ids, days, now=None):
    """Every engineer's stored FIELDEZ cells, by YYYY-MM-DD. One query."""
    now = now or timezone.now()
    stored = km.rows(ids, days)
    return [
        {
            "engineer_id": engineer_id,
            "days": {day.isoformat(): fieldez_cell(stored.get((engineer_id, day)), day, now) for day in days},
        }
        for engineer_id in ids
    ]
