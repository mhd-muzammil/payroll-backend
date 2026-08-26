"""Employee requests: money up front, or a matter to be looked at.

The promise these tests protect: approving a money request records a DECISION
and nothing more. Payroll stays exactly where HR left it.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from employees.models import Employee

from .models import EmployeeRequest, RequestMessage

User = get_user_model()

LIST = "/api/requests/"


def detail(pk, suffix=""):
    return f"{LIST}{pk}/{suffix}"


class EmployeeRequestTests(TestCase):
    def setUp(self):
        self.hr_user = User.objects.create_user(username="hr", password="x", role="hr")
        self.hr = APIClient()
        self.hr.force_authenticate(self.hr_user)

        self.eng_user = User.objects.create_user(username="praveen", password="x", role="employee")
        self.engineer = Employee.objects.create(
            user=self.eng_user,
            employee_name="Praveen S",
            branch="Chennai",
            salary=Decimal("30000"),
            staff_advance=Decimal("0"),
        )
        self.eng = APIClient()
        self.eng.force_authenticate(self.eng_user)

    def _raise(self, client=None, **overrides):
        payload = {
            "request_type": EmployeeRequest.SALARY_ADVANCE,
            "amount": "5000.00",
            "reason": "Medical expense at home",
        }
        payload.update(overrides)
        return (client or self.eng).post(LIST, payload, format="json")

    # -- raising --------------------------------------------------------------

    def test_employee_can_raise_a_salary_advance(self):
        res = self._raise()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["status"], "Pending")
        self.assertEqual(res.data["employee_name"], "Praveen S")
        self.assertEqual(res.data["request_type_label"], "Salary advance")
        self.assertEqual(Decimal(res.data["amount"]), Decimal("5000.00"))

    def test_petrol_and_other_amount_types_work_the_same_way(self):
        for kind in (
            EmployeeRequest.PETROL_ADVANCE,
            EmployeeRequest.EXPENSE,
            EmployeeRequest.OTHER_AMOUNT,
        ):
            res = self._raise(request_type=kind, amount="1200.00", reason="Site visits this week")
            self.assertEqual(res.status_code, 201, res.data)
            self.assertEqual(Decimal(res.data["amount"]), Decimal("1200.00"))

    def test_an_engineer_can_claim_an_expense_they_already_paid_for(self):
        res = self._raise(
            request_type=EmployeeRequest.EXPENSE,
            amount="340.00",
            reason="Bus fare to the Coimbatore site",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["request_type"], "expense")
        self.assertEqual(res.data["request_type_label"], "Expense claim")
        self.assertEqual(Decimal(res.data["amount"]), Decimal("340.00"))
        self.assertEqual(res.data["status"], "Pending")

    def test_an_expense_claim_with_no_figure_is_rejected(self):
        res = self._raise(request_type=EmployeeRequest.EXPENSE, amount=None, reason="Bus fare")
        self.assertEqual(res.status_code, 400)
        self.assertIn("amount", res.data)

    def test_an_expense_carries_the_same_two_way_conversation(self):
        """The point of putting expenses here: the claim and the questions about
        it live in one thread, and each side is told when the other has spoken."""
        claim = self._raise(
            request_type=EmployeeRequest.EXPENSE, amount="340.00", reason="Bus fare"
        ).data["id"]

        # Office asks. The engineer is told; the office has nothing waiting.
        self.hr.post(detail(claim, "messages/"), {"body": "Do you have the ticket?"}, format="json")
        self.assertEqual(self.eng.get(f"{LIST}summary/").data["unread_messages"], 1)
        self.assertEqual(self.hr.get(f"{LIST}summary/").data["unread_messages"], 0)

        # Engineer reads it, which clears their side only, then answers.
        self.eng.get(detail(claim, "messages/"))
        self.assertEqual(self.eng.get(f"{LIST}summary/").data["unread_messages"], 0)
        reply = self.eng.post(
            detail(claim, "messages/"), {"body": "Yes, attaching it"}, format="json"
        )
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertTrue(reply.data["from_employee"])

        # Now it is the office that is told, and both see the whole exchange.
        self.assertEqual(self.hr.get(f"{LIST}summary/").data["unread_messages"], 1)
        for side in (self.eng, self.hr):
            thread = side.get(detail(claim, "messages/")).data
            self.assertEqual(
                [m["body"] for m in thread], ["Do you have the ticket?", "Yes, attaching it"]
            )

    def test_a_money_request_without_an_amount_is_rejected(self):
        res = self._raise(amount=None)
        self.assertEqual(res.status_code, 400)
        self.assertIn("amount", res.data)

    def test_a_zero_or_negative_amount_is_rejected(self):
        for bad in ("0", "-500"):
            res = self._raise(amount=bad)
            self.assertEqual(res.status_code, 400, f"{bad} should be rejected")
            self.assertIn("amount", res.data)

    def test_a_report_needs_no_amount_and_never_carries_one(self):
        res = self._raise(
            request_type=EmployeeRequest.REPORT,
            amount="9999.00",
            reason="Customer at site was abusive, please look into it",
        )
        self.assertEqual(res.status_code, 201, res.data)
        # An amount sent on a report is dropped, so it can never resurface as an
        # approved figure nobody meant to grant.
        self.assertIsNone(res.data["amount"])

    def test_a_request_needs_a_reason(self):
        res = self._raise(reason="   ")
        self.assertEqual(res.status_code, 400)
        self.assertIn("reason", res.data)

    def test_an_employee_cannot_raise_a_request_for_someone_else(self):
        victim = Employee.objects.create(
            user=User.objects.create_user(username="other", password="x", role="employee"),
            employee_name="Someone Else",
            branch="Chennai",
            salary=Decimal("10000"),
        )
        res = self._raise(employee=victim.id)
        self.assertEqual(res.status_code, 201, res.data)
        # employee is read-only: it comes from the caller, not the payload.
        self.assertEqual(EmployeeRequest.objects.get(pk=res.data["id"]).employee_id, self.engineer.id)

    # -- visibility -----------------------------------------------------------

    def test_an_employee_sees_only_their_own_requests(self):
        self._raise()
        other_user = User.objects.create_user(username="vishnu", password="x", role="employee")
        other = Employee.objects.create(
            user=other_user, employee_name="Vishnu", branch="Chennai", salary=Decimal("20000")
        )
        EmployeeRequest.objects.create(
            employee=other, request_type=EmployeeRequest.PETROL_ADVANCE, amount=500, reason="fuel"
        )

        res = self.eng.get(LIST)
        rows = res.data["results"] if "results" in res.data else res.data
        self.assertEqual([r["employee_name"] for r in rows], ["Praveen S"])

    def test_staff_see_requests_from_their_branches(self):
        self._raise()
        res = self.hr.get(LIST)
        rows = res.data["results"] if "results" in res.data else res.data
        self.assertEqual(len(rows), 1)

    def test_branch_scoped_staff_do_not_see_another_branch(self):
        self._raise()
        scoped_user = User.objects.create_user(
            username="vellore-hr",
            password="x",
            role="hr",
            assigned_branch="Vellore",
            allowed_sections={"payroll": ["Vellore"]},
        )
        client = APIClient()
        client.force_authenticate(scoped_user)

        res = client.get(LIST)
        rows = res.data["results"] if "results" in res.data else res.data
        self.assertEqual(rows, [])

    # -- decisions ------------------------------------------------------------

    def test_approving_records_the_decision_and_leaves_payroll_alone(self):
        """The core promise: an approval must never move anyone's pay."""
        request_id = self._raise().data["id"]
        before = Employee.objects.get(pk=self.engineer.pk).staff_advance

        res = self.hr.post(detail(request_id, "approve/"), {"note": "Approved, collect Friday"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], "Approved")
        self.assertEqual(res.data["reviewed_by_name"], "hr")
        self.assertIsNotNone(res.data["reviewed_at"])

        after = Employee.objects.get(pk=self.engineer.pk).staff_advance
        self.assertEqual(after, before, "approving must not touch the payroll deduction")
        self.assertEqual(after, Decimal("0"))

    def test_the_decision_note_lands_in_the_thread(self):
        request_id = self._raise().data["id"]
        self.hr.post(detail(request_id, "approve/"), {"note": "Approved, collect Friday"}, format="json")

        res = self.eng.get(detail(request_id, "messages/"))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]["body"], "Approved, collect Friday")
        self.assertTrue(res.data[0]["is_decision"])
        self.assertFalse(res.data[0]["from_employee"])

    def test_a_decision_without_a_note_still_says_what_happened(self):
        request_id = self._raise().data["id"]
        self.hr.post(detail(request_id, "reject/"), {}, format="json")
        res = self.eng.get(detail(request_id, "messages/"))
        self.assertEqual(res.data[0]["body"], "Rejected.")

    def test_an_employee_cannot_approve_anything(self):
        request_id = self._raise().data["id"]
        res = self.eng.post(detail(request_id, "approve/"), {}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(EmployeeRequest.objects.get(pk=request_id).status, "Pending")

    def test_a_request_cannot_be_decided_twice(self):
        request_id = self._raise().data["id"]
        self.hr.post(detail(request_id, "approve/"), {}, format="json")

        res = self.hr.post(detail(request_id, "reject/"), {}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("already approved", res.data["detail"].lower())
        self.assertEqual(EmployeeRequest.objects.get(pk=request_id).status, "Approved")

    # -- editing / withdrawing -------------------------------------------------

    def test_an_employee_can_fix_a_pending_request(self):
        request_id = self._raise().data["id"]
        res = self.eng.patch(detail(request_id), {"amount": "7000.00"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(Decimal(res.data["amount"]), Decimal("7000.00"))

    def test_an_employee_cannot_change_a_decided_request(self):
        request_id = self._raise().data["id"]
        self.hr.post(detail(request_id, "approve/"), {}, format="json")

        res = self.eng.patch(detail(request_id), {"amount": "99999.00"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            EmployeeRequest.objects.get(pk=request_id).amount, Decimal("5000.00")
        )

    def test_an_employee_can_withdraw_only_while_pending(self):
        request_id = self._raise().data["id"]
        self.assertEqual(self.eng.delete(detail(request_id)).status_code, 204)

        request_id = self._raise().data["id"]
        self.hr.post(detail(request_id, "approve/"), {}, format="json")
        self.assertEqual(self.eng.delete(detail(request_id)).status_code, 400)

    # -- conversation ----------------------------------------------------------

    def test_both_sides_can_talk_on_the_request(self):
        request_id = self._raise().data["id"]

        res = self.hr.post(detail(request_id, "messages/"), {"body": "What is this for?"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertFalse(res.data["from_employee"])

        res = self.eng.post(detail(request_id, "messages/"), {"body": "Hospital bill"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["from_employee"])
        self.assertEqual(res.data["sender_name"], "Praveen S")

        thread = self.eng.get(detail(request_id, "messages/")).data
        self.assertEqual([m["body"] for m in thread], ["What is this for?", "Hospital bill"])

    def test_an_empty_message_is_rejected(self):
        request_id = self._raise().data["id"]
        res = self.eng.post(detail(request_id, "messages/"), {"body": "   "}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_an_employee_cannot_read_someone_elses_thread(self):
        other_user = User.objects.create_user(username="nosy", password="x", role="employee")
        other = Employee.objects.create(
            user=other_user, employee_name="Nosy", branch="Chennai", salary=Decimal("1")
        )
        theirs = EmployeeRequest.objects.create(
            employee=other, request_type=EmployeeRequest.REPORT, reason="private"
        )
        res = self.eng.get(detail(theirs.id, "messages/"))
        # Not in their queryset at all, so it is simply not found.
        self.assertEqual(res.status_code, 404)

    def test_unread_counts_follow_the_side_that_has_not_looked(self):
        request_id = self._raise().data["id"]
        self.hr.post(detail(request_id, "messages/"), {"body": "Why?"}, format="json")

        # The employee has an unread staff message; staff have nothing unread.
        self.assertEqual(self.eng.get(f"{LIST}summary/").data["unread_messages"], 1)
        self.assertEqual(self.hr.get(f"{LIST}summary/").data["unread_messages"], 0)

        # Opening the thread clears it for the reader only.
        self.eng.get(detail(request_id, "messages/"))
        self.assertEqual(self.eng.get(f"{LIST}summary/").data["unread_messages"], 0)

        self.eng.post(detail(request_id, "messages/"), {"body": "Hospital"}, format="json")
        self.assertEqual(self.hr.get(f"{LIST}summary/").data["unread_messages"], 1)
        self.assertEqual(self.eng.get(f"{LIST}summary/").data["unread_messages"], 0)

    def test_summary_counts_the_queue(self):
        approved = self._raise().data["id"]
        self.hr.post(detail(approved, "approve/"), {}, format="json")
        self._raise(reason="another one")

        summary = self.hr.get(f"{LIST}summary/").data
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["approved"], 1)
        self.assertEqual(summary["rejected"], 0)

    def test_a_login_with_no_employee_record_cannot_raise_a_request(self):
        orphan = User.objects.create_user(username="orphan", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(orphan)
        res = client.post(
            LIST,
            {"request_type": EmployeeRequest.REPORT, "reason": "hello"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(EmployeeRequest.objects.count(), 0)

    def test_messages_survive_the_sender_login_being_deleted(self):
        request_id = self._raise().data["id"]
        self.hr.post(detail(request_id, "messages/"), {"body": "Noted"}, format="json")
        self.hr_user.delete()

        thread = self.eng.get(detail(request_id, "messages/")).data
        self.assertEqual(len(thread), 1)
        self.assertEqual(thread[0]["body"], "Noted")
        self.assertEqual(thread[0]["sender_name"], "Office")
        self.assertFalse(thread[0]["from_employee"], "still reads as an office message")
        self.assertEqual(RequestMessage.objects.count(), 1)
