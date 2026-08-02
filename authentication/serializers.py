from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework import serializers
from .models import User


class RoleTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = "superadmin" if user.is_superuser else user.role
        token["first_name"] = user.first_name
        token["last_name"] = user.last_name
        token["username"] = user.username
        token["allowed_sections"] = user.allowed_sections
        token["assigned_branch"] = user.assigned_branch
        employee_profile = getattr(user, "employee_profile", None)
        token["employee_id"] = employee_profile.id if employee_profile else None
        return token


PRIVILEGED_ROLES = ["admin", "superadmin"]


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "role",
            "is_active",
            "date_joined",
            "password",
            "allowed_sections",
            "assigned_branch",
        ]
        read_only_fields = ["date_joined"]

    def validate(self, attrs):
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
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User.objects.create(**validated_data)
        if password:
            user.set_password(password)
            user.plain_password = password  # viewable copy for admins
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
            instance.plain_password = password  # keep viewable copy in sync
        instance.save()
        return instance
