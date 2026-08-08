from django.contrib import admin

from .models import EmployeeRequest, RequestMessage


class RequestMessageInline(admin.TabularInline):
    model = RequestMessage
    extra = 0
    readonly_fields = ("sender", "from_employee", "is_decision", "created_at")


@admin.register(EmployeeRequest)
class EmployeeRequestAdmin(admin.ModelAdmin):
    list_display = ("employee", "request_type", "amount", "status", "reviewed_by", "created_at")
    list_filter = ("status", "request_type")
    search_fields = ("employee__employee_name", "reason")
    inlines = [RequestMessageInline]
