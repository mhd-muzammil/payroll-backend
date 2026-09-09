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
        # `employee` is writable on purpose: the office posts the id of the
        # person it is marking, and without it the row is saved belonging to
        # nobody -- see _link_office_record. An employee's own row does not
        # get to choose: validate() overwrites it from their profile.

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

    def _link_office_record(self, attrs):
        """Attach the employee a row the office is creating is about.

        Prefers the id the page sends. Falls back to the name, and only to an
        unambiguous match: an active employee if exactly one is called that,
        otherwise any one employee called that, otherwise nothing. Namesakes
        are left unlinked -- two cards for one name is a smaller lie than one
        person's absence landing on somebody else's record.

        Never raises. A row that cannot be attributed is still saved, exactly
        as it was before, so nobody is stopped from marking attendance for a
        name the employee list does not have.
        """
        if attrs.get("employee") is not None:
            return attrs

        # On a PATCH the payload carries only what changed, so the name comes
        # off the row itself. That is what lets opening an old unlinked row to
        # correct its status also give it back its owner.
        name = (attrs.get("employee_name") or getattr(self.instance, "employee_name", "") or "").strip()
        if not name:
            return attrs

        named = Employee.objects.filter(employee_name__iexact=name)
        active = list(named.exclude(status="relieved")[:2])
        matches = active if len(active) == 1 else list(named[:2])
        if len(matches) == 1:
            attrs["employee"] = matches[0]
        return attrs

    def validate(self, attrs):
        # A record has to say WHICH DAY it is about.
        #
        # The office marked somebody absent, left both times empty -- there is
        # no punch on a day nobody came in -- and the row was created with no
        # date at all. Every list on the page is grouped and filtered by the
        # date, which is read off intime, so the record existed and could not
        # be seen or corrected by anyone. Refusing it says so at the moment it
        # happens. A day with no punch is stored as midnight on that date, the
        # same as the Excel import writes one.
        if self.instance is None and not attrs.get("intime") and not attrs.get("outtime"):
            raise serializers.ValidationError(
                {"intime": "Please give the date this record is for."}
            )

        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return attrs

        user = request.user
        role = "superadmin" if user.is_superuser else user.role
        if role == "employee":
            # Their own row, whatever the payload claims.
            employee = self._get_employee_for_user(user)
            attrs = self._copy_employee_fields(attrs, employee)
        elif self.instance is None or self.instance.employee_id is None:
            # The office marking somebody. Also on edit while the row still
            # belongs to nobody: opening an old row to correct it is a fair
            # moment to give it back its owner. An already-linked row is left
            # alone -- a rename must not move a day onto another person.
            attrs = self._link_office_record(attrs)
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
