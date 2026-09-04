"""Delete positions an engineer's phone never sent.

Anyone signed in as an engineer used to be tracked as that engineer. The office
opened one engineer's account in a desktop browser two hundred and fifty
kilometres away, to see what he sees, and the page posted the OFFICE LAPTOP'S
position as his: a straight line from Hosur to the coast, 519.65 km for a day
the engineer himself put at 45. Kilometres feed allowances, so the rows have to
come out, not just be explained.

That hole is closed -- the app is the only client allowed to report a position
now -- but the rows already written are still there, and there is nothing on a
LocationPing that says which device sent it. What there is, is distance: the
engineer worked around Hosur and the laptop was on the coast. So this deletes
the fixes that sit outside a radius around where the engineer actually works.

Nothing is guessed and nothing is swept: one engineer, one day, a radius you
choose, and a dry run that shows the rows and what the day's distance becomes
before anything is removed.

    python manage.py prune_stray_pings --engineer "Kausar Basha"
    python manage.py prune_stray_pings --engineer "Kausar Basha" --radius-km 60
    python manage.py prune_stray_pings --engineer "Kausar Basha" --apply

CAREFUL: an engineer who genuinely drove to another district that day has real
fixes out there too. Read the dry run before applying it -- the times and the
distances are printed for exactly that reason.
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from cases.models import LocationPing
from cases.views import _trail_km, haversine_km
from employees.models import Employee

# Wide enough to hold a service engineer's district, far short of the next one.
DEFAULT_RADIUS_KM = 60.0


class Command(BaseCommand):
    help = "Delete an engineer's location fixes that lie outside a radius, for one day."

    def add_arguments(self, parser):
        parser.add_argument(
            "--engineer",
            required=True,
            help="Employee name, or part of it. Must match exactly one employee.",
        )
        parser.add_argument("--date", help="The day to clean, YYYY-MM-DD. Defaults to today.")
        parser.add_argument(
            "--radius-km",
            type=float,
            default=DEFAULT_RADIUS_KM,
            help=f"Fixes further than this from the centre are stray. Default {DEFAULT_RADIUS_KM:g}.",
        )
        parser.add_argument(
            "--centre",
            help=(
                "lat,lon to measure from. Defaults to the employee's work location, "
                "and failing that the first fix of the day."
            ),
        )
        parser.add_argument(
            "--apply", action="store_true", help="Delete them. Without it, nothing is removed."
        )

    def handle(self, *args, **options):
        name = options["engineer"].strip()
        matches = Employee.objects.filter(
            Q(employee_name__iexact=name) | Q(employee_name__icontains=name)
        ).distinct()
        if not matches:
            raise CommandError(f"No employee matches {name!r}.")
        if matches.count() > 1:
            names = ", ".join(sorted(matches.values_list("employee_name", flat=True))[:10])
            raise CommandError(f"{name!r} matches {matches.count()} employees: {names}")
        engineer = matches.first()

        day = timezone.localdate()
        if options.get("date"):
            day = datetime.strptime(options["date"], "%Y-%m-%d").date()

        pings = list(
            LocationPing.objects.filter(engineer=engineer, timestamp__date=day).order_by(
                "timestamp"
            )
        )
        if not pings:
            self.stdout.write(f"{engineer.employee_name}: no fixes at all on {day}.")
            return

        if options.get("centre"):
            try:
                lat_text, lon_text = options["centre"].split(",")
                centre = (float(lat_text), float(lon_text))
                centre_from = "the centre you gave"
            except ValueError as exc:
                raise CommandError("--centre must be lat,lon") from exc
        elif engineer.work_lat is not None and engineer.work_lon is not None:
            centre = (engineer.work_lat, engineer.work_lon)
            centre_from = "their work location"
        else:
            centre = (pings[0].latitude, pings[0].longitude)
            centre_from = f"their first fix of the day ({timezone.localtime(pings[0].timestamp):%H:%M})"

        radius = options["radius_km"]
        stray = [
            ping
            for ping in pings
            if haversine_km(centre[0], centre[1], ping.latitude, ping.longitude) > radius
        ]

        self.stdout.write(
            f"{engineer.employee_name} on {day}: {len(pings)} fixes, "
            f"measured from {centre_from} ({centre[0]:.5f}, {centre[1]:.5f}), "
            f"radius {radius:g} km"
        )
        if not stray:
            self.stdout.write(self.style.SUCCESS("Nothing outside the radius. Nothing to do."))
            return

        # Grouped, because a browser left open pings every thirty seconds and a
        # hundred identical lines say no more than one line and a count.
        first = timezone.localtime(stray[0].timestamp)
        last = timezone.localtime(stray[-1].timestamp)
        far = max(
            haversine_km(centre[0], centre[1], p.latitude, p.longitude) for p in stray
        )
        self.stdout.write(
            f"  {len(stray)} stray fix(es), {first:%H:%M} to {last:%H:%M}, "
            f"up to {far:.1f} km away"
        )
        for ping in stray[:3]:
            away = haversine_km(centre[0], centre[1], ping.latitude, ping.longitude)
            self.stdout.write(
                f"    {timezone.localtime(ping.timestamp):%H:%M}  "
                f"{ping.latitude:.5f}, {ping.longitude:.5f}  ({away:.1f} km away)"
            )
        if len(stray) > 3:
            self.stdout.write(f"    ... and {len(stray) - 3} more")

        kept = [p for p in pings if p not in stray]
        self.stdout.write(
            f"  Distance for the day: {_trail_km(pings):g} km now, "
            f"{_trail_km(kept):g} km once these are gone"
        )

        if not options.get("apply"):
            self.stdout.write("\nNothing was deleted. Run it again with --apply.")
            return

        LocationPing.objects.filter(pk__in=[p.pk for p in stray]).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"\nDeleted {len(stray)} fix(es). The day now reads {_trail_km(kept):g} km."
            )
        )
