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

    # The longest the phone's own tracker runs (TrackerRules in the APK). The
    # server ends a forgotten duty sooner: at the 12:00 AM after the day it
    # began (TrackingViewSet._close_forgotten_sessions).
    MAX_DURATION_HOURS = 16

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="duty_sessions",
    )
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    # NULL means the engineer is still on duty right now.
    ended_at = models.DateTimeField(null=True, blank=True)
    # Set when the midnight sweep ended it instead of the engineer.
    auto_closed = models.BooleanField(default=False)
    # The last time the phone was heard from on this duty: a fix it had not
    # sent before, or a post to events/ (an empty one is the heartbeat).
    #
    # Not a page load, a Login or a re-sent fix. Those prove a page is open,
    # not that anything is recording: a phone whose tracker had stopped kept
    # asking for its duty every minute and would have read as live all day.
    # Null until the first.
    last_contact_at = models.DateTimeField(null=True, blank=True)

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


class PlaceName(models.Model):
    """One coordinate's street address, looked up once and kept.

    The timeline shows where each entry happened, and most entries get that for
    free -- a call the engineer reached carries the customer's address already.
    This is for the rest: where they stood still away from a customer, and where
    the trail went dark. Ola is metered and rate-limited per minute, so a place
    is asked about once and then remembered.

    Keyed on the coordinate rounded to four decimals, about eleven metres: fine
    enough to tell two customers on a street apart, coarse enough that standing
    in the same yard twice is one row.
    """

    lat_key = models.DecimalField(max_digits=9, decimal_places=4)
    lon_key = models.DecimalField(max_digits=9, decimal_places=4)
    address = models.CharField(max_length=300)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["lat_key", "lon_key"], name="one_name_per_place"),
        ]

    def __str__(self):
        return f"{self.lat_key},{self.lon_key} -> {self.address[:40]}"


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
    #
    # What it is worth changed with km_rule's cutover. Older apps set it on
    # every reload and restart as well, and it was often lost on the way, so
    # since then it only marks the rail; the kilometres read the Login and the
    # phone's own gps_off events (PhoneEvent) instead.
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


class PhoneEvent(models.Model):
    """Something the engineer's phone noticed about itself, and when.

    A hole in the trail used to be read off the fixes alone, and fixes cannot
    say why they stopped. Location switched off, the network gone, the phone
    closing the app, or simply an engineer standing still all leave the same
    silence. The phone knows which it was, so it says: one row per thing it
    noticed, posted to /api/tracking/events/.

    Two of them change the kilometres (cases/km_rule.py): Location switched
    off while the duty was running, and the phone stopping the app. The rest
    only explain the day on the rail.
    """

    KINDS = (
        "gps_off",
        "gps_on",
        "net_off",
        "net_on",
        "tracker_stopped",
        "tracker_started",
        "app_opened",
    )

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="phone_events",
    )
    kind = models.CharField(max_length=24)
    # When it happened, as the phone says -- held events arrive late, like
    # held fixes -- checked the way a fix's timestamp is.
    at = models.DateTimeField(db_index=True)
    # When we got it. Ours. Whether the reason for a gap was already here when
    # the fix after it arrived is what keeps kilometres from going down.
    received_at = models.DateTimeField()
    # The phone's own id for it, so an event posted twice is stored once.
    client_key = models.CharField(max_length=64)
    # Where the phone last was, when it knew.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # "switched" (seen changing while the duty ran) or "found_off" /
    # "found_on" (already so when the phone looked) for GPS; free words for
    # the rest.
    detail = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["engineer", "client_key"], name="one_phone_event_per_client_key"
            )
        ]

    def __str__(self):
        return f"{self.engineer.employee_name} {self.kind} @ {self.at:%Y-%m-%d %H:%M}"


