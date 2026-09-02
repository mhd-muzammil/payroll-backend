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

    # WHERE the engineer punched in and out of this call.
    #
    # reached_at and completed_at already say WHEN; these say where they were
    # standing when they said it. Without them "reached 2:40pm" is a claim with
    # nothing behind it — the office could see the trail passing nearby but not
    # that the engineer was at the customer when they marked the work started.
    #
    # Null on every case that predates the punch buttons, and on any case moved
    # by the older accept/start_travel/reached actions, which are still there.
    punch_in_lat = models.FloatField(null=True, blank=True)
    punch_in_lon = models.FloatField(null=True, blank=True)
    punch_in_accuracy = models.FloatField(null=True, blank=True, help_text="Metres, as the phone reported")
    punch_out_lat = models.FloatField(null=True, blank=True)
    punch_out_lon = models.FloatField(null=True, blank=True)
    punch_out_accuracy = models.FloatField(null=True, blank=True, help_text="Metres, as the phone reported")

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

    # How much charge the phone had. Null when the app is too old to report it.
    # This is what turns "no signal" from a shrug into an answer: a last fix at
    # 4% says the phone died, one at 80% says the signal went.
    battery_level = models.IntegerField(
        null=True, blank=True, help_text="Percent, 0-100, as the phone reported it"
    )
    is_charging = models.BooleanField(null=True, blank=True)

    # WHEN THIS HAPPENED, as the phone says. The phone keeps fixes it could not
    # send and posts them when the signal comes back, so this is not the same as
    # when we received it — and the route has to be drawn in the order the
    # engineer travelled, not the order the network delivered.
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    # WHEN WE GOT IT. Ours, not the phone's, so a phone with a wrong clock or a
    # bad actor cannot rewrite history: a gap between the two is what identifies
    # a fix that spent time queued on the phone. Null on rows that predate this.
    received_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # The phone's own id for this fix, so replaying a batch it already sent (a
    # retry after a timeout it never saw the answer to) does not double the
    # trail. Null from an app that does not send one.
    client_key = models.CharField(max_length=64, null=True, blank=True)
    # True when the phone had STOPPED tracking before this fix -- its location
    # was switched off, or the permission was withdrawn -- and this is the first
    # fix after it came back.
    #
    # Why a flag and not a time gap. A hole in the trail has two completely
    # different meanings and they must be treated oppositely: an engineer
    # standing still produces no new rows either (the native watcher only fires
    # after 10m of movement, and the 30s re-send of an unchanged fix is deduped
    # on client_key), so a 40-minute hole is either 40 minutes of untracked
    # driving or 40 minutes of work at one customer. Guessing from the gap alone
    # threw away the journey after every long stop. Only the phone knows which
    # it was, so the phone says.
    after_gap = models.BooleanField(default=False)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["engineer", "timestamp"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["engineer", "client_key"],
                condition=models.Q(client_key__isnull=False),
                name="one_ping_per_client_key",
            )
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


class EngineerScorecard(models.Model):
    """What OpenCall's Engineer Productivity page says about one engineer, today.

    Not computed here. Assigned / Attended / Closed are decided by one function
    in OpenCall (`computeEngineerProductivity`), and the whole point of carrying
    the numbers across rather than deriving our own is that an engineer reading
    their phone and a manager reading the dashboard must never see two different
    figures for the same day. The existing case sync already reuses that same
    function, so these ride the bridge that is already right.

    ONE row per engineer, replaced in place every sync. Deliberately not a
    per-day history table: an append-only table that nothing prunes is exactly
    what silently rotted the OpenCall dispatch query until it timed out, and
    nothing here needs yesterday.
    """

    engineer = models.OneToOneField(
        Employee,
        on_delete=models.CASCADE,
        related_name="scorecard",
    )
    # The IST day the today-figures belong to. A stale card is worse than no
    # card, so the reader compares this against today and shows zeros if the
    # sync has stopped rather than yesterday's numbers dressed as today's.
    as_of = models.DateField()

    assigned = models.IntegerField(default=0)
    attended = models.IntegerField(default=0)
    closed = models.IntegerField(default=0)

    # Closes since the 1st of the month, for the month-to-date target.
    month_closed = models.IntegerField(default=0)
    # Carried from OpenCall rather than hard-coded, so changing the target there
    # changes it here without a deploy on this side.
    daily_target = models.IntegerField(default=0)
    monthly_target = models.IntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["as_of"])]

    def __str__(self):
        return (
            f"{self.engineer.employee_name} {self.as_of}: "
            f"{self.assigned}/{self.attended}/{self.closed}"
        )
