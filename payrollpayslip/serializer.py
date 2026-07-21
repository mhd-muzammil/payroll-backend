from rest_framework import serializers
from .models import Payslip, BranchFinancial
from employees.serializer import EmployeeSerializer

class PayslipSerializer(serializers.ModelSerializer):
    employee_details = EmployeeSerializer(source='employee', read_only=True)

    class Meta:
        model = Payslip
        fields = '__all__'
        # Lock every computed field. Payslips are only ever written through the
        # generate_all / recalculate / revert actions (which use setattr, not
        # this serializer). Raw PATCH may only change `status` (e.g. mark Paid),
        # so clients can never overwrite net_salary, deductions, employee, etc.
        read_only_fields = [f.name for f in Payslip._meta.fields if f.name != 'status']


class BranchFinancialSerializer(serializers.ModelSerializer):
    class Meta:
        model = BranchFinancial
        fields = '__all__'
