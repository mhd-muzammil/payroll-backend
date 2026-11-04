"""Somebody relieved still has the months they worked to be paid.

The Payroll roster and the Generate tab list the staff, and a relieved person
is not staff, so a month they worked before leaving had no row to generate
from unless its slip already existed: Samim K worked in October's cycle, was
relieved before October was generated, and October had nobody to generate.
relieved_worked gives back everybody relieved with a worked day in a month's
cycle, for both lists to add. Generating them is one at a time; Bulk Generate
stays with the staff.
"""
import datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee

from .models import Payslip

URL = "/api/payslips/relieved_worked/"


def _at(day, hour=9, minute=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


def _person(name, status="relieved", branch="Chennai"):
    return Employee.objects.create(
        employee_name=name, email=f"{name.replace(' ', '.').lower()}@example.com",
        role="Engineer", department="Service", branch=branch,
        salary=Decimal("30000"), date_of_joining=datetime.date(2026, 1, 1), status=status,
    )


def _row(emp, when, status="Present"):
    return Attendance.objects.create(
        employee=emp, employee_name=emp.employee_name, role=emp.role,
        department=emp.department, salary=emp.salary, status=status, intime=when,
    )


class RelievedWorkedTests(APITestCase):
    def setUp(self):
        self.office = User.objects.create_user(username="rw-office", password="x", role="hr")
        self.client.force_authenticate(self.office)

    def _names(self, month=10, year=2026, user=None):
        if user is not None:
            self.client.force_authenticate(user)
        response = self.client.get(URL, {"month": month, "year": year})
        self.assertEqual(response.status_code, 200, response.data)
        return [row["employee_name"] for row in response.data]

    def test_a_relieved_person_who_worked_in_the_cycle_is_listed(self):
        samim = _person("Samim Like")
        _row(samim, _at(datetime.date(2026, 10, 6)))
        rows = self.client.get(URL, {"month": 10, "year": 2026}).data
        self.assertEqual([r["employee_name"] for r in rows], ["Samim Like"])
        # Shaped like the staff rows the lists already show, status included.
        self.assertEqual(rows[0]["status"], "relieved")
        self.assertEqual(rows[0]["id"], samim.id)

    def test_only_absent_and_sunday_rows_is_not_a_month_they_worked(self):
        away = _person("Never Came")
        _row(away, _at(datetime.date(2026, 10, 5), 0, 0), status="Absent")
        _row(away, _at(datetime.date(2026, 10, 4), 0, 0), status="Leave")
        self.assertEqual(self._names(), [])

    def test_every_worked_status_counts(self):
        for n, status in enumerate(("Late", "Overtime", "overTime", "Half Day")):
            _row(_person(f"Worked {n}"), _at(datetime.date(2026, 10, 7)), status=status)
        self.assertEqual(len(self._names()), 4)

    def test_the_cycle_is_the_25th_before_to_this_months_24th(self):
        first = _person("First Day")
        _row(first, _at(datetime.date(2026, 9, 25), 0, 30))
        last = _person("Last Day")
        _row(last, _at(datetime.date(2026, 10, 24), 23, 30))
        after = _person("Next Cycle")
        _row(after, _at(datetime.date(2026, 10, 25), 9, 0))
        before = _person("Last Cycle")
        _row(before, _at(datetime.date(2026, 9, 24), 18, 0))
        self.assertEqual(sorted(self._names()), ["First Day", "Last Day"])
        self.assertEqual(self._names(month=11), ["Next Cycle"])
        self.assertEqual(self._names(month=9), ["Last Cycle"])

    def test_january_reaches_back_into_december(self):
        _row(_person("New Year"), _at(datetime.date(2025, 12, 28)))
        self.assertEqual(self._names(month=1, year=2026), ["New Year"])

    def test_the_staff_are_not_in_it(self):
        # They are on the staff list already; only the relieved are added.
        _row(_person("Still Here", status="active"), _at(datetime.date(2026, 10, 6)))
        _row(_person("Off Roster", status="inactive"), _at(datetime.date(2026, 10, 6)))
        _row(_person("Away Now", status="onleave"), _at(datetime.date(2026, 10, 6)))
        self.assertEqual(self._names(), [])

    def test_a_month_before_they_joined_is_left_out(self):
        # A cycle that ended before the joining date is not theirs: Generate
        # would refuse it, so the list does not offer it.
        early = _person("Early Rows")
        early.date_of_joining = datetime.date(2026, 11, 1)
        early.save()
        _row(early, _at(datetime.date(2026, 10, 6)))
        self.assertEqual(self._names(), [])

    def test_an_office_without_the_payslips_section_sees_nobody(self):
        _row(_person("Somebody"), _at(datetime.date(2026, 10, 6)))
        no_payslips = User.objects.create_user(
            username="rw-nopayslips", password="x", role="hr",
            allowed_sections={"payroll": ["All"]},
        )
        self.assertEqual(self._names(user=no_payslips), [])

    def test_a_branch_scoped_office_sees_its_own_branch(self):
        _row(_person("Salem Worker", branch="Salem"), _at(datetime.date(2026, 10, 6)))
        _row(_person("Chennai Worker"), _at(datetime.date(2026, 10, 6)))
        salem_hr = User.objects.create_user(
            username="rw-salem", password="x", role="hr",
            allowed_sections={"payslips": ["Salem"]},
        )
        self.assertEqual(self._names(user=salem_hr), ["Salem Worker"])

    def test_an_employee_may_not_ask(self):
        _row(_person("Somebody"), _at(datetime.date(2026, 10, 6)))
        engineer = User.objects.create_user(username="rw-engineer", password="x", role="employee")
        self.client.force_authenticate(engineer)
        self.assertEqual(self.client.get(URL, {"month": 10, "year": 2026}).status_code, 403)

    def test_a_month_and_year_are_required(self):
        for params in (
            {}, {"month": 10}, {"month": 13, "year": 2026}, {"month": "x", "year": 2026},
            {"month": 0, "year": 2026}, {"month": 10, "year": 10 ** 30},
        ):
            self.assertEqual(self.client.get(URL, params).status_code, 400, params)


class GeneratingTheirMonthTests(APITestCase):
    """What the rows are for: the office generates a relieved person's month
    from their own row. Bulk Generate is unchanged and still leaves them out."""

    def setUp(self):
        self.office = User.objects.create_user(username="rw-gen", password="x", role="hr")
        self.client.force_authenticate(self.office)
        self.samim = _person("Samim Like")
        for day in (6, 7, 8):
            _row(self.samim, _at(datetime.date(2026, 10, day)))
        _row(self.samim, _at(datetime.date(2026, 10, 9), 0, 0), status="Absent")

    def test_their_own_row_generates_the_month(self):
        response = self.client.post(
            "/api/payslips/generate_all/",
            {"month": 10, "year": 2026, "employee_id": self.samim.id}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        slip = Payslip.objects.get(employee=self.samim, month=10, year=2026)
        self.assertEqual(slip.lop_days, Decimal("1"))

    def test_bulk_generate_still_leaves_them_out(self):
        staff = _person("Staff Member", status="active")
        response = self.client.post(
            "/api/payslips/generate_all/", {"month": 10, "year": 2026}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(Payslip.objects.filter(employee=staff, month=10, year=2026).exists())
        self.assertFalse(Payslip.objects.filter(employee=self.samim, month=10, year=2026).exists())

    def test_a_month_they_did_not_work_is_refused(self):
        # November: nothing of theirs is marked, so it would be full pay for a
        # month after they left. Whatever a screen shows, the server says no.
        response = self.client.post(
            "/api/payslips/generate_all/",
            {"month": 11, "year": 2026, "employee_id": self.samim.id}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("no worked day", response.data["error"])
        self.assertFalse(Payslip.objects.filter(employee=self.samim, month=11, year=2026).exists())

    def test_a_slip_they_already_have_can_still_be_regenerated(self):
        # Made while they were staff, for a month with nothing marked on it.
        from .views import compute_payslip_fields

        Payslip.objects.create(
            employee=self.samim, month=8, year=2026, status="Generated",
            **compute_payslip_fields(self.samim, 31, 0),
        )
        response = self.client.post(
            "/api/payslips/generate_all/",
            {"month": 8, "year": 2026, "employee_id": self.samim.id}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)


class GenerateAllCountIsStaffOnlyTests(APITestCase):
    """company_stats' notGeneratedCount drives the Payroll page's "Generate
    All Slips (N)" button, which makes the staff's slips. A relieved person's
    slip, generated from their own row, must not take one off it."""

    def setUp(self):
        self.office = User.objects.create_user(username="rw-stats", password="x", role="hr")
        self.client.force_authenticate(self.office)

    def test_a_relieved_slip_does_not_hide_a_missing_staff_slip(self):
        _person("Staff Without Slip", status="active")
        gone = _person("Relieved With Slip")
        _row(gone, _at(datetime.date(2026, 10, 6)))
        response = self.client.post(
            "/api/payslips/generate_all/",
            {"month": 10, "year": 2026, "employee_id": gone.id}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        stats = self.client.get("/api/payslips/company_stats/?month=10&year=2026").data
        self.assertEqual(stats["slipsCount"], 1)
        self.assertEqual(stats["notGeneratedCount"], 1)
