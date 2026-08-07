from rest_framework import serializers
from django.db.models import Q
from django.utils import timezone
from .models import Attendance, LeaveRequest
from employees.models import Employee

class AttendanceSerializer(serializers.ModelSerializer):
    employee_id = serializers.IntegerField(source="employee.id", read_only=True)
    branch = serializers.SerializerMethodField()
    email = serializers.SerializerMethodField()

    class Meta:
        model = Attendance
        fields = '__all__'
        read_only_fields = ("employee",)

    def get_branch(self, obj):
        if obj.employee:
            return obj.employee.branch
        return "Chennai"

    def get_email(self, obj):
        # Expose the employee's email so the attendance view can show it and
        # duplicate/namesake rows can be told apart.
        return obj.employee.email if obj.employee else None

    def _get_employee_for_user(self, user):
        try:
            return user.employee_profile
        except Employee.DoesNotExist:
            employee = None
            if user.email:
                employee = Employee.objects.filter(email__iexact=user.email, user__isnull=True).first()
            if not employee:
                employee = Employee.objects.filter(employee_name__iexact=user.username, user__isnull=True).first()
            if employee:
                employee.user = user
                employee.save(update_fields=["user"])
                return employee
            raise serializers.ValidationError(
                "Employee profile is not linked. Ask admin to link this user in Employee.user."
            )

    def _copy_employee_fields(self, attrs, employee):
        attrs["employee"] = employee
        attrs["employee_name"] = employee.employee_name
        attrs["role"] = employee.role
        attrs["department"] = employee.department
        attrs["salary"] = employee.salary
        return attrs

    def validate(self, attrs):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return attrs

        user = request.user
        role = "superadmin" if user.is_superuser else user.role
        if role == "employee":
            employee = self._get_employee_for_user(user)
            attrs = self._copy_employee_fields(attrs, employee)
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            user = request.user
            role = "superadmin" if user.is_superuser else user.role
            if role == "employee":
                employee = self._get_employee_for_user(user)
                validated_data = self._copy_employee_fields(validated_data, employee)
                submitted_dt = validated_data.get("intime") or validated_data.get("outtime")
                target_date = timezone.localtime(submitted_dt).date() if submitted_dt else timezone.localdate()
                existing = (
                    Attendance.objects.filter(employee=employee)
                    .filter(Q(intime__date=target_date) | Q(outtime__date=target_date))
                    .order_by("-id")
                    .first()
                )

                incoming_intime = validated_data.get("intime")
                incoming_outtime = validated_data.get("outtime")

                if existing:
                    if incoming_intime and existing.intime:
                        raise serializers.ValidationError("In Time already submitted for today.")
                    if incoming_outtime and existing.outtime:
                        raise serializers.ValidationError("Out Time already submitted for today.")

                    changed = False
                    if incoming_intime and not existing.intime:
                        existing.intime = incoming_intime
                        changed = True
                    if incoming_outtime and not existing.outtime:
                        existing.outtime = incoming_outtime
                        changed = True

                    if changed:
                        existing.status = validated_data.get("status", existing.status)
                        existing.save()
                    return existing

        return super().create(validated_data)


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.employee_name", read_only=True)
    branch = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = "__all__"
        read_only_fields = ("employee", "status", "applied_on")

    def get_branch(self, obj):
        if obj.employee:
            return obj.employee.branch
        return "Chennai"

    def validate(self, attrs):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            user = request.user
            role = "superadmin" if user.is_superuser else user.role
            employee = getattr(user, "employee_profile", None)
            if not employee:
                raise serializers.ValidationError("Your account has no Employee profile linked. Cannot create Leave request.")
            attrs["employee"] = employee
        return attrs