class SnappedTrack(models.Model):
    """One engineer's day, with every fix already moved onto a road.

    Snapping is a paid call to Ola, so a fix is snapped exactly ONCE and the
    result kept. Without this the trail would be re-snapped on every read, and
    the tracking board polls every 30 seconds: one board left open for an hour
    would spend an hour's quota on one engineer.

    `ping_ids` is how "once" is enforced. A read sends only the fixes past the
    stored points that still match its trail, so the cost over a month is about
    (fixes / 50) requests no matter how often anyone looks. A late fix -- held
    on the phone, delivered after fixes taken later than it -- cuts the stored
    route where it belongs, and the fixes after it are snapped again, so the
    route stays in the order it was travelled (cases/tracks.py).
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
    # The LocationPing behind each stored point, one for one and in the same
    # order: points[i] is fix ping_ids[i] put on a road. Empty on a track
    # stored before this field existed; tracks.py adopts those on first read.
    ping_ids = models.JSONField(default=list, blank=True)
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


class EngineerScorecardDay(models.Model):
    """One engineer's Assigned / Attended / Closed and targets for one day, kept.

    The live card above stays one row per engineer, overwritten every sync.
    This is the record of each day beside it, for the engineer's own history:
    the same OpenCall numbers, filed under the day they belong to.

    Two writers. The live push (POST scorecards/) writes today's row with the
    card, every two minutes -- source "live". OpenCall's history push (POST
    scorecard_days/) rewrites the last few days after late Evening edits and a
    Final EOD, and backfills older ones -- source "history". Today belongs to
    the live push: a history push never overwrites today's live row.

    One row per (engineer, day), updated in place, and only ever read for one
    engineer over a range of days, through the index on exactly that. It grows
    by a row per engineer per day, and no read of it scans the rest.
    """

    SOURCE_LIVE = "live"
    SOURCE_HISTORY = "history"

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="scorecard_days",
    )
    # The IST day the numbers are for, as OpenCall's reports count days.
    day = models.DateField()

    assigned = models.PositiveIntegerField(default=0)
    attended = models.PositiveIntegerField(default=0)
    closed = models.PositiveIntegerField(default=0)

    # Kept on each day, so a later change of target does not rewrite what the
    # engineer was measured against on the day.
    daily_target = models.PositiveIntegerField(default=0)
    monthly_target = models.PositiveIntegerField(default=0)

    # SOURCE_LIVE or SOURCE_HISTORY: which push wrote it last.
    source = models.CharField(max_length=16)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["engineer", "day"], name="one_scorecard_per_engineer_day"
            )
        ]
        indexes = [
            models.Index(fields=["engineer", "day"]),
        ]

    def __str__(self):
        return (
            f"{self.engineer.employee_name} {self.day}: "
            f"{self.assigned}/{self.attended}/{self.closed} ({self.source})"
        )


class CaseVisit(models.Model):
    """One trip to a call: the day the engineer was sent, and the two taps.

    A call can be dispatched more than once -- a second day for the part that
    did not arrive, a third for the customer who was out. The Case row holds
    one pair of punch times, so without this the second trip could only
    overwrite the first, and the day board would lose the first day's arrival
    the moment the engineer arrived on the second.

    One row per (case, plan day). Nothing here is ever rewritten by a later
    trip: yesterday's row keeps yesterday's times whatever happens today.

    The Case still carries the LATEST trip's times in its own columns, which is
    what every existing screen reads. This is the record behind them.
    """

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="visits",
    )
    # Who it was booked to on that day. Kept per trip rather than read off the
    # case, because a ticket that moves to another engineer must not rewrite who
    # made yesterday's visit. SET_NULL so removing an employee cannot delete
    # the record of a visit that happened.
    engineer = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="case_visits",
    )
    # The plan day this trip belongs to, as the originating system counts days.
    plan_date = models.DateField(db_index=True)

    # When the office put the call on that day's list. Not the same as
    # Case.assigned_at, which a second dispatch re-stamps -- this keeps the
    # first day's own answer.
    assigned_at = models.DateTimeField(null=True, blank=True)

    # The engineer's two taps, and where they were standing for each. Null
    # until they press them; a trip that was dispatched and never visited keeps
    # both null, which is itself the answer.
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    punch_in_lat = models.FloatField(null=True, blank=True)
    punch_in_lon = models.FloatField(null=True, blank=True)
    punch_in_accuracy = models.FloatField(null=True, blank=True, help_text="Metres, as the phone reported")
    punch_out_lat = models.FloatField(null=True, blank=True)
    punch_out_lon = models.FloatField(null=True, blank=True)
    punch_out_accuracy = models.FloatField(null=True, blank=True, help_text="Metres, as the phone reported")

    # What the engineer said they did on THIS trip. On the case there is one
    # field, so a second check-out with nothing typed used to leave the first
    # trip's answer standing as the second's.
    resolution_notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One trip per day per call. A second visit to the same customer on the
        # same day is not something this system has ever been able to record,
        # and pretending otherwise here would give the day board two arrivals
        # it cannot tell apart.
        constraints = [
            models.UniqueConstraint(fields=["case", "plan_date"], name="one_visit_per_case_per_day"),
        ]
        # The day board asks "this engineer, this day"; the card asks "this
        # case, latest". Indexed for both, because this table only grows.
        indexes = [
            models.Index(fields=["engineer", "plan_date"]),
            models.Index(fields=["case", "-plan_date"]),
        ]
        ordering = ["plan_date", "id"]

    def __str__(self):
        return f"{self.case_id} on {self.plan_date}"


class RouteKmDay(models.Model):
    """One engineer's day, measured by road through the places they marked.

    Login, every Check in, Logout -- in the order they happened -- and the road
    distance through them, from Ola's Directions. It is the KM report's second
    table, beside the GPS kilometres, and it answers a different question: not
    how far the phone saw them go, but how far it is by road between the places
    the engineer themselves said they were.

    Measured ONCE and kept. Directions is metered and Ola throttles per minute,
    and a bill cycle of twenty engineers is six hundred cells, so a cell is
    measured again only when the points under it change -- `signature` is how
    that is known without asking Ola. `final` is set once the day is settled,
    and a final row is served as it is, its points never read again.
    """

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="route_km_days",
    )
    # The IST calendar day, the way the KM report asks for it.
    day = models.DateField(db_index=True)
    # The points the km was measured through: [{"kind", "at", "latitude",
    # "longitude", "case_number"}, ...] in time order, "at" as an ISO string.
    # Kept beside the figure so the map drawn next to a km is the route that km
    # is for, even while a later tap is waiting to be measured.
    points = models.JSONField(default=list, blank=True)
    # A hash of those points' kinds and places. The same signature on the next
    # look means nothing has moved and the stored km still holds, so no request
    # is spent finding that out.
    signature = models.CharField(max_length=40, blank=True, default="")
    # pending, ready, no_points (fewer than two places marked that day) or
    # failed (Ola could not route it).
    status = models.CharField(max_length=12, default="pending")
    # Null unless ready. 0.0 is an answer, not a gap: every point in one place.
    km = models.FloatField(null=True, blank=True)
    # Ola's road route as [[lat, lon], ...], for the map. Empty unless ready.
    path = models.JSONField(default=list, blank=True)
    # Why Ola could not route it, when failed. Never carries the key -- see
    # route_km._post. "rate limited by Ola" while Ola is making every worker
    # wait, whatever the status.
    error = models.CharField(max_length=200, blank=True, default="")
    # How many times in a row Ola has failed this day. A settled day that has
    # failed route_km.MAX_FAILED_ATTEMPTS times is final, not retried for ever.
    attempts = models.PositiveSmallIntegerField(default=0)
    # When the points were last read, and when Ola was last asked. The second
    # is indexed because the per-minute throttle counts it, across every worker.
    checked_at = models.DateTimeField(null=True, blank=True)
    measured_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Set once the day is settled. A final row is never looked at again.
    final = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["engineer", "day"], name="one_route_km_per_engineer_day"
            )
        ]
        indexes = [
            models.Index(fields=["engineer", "day"]),
        ]

    def __str__(self):
        return f"{self.engineer.employee_name} {self.day} ({self.status})"


class ImportedKmDay(models.Model):
    """An engineer's km for a day the app did not record, taken from a report.

    Before this app tracked them, the engineers were tracked by another one,
    and its Monthly Report is the only record of how far they went on those
    days. A row here IS that day's km: _DayKm reads it in place of the GPS
    trail, so the KM report, Engineer Productivity, the board and the
    engineer's own history all show the same figure for it. Never for today:
    a day still going on is the app's.

    Written by `manage.py import_old_app_km`, which checks who each figure is
    for before it writes one.
    """

    engineer = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="imported_km_days",
    )
    # The IST calendar day, the way the KM report asks for it.
    day = models.DateField()
    km = models.FloatField()
    # Where the figure came from, so it is never mistaken for the app's own.
    source = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["engineer", "day"], name="one_imported_km_per_engineer_day"
            )
        ]

    def __str__(self):
        return f"{self.engineer.employee_name} {self.day}: {self.km} km ({self.source})"


class PhoneStatus(models.Model):
    """How an employee's phone is set up to record, one row per change.

    The office could not see it. A battery left to the phone's optimiser stops
    the app in the background and the kilometres go with it; Location and
    notifications can be refused; GPS can be off. The app reports these when it
    opens (/api/tracking/phone_status/). A report that changes nothing only
    moves last_seen_at, so the rows are the history of what changed and when,
    and the newest row is the phone as it is.
    """

    PERMISSIONS = ("granted", "denied", "prompt", "prompt-with-rationale")

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="phone_statuses",
    )
    app_version = models.CharField(max_length=40, blank=True, default="")
    android_version = models.CharField(max_length=20, blank=True, default="")
    # None: the phone could not say. Only APK 1.5 reports the battery; an older
    # one says GPS only when the Location plugin's answer gives it away.
    battery_exempt = models.BooleanField(null=True)
    # One of PERMISSIONS, or "" when the phone did not say.
    location_permission = models.CharField(max_length=24, blank=True, default="")
    notification_permission = models.CharField(max_length=24, blank=True, default="")
    gps_on = models.BooleanField(null=True)
    # The first report with these values, and the latest one still saying them.
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-first_seen_at", "-id"]
        indexes = [
            # The newest row per employee, read once per employee by App Usage.
            models.Index(fields=["employee", "-first_seen_at", "-id"]),
        ]

    def __str__(self):
        return f"{self.employee.employee_name} phone at {self.first_seen_at:%Y-%m-%d %H:%M}"
