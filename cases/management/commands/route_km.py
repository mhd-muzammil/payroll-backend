"""Where an engineer marked their day, and how far that is by road.

ROUTE KM in the KM report is the road distance through the places an engineer
marked: their Login, every Check in, their Logout, in the order they happened.
This prints those points, asks Ola for the road distance through them, and
prints a Google Maps link with the same points in the same order -- so a figure
on the report can be checked against Google by hand.

It CHANGES NOTHING: no RouteKmDay row is read or written, so running it can
never move a figure on the report.

    python manage.py route_km --engineer 114 --date 2026-09-25
    python manage.py route_km --engineer "Sarwana" --date 2026-09-25

Cost: one Directions request per 25 legs, so an ordinary day is one request.
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from cases.olamaps import SnapUnavailable
from cases.route_km import key_points, measure
from employees.models import Employee

GOOGLE_DIRECTIONS_URL = "https://www.google.com/maps/dir/"

# The markers the report's map puts on each kind of point.
MARKERS = {"login": "L", "logout": "D"}


class Command(BaseCommand):
    help = "Print a day's ROUTE KM points, Ola's road km and a Google Maps link, changing nothing."

    def add_arguments(self, parser):
        parser.add_argument("--engineer", required=True, help="Employee id, or part of their name.")
        parser.add_argument("--date", help="YYYY-MM-DD. Defaults to today.")

    def handle(self, *args, **options):
        engineer = self._engineer(options["engineer"])
        day = timezone.localdate()
        if options.get("date"):
            try:
                day = datetime.strptime(options["date"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--date must be YYYY-MM-DD") from exc

        points = key_points(engineer, day)
        self.stdout.write(
            "%s on %s: %d point%s"
            % (engineer.employee_name, day, len(points), "" if len(points) == 1 else "s")
        )
        calls = 0
        for point in points:
            if point["kind"] == "checkin":
                calls += 1
                marker = str(calls)
            else:
                marker = MARKERS[point["kind"]]
            self.stdout.write(
                "  %-2s %-8s %s  %s, %s  %s"
                % (
                    marker,
                    point["kind"],
                    timezone.localtime(parse_datetime(point["at"])).strftime("%H:%M:%S"),
                    point["latitude"],
                    point["longitude"],
                    # A Login or Logout with no fix near it, placed at the office.
                    "(office)" if point.get("office") else point["case_number"] or "",
                )
            )

        if len(points) < 2:
            self.stdout.write("Fewer than two points: nothing to measure.")
            return

        self.stdout.write("")
        self.stdout.write(
            "  Google Maps : %s"
            % (
                GOOGLE_DIRECTIONS_URL
                + "/".join("%s,%s" % (point["latitude"], point["longitude"]) for point in points)
            )
        )
        try:
            km, path = measure(points)
        except SnapUnavailable as exc:
            raise CommandError("Ola could not measure it: %s" % exc) from exc
        self.stdout.write("  Ola by road : %.2f km  (%d points in the route drawn)" % (km, len(path)))
        self.stdout.write(self.style.NOTICE("\nNothing was changed. This only measures."))

    # ------------------------------------------------------------------ helpers

    def _engineer(self, who):
        if str(who).isdigit():
            try:
                return Employee.objects.get(pk=int(who))
            except Employee.DoesNotExist as exc:
                raise CommandError("No employee with id %s." % who) from exc
        matches = Employee.objects.filter(
            Q(employee_name__iexact=who) | Q(employee_name__icontains=who)
        ).distinct()
        if not matches:
            raise CommandError("No employee matches %r." % who)
        if matches.count() > 1:
            names = ", ".join(sorted(matches.values_list("employee_name", flat=True))[:8])
            raise CommandError("%r matches %d employees: %s" % (who, matches.count(), names))
        return matches.first()
