"""What a day's trail measures when it follows the road instead of cutting.

The kilometres on the board are the sum of straight lines between the fixes the
phone reported. The road bends between them, so the figure is always short of a
bike odometer -- measured on a real trail, reading it at one fix a minute
instead of one every thirty seconds already loses 3.2%, and the road's own
curvature between fixes is lost on top of that.

This asks Ola for the actual road distance and prints both, side by side. It
CHANGES NOTHING: no column is written, no board figure moves. It is here so the
difference can be looked at on real days before anybody decides to pay
allowances on it.

    python manage.py road_km --engineer 114 --date 2026-09-08
    python manage.py road_km --engineer "Sarwana" --date 2026-09-08 --verbose

Cost: the trail is thinned to one waypoint every WAYPOINT_SPACING_M and sent 25
legs to a request, so a 120 km day is about a dozen calls. Ola's free tier is
100,000 Directions requests a month; twenty engineers every day of the month
come to roughly seven thousand.

A leg whose road distance is wildly longer than the straight line between its
ends is NOT counted as road: that is the router sending the trip round a block
the engineer cut through, and a figure that can exceed the truth is worse than
one that falls short of it.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from cases.models import LocationPing
from cases.olamaps import api_key
from cases.views import _moving_trail, haversine_km
from employees.models import Employee

DIRECTIONS_URL = "https://api.olamaps.io/routing/v1/directions"

# One waypoint per this much travel. Closer than this buys nothing -- a few
# hundred metres of road has one sensible path -- and costs a request.
WAYPOINT_SPACING_M = 400

# Points per request. 26 is proven against the live API; 51 is refused.
POINTS_PER_CALL = 26

# A leg the router made this much longer than the straight line between its
# ends did not follow the engineer: it went round something they cut through.
# The straight line is used for those, so the total can never exceed the truth
# by a router's opinion.
MAX_ROAD_RATIO = 1.8


class Command(BaseCommand):
    help = "Compare a day's straight-line kilometres with the road distance, changing nothing."

    def add_arguments(self, parser):
        parser.add_argument("--engineer", required=True, help="Employee id, or part of their name.")
        parser.add_argument("--date", help="YYYY-MM-DD. Defaults to today.")
        parser.add_argument(
            "--spacing",
            type=int,
            default=WAYPOINT_SPACING_M,
            help=f"Metres between waypoints. Default {WAYPOINT_SPACING_M}.",
        )
        parser.add_argument("--verbose", action="store_true", help="Print every leg.")

    def handle(self, *args, **options):
        engineer = self._engineer(options["engineer"])
        day = timezone.localdate()
        if options.get("date"):
            try:
                day = datetime.strptime(options["date"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--date must be YYYY-MM-DD") from exc

        pings = list(
            LocationPing.objects.filter(engineer=engineer, timestamp__date=day).order_by("timestamp")
        )
        trail = _moving_trail(pings)
        if len(trail) < 2:
            self.stdout.write(f"{engineer.employee_name} on {day}: nothing to measure.")
            return

        straight = sum(
            haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)
            for a, b in zip(trail, trail[1:])
        )

        waypoints = self._thin(trail, options["spacing"])
        self.stdout.write(
            "%s on %s: %d fixes, %d waypoints, %d request%s"
            % (
                engineer.employee_name,
                day,
                len(trail),
                len(waypoints),
                self._calls(len(waypoints)),
                "" if self._calls(len(waypoints)) == 1 else "s",
            )
        )

        road = 0.0
        fell_back = 0
        calls = 0
        for batch in self._batches(waypoints):
            calls += 1
            try:
                legs = self._directions(batch)
            except Exception as exc:  # noqa: BLE001 - a failed batch is not fatal
                self.stdout.write(self.style.WARNING("  request %d failed: %s" % (calls, exc)))
                # The straight line for the whole batch, so the total stays honest.
                road += sum(
                    haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(batch, batch[1:])
                )
                continue
            for (a, b), metres in zip(zip(batch, batch[1:]), legs):
                line = haversine_km(a[0], a[1], b[0], b[1])
                by_road = (metres or 0) / 1000.0
                if line > 0 and by_road / line > MAX_ROAD_RATIO:
                    fell_back += 1
                    road += line
                    if options["verbose"]:
                        self.stdout.write(
                            "    leg %.3f km straight, %.3f by road -- too far round, using the line"
                            % (line, by_road)
                        )
                    continue
                road += max(by_road, line)
                if options["verbose"]:
                    self.stdout.write("    leg %.3f km straight, %.3f by road" % (line, by_road))

        gain = road - straight
        self.stdout.write("")
        self.stdout.write("  straight line (what the board shows) : %6.2f km" % straight)
        self.stdout.write("  along the road                       : %6.2f km" % road)
        self.stdout.write(
            "  difference                           : %6.2f km  (%+.1f%%)"
            % (gain, (100 * gain / straight) if straight else 0)
        )
        if fell_back:
            self.stdout.write(
                "  %d leg%s kept its straight line -- the router went round something."
                % (fell_back, "" if fell_back == 1 else "s")
            )
        self.stdout.write(self.style.NOTICE("\nNothing was changed. This only measures."))

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _calls(points):
        if points < 2:
            return 0
        return max(1, -(-(points - 1) // (POINTS_PER_CALL - 1)))

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

    @staticmethod
    def _thin(trail, spacing_m):
        """One point per `spacing_m` of travel, keeping both ends.

        The router only needs enough waypoints to be sure which road was taken;
        a few hundred metres apart there is usually only one answer, and every
        extra point is a slice of the request budget for nothing.
        """
        kept = [(trail[0].latitude, trail[0].longitude)]
        since = 0.0
        for previous, current in zip(trail, trail[1:]):
            since += haversine_km(
                previous.latitude, previous.longitude, current.latitude, current.longitude
            ) * 1000
            if since >= spacing_m:
                kept.append((current.latitude, current.longitude))
                since = 0.0
        last = (trail[-1].latitude, trail[-1].longitude)
        if kept[-1] != last:
            kept.append(last)
        return kept

    @staticmethod
    def _batches(points):
        """Overlapping runs, so no leg is lost at a seam."""
        step = POINTS_PER_CALL - 1
        for start in range(0, len(points) - 1, step):
            batch = points[start : start + POINTS_PER_CALL]
            if len(batch) >= 2:
                yield batch

    @staticmethod
    def _directions(points):
        """Metres for each leg of one request, in order."""
        key = api_key()
        if not key:
            raise RuntimeError("OLA_MAPS_API_KEY is not set")
        params = {
            "origin": "%s,%s" % points[0],
            "destination": "%s,%s" % points[-1],
            "api_key": key,
        }
        middle = points[1:-1]
        if middle:
            params["waypoints"] = "|".join("%s,%s" % p for p in middle)
        url = DIRECTIONS_URL + "?" + urllib.parse.urlencode(params, safe="|,")
        request = urllib.request.Request(url, method="POST", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
        routes = body.get("routes") or []
        if not routes:
            raise RuntimeError("Ola returned no route (%s)" % body.get("status"))
        return [leg.get("distance") for leg in (routes[0].get("legs") or [])]
