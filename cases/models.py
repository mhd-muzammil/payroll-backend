from django.db import models
from django.utils import timezone
from employees.models import Employee


class Case(models.Model):
    """A customer complaint / service request that gets dispatched to a field
    engineer. The lifecycle status is driven by the engineer's actions in the
    field (accept -> travel -> reached -> working -> completed)."""

    PRIORITY_CHOICES = (
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("urgent", "Urgent"),
    )
    STATUS_CHOICES = (
        ("open", "Open"),              # created, not yet assigned
        ("assigned", "Assigned"),      # assigned to an engineer, not accepted
        ("accepted", "Accepted"),      # engineer accepted the case
        ("on_the_way", "On the way"),  # engineer travelling to the site
        ("reached", "Reached"),        # engineer arrived at the site
        ("working", "Working"),        # engineer working on the problem
        ("completed", "Completed"),    # case closed
        ("cancelled", "Cancelled"),
    )

    # Human-friendly reference (e.g. OC-000042). Filled in save() on first save.
    # Nullable ON PURPOSE, and never the empty string. The number is built from
    # the pk, so it cannot be known until the row exists; save() therefore
    # inserts a placeholder and fills it in immediately afterwards. A unique
    # column accepts any number of NULLs but only ONE empty string, so with ""
    # as the placeholder two cases created in the same instant collided on it —
    # which is how a whole day of syncing died with
    # "duplicate key value violates unique constraint ... Key (case_number)=()".
    case_number = models.CharField(
        max_length=20, unique=True, blank=True, null=True, db_index=True
    )

    # Reference from the originating system (e.g. OpenCall ticket id). Used to
    # make dispatch idempotent so re-assigning/re-scheduling the same ticket
    # updates the one case instead of creating duplicates.
    external_ref = models.CharField(max_length=100, blank=True, default="", db_index=True)

    customer_name = models.CharField(max_length=150)
    customer_phone = models.CharField(max_length=20, blank=True, default="")

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    # Where the engineer has to go. address is human readable; lat/lon drive the map.
    address = models.TextField(blank=True, default="")
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default="medium")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open", db_index=True)

    assigned_to = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cases",
    )
    assigned_by = models.ForeignKey(
        "authentication.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatched_cases",
    )

    # Lifecycle timestamps (each stamped when the engineer moves the status).
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)     # travel started
    reached_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    resolution_notes = models.TextField(blank=True, default="")

    # Is this ticket in the originating system's CURRENT plan for its engineer?
    #
    # Owned by the sync, and deliberately separate from `status`. The engineer's
    # list is built from this flag alone, so it holds exactly as many cases as
    # OpenCall's Assigned column shows — no more, no fewer. Keeping it apart from
    # status is what lets both facts stay true at once: what happened to the call
    # (status) and whether it is still booked to them (this).
    in_current_plan = models.BooleanField(default=True, db_index=True)

    # WHICH day's plan last carried this ticket, as the originating system counts
    # days. The engineer's list shows today's only, so yesterday's calls fall off
    # by themselves at midnight — no sweep has to run and no stale ticket can
    # survive the sync stopping. A call still open today is pushed again today and
    # keeps this current; one that has left the plan simply stops being renewed.
    # Null for a case created by hand in Payroll, which no plan owns.
    plan_date = models.DateField(null=True, blank=True, db_index=True)

    # Everything the originating system knows about the ticket — case id, WIP
    # aging, product and serial, account, contact, the postal address. Stored as
    # a bag rather than columns on purpose: OpenCall's report gains and renames
    # columns, and an engineer should not wait for a migration here to see one.
    # The fields an engineer acts on (customer_name, customer_phone, address)
    # are ALSO copied to the real columns above, so they stay searchable and the
    # rest of the app keeps working without knowing about this.
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        creating = self._state.adding
        # Blank means "not numbered yet", and that has to reach the database as
        # NULL rather than "" — see the field. Applied on every save, so a form
        # or admin edit that clears the box cannot reintroduce the collision.
        if not self.case_number:
            self.case_number = None
        super().save(*args, **kwargs)
        # A stable, readable number from the pk, which only exists now.
        if creating and not self.case_number:
            self.case_number = f"OC-{self.pk:06d}"
            super().save(update_fields=["case_number"])

    def __str__(self):
        return f"{self.case_number or 'OC-?'} - {self.customer_name}"


