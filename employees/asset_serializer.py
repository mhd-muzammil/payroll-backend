from rest_framework import serializers
from .models import Asset

class AssetSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='assigned_to.employee_name', read_only=True)

    class Meta:
        model = Asset
        fields = [
            'id',
            'asset_name',
            'asset_type',
            'serial_number',
            'status',
            'assigned_to',
            'employee_name',
            'purchase_date',
            'cost',
            'notes',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_serial_number(self, value):
        if value:
            # Check if serial number already exists for another asset
            qs = Asset.objects.filter(serial_number=value)
            if self.instance:
                qs = qs.exclude(id=self.instance.id)
            if qs.exists():
                raise serializers.ValidationError("An asset with this serial number already exists.")
        return value
