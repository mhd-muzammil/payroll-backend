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
    case_number = models.CharField(max_length=20, unique=True, blank=True, db_index=True)

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

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        creating = self._state.adding
        super().save(*args, **kwargs)
        # Generate a stable, readable case number from the PK after the first save.
        if creating and not self.case_number:
            self.case_number = f"OC-{self.pk:06d}"
            super().save(update_fields=["case_number"])

    def __str__(self):
        return f"{self.case_number or 'OC-?'} - {self.customer_name}"


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
