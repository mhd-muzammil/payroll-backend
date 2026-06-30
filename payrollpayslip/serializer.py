from rest_framework import serializers
from .models import Payslip, BranchFinancial
from employees.serializer import EmployeeSerializer

class PayslipSerializer(serializers.ModelSerializer):
    employee_details = EmployeeSerializer(source='employee', read_only=True)
    
    class Meta:
        model = Payslip
        fields = '__all__'
        read_only_fields = ['created_at']


class BranchFinancialSerializer(serializers.ModelSerializer):
    class Meta:
        model = BranchFinancial
        fields = '__all__'
