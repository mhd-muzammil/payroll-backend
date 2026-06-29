from rest_framework import serializers
from .models import Employee
from onboarding.models import Onboarding

class EmployeeSerializer(serializers.ModelSerializer):
    dob = serializers.SerializerMethodField()
    bank_name = serializers.SerializerMethodField()
    account_holder_name = serializers.SerializerMethodField()
    account_number = serializers.SerializerMethodField()
    ifsc_code = serializers.SerializerMethodField()
    bank_branch = serializers.SerializerMethodField()
    work_location = serializers.SerializerMethodField()

    def get_onboarding(self, obj):
        if not hasattr(self, '_onboarding_cache'):
            self._onboarding_cache = {}
        if obj.id not in self._onboarding_cache:
            onboarding = Onboarding.objects.filter(employee_id=str(obj.id)).first()
            if not onboarding and obj.email:
                onboarding = Onboarding.objects.filter(email_id=obj.email).first()
            if not onboarding:
                onboarding = Onboarding.objects.filter(employee_name__iexact=obj.employee_name).first()
            self._onboarding_cache[obj.id] = onboarding
        return self._onboarding_cache[obj.id]

    def get_dob(self, obj):
        onboarding = self.get_onboarding(obj)
        return onboarding.dob.strftime('%d-%m-%Y') if onboarding and onboarding.dob else None

    def get_bank_name(self, obj):
        onboarding = self.get_onboarding(obj)
        return onboarding.bank_name if onboarding else None

    def get_account_holder_name(self, obj):
        onboarding = self.get_onboarding(obj)
        return onboarding.account_holder_name if onboarding else None

    def get_account_number(self, obj):
        onboarding = self.get_onboarding(obj)
        return onboarding.account_number if onboarding else None

    def get_ifsc_code(self, obj):
        onboarding = self.get_onboarding(obj)
        return onboarding.ifsc_code if onboarding else None

    def get_bank_branch(self, obj):
        onboarding = self.get_onboarding(obj)
        return onboarding.bank_branch if onboarding else None

    def get_work_location(self, obj):
        onboarding = self.get_onboarding(obj)
        return onboarding.work_location if onboarding else None

    def validate_email(self, value):
        return value or None

    def validate_phone(self, value):
        return value or None

    def validate_work_lat(self, value):
        return value if value else None

    def validate_work_lon(self, value):
        return value if value else None

    class Meta:
        model = Employee
        fields = '__all__'


class SimpleEmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ['id', 'employee_name', 'email', 'role', 'department']

