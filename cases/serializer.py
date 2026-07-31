from rest_framework import serializers
from .models import Case, LocationPing


class CaseSerializer(serializers.ModelSerializer):
    assigned_to_name = serializers.CharField(source="assigned_to.employee_name", read_only=True)
    assigned_by_name = serializers.CharField(source="assigned_by.username", read_only=True)
    branch = serializers.SerializerMethodField()

    class Meta:
        model = Case
        fields = "__all__"
        # These are set by the server (dispatch action / lifecycle transitions),
        # never trusted from the client payload.
        read_only_fields = (
            "case_number",
            "assigned_by",
            "assigned_at",
            "started_at",
            "reached_at",
            "completed_at",
            "created_at",
            "updated_at",
        )

    def get_branch(self, obj):
        if obj.assigned_to:
            return obj.assigned_to.branch
        return None


class LocationPingSerializer(serializers.ModelSerializer):
    engineer_name = serializers.CharField(source="engineer.employee_name", read_only=True)

    class Meta:
        model = LocationPing
        fields = "__all__"
        read_only_fields = ("engineer", "timestamp")


class LiveEngineerSerializer(serializers.Serializer):
    """Read-only shape for the admin live map: one row per engineer with their
    latest known position and what they are currently working on."""

    engineer_id = serializers.IntegerField()
    engineer_name = serializers.CharField()
    branch = serializers.CharField(allow_null=True)
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    accuracy = serializers.FloatField(allow_null=True)
    speed = serializers.FloatField(allow_null=True)
    status = serializers.CharField(allow_blank=True)
    timestamp = serializers.DateTimeField()
    active_case_id = serializers.IntegerField(allow_null=True)
    active_case_number = serializers.CharField(allow_null=True)
