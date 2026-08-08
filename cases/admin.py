from django.contrib import admin
from .models import Case, EngineerAlias, LocationPing


@admin.register(EngineerAlias)
class EngineerAliasAdmin(admin.ModelAdmin):
    """Where an operator fixes an engineer whose OpenCall name Payroll can't match.
    The names needing an entry come back in bulk_dispatch's `unmatched_engineers`."""

    list_display = ("external_name", "employee", "note", "created_at")
    search_fields = ("external_name", "employee__employee_name")
    autocomplete_fields = ("employee",)


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = ("case_number", "customer_name", "title", "priority", "status", "assigned_to", "created_at")
    list_filter = ("status", "priority")
    search_fields = ("case_number", "customer_name", "customer_phone", "title")


@admin.register(LocationPing)
class LocationPingAdmin(admin.ModelAdmin):
    list_display = ("engineer", "case", "latitude", "longitude", "accuracy", "timestamp")
    list_filter = ("engineer",)
