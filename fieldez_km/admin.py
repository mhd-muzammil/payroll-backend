from django.contrib import admin

from .models import FieldezWorkOrder


@admin.register(FieldezWorkOrder)
class FieldezWorkOrderAdmin(admin.ModelAdmin):
    """What the FieldEZ worker read for each WO. A WO whose reads failed
    route_km.MAX_FAILED_ATTEMPTS times is not asked about again; setting its
    attempts back to 0 here puts it back in the worker's list, for as long as
    its check-in is within the days the worker asks about. A FIELDEZ day
    already final keeps its km: only a day not yet settled, or one held open
    by another check-in (km.REREAD_REASONS), takes the new read."""

    list_display = ("wo_key", "status", "fieldez_ticket_id", "attempts", "fetched_at")
    list_filter = ("status",)
    search_fields = ("wo_key", "fieldez_ticket_id")
