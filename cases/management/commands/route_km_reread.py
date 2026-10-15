"""Measure settled ROUTE KM and FIELDEZ days again for the 15 Oct 2026 rule.

A day is final SETTLE_HOURS after it ends (route_km.settled), and a final cell
is never read again: it keeps the points and the km it was measured with. On
15 Oct 2026 two kinds of place became points, as the day timeline places them
-- the Logout of a duty the system closed, and a Check in the phone gave no
position for -- and the days already final kept the old rule's points.

This reads every final cell of the days asked for, on both tables, and lists
the ones the new rule changes. A cell is taken only when the new rule is the
whole difference: its stored points are exactly what the old rule makes of
today's data, so measuring it again adds the new places and moves nothing
else. A cell whose points moved for any other reason -- a branch changed, a
call handed on, a WO read again -- is listed apart and never touched.

With --apply each cell taken is measured again, one at a time, through the
table's own refresh: the same claims, the same per-minute allowance, the same
wait after a 429, with a pause between cells so the report keeps a share of
Ola. Run it when the KM report is not in use (after office hours). A cell Ola
cannot measure keeps the km it had and stays final; three such cells in a row
stop the run.

Without --apply it CHANGES NOTHING.

    python manage.py route_km_reread --from 2026-09-25 --to 2026-10-13
    python manage.py route_km_reread --from 2026-09-25 --to 2026-10-13 --apply
    python manage.py route_km_reread --from 2026-10-01 --to 2026-10-01 --engineer 114 --apply

Cost: one Directions request per cell taken (a day of up to 26 points is one
request), about four a minute. It can be stopped at any time and run again: a
cell it did not finish is put back as it was, and the next run takes it up.
"""

import collections
import datetime
import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from cases import olamaps, route_km
from cases.models import DutySession, RouteKmDay
from employees.models import Employee

# How long one cell is waited for -- Ola's minute, a 429's two minutes, the
# report measuring at the same time -- before it is put back as it was.
CELL_WAIT_SECONDS = 10 * 60
# Between two tries at the same cell.
RETRY_SECONDS = 10
# Between two cells: about four a minute, leaving the report some of the six.
PAUSE_SECONDS = 15
# Cells in a row Ola could not measure before the run stops: past this it is
# Ola that is failing, not the cells.
MAX_FAILURES_IN_A_ROW = 3

WORDS = {"login": "Login", "checkin": "Check in", "logout": "Logout"}


def _auto_logouts(engineer, day):
    """When each duty the system closed on `day` ended: the moments of the
    Logouts only the new rule makes points."""
    return set(
        DutySession.objects.filter(
            engineer=engineer, started_at__date=day, auto_closed=True, ended_at__isnull=False
        ).values_list("ended_at", flat=True)
    )


def _fieldez_points(engineer, day, now):
    """FIELDEZ's points now, and as the old rule made them: the same, without
    the Logouts of auto-closed duties (FIELDEZ's check-ins never stood at the
    phone's position, so the other half of the rule does not reach it)."""
    from fieldez_km.points import fieldez_points

    points = fieldez_points(engineer, day, now)[0]
    closed = _auto_logouts(engineer, day)
    before = [
        point
        for point in points
        if not (point["kind"] == "logout" and parse_datetime(point["at"]) in closed)
    ]
    return points, before


def _route_points(engineer, day, now):
    """ROUTE KM's points now, and as the old rule made them."""
    return (
        route_km.key_points(engineer, day),
        route_km.key_points(engineer, day, as_before_15_oct=True),
    )


def _tables():
    """(name, model, points of a day, refresh) for each table, ROUTE KM first.

    FIELDEZ takes its Login and Logout from route_km.key_points, so the rule
    reaches its points too. Imported here: FIELDEZ builds on cases, not the
    other way round.
    """
    from fieldez_km import km as fieldez_km
    from fieldez_km.models import FieldezKmDay

    return (
        ("ROUTE KM", RouteKmDay, _route_points, route_km.refresh),
        ("FIELDEZ", FieldezKmDay, _fieldez_points, fieldez_km.refresh),
    )


def _place(point):
    return (point["kind"], round(point["latitude"], 5), round(point["longitude"], 5))


