"""The adversarial review of the Employee Pay Report, each finding reproduced.

Every test here is a scenario a reviewer ran against the code and got a wrong
figure out of, with the reviewer's own numbers where they gave them:

  * August generated at 30,000; a raise to 36,000; then only August's TDS
    changed by 100 -- and August's net went UP by 5,900;
  * a Paid-days edit dropped the casual leave and the next TDS edit paid it
    again: a 100 deduction raised the net by 900;
  * Bulk Generate rewrote a Paid slip and erased what had been paid;
  * Total 30.5 was priced over 30.5 days and stored as 30;
  * any login could write a branch's P&L.

They must all come out right now.
"""
import datetime
from decimal import Decimal

from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

from .models import BranchFinancial, Payslip

STRUCTURE = dict(
    salary=Decimal("30000"), basic=Decimal("15000"), hra=Decimal("7500"),
    conveyance=Decimal("1600"), child_edu=Decimal("200"), personal_allowance=Decimal("4700"),
    incentive=Decimal("1000"), other_earnings=Decimal("0"), epf=Decimal("1800"),
    esi=Decimal("0"), prof_tax=Decimal("208.30"), staff_advance=Decimal("500"),
    tds=Decimal("0"), other_deduction=Decimal("0"), deduction_insurance=Decimal("0"),
)


def _employee(**extra):
    fields = {
        "employee_name": "Praveen S", "email": "praveen@example.com", "phone": "9000000201",
        "department": "Service", "role": "Engineer", "branch": "Chennai", "status": "active",
        **STRUCTURE,
    }
    fields.update(extra)
    return Employee.objects.create(**fields)


class Office(APITestCase):
    def setUp(self):
        self.hr = User.objects.create_user(username="hr_review2", password="x", role="hr")
        self.client.force_authenticate(self.hr)

    def generate(self, emp, month=9, year=2026):
        response = self.client.post(
            "/api/payslips/generate_all/", {"month": month, "year": year, "employee_id": emp.id},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return Payslip.objects.get(employee=emp, month=month, year=year)

    def recalc(self, slip, **body):
        return self.client.post(f"/api/payslips/{slip.id}/recalculate/", body, format="json")


class AnOldMonthKeepsItsOwnSalary(Office):
    def test_a_raise_does_not_re_price_august_when_its_tds_is_corrected(self):
        emp = _employee()
        august = self.generate(emp, month=8)
        self.assertEqual(august.net_salary, Decimal("27491.70"))

        # HR gives a raise for the months after August.
        emp.salary = Decimal("36000")
        emp.personal_allowance = Decimal("10700")
        emp.save()

        response = self.recalc(august, tds="100")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["gross_salary"], "30000.00", "August is still priced at 30,000")
        self.assertEqual(response.data["net_salary"], "27391.70", "and its net moved by exactly -100")

    def test_a_day_edit_on_august_keeps_august_s_salary_too(self):
        emp = _employee()
        august = self.generate(emp, month=8)
        emp.salary = Decimal("36000")
        emp.personal_allowance = Decimal("10700")
        emp.save()
        response = self.recalc(august, total_days=31, lop_days="0")
        self.assertEqual(response.data["gross_salary"], "30000.00")

    def test_undo_edits_goes_back_to_august_as_it_was_generated(self):
        emp = _employee()
        august = self.generate(emp, month=8)
        self.recalc(august, tds="100")
        emp.salary = Decimal("36000")
        emp.personal_allowance = Decimal("10700")
        emp.save()
        response = self.client.post(f"/api/payslips/{august.id}/revert/", {}, format="json")
        self.assertEqual(response.data["gross_salary"], "30000.00")

    def test_regenerate_is_how_a_new_salary_reaches_an_old_month(self):
        emp = _employee()
        august = self.generate(emp, month=8)
        emp.salary = Decimal("36000")
        emp.personal_allowance = Decimal("10700")
        emp.save()
        august = self.generate(emp, month=8)
        self.assertEqual(august.gross_salary, Decimal("36000.00"))

    def test_a_slip_from_before_snapshots_is_refused_once_the_salary_has_moved(self):
        emp = _employee()
        august = self.generate(emp, month=8)
        # As every slip generated before this change looks: no snapshot.
        Payslip.objects.filter(id=august.id).update(structure={})
        emp.salary = Decimal("36000")
        emp.personal_allowance = Decimal("10700")
        emp.save()
        response = self.recalc(august, tds="100")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Regenerate", response.data["error"])
        august.refresh_from_db()
        self.assertEqual(august.net_salary, Decimal("27491.70"), "and nothing on it moved")

    def test_a_slip_from_before_snapshots_still_edits_while_the_salary_is_unchanged(self):
        emp = _employee()
        august = self.generate(emp, month=8)
        Payslip.objects.filter(id=august.id).update(structure={})
        response = self.recalc(august, tds="100")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["net_salary"], "27391.70")
        august.refresh_from_db()
        self.assertTrue(august.structure, "and keeps its structure from now on")