class EngineerAlias(models.Model):
    """Maps a name used by the originating system (OpenCall) onto a Payroll employee.

    Automatic matching deliberately refuses to guess: it will not pick between
    four people called "VIJAYAKUMAR", and it cannot know that OpenCall's "Lava"
    is Payroll's "LAVAKUMAR". Rather than loosening the rules — which would
    silently route one engineer's jobs to another person — the mapping is stated
    explicitly here, once per engineer, and consulted before any name matching.

    Fill this in from the `unmatched_engineers` list that /cases/bulk_dispatch/
    returns after a sync; those are exactly the names that need an entry.
    """

    external_name = models.CharField(
        max_length=150,
        unique=True,
        help_text="The engineer's name exactly as the other system sends it (case-insensitive).",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="external_aliases",
        help_text="The Payroll employee those cases belong to.",
    )
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["external_name"]
        verbose_name_plural = "Engineer aliases"

    def save(self, *args, **kwargs):
        # Store normalised so lookup is a plain exact match and duplicates that
        # differ only by case/padding collide on the unique constraint.
        self.external_name = (self.external_name or "").strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.external_name} -> {self.employee.employee_name}"


class DutySession(models.Model):
    """One stretch of an engineer being on duty, from Start Duty to Stop Duty.

    Duty is a STATE the engineer declares, not something inferred from whether
    their phone happens to be sending GPS. A locked phone, a backgrounded tab or
    a dead signal stops the pings, but the engineer is still on duty — so the
    live view can say "on duty, no signal for 15m" instead of dropping them off
    the board as if they had gone home. It also gives a record of who was on
    duty, for how long, and how far they travelled in that stretch.
    """

    # A session left open this long is treated as forgotten and auto-closed, so
    # one missed Stop Duty doesn't show an engineer on duty for days.
    MAX_DURATION_HOURS = 16

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="duty_sessions",
    )
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    # NULL means the engineer is still on duty right now.
    ended_at = models.DateTimeField(null=True, blank=True)
    # Set when MAX_DURATION_HOURS closed it instead of the engineer.
    auto_closed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["engineer", "ended_at"]),
        ]

    @property
    def is_open(self):
        return self.ended_at is None

    def duration_minutes(self):
        return int(((self.ended_at or timezone.now()) - self.started_at).total_seconds() // 60)

    def __str__(self):
        state = "on duty" if self.is_open else "ended"
        return f"{self.engineer.employee_name} {state} since {self.started_at:%Y-%m-%d %H:%M}"


class LocationPing(models.Model):
    """One live GPS reading sent by an engineer's app while on duty. A trail of
    these draws the travel path; the latest per engineer is their live position.
    Kept as an append-only log so distance travelled can be computed from it."""

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="location_pings",
    )
    # The case the engineer is currently attending, if any (null = general duty).
    case = models.ForeignKey(
        Case,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="location_pings",
    )
    latitude = models.FloatField()
    longitude = models.FloatField()
    accuracy = models.FloatField(null=True, blank=True, help_text="Reported accuracy in meters")
    speed = models.FloatField(null=True, blank=True, help_text="Reported speed in m/s")
    # Free-form working status the engineer's app reports alongside the location.
    status = models.CharField(max_length=20, blank=True, default="")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["engineer", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.engineer.employee_name} @ {self.timestamp:%Y-%m-%d %H:%M}"


class SnappedTrack(models.Model):
    """One engineer's day, with every fix already moved onto a road.

    Snapping is a paid call to Ola, so a fix is snapped exactly ONCE and the
    result kept. Without this the trail would be re-snapped on every read, and
    the tracking board polls every 30 seconds: one board left open for an hour
    would spend an hour's quota on one engineer.

    `last_ping_id` is how "once" is enforced. Only fixes newer than it are sent,
    and their snapped positions are appended, so the cost over a month is
    (fixes / 50) requests no matter how often anyone looks.
    """

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="snapped_tracks",
    )
    # The IST calendar day this trail belongs to, matching how the board asks
    # for it (?date=YYYY-MM-DD).
    day = models.DateField(db_index=True)
    # [[lat, lon], ...] in travel order. A list rather than an encoded polyline
    # so the map can read it without a decoder, and so a partial append is a
    # plain list concatenation.
    points = models.JSONField(default=list, blank=True)
    # The highest LocationPing id already represented in `points`. 0 means
    # nothing has been snapped yet.
    last_ping_id = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["engineer", "day"], name="one_snapped_track_per_engineer_day"
            )
        ]
        indexes = [
            models.Index(fields=["engineer", "day"]),
        ]

    def __str__(self):
        return f"{self.engineer.employee_name} {self.day} ({len(self.points)} pts)"
