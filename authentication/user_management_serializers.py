from rest_framework import serializers
from .models import User


class UserManagementSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=False)
    branch = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "role",
            "is_active",
            "is_staff",
            "phone_number",
            "password",
            "plain_password",
            "date_joined",
            "branch",
            "assigned_branch",
            "allowed_sections",
        )
        read_only_fields = ("date_joined", "is_staff")

    def get_branch(self, obj):
        employee_profile = getattr(obj, "employee_profile", None)
        return employee_profile.branch if employee_profile else None

    def validate_phone_number(self, value):
        if not value:
            return None
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
            user.plain_password = password
        else:
            user.set_unusable_password()
            user.plain_password = ""
        user.is_staff = user.role in ["admin", "superadmin"] or user.is_superuser
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
            instance.plain_password = password
        instance.is_staff = instance.role in ["admin", "superadmin"] or instance.is_superuser
        instance.save()
        return instance
