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
