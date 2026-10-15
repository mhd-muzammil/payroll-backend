"""How many calls each engineer's day lists, and how many were closed that day:
the case counts beside every km in the KM report.

The day timeline (views.TrackingViewSet.day) lists, for one engineer and one
day, the calls that belong to that day, and the Live Tracking board counts them
as its "CASES" and calls the ones checked out that day "Closed". The KM report
asks the same of a whole grid -- every FSE, every day of a cycle -- so this asks
it in a few queries rather than one request per engineer-day, by the timeline's
own rules:

  planned  assigned to them, in the current plan, not cancelled, with that day
           as its plan day -- today, also a call with no plan day at all;
  touched  assigned to them, with an assignment, a start, a check in or a check
           out stamped on that day -- cancelled ones too, as the timeline lists
           them;
  visited  assigned to them, not cancelled, with a visit row for that day:
           planned for it, or checked into or out of on it.

  closed   of those, the ones whose Check out falls on that day: the day's visit
           row's own, or the call's when the row has none (views._day_moments),
           the visit being the first that matches the day by plan day and id,
           as the timeline takes it. "Closed earlier" -- a call already closed
           on another day -- is not closed here, as it is not on the board.

Not counted: a "Stopped nearby" row the board adds for a stop whose pings were
tagged with a call that is no longer the engineer's. It is rare, and counting
tagged pings instead of stops would count calls the board does not show.

Parity with the timeline is pinned by cases/test_day_cases.py, which asks
/api/tracking/day/ itself.
"""

from __future__ import annotations

import datetime

from django.db.models import Q
from django.utils import timezone

from .models import Case, CaseVisit


def _day_of(moment):
    """The IST day a stamp falls on, or None."""
    return timezone.localtime(moment).date() if moment else None


def cases_by_day(engineer_ids, days, today=None) -> dict:
    """{(engineer id, day): {"cases": n, "closed": m, "checkins": k}} for every
    engineer and every day asked about, zeros included.

    "checkins" is the board's "CHECK-INS" for the day: its punches of kind
    "in", as views._punches_for_day finds them -- every visit row checked into
    that day, and, for a call with no visit row punched that day, its own
    Check in column (15 Oct 2026: the office reads this as the day's visits).
    """
    ids = list(dict.fromkeys(engineer_ids))
    days = sorted(set(days))
    counts = {
        (engineer_id, day): {"cases": 0, "closed": 0, "checkins": 0}
        for engineer_id in ids
        for day in days
    }
    if not ids or not days:
        return counts
    today = today or timezone.localdate()
    wanted = set(days)
    zone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(days[0], datetime.time.min), zone)
    end = timezone.make_aware(
        datetime.datetime.combine(days[-1] + datetime.timedelta(days=1), datetime.time.min), zone
    )

    # (engineer id, day) -> {case id: case}, the timeline's three lists merged.
    listed = {}

    def add(case, day):
        if day in wanted and case.assigned_to_id in ids:
            listed.setdefault((case.assigned_to_id, day), {})[case.pk] = case

    planned = Case.objects.filter(assigned_to_id__in=ids, in_current_plan=True).exclude(
        status="cancelled"
    )
    on_plan = Q(plan_date__in=days)
    if today in wanted:
        on_plan |= Q(plan_date__isnull=True)
    for case in planned.filter(on_plan):
        add(case, case.plan_date or today)

    stamped = Q()
    for field in ("assigned_at", "started_at", "reached_at", "completed_at"):
        stamped |= Q(**{f"{field}__gte": start, f"{field}__lt": end})
    for case in Case.objects.filter(assigned_to_id__in=ids).filter(stamped):
        for moment in (case.assigned_at, case.started_at, case.reached_at, case.completed_at):
            add(case, _day_of(moment))

    # The visit each call has for each day: the first, by plan day and id, of
    # the rows that match the day -- the timeline's own pick.
    visit_of = {}
    # The day's punches (views._punches_for_day): every visit row checked into
    # the day counts, and the calls with a row punched that day are
    # `punched`, so their own columns are not read for it as well.
    checkins = {}
    punched = {}
    for visit in (
        CaseVisit.objects.filter(case__assigned_to_id__in=ids)
        .filter(
            Q(plan_date__in=days)
            | Q(checked_in_at__gte=start, checked_in_at__lt=end)
            | Q(checked_out_at__gte=start, checked_out_at__lt=end)
        )
        .select_related("case")
        .order_by("plan_date", "id")
    ):
        for day in {visit.plan_date, _day_of(visit.checked_in_at), _day_of(visit.checked_out_at)}:
            if day in wanted and (visit.case_id, day) not in visit_of:
                visit_of[(visit.case_id, day)] = visit
                if visit.case.status != "cancelled":
                    add(visit.case, day)
        engineer_id = visit.case.assigned_to_id
        for day in {_day_of(visit.checked_in_at), _day_of(visit.checked_out_at)}:
            if day in wanted:
                punched.setdefault((engineer_id, day), set()).add(visit.case_id)
        checked_in = _day_of(visit.checked_in_at)
        if checked_in in wanted:
            checkins[(engineer_id, checked_in)] = checkins.get((engineer_id, checked_in), 0) + 1

    # A call with no visit row punched that day answers from its own column.
    for case in Case.objects.filter(
        assigned_to_id__in=ids, reached_at__gte=start, reached_at__lt=end
    ).only("assigned_to_id", "reached_at"):
        cell = (case.assigned_to_id, _day_of(case.reached_at))
        if cell[1] in wanted and case.pk not in punched.get(cell, set()):
            checkins[cell] = checkins.get(cell, 0) + 1

    for (engineer_id, day), cases in listed.items():
        closed = 0
        for case in cases.values():
            visit = visit_of.get((case.pk, day))
            checked_out = (visit.checked_out_at or case.completed_at) if visit else case.completed_at
            if _day_of(checked_out) == day:
                closed += 1
        counts[(engineer_id, day)].update(cases=len(cases), closed=closed)
    for cell, number in checkins.items():
        if cell in counts:
            counts[cell]["checkins"] = number
    return counts
