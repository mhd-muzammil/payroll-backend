"""The read-only report on what the onboarding signal reached too early.

What matters about it is that it tells a junk row the signal made from a real
employee the signal may have written over -- and that it changes nothing while
saying so.
"""
import datetime
from io import StringIO

from django.core.management import call_command
from django.db.models.signals import post_save
from django.test import TestCase

from attendance.models import Attendance
from employees.models import Employee

from .models import Onboarding, sync_onboarding_to_employee


def _run():
    out = StringIO()
    call_command("onboarding_employee_leaks", stdout=out)
    return out.getvalue()


class LeakReportTests(TestCase):
    def setUp(self):
        # The report is about what the OLD, ungated signal left behind, so the
        # rows are planted with it switched off and the damage made by hand.
        post_save.disconnect(sync_onboarding_to_employee, sender=Onboarding)

    def tearDown(self):
        post_save.connect(sync_onboarding_to_employee, sender=Onboarding)

    def test_nothing_to_report_says_so(self):
        self.assertIn("Nothing to check", _run())

    def test_a_row_the_signal_made_for_a_vendor_reads_created(self):
        Onboarding.objects.create(
            employee_name="Vendor Contact", email_id="vendor@example.com",
            mobile_number="9100000001", work_location="Salem", category="Vendor",
        )
        Employee.objects.create(
            employee_name="Vendor Contact", email="vendor@example.com", phone="9100000001",
            department="General", role="Staff", branch="Salem", salary=0,
        )
        output = _run()
        self.assertIn("CREATED", output)
        self.assertNotIn("EXISTING ", output.split("record(s) checked")[0])

    def test_a_real_employee_the_link_reached_reads_existing(self):
        engineer = Employee.objects.create(
            employee_name="Sarwana N", email="sarwana@example.com", phone="9222222222",
            department="Service", role="Engineer", branch="Vellore", salary=30000,
        )
        Attendance.objects.create(
            employee=engineer, employee_name="Sarwana N", role="Engineer",
            department="Service", salary=30000, status="Present",
            intime=datetime.datetime(2026, 9, 1, 9, 0, tzinfo=datetime.timezone.utc),
        )
        Onboarding.objects.create(
            employee_name="Stranger", email_id="sarwana@example.com", mobile_number="9333333333",
            work_location="Chennai", category="Employee", status="Pending Review", source="Self",
        )
        output = _run()
        self.assertIn("EXISTING", output)
        self.assertIn("Sarwana N", output)
        self.assertIn("Check every EXISTING one by hand", output)

    def test_it_changes_nothing(self):
        Onboarding.objects.create(
            employee_name="Vendor Contact", email_id="vendor@example.com",
            mobile_number="9100000001", work_location="Salem", category="Vendor",
        )
        employee = Employee.objects.create(
            employee_name="Vendor Contact", email="vendor@example.com", phone="9100000001",
            department="General", role="Staff", branch="Salem", salary=0,
        )
        before = (Employee.objects.count(), Onboarding.objects.count())
        _run()
        self.assertEqual((Employee.objects.count(), Onboarding.objects.count()), before)
        employee.refresh_from_db()
        self.assertEqual(employee.employee_name, "Vendor Contact")
