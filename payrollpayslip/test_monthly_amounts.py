"""This month's amounts, set on one slip, and who may set them.

The Employee Pay Report lets the office change, on one month's slip, the
amounts that genuinely move month to month. What has to hold:

  * the change reaches the net, and the totals still add up -- an incentive
    typed in and not paid is the failure to fear, because the personal
    allowance is a balancing figure and would quietly absorb it;
  * editing a day count afterwards keeps it -- Other Deduction used to vanish
    exactly that way;
  * Undo Edits and Regenerate clear it, like every other manual edit;
  * and an employee cannot do any of it, nor generate, revert, mark Paid or
    delete, which until now only the hidden buttons prevented.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

from .models import Payslip
from .views import compute_payslip_fields


def _employee(**extra):
    fields = {
        "employee_name": "Praveen S", "email": "praveen@example.com", "phone": "9000000101",
        "department": "Service", "role": "Engineer", "branch": "Chennai", "status": "active",
        "salary": Decimal("30000"), "basic": Decimal("15000"), "hra": Decimal("7500"),
        "conveyance": Decimal("1600"), "child_edu": Decimal("200"),
        "personal_allowance": Decimal("4700"), "incentive": Decimal("1000"),
        "other_earnings": Decimal("0"), "epf": Decimal("1800"), "esi": Decimal("0"),
        "prof_tax": Decimal("208.30"), "lwf": Decimal("0"), "staff_advance": Decimal("500"),
        "tds": Decimal("0"), "other_deduction": Decimal("0"), "deduction_insurance": Decimal("0"),
    }
    fields.update(extra)
    return Employee.objects.create(**fields)


def _sum_earned(slip):
    return sum(Decimal(str(slip[f])) for f in (
        "earned_basic", "earned_hra", "earned_conveyance", "earned_child_edu",
        "earned_personal_allowance", "earned_incentive", "earned_other_earnings",
    ))


def _sum_deductions(slip):
    return sum(Decimal(str(slip[f])) for f in (
        "deduction_epf", "deduction_esi", "deduction_prof_tax", "deduction_lwf",
        "deduction_staff_advance", "deduction_tds", "deduction_other", "deduction_insurance",
    ))


class TheEngine(APITestCase):
    def test_an_incentive_reaches_the_net_and_the_totals_still_add_up(self):
        emp = _employee()
        before = compute_payslip_fields(emp, 30, 0)
        after = compute_payslip_fields(emp, 30, 0, overrides={"incentive": "2500"})
        self.assertEqual(after["earned_incentive"], Decimal("2500.00"))
        self.assertEqual(after["net_salary"] - before["net_salary"], Decimal("1500.00"),
                         "2,500 set where 1,000 was: net up by exactly 1,500")
        self.assertEqual(_sum_earned(after), after["gross_earnings"],
                         "and the components still add up to the total")
        self.assertEqual(after["earned_personal_allowance"], before["earned_personal_allowance"],
                         "the balancing allowance did not swallow it")

    def test_an_incentive_on_a_month_with_absence_is_paid_in_full(self):
        emp = _employee()
        after = compute_payslip_fields(emp, 30, 6, overrides={"incentive": "2000"})
        self.assertEqual(after["earned_incentive"], Decimal("2000.00"))
        self.assertEqual(_sum_earned(after), after["gross_earnings"])

    def test_a_deduction_replaces_its_line_and_the_net_follows(self):
        emp = _employee()
        before = compute_payslip_fields(emp, 30, 0)
        after = compute_payslip_fields(emp, 30, 0, overrides={"staff_advance": "2000", "tds": "300"})
        self.assertEqual(after["deduction_staff_advance"], Decimal("2000.00"))
        self.assertEqual(after["deduction_tds"], Decimal("300.00"))
        self.assertEqual(_sum_deductions(after), after["gross_deductions"])
        self.assertEqual(before["net_salary"] - after["net_salary"], Decimal("1800.00"))

    def test_the_net_is_always_earnings_plus_extras_less_deductions(self):
        emp = _employee()
        slip = compute_payslip_fields(
            emp, 30, 3, casual_leave_days=1, special_work_days=2,
            overrides={"incentive": "1200", "other_earnings": "800", "insurance": "450",
                       "other_deduction": "100"},
        )
        expected = (slip["gross_earnings"] + slip["casual_leave_pay"] + slip["special_work_pay"]
                    - slip["gross_deductions"])
        self.assertEqual(slip["net_salary"], expected.quantize(Decimal("0.01")))

    def test_nothing_set_is_exactly_what_it_was(self):
        emp = _employee()
        self.assertEqual(compute_payslip_fields(emp, 30, 2),
                         compute_payslip_fields(emp, 30, 2, overrides={}))


class TheReportsEdits(APITestCase):
    def setUp(self):
        self.hr = User.objects.create_user(username="hr_pay", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        self.emp = _employee()
        self.client.post("/api/payslips/generate_all/",
                         {"month": 9, "year": 2026, "employee_id": self.emp.id}, format="json")
        self.slip = Payslip.objects.get(employee=self.emp, month=9, year=2026)
        self.url = f"/api/payslips/{self.slip.id}/recalculate/"

    def test_setting_an_incentive_lands_on_the_slip(self):
        response = self.client.post(self.url, {"incentive": "2500"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["earned_incentive"], "2500.00")
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.manual_overrides, {"incentive": "2500.00"})

    def test_editing_the_days_afterwards_keeps_this_month_s_amounts(self):
        self.client.post(self.url, {"incentive": "2500", "other_deduction": "400"}, format="json")
        response = self.client.post(self.url, {"lop_days": "2"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["earned_incentive"], "2500.00")
        self.assertEqual(response.data["deduction_other"], "400.00",
                         "the Other Deduction that used to vanish on the next edit")
        self.assertEqual(Decimal(response.data["lop_days"]), Decimal("2"))

    def test_a_negative_amount_is_refused(self):
        response = self.client.post(self.url, {"tds": "-5"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_undo_edits_clears_them(self):
        self.client.post(self.url, {"incentive": "2500"}, format="json")
        response = self.client.post(f"/api/payslips/{self.slip.id}/revert/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["earned_incentive"], "1000.00")
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.manual_overrides, {})

    def test_regenerate_starts_over(self):
        self.client.post(self.url, {"incentive": "2500"}, format="json")
        self.client.post("/api/payslips/generate_all/",
                         {"month": 9, "year": 2026, "employee_id": self.emp.id}, format="json")
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.manual_overrides, {})
        self.assertEqual(self.slip.earned_incentive, Decimal("1000.00"))

    def test_a_paid_slip_stays_locked(self):
        self.client.patch(f"/api/payslips/{self.slip.id}/", {"status": "Paid"}, format="json")
        response = self.client.post(self.url, {"incentive": "9999"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_the_override_cannot_be_written_directly(self):
        self.client.patch(f"/api/payslips/{self.slip.id}/",
                          {"manual_overrides": {"incentive": "99999"}}, format="json")
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.manual_overrides, {})


class OnlyTheOfficeChangesASlip(APITestCase):
    """An employee with their own token, against their own released slip."""

    def setUp(self):
        hr = User.objects.create_user(username="hr_lock", password="x", role="hr")
        self.client.force_authenticate(hr)
        self.worker = User.objects.create_user(username="worker_lock", password="x", role="employee")
        self.emp = _employee(user=self.worker)
        self.client.post("/api/payslips/generate_all/",
                         {"month": 9, "year": 2026, "employee_id": self.emp.id}, format="json")
        self.slip = Payslip.objects.get(employee=self.emp, month=9, year=2026)
        self.client.post(f"/api/payslips/{self.slip.id}/send/", {}, format="json")
        self.net = self.slip.net_salary
        self.client.force_authenticate(self.worker)

    def _unchanged(self):
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.net_salary, self.net)
        self.assertEqual(self.slip.status, "Generated")

    def test_cannot_pay_themselves_special_work(self):
        response = self.client.post(f"/api/payslips/{self.slip.id}/recalculate/",
                                    {"special_work_days": "10"}, format="json")
        self.assertEqual(response.status_code, 403)
        self._unchanged()

    def test_cannot_set_their_own_incentive(self):
        response = self.client.post(f"/api/payslips/{self.slip.id}/recalculate/",
                                    {"incentive": "50000"}, format="json")
        self.assertEqual(response.status_code, 403)
        self._unchanged()

    def test_cannot_mark_it_paid(self):
        response = self.client.patch(f"/api/payslips/{self.slip.id}/", {"status": "Paid"}, format="json")
        self.assertEqual(response.status_code, 403)
        self._unchanged()

    def test_cannot_delete_it(self):
        response = self.client.delete(f"/api/payslips/{self.slip.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Payslip.objects.filter(id=self.slip.id).exists())

    def test_cannot_revert_or_email_it(self):
        self.assertEqual(self.client.post(f"/api/payslips/{self.slip.id}/revert/").status_code, 403)
        self.assertEqual(self.client.post(f"/api/payslips/{self.slip.id}/email_payslip/").status_code, 403)

    def test_cannot_regenerate_the_company(self):
        response = self.client.post("/api/payslips/generate_all/", {"month": 9, "year": 2026}, format="json")
        self.assertEqual(response.status_code, 403)
        self._unchanged()

    def test_can_still_see_and_download_their_own(self):
        listed = self.client.get("/api/payslips/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row["id"] for row in listed.data], [self.slip.id])
        ticket = self.client.get(f"/api/payslips/{self.slip.id}/pdf_ticket/")
        self.assertEqual(ticket.status_code, 200, "their only way to keep a copy, in the app")