class CasualLeaveDoesNotComeBack(Office):
    def _eligible(self):
        # Joined well over six months before September 2026.
        return _employee(date_of_joining=datetime.date(2024, 1, 10))

    def test_a_tds_edit_after_a_paid_days_edit_moves_the_net_by_the_tds(self):
        slip = self.generate(self._eligible())
        after_paid = self.recalc(slip, total_days=31, paid_days="28")
        self.assertEqual(after_paid.status_code, 200, after_paid.data)
        self.assertEqual(Decimal(after_paid.data["casual_leave_used"]), Decimal("0"))
        net_before = Decimal(after_paid.data["net_salary"])

        after_tds = self.recalc(slip, tds="100")
        self.assertEqual(Decimal(after_tds.data["casual_leave_used"]), Decimal("0"),
                         "the leave the Paid figure dropped stays dropped")
        self.assertEqual(net_before - Decimal(after_tds.data["net_salary"]), Decimal("100.00"))

    def test_special_work_and_total_edits_do_not_bring_it_back_either(self):
        slip = self.generate(self._eligible())
        self.recalc(slip, total_days=31, paid_days="28")
        # The sheet sends the row's CURRENT LOP with these edits -- 3, after the
        # Paid edit. Read it back first: sending the stale pre-edit figure
        # would CHANGE the LOP and test a different path entirely.
        slip.refresh_from_db()
        self.assertEqual(slip.lop_days, Decimal("3.00"))
        r1 = self.recalc(slip, total_days=31, lop_days=str(slip.lop_days), special_work_days="1")
        r2 = self.recalc(slip, total_days=31, lop_days=str(slip.lop_days))
        self.assertEqual(Decimal(r1.data["casual_leave_used"]), Decimal("0"))
        self.assertEqual(Decimal(r2.data["casual_leave_used"]), Decimal("0"))

    def test_typing_a_new_lop_still_applies_earned_leave(self):
        """The case the re-derivation exists for: HR typing the real LOP in."""
        slip = self.generate(self._eligible())
        response = self.recalc(slip, total_days=31, lop_days="3")
        self.assertEqual(Decimal(response.data["casual_leave_used"]), Decimal("1"))


class GenerateLeavesPaidSlipsAlone(Office):
    def test_bulk_generate_does_not_rewrite_a_paid_slip(self):
        emp = _employee()
        slip = self.generate(emp)
        self.recalc(slip, incentive="5000")
        self.client.patch(f"/api/payslips/{slip.id}/", {"status": "Paid"}, format="json")
        slip.refresh_from_db()
        paid_net = slip.net_salary

        response = self.client.post("/api/payslips/generate_all/", {"month": 9, "year": 2026}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["skipped_paid"], 1)
        self.assertIn("already marked Paid", response.data["message"])

        slip.refresh_from_db()
        self.assertEqual(slip.status, "Paid")
        self.assertEqual(slip.net_salary, paid_net, "what was paid is still what it says")
        self.assertEqual(slip.manual_overrides, {"incentive": "5000.00"})

    def test_regenerating_one_paid_slip_is_refused(self):
        emp = _employee()
        slip = self.generate(emp)
        self.client.patch(f"/api/payslips/{slip.id}/", {"status": "Paid"}, format="json")
        response = self.client.post(
            "/api/payslips/generate_all/", {"month": 9, "year": 2026, "employee_id": emp.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_an_unpaid_slip_is_still_regenerated(self):
        emp = _employee()
        slip = self.generate(emp)
        self.recalc(slip, incentive="5000")
        slip = self.generate(emp)
        self.assertEqual(slip.manual_overrides, {})


class DaysAreStoredAsTheyWerePriced(Office):
    def test_half_a_day_of_cycle_is_refused(self):
        slip = self.generate(_employee())
        response = self.recalc(slip, total_days="30.5", lop_days="2")
        self.assertEqual(response.status_code, 400)

    def test_a_three_place_lop_is_priced_at_the_two_places_it_is_stored_at(self):
        slip = self.generate(_employee())
        first = self.recalc(slip, total_days=31, lop_days="2.555")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["lop_days"], "2.56")
        second = self.recalc(slip, tds="100")
        self.assertEqual(
            Decimal(first.data["net_salary"]) - Decimal(second.data["net_salary"]), Decimal("100.00"),
            "the next unrelated edit moves the net by itself and nothing else",
        )


class BranchFinancialsAreTheOffice_s(APITestCase):
    def test_an_employee_cannot_write_a_branch_s_p_and_l(self):
        worker = User.objects.create_user(username="worker_pnl", password="x", role="employee")
        self.client.force_authenticate(worker)
        response = self.client.post(
            "/api/branch-financials/",
            {"branch": "Chennai", "month": 9, "year": 2026, "revenue": 1, "other_expenses": 999999},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BranchFinancial.objects.exists())

    def test_the_office_still_can(self):
        hr = User.objects.create_user(username="hr_pnl", password="x", role="hr")
        self.client.force_authenticate(hr)
        response = self.client.get("/api/branch-financials/")
        self.assertEqual(response.status_code, 200)
