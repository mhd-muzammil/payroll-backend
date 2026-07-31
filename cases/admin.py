from django.contrib import admin
from .models import Case, LocationPing


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = ("case_number", "customer_name", "title", "priority", "status", "assigned_to", "created_at")
    list_filter = ("status", "priority")
    search_fields = ("case_number", "customer_name", "customer_phone", "title")


@admin.register(LocationPing)
class LocationPingAdmin(admin.ModelAdmin):
    list_display = ("engineer", "case", "latitude", "longitude", "accuracy", "timestamp")
    list_filter = ("engineer",)