def difference(old, new):
    """What is new and what is gone between two lists of points, in words:
    ["+ Logout 19:41", "+ Check in OC-004698 11:30"]."""

    def words(sign, point):
        text = "%s %s" % (sign, WORDS.get(point["kind"], point["kind"]))
        if point.get("case_number"):
            text += " " + point["case_number"]
        moment = parse_datetime(point["at"]) if isinstance(point.get("at"), str) else None
        if moment is not None:
            text += " " + timezone.localtime(moment).strftime("%H:%M")
        if point.get("office"):
            text += " (office)"
        return text

    out = []
    unmatched = collections.Counter(_place(point) for point in old)
    for point in new:
        if unmatched[_place(point)]:
            unmatched[_place(point)] -= 1
        else:
            out.append(words("+", point))
    unmatched = collections.Counter(_place(point) for point in new)
    for point in old:
        if unmatched[_place(point)]:
            unmatched[_place(point)] -= 1
        else:
            out.append(words("-", point))
    return out


def _km(row):
    if row.status == "ready" and row.km is not None:
        return "%.2f km" % row.km
    return row.status


class Command(BaseCommand):
    help = (
        "List the final ROUTE KM and FIELDEZ cells the 15 Oct 2026 points rule "
        "changes; with --apply, measure them again."
    )

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
        parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
        parser.add_argument("--engineer", help="Only this employee: id, or part of their name.")
        parser.add_argument(
            "--apply", action="store_true", help="Measure them again. Without it nothing changes."
        )

    def handle(self, *args, **options):
        start, end = self._day(options["start"], "--from"), self._day(options["end"], "--to")
        if end < start:
            raise CommandError("--to is before --from.")
        if (end - start).days + 1 > route_km.MAX_DAYS:
            raise CommandError("At most %d days at a time." % route_km.MAX_DAYS)
        if options["apply"] and not olamaps.is_configured():
            raise CommandError("No Ola key here (OLA_MAPS_API_KEY): nothing was changed.")
        now = timezone.now()
        asked = [start + datetime.timedelta(days=n) for n in range((end - start).days + 1)]
        days = [day for day in asked if route_km.settled(day, now)]
        if len(days) < len(asked):
            self.stdout.write(
                "%d day(s) asked for are not final yet: the report reads those again itself."
                % (len(asked) - len(days))
            )
        engineer = self._engineer(options["engineer"]) if options.get("engineer") else None
        retry_after = now - datetime.timedelta(minutes=route_km.FAILED_RETRY_MINUTES)

        taken, other, later = [], [], []
        for name, model, points_of, refresh in _tables():
            # Final, or one a stopped run left open. A pending cell that was
            # never measured is the report's: it measures those itself.
            cells = (
                model.objects.filter(day__in=days)
                .filter(Q(final=True) | Q(checked_at__isnull=True))
                .exclude(status="pending", signature="")
                .defer("path")
            )
            if engineer is not None:
                cells = cells.filter(engineer=engineer)
            cells = list(cells.order_by("day", "engineer_id"))
            engineers = Employee.objects.in_bulk({row.engineer_id for row in cells})
            for row in cells:
                person = engineers[row.engineer_id]
                points, before = points_of(person, row.day, now)
                if route_km.signature(points) == row.signature:
                    continue
                entry = (name, model, refresh, person, row, points)
                if route_km.signature(before) != row.signature:
                    other.append(entry)
                elif row.status == "failed" and row.measured_at and row.measured_at > retry_after:
                    # refresh will not try it again inside its hour.
                    later.append(entry)
                else:
                    taken.append(entry)
            self.stdout.write("%s: %d final cell(s) read." % (name, len(cells)))

        for title, entries in (
            ("Changed by the 15 Oct rule", taken),
            ("Changed for another reason: NOT touched", other),
            ("Failed less than an hour ago: run again later", later),
        ):
            if not entries:
                continue
            self.stdout.write("%s (%d):" % (title, len(entries)))
            for number, (name, _, _, person, row, points) in enumerate(entries, 1):
                self.stdout.write(
                    "  %d. %-8s %s  %s (%s)  %s  %s"
                    % (
                        number,
                        name,
                        row.day.isoformat(),
                        person.employee_name,
                        person.id,
                        _km(row),
                        ", ".join(difference(row.points or [], points)),
                    )
                )
        if not taken:
            self.stdout.write("Nothing to measure again.")
            return
        self.stdout.write(
            "%d cell(s) to measure again, about %d minute(s)."
            % (len(taken), max(1, round(len(taken) * PAUSE_SECONDS / 60)))
        )
        if not options["apply"]:
            self.stdout.write(self.style.NOTICE("Nothing was changed. Run again with --apply."))
            return

        done = kept = 0
        failures = 0
        try:
            for number, (name, model, refresh, person, row, _) in enumerate(taken, 1):
                if number > 1:
                    time.sleep(PAUSE_SECONDS)
                outcome, answer = self._measure(model, refresh, row)
                label = "  %d. %-8s %s  %s:" % (number, name, row.day.isoformat(), person.employee_name)
                if outcome == "done":
                    done += 1
                    failures = 0
                    self.stdout.write("%s %s -> %s" % (label, _km(row), _km(answer)))
                    continue
                kept += 1
                if outcome == "failed":
                    failures += 1
                    self.stdout.write(
                        "%s Ola could not measure it (%s). Kept %s." % (label, answer.error[:80], _km(row))
                    )
                elif outcome == "waiting":
                    self.stdout.write("%s a check-in waits on FieldEZ again. Kept %s." % (label, _km(row)))
                else:
                    self.stdout.write("%s Ola stayed busy. Kept %s; run again later." % (label, _km(row)))
                if failures >= MAX_FAILURES_IN_A_ROW:
                    self.stdout.write(
                        "Stopped: Ola failed %d cells in a row. Nothing was lost; run again later."
                        % failures
                    )
                    break
        except KeyboardInterrupt:
            self.stdout.write(
                "Stopped after %d. The cell in hand was put back as it was; run again to finish."
                % (done,)
            )
            return
        self.stdout.write(
            self.style.SUCCESS("Done: %d measured again, %d kept as they were." % (done, kept))
        )

    def _measure(self, model, refresh, row):
        """Read one cell again through its table's refresh, waiting while Ola
        is busy. ("done", the row now), ("failed" or "waiting", the row as
        read) or ("busy", None). Unless done, the cell is put back exactly as
        it was -- km, points, final -- so nothing the run could not finish is
        lost."""
        cell = model.objects.filter(pk=row.pk)
        kept = [
            field.attname
            for field in model._meta.concrete_fields
            # Not when it was last measured, nor its error: those pace every
            # worker against Ola's minute and its 429s.
            if field.attname not in ("id", "engineer_id", "day", "measured_at", "error")
        ]
        before = cell.values(*kept).get()
        # Readable again: refresh passes over a final cell, and over one read
        # in the last hour. From here, a read by anyone sets checked_at.
        cell.update(final=False, checked_at=None)
        outcome = "busy"
        try:
            deadline = time.monotonic() + CELL_WAIT_SECONDS
            while True:
                now = timezone.now()
                refresh(
                    [row.engineer_id], [row.day], now=now, work_limit=1, measure_limit=1, time_budget=30.0
                )
                answer = model.objects.defer("path").get(pk=row.pk)
                # Read since it was opened -- by this call, or by the report --
                # and not left waiting on Ola.
                if answer.checked_at is not None and answer.status != "pending":
                    if answer.status in ("ready", "no_points"):
                        outcome = "done"
                        return outcome, answer
                    # Ola failed it, or a FIELDEZ check-in waits on FieldEZ
                    # again. Put back as it was, unless read again since.
                    outcome = "failed" if answer.status == "failed" else "waiting"
                    cell.filter(checked_at=answer.checked_at, status=answer.status).update(**before)
                    return outcome, answer
                if time.monotonic() >= deadline:
                    return outcome, None
                time.sleep(RETRY_SECONDS)
        finally:
            if outcome == "busy":
                # Never read since it was opened -- a pending FIELDEZ cell
                # included: put back as it was.
                cell.filter(checked_at__isnull=True).update(**before)

    # ------------------------------------------------------------------ helpers

    def _day(self, text, flag):
        try:
            return datetime.datetime.strptime(text, "%Y-%m-%d").date()
        except (TypeError, ValueError) as exc:
            raise CommandError("%s must be YYYY-MM-DD." % flag) from exc

    def _engineer(self, who):
        if str(who).isdigit():
            try:
                return Employee.objects.get(pk=int(who))
            except Employee.DoesNotExist as exc:
                raise CommandError("No employee with id %s." % who) from exc
        # The whole name first: "Mohan" is Mohan, not also Mohan Kumar.
        exact = Employee.objects.filter(employee_name__iexact=who)
        if exact.count() == 1:
            return exact.first()
        matches = Employee.objects.filter(
            Q(employee_name__iexact=who) | Q(employee_name__icontains=who)
        ).distinct()
        if not matches:
            raise CommandError("No employee matches %r." % who)
        if matches.count() > 1:
            names = ", ".join(sorted(matches.values_list("employee_name", flat=True))[:8])
            raise CommandError("%r matches %d employees: %s" % (who, matches.count(), names))
        return matches.first()
