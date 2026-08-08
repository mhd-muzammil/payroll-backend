from decimal import Decimal

from rest_framework import serializers

from .models import EmployeeRequest, RequestMessage


class RequestMessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField()

    class Meta:
        model = RequestMessage
        fields = (
            "id",
            "body",
            "from_employee",
            "is_decision",
            "sender_name",
            "created_at",
        )
        read_only_fields = fields

    def get_sender_name(self, obj):
        if obj.from_employee:
            return obj.request.employee.employee_name
        # Staff reply: show the person, falling back to a neutral label if their
        # login has since been removed.
        if obj.sender:
            employee = getattr(obj.sender, "employee_profile", None)
            return employee.employee_name if employee else obj.sender.username
        return "Office"


class EmployeeRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.employee_name", read_only=True)
    branch = serializers.CharField(source="employee.branch", read_only=True)
    request_type_label = serializers.CharField(source="get_request_type_display", read_only=True)
    reviewed_by_name = serializers.CharField(source="reviewed_by.username", read_only=True)
    messages = RequestMessageSerializer(many=True, read_only=True)
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = EmployeeRequest
        fields = (
            "id",
            "employee",
            "employee_name",
            "branch",
            "request_type",
            "request_type_label",
            "amount",
            "reason",
            "status",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "created_at",
            "updated_at",
            "messages",
            "unread_count",
        )
        # The employee is taken from the caller, and the decision fields move
        # only through approve/reject — never from a raw payload.
        read_only_fields = (
            "employee",
            "status",
            "reviewed_by",
            "reviewed_at",
            "created_at",
            "updated_at",
        )

    def get_unread_count(self, obj):
        """Messages the CALLER has not read, from their side of the thread."""
        viewer_is_employee = self.context.get("viewer_is_employee", False)
        field = "read_by_employee" if viewer_is_employee else "read_by_staff"
        return sum(
            1
            for m in obj.messages.all()
            if not getattr(m, field) and m.from_employee != viewer_is_employee
        )

    def validate(self, attrs):
        request_type = attrs.get("request_type", getattr(self.instance, "request_type", None))
        amount = attrs.get("amount", getattr(self.instance, "amount", None))

        if request_type in EmployeeRequest.AMOUNT_TYPES:
            if amount is None:
                raise serializers.ValidationError(
                    {"amount": "How much are you asking for? An amount is required."}
                )
            if amount <= Decimal("0"):
                raise serializers.ValidationError({"amount": "Amount must be more than zero."})
        else:
            # A report carries no money; drop anything sent so it cannot show up
            # later as an approved figure nobody intended.
            attrs["amount"] = None

        reason = attrs.get("reason", getattr(self.instance, "reason", "")) or ""
        if not reason.strip():
            raise serializers.ValidationError({"reason": "Please say what this is for."})
        return attrs
