from rest_framework import serializers
from .models import Task, Employee

class TaskSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='assigned_to.employee_name', read_only=True)
    assigned_by_name = serializers.CharField(source='assigned_by.username', read_only=True)

    class Meta:
        model = Task
        fields = [
            'id',
            'title',
            'description',
            'assigned_to',
            'employee_name',
            'assigned_by',
            'assigned_by_name',
            'status',
            'priority',
            'due_date',
            'employee_notes',
            'checklist',
            'activity_log',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['assigned_by', 'created_at', 'updated_at']
