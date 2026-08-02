from rest_framework import serializers
from .models import Performance, Employee

class PerformanceSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.employee_name', read_only=True)
    reviewer_name = serializers.CharField(source='reviewer.username', read_only=True)

    class Meta:
        model = Performance
        fields = [
            'id',
            'employee',
            'employee_name',
            'reviewer',
            'reviewer_name',
            'review_period',
            'work_quality',
            'attendance',
            'communication',
            'dependability',
            'overall_score',
            'comments',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['reviewer', 'overall_score', 'created_at', 'updated_at']

    def validate(self, attrs):
        # Ratings are on a 0-10 scale; reject out-of-range values (they produce
        # nonsense scores and previously could overflow overall_score).
        for field in ('work_quality', 'attendance', 'communication', 'dependability'):
            val = attrs.get(field)
            if val is not None and (val < 0 or val > 10):
                raise serializers.ValidationError({field: "Rating must be between 0 and 10."})
        return attrs
