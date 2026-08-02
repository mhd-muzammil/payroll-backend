"""Casual-leave accrual + payslip offset tests. Deletes its own test rows.
Run: python test_casual_leave.py
"""
import os
import django
from decimal import Decimal
import datetime

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "payroll.settings")
os.environ.setdefault("DEBUG", "1")
django.setup()

from employees.models import Employee
from payrollpayslip.views import (
    casual_leave_available,
    compute_payslip_fields,
    _months_of_service,
)


def cleanup():
    Employee.objects.filter(employee_name__in=["CL Eligible", "CL NoDOJ"]).delete()


def main():
    cleanup()

    # months-of-service boundary
    assert _months_of_service(datetime.date(2025, 1, 15), datetime.date(2025, 7, 14)) == 5
    assert _months_of_service(datetime.date(2025, 1, 15), datetime.date(2025, 7, 15)) == 6
    print("1. months_of_service boundary ok")

    # Eligible employee: joined ~10 months before Aug 2026.
    emp = Employee.objects.create(
        employee_name="CL Eligible", email="cle@demo.com", role="Engineer",
        department="Service", salary=Decimal("30000"), branch="Chennai",
        date_of_joining=datetime.date(2025, 10, 1),
    )
    avail = casual_leave_available(emp, 2026, 8)
    # Qualifying months in 2026 (service >= 6): Apr..Aug = 5
    assert avail == Decimal(5), ("expected 5 CL available, got", avail)
    print(f"2. CL accrual: {avail} days available by Aug 2026 (correct)")

    # Employee with NO date_of_joining -> feature is opt-in, gets 0 (unaffected).
    emp2 = Employee.objects.create(
        employee_name="CL NoDOJ", email="cnd@demo.com", role="Engineer",
        department="Service", salary=Decimal("30000"), branch="Chennai",
    )
    assert casual_leave_available(emp2, 2026, 8) == Decimal(0)
    print("3. Employee without DOJ -> 0 CL (existing employees unaffected)")

    # The user's example: 2 days leave, 1 CL -> 1 paid, 1 deducted.
    d = compute_payslip_fields(emp, total_days=30, lop_days=2, casual_leave_days=1)
    assert d["casual_leave_used"] == Decimal(1), d["casual_leave_used"]
    # effective LOP = 2 - 1 = 1 -> deduction = 30000 * 1/30 = 1000 -> earnings 29000
    assert d["paid_days"] == Decimal(29), ("paid_days", d["paid_days"])
    print(f"4. 2 leave + 1 CL -> paid_days={d['paid_days']}, CL used={d['casual_leave_used']} "
          f"(1 day paid via CL, only 1 day deducted)")

    # Without CL the same 2 days would both be deducted (paid_days 28).
    d0 = compute_payslip_fields(emp, total_days=30, lop_days=2, casual_leave_days=0)
    assert d0["paid_days"] == Decimal(28)
    print(f"5. Same 2 leave, 0 CL -> paid_days={d0['paid_days']} (both deducted) — CL clearly adds 1 paid day")

    cleanup()
    print("\nALL GOOD - casual leave accrual + payslip offset works.")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAILED -", e)
        cleanup()
        raise
