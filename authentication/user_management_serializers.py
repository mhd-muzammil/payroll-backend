from rest_framework import serializers
from .models import User

PRIVILEGED_ROLES = ["admin", "superadmin"]


class UserManagementSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=False)
    branch = serializers.SerializerMethodField()
    plain_password = serializers.SerializerMethodField()

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

    def get_plain_password(self, obj):
        """Only reveal a viewable password when it is safe to: a superadmin may
        see any account's; anyone else may see ONLY plain (employee) accounts'.
        This stops a branch-scoped admin from reading an admin/superadmin's
        plaintext credentials (privilege escalation)."""
        request = self.context.get("request")
        viewer = getattr(request, "user", None) if request else None
        viewer_is_superadmin = bool(
            viewer and (viewer.is_superuser or getattr(viewer, "role", None) == "superadmin")
        )
        target_privileged = obj.is_superuser or obj.is_staff or obj.role in PRIVILEGED_ROLES
        if viewer_is_superadmin or not target_privileged:
            return obj.plain_password
        return None

    def validate_phone_number(self, value):
        if not value:
            return None
        return value

    def validate(self, attrs):
        """Prevent privilege escalation.

        - Only a superadmin may create/modify admin or superadmin accounts.
        - A user may never change their own role or active flag.
        """
        request = self.context.get("request")
        actor = getattr(request, "user", None) if request else None
        actor_is_superadmin = bool(
            actor and (actor.is_superuser or getattr(actor, "role", None) == "superadmin")
        )

        target_role = attrs.get("role")
        existing_role = getattr(self.instance, "role", None)
        if (target_role in PRIVILEGED_ROLES or existing_role in PRIVILEGED_ROLES) and not actor_is_superadmin:
            raise serializers.ValidationError(
                "Only a superadmin may create or modify admin/superadmin accounts."
            )

        if actor is not None and self.instance is not None and self.instance.pk == actor.pk:
            if target_role is not None and target_role != actor.role:
                raise serializers.ValidationError("You cannot change your own role.")
            if "is_active" in attrs and attrs["is_active"] != actor.is_active:
                raise serializers.ValidationError("You cannot change your own active status.")

        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
            user.plain_password = password  # keep a viewable copy for admins
        else:
            user.set_unusable_password()
        user.is_staff = user.role in PRIVILEGED_ROLES or user.is_superuser
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
            instance.plain_password = password  # keep the viewable copy in sync
        instance.is_staff = instance.role in PRIVILEGED_ROLES or instance.is_superuser
        instance.save()
        return instance
