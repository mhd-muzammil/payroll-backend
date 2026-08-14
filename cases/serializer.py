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
        # status and assigned_to are driven ONLY by the assign / transition
        # actions (which enforce the state machine + permissions), never by a
        # raw create/update payload — otherwise a re-dispatched ticket could
        # reopen a completed case or silently reassign it.
        read_only_fields = (
            "case_number",
            "status",
            # Owned by the sync's mirror pass — it is what makes the engineer's
            # count equal OpenCall's Assigned column, so a raw PATCH must not
            # move a case in or out of the plan.
            "in_current_plan",
            "plan_date",
            # Mirrors the originating system's ticket row; only the sync writes it.
            "details",
            "assigned_to",
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
    """Read-only shape for the admin live map: one row per ON-DUTY engineer with
    their last known position, how long they have been on duty and how far they
    have travelled on it.

    Position fields are nullable: an engineer can be on duty with no fix yet, or
    with an old one. `stale` says the position is not current; `on_duty` still
    being true is the point — they have not gone home, their phone has just
    stopped reporting.
    """

    engineer_id = serializers.IntegerField()
    engineer_name = serializers.CharField()
    branch = serializers.CharField(allow_null=True)

    on_duty = serializers.BooleanField()
    duty_started_at = serializers.DateTimeField()
    duty_minutes = serializers.IntegerField()
    stale = serializers.BooleanField()
    last_seen_minutes = serializers.IntegerField(allow_null=True)
    distance_km = serializers.FloatField()

    latitude = serializers.FloatField(allow_null=True)
    longitude = serializers.FloatField(allow_null=True)
    accuracy = serializers.FloatField(allow_null=True)
    speed = serializers.FloatField(allow_null=True)
    status = serializers.CharField(allow_blank=True)
    timestamp = serializers.DateTimeField(allow_null=True)
    active_case_id = serializers.IntegerField(allow_null=True)
    active_case_number = serializers.CharField(allow_null=True)
