from django.db import models

from employees.models import Employee


class FieldezWorkOrder(models.Model):
    """What FieldEZ says about one work order: where its Problem Resolution was.

    FieldEZ is the customer's own system, and the place it recorded for a WO's
    "Problem Resolution" status is the place the FIELDEZ table routes a
    check-in through, instead of the phone's own fix. Payroll cannot read
    FieldEZ itself -- it takes a person's login in a browser -- so the
    fieldez_km_worker on the office PC reads it and posts what it found here
    (/api/fieldez_km/work_orders/). One row per WO, replaced on every read.

    `status` says what the read found:
      pending        -- not read yet
      found          -- at least one Problem Resolution with a place
      no_resolution  -- the WO is in FieldEZ, with no Problem Resolution placed
      not_found      -- FieldEZ has no such WO
      failed         -- the read itself went wrong; tried again, up to
                        route_km.MAX_FAILED_ATTEMPTS times in a row
    """

    STATUSES = ("pending", "found", "no_resolution", "not_found", "failed")

    # The WO as points.wo_key writes it ("WO-036165368"), so the same WO typed
    # two ways is one row.
    wo_key = models.CharField(max_length=40, unique=True)
    # FieldEZ's own id for the ticket, for whoever has to look it up by hand.
    fieldez_ticket_id = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=16, default="pending")
    # Every Problem Resolution FieldEZ placed for the WO: [{"at": ISO-8601 with
    # its offset, "user", "latitude", "longitude", "address"}, ...]. More than
    # one when the WO was resolved more than once; points.py picks the one that
    # belongs to each check-in.
    resolutions = models.JSONField(default=list, blank=True)
    # Why the read failed, when it did. Never a FieldEZ response body.
    error = models.CharField(max_length=200, blank=True, default="")
    # Failed reads in a row. Any answer that is not a failure sets it back to 0.
    attempts = models.PositiveSmallIntegerField(default=0)
    # When the worker last read it. Indexed: what to read next is decided by it.
    fetched_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.wo_key} ({self.status})"


class FieldezKmDay(models.Model):
    """One engineer's day, by road through the places FieldEZ recorded.

    cases.RouteKmDay column for column, for the KM report's FIELDEZ table:
    Login and Logout placed exactly as ROUTE KM places them, and each check-in
    at the place FieldEZ recorded for its WO's Problem Resolution rather than
    where the phone was. A check-in FieldEZ has no place for is left out, never
    put back at the phone's fix, and `missing` says which and why.

    Kept apart from RouteKmDay on purpose: ROUTE KM reads and writes only its
    own table, so nothing here can change a figure it shows.
    """

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="fieldez_km_days",
    )
    # The IST calendar day, the way the KM report asks for it.
    day = models.DateField(db_index=True)
    # The points the km was measured through, in time order, "at" as an ISO
    # string. A check-in's point also carries its "wo" and the Problem
    # Resolution's "resolved_at", "address" and "user".
    points = models.JSONField(default=list, blank=True)
    # route_km.signature of those points: the same on the next look means the
    # stored km still holds.
    signature = models.CharField(max_length=40, blank=True, default="")
    # waiting (a check-in's WO not read from FieldEZ yet), pending, ready,
    # no_points (fewer than two places) or failed (Ola could not route it).
    status = models.CharField(max_length=12, default="pending")
    # Null unless ready. 0.0 is an answer, not a gap: every point in one place.
    km = models.FloatField(null=True, blank=True)
    # Ola's road route as [[lat, lon], ...], for the map. Empty unless ready.
    path = models.JSONField(default=list, blank=True)
    # Why Ola could not route it, when failed; route_km.RATE_LIMITED while Ola
    # is making every worker wait, whatever the status.
    error = models.CharField(max_length=200, blank=True, default="")
    # How many times in a row Ola has failed this day.
    attempts = models.PositiveSmallIntegerField(default=0)
    # When the points were last read, and when Ola was last asked. The second
    # is indexed because the per-minute pacing counts it, across every worker.
    checked_at = models.DateTimeField(null=True, blank=True)
    measured_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Set once the day is settled, nothing in it waits on FieldEZ and nothing
    # left out of it will be read from FieldEZ again (km.REREAD_REASONS).
    final = models.BooleanField(default=False)
    # Every check-in of the day, the ones left out or waiting included.
    checkins = models.PositiveSmallIntegerField(default=0)
    # The check-ins left out: [{"case_number", "wo", "reason"}, ...], reason
    # one of no_wo, not_in_fieldez, no_problem_resolution, fieldez_failed,
    # not_read (see points.MAX_WANTED_DAYS).
    missing = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["engineer", "day"], name="one_fieldez_km_per_engineer_day"
            )
        ]
        indexes = [
            models.Index(fields=["engineer", "day"]),
        ]

    def __str__(self):
        return f"{self.engineer.employee_name} {self.day} ({self.status})"
