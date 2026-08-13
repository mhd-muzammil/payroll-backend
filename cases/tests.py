"""Tests for the OpenCall -> Payroll case sync.

The scenario that matters in production: OpenCall re-syncs TODAY's "Assigned"
set every few minutes. A service call stays assigned for several days until it
is closed, so the same ticket is re-sent day after day. The engineer must keep
seeing it every one of those days, and their own field progress must survive
the next sync tick.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from employees.models import Employee

from .models import Case, DutySession, EngineerAlias, LocationPing

User = get_user_model()


class CaseSyncTests(TestCase):
    def setUp(self):
        self.bot = User.objects.create_user(
            username="opencall-bot", password="x", role="admin", is_staff=True
        )
        self.engineer_user = User.objects.create_user(
            username="praveen", password="x", role="employee"
        )
        self.engineer = Employee.objects.create(
            user=self.engineer_user,
            employee_name="Praveen S",
            branch="Chennai",
            salary=0,
        )

        self.bot_client = APIClient()
        self.bot_client.force_authenticate(self.bot)
        self.engineer_client = APIClient()
        self.engineer_client.force_authenticate(self.engineer_user)

    def _sync(self, tickets, **extra):
        """One OpenCall auto-sync tick carrying the current Assigned set."""
        body = {
            "cases": [
                {
                    "external_ref": ticket,
                    "title": f"Service call ({ticket})",
                    "engineer_name": "Praveen",
                    "status": "assigned",
                }
                for ticket in tickets
            ]
        }
        body.update(extra)
        res = self.bot_client.post("/api/cases/bulk_dispatch/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def _engineer_cases(self):
        res = self.engineer_client.get("/api/cases/")
        self.assertEqual(res.status_code, 200, res.data)
        data = res.data
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def _age_by_days(self, days):
        """Pretend every existing case was dispatched `days` days ago."""
        for case in Case.objects.all():
            case.assigned_at = timezone.now() - timedelta(days=days)
            case.save(update_fields=["assigned_at"])

    # -- the day-2 regression -------------------------------------------------

    def test_engineer_sees_case_still_assigned_from_an_earlier_day(self):
        """A ticket first synced yesterday and STILL in today's Assigned set
        must show in the engineer's list today."""
        self._sync(["TKT-1"])
        self.assertEqual(len(self._engineer_cases()), 1, "day 1 should show the case")

        # Next day: the call is not closed yet, so OpenCall sends it again.
        self._age_by_days(1)
        self._sync(["TKT-1"])

        cases = self._engineer_cases()
        self.assertEqual(len(cases), 1, "day 2 should still show the case")
        self.assertEqual(cases[0]["external_ref"], "TKT-1")

    def test_engineer_field_progress_survives_the_next_sync_tick(self):
        """Auto-sync runs every 5 min; it must not reset an engineer who has
        already started travelling back to 'assigned'."""
        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")

        res = self.engineer_client.post(f"/api/cases/{case.id}/accept/")
        self.assertEqual(res.status_code, 200, res.data)
        res = self.engineer_client.post(f"/api/cases/{case.id}/start_travel/")
        self.assertEqual(res.status_code, 200, res.data)

        self._sync(["TKT-1"])
        case.refresh_from_db()
        self.assertEqual(case.status, "on_the_way")

    def test_a_completed_case_stays_listed_while_it_is_still_in_the_plan(self):
        """Membership is the plan, not the status. Hiding a completed case made
        the engineer's count smaller than OpenCall's Assigned column."""
        self._sync(["TKT-1", "TKT-2"])
        case = Case.objects.get(external_ref="TKT-1")
        case.status = "completed"
        case.save(update_fields=["status"])

        rows = {c["external_ref"]: c["status"] for c in self._engineer_cases()}
        self.assertEqual(set(rows), {"TKT-1", "TKT-2"}, "both still assigned upstream")
        self.assertEqual(rows["TKT-1"], "completed", "shown as finished, not hidden")

    def test_ticket_dropped_from_the_assigned_set_disappears(self):
        """Mirror mode: a ticket no longer in the Assigned set is cancelled
        (not deleted) so the engineer's list matches the productivity view."""
        self._sync(["TKT-1", "TKT-2"])
        self.assertEqual(len(self._engineer_cases()), 2)

        self._sync(["TKT-2"])
        refs = {c["external_ref"] for c in self._engineer_cases()}
        self.assertEqual(refs, {"TKT-2"})
        self.assertEqual(Case.objects.get(external_ref="TKT-1").status, "cancelled")
        self.assertEqual(Case.objects.count(), 2, "cancelled, never deleted")

    def test_a_cancelled_ticket_comes_back_if_it_is_reassigned(self):
        """Mirror cancels on a sync where the ticket is absent; if OpenCall
        assigns it again the engineer must see it again."""
        self._sync(["TKT-1"])
        self._sync(["TKT-2"])
        self.assertEqual(Case.objects.get(external_ref="TKT-1").status, "cancelled")

        self._sync(["TKT-1", "TKT-2"])
        refs = {c["external_ref"] for c in self._engineer_cases()}
        self.assertEqual(refs, {"TKT-1", "TKT-2"})

    def test_unmatched_engineer_is_skipped_not_orphaned(self):
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-9",
                        "title": "Service call (TKT-9)",
                        "engineer_name": "Nobody At All",
                        "status": "assigned",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["skipped"], 1)
        self.assertFalse(Case.objects.filter(external_ref="TKT-9").exists())

    def test_completed_case_is_not_resurrected_by_the_next_sync(self):
        """The engineer finishes the job before OpenCall's row is closed. The
        next tick still says "assigned" — that must not undo the completion."""
        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        for step in ("accept", "start_travel", "reached", "start_work", "complete"):
            res = self.engineer_client.post(f"/api/cases/{case.id}/{step}/")
            self.assertEqual(res.status_code, 200, f"{step}: {res.data}")

        self._sync(["TKT-1"])
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")
        self.assertIsNotNone(case.completed_at)

    def test_mirror_does_not_cancel_a_completed_case(self):
        """Once the call is closed upstream it leaves the Assigned set. The
        engineer's completed record must survive the mirror sweep."""
        self._sync(["TKT-1", "TKT-2"])
        case = Case.objects.get(external_ref="TKT-1")
        case.status = "completed"
        case.completed_at = timezone.now()
        case.save(update_fields=["status", "completed_at"])

        self._sync(["TKT-2"])
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")

    def test_closed_upstream_still_wins_over_field_status(self):
        """OpenCall reporting the call closed is authoritative even if the
        engineer left the case mid-flight."""
        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        self.engineer_client.post(f"/api/cases/{case.id}/accept/")

        self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-1",
                        "title": "Service call (TKT-1)",
                        "engineer_name": "Praveen",
                        "status": "closed",
                    }
                ]
            },
            format="json",
        )
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")
        # Still counted: it was in this push, so it is still in the plan. The
        # engineer sees it marked completed rather than having it vanish.
        rows = {c["external_ref"]: c["status"] for c in self._engineer_cases()}
        self.assertEqual(rows, {"TKT-1": "completed"})

    def test_reassigned_ticket_moves_to_the_new_engineer_and_restarts(self):
        other_user = User.objects.create_user(username="samim", password="x", role="employee")
        other = Employee.objects.create(
            user=other_user, employee_name="Samim", branch="Chennai", salary=0
        )
        other_client = APIClient()
        other_client.force_authenticate(other_user)

        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        self.engineer_client.post(f"/api/cases/{case.id}/accept/")
        self.engineer_client.post(f"/api/cases/{case.id}/start_travel/")

        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-1",
                        "title": "Service call (TKT-1)",
                        "engineer_name": "Samim",
                        "status": "assigned",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        case.refresh_from_db()
        self.assertEqual(case.assigned_to_id, other.id)
        self.assertEqual(case.status, "assigned", "new engineer starts fresh")
        self.assertEqual(self._engineer_cases(), [], "old engineer loses it")
        res = other_client.get("/api/cases/")
        self.assertEqual(len(res.data["results"] if "results" in res.data else res.data), 1)

    def test_sync_is_idempotent(self):
        self._sync(["TKT-1"])
        self._sync(["TKT-1"])
        self._sync(["TKT-1"])
        self.assertEqual(Case.objects.filter(external_ref="TKT-1").count(), 1)

    # -- diagnosing a short or empty sync ------------------------------------

    def test_skipped_item_names_the_unmatched_engineer(self):
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {"external_ref": "TKT-1", "title": "a", "engineer_name": "Praveen", "status": "assigned"},
                    {"external_ref": "TKT-8", "title": "b", "engineer_name": "Lava", "status": "assigned"},
                    {"external_ref": "TKT-9", "title": "c", "engineer_name": "Lava", "status": "assigned"},
                    {"external_ref": "TKT-7", "title": "d", "engineer_name": "VijayaKumar Egmore", "status": "assigned"},
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(res.data["skipped"], 3)
        # Reported once per PERSON, not once per ticket — this is the onboarding list.
        self.assertEqual(res.data["unmatched_engineers"], ["Lava", "VijayaKumar Egmore"])
        skipped = [d for d in res.data["details"] if d["result"] == "skipped"]
        self.assertTrue(all(d["engineer_name"] for d in skipped))

    def test_login_without_an_employee_record_gets_a_clear_error(self):
        """Not an empty list — an engineer handed an unlinked login must be told."""
        orphan = User.objects.create_user(username="orphan", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(orphan)

        res = client.get("/api/cases/")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("not linked", res.data["detail"].lower())

    def test_name_match_never_lands_on_an_employee_with_no_login(self):
        """A stale duplicate row with no login must not swallow the cases —
        the live row with the login gets them."""
        Employee.objects.create(employee_name="Praveen S", branch="Vellore", salary=0)  # no user

        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        self.assertEqual(case.assigned_to_id, self.engineer.id)
        self.assertEqual(len(self._engineer_cases()), 1)

    def test_only_a_loginless_namesake_is_skipped_not_silently_assigned(self):
        Employee.objects.create(employee_name="Ghost Engineer", branch="Chennai", salary=0)

        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-5", "title": "x", "engineer_name": "Ghost Engineer", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["skipped"], 1)
        self.assertEqual(res.data["unmatched_engineers"], ["Ghost Engineer"])
        self.assertFalse(Case.objects.filter(external_ref="TKT-5").exists())

    # -- explicit alias for names the automatic rules refuse -----------------

    def test_alias_resolves_a_name_with_nothing_in_common(self):
        """OpenCall "Lava" is Payroll "LAVAKUMAR" — no prefix, no email, no phone.
        Only an explicit alias can connect them."""
        lava = Employee.objects.create(
            user=User.objects.create_user(username="lavakumar", password="x", role="employee"),
            employee_name="LAVAKUMAR",
            branch="Chennai",
            salary=0,
        )
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-L", "title": "x", "engineer_name": "Lava", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["skipped"], 1, "no alias yet -> skipped, never guessed")
        self.assertEqual(res.data["unmatched_engineers"], ["Lava"])

        EngineerAlias.objects.create(external_name="Lava", employee=lava)
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-L", "title": "x", "engineer_name": "Lava", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(Case.objects.get(external_ref="TKT-L").assigned_to_id, lava.id)

    def test_alias_picks_the_right_person_among_namesakes(self):
        """Four employees called VIJAYAKUMAR; matching must stay refused until an
        alias says which one, and then land on exactly that one."""
        for branch in ("Chennai", "Vellore", "Salem"):
            Employee.objects.create(
                user=User.objects.create_user(username=f"vk-{branch}", password="x", role="employee"),
                employee_name="VIJAYAKUMAR",
                branch=branch,
                salary=0,
            )
        target = Employee.objects.create(
            user=User.objects.create_user(username="vk-kanchi", password="x", role="employee"),
            employee_name="VIJAYAKUMAR",
            branch="Kanchipuram",
            salary=0,
        )

        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-V", "title": "x", "engineer_name": "Vijayakumar", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["skipped"], 1, "ambiguous -> refuse, never pick one")

        EngineerAlias.objects.create(external_name="Vijayakumar", employee=target, note="Kanchipuram")
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-V", "title": "x", "engineer_name": "Vijayakumar", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(Case.objects.get(external_ref="TKT-V").assigned_to_id, target.id)

    def test_alias_lookup_ignores_case_and_padding(self):
        EngineerAlias.objects.create(external_name="  LaVa  ", employee=self.engineer)
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-C", "title": "x", "engineer_name": "lava", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(Case.objects.get(external_ref="TKT-C").assigned_to_id, self.engineer.id)

    def test_alias_beats_an_exact_name_match_on_someone_else(self):
        """An alias is the operator's explicit instruction; it must not be
        overridden by a coincidental exact-name row."""
        Employee.objects.create(
            user=User.objects.create_user(username="other-praveen", password="x", role="employee"),
            employee_name="Praveen",
            branch="Vellore",
            salary=0,
        )
        EngineerAlias.objects.create(external_name="Praveen", employee=self.engineer)

        self._sync(["TKT-1"])
        self.assertEqual(Case.objects.get(external_ref="TKT-1").assigned_to_id, self.engineer.id)

    # -- the ticket detail the engineer needs on site -------------------------

    def test_the_ticket_detail_reaches_the_engineer(self):
        detail = {
            "ticket_id": "WO-035444535",
            "case_id": "5163050263",
            "wip_aging": "4",
            "location": "Padi",
            "engineer": "Praveen",
            "product_name": "HP LaserJet M404dn",
            "product_serial_no": "CNB1234567",
            "product_line_name": "LaserJet",
            "work_location": "ASPS01461",
            "account_name": "Acme Industries",
            "customer_name": "Ramesh Kumar",
            "contact": "9876543210",
            "customer_mail": "ramesh@example.com",
            "common_address": "12 Anna Salai, Padi",
            "customer_address": "12 Anna Salai",
            "customer_pincode": "600050",
        }
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "WO-035444535",
                        "title": "Service call (WO-035444535)",
                        "engineer_name": "Praveen",
                        "status": "assigned",
                        "customer_name": "Ramesh Kumar",
                        "customer_phone": "9876543210",
                        "address": "12 Anna Salai, Padi - 600050",
                        "details": detail,
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        rows = self._engineer_cases()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # The three the engineer acts on are real columns, not buried in the bag.
        self.assertEqual(row["customer_name"], "Ramesh Kumar")
        self.assertEqual(row["customer_phone"], "9876543210")
        self.assertIn("Anna Salai", row["address"])
        self.assertIn("600050", row["address"], "the pincode travels with the address")
        # And every field asked for is on the record.
        self.assertEqual(row["details"], detail)

    def test_a_later_sync_replaces_the_detail_rather_than_merging_it(self):
        """The originating system is the authority — a value it has cleared must
        not survive here."""
        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        case.details = {"contact": "9999999999", "product_name": "Old printer"}
        case.save(update_fields=["details"])

        self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-1",
                        "title": "Service call (TKT-1)",
                        "engineer_name": "Praveen",
                        "status": "assigned",
                        "details": {"product_name": "New printer"},
                    }
                ]
            },
            format="json",
        )
        case.refresh_from_db()
        self.assertEqual(case.details, {"product_name": "New printer"})

    def test_a_malformed_detail_payload_is_ignored(self):
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-1",
                        "title": "a",
                        "engineer_name": "Praveen",
                        "status": "assigned",
                        "details": "not a dict",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(Case.objects.get(external_ref="TKT-1").details, {})

    def test_a_ticket_with_no_detail_still_arrives(self):
        """Detail is a bonus; a missing report row must never cost the engineer
        the case itself."""
        self._sync(["TKT-1"])
        rows = self._engineer_cases()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["details"], {})

    def test_an_engineer_cannot_edit_the_detail(self):
        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        res = self.engineer_client.patch(
            f"/api/cases/{case.id}/", {"details": {"contact": "0000000000"}}, format="json"
        )
        self.assertEqual(res.status_code, 403)
        case.refresh_from_db()
        self.assertEqual(case.details, {})

    # -- the count must equal OpenCall's Assigned column ----------------------

    def _assert_parity(self, tickets):
        """The engineer sees exactly the pushed Assigned set — no more, no fewer."""
        seen = {c["external_ref"] for c in self._engineer_cases()}
        self.assertEqual(
            seen,
            set(tickets),
            f"Payroll shows {len(seen)} case(s), OpenCall assigned {len(tickets)}",
        )

    def test_the_list_holds_exactly_as_many_cases_as_were_assigned(self):
        tickets = ["WO-1", "WO-2", "WO-3", "WO-4", "WO-5"]
        self._sync(tickets)
        self._assert_parity(tickets)

    def test_a_case_the_engineer_completed_still_counts(self):
        """This is the drift that made 5 upstream read as 4 here: filtering the
        list by status dropped work the engineer had finished, while OpenCall
        went on counting it as assigned."""
        tickets = ["WO-1", "WO-2", "WO-3", "WO-4", "WO-5"]
        self._sync(tickets)

        case = Case.objects.get(external_ref="WO-3")
        for step in ("accept", "start_travel", "reached", "start_work", "complete"):
            res = self.engineer_client.post(f"/api/cases/{case.id}/{step}/")
            self.assertEqual(res.status_code, 200, f"{step}: {res.data}")

        # Still five, and the finished one still says so.
        self._assert_parity(tickets)
        rows = {c["external_ref"]: c["status"] for c in self._engineer_cases()}
        self.assertEqual(rows["WO-3"], "completed")

        # And it survives the next tick unchanged.
        self._sync(tickets)
        self._assert_parity(tickets)
        self.assertEqual(Case.objects.get(external_ref="WO-3").status, "completed")

    def test_a_ticket_that_leaves_the_plan_leaves_the_list(self):
        self._sync(["WO-1", "WO-2", "WO-3"])
        self._assert_parity(["WO-1", "WO-2", "WO-3"])

        self._sync(["WO-1", "WO-3"])
        self._assert_parity(["WO-1", "WO-3"])

    def test_a_completed_ticket_that_leaves_the_plan_also_leaves_the_list(self):
        """Parity works in both directions — a finished call that is no longer
        booked must not linger and inflate the count."""
        self._sync(["WO-1", "WO-2"])
        case = Case.objects.get(external_ref="WO-2")
        for step in ("accept", "start_travel", "reached", "start_work", "complete"):
            self.engineer_client.post(f"/api/cases/{case.id}/{step}/")
        self._assert_parity(["WO-1", "WO-2"])

        self._sync(["WO-1"])

        self._assert_parity(["WO-1"])
        case.refresh_from_db()
        self.assertEqual(case.status, "completed", "the outcome is never rewritten")
        self.assertFalse(case.in_current_plan)

    def test_a_ticket_that_comes_back_is_counted_again(self):
        self._sync(["WO-1", "WO-2"])
        self._sync(["WO-1"])
        self._assert_parity(["WO-1"])

        self._sync(["WO-1", "WO-2"])
        self._assert_parity(["WO-1", "WO-2"])

    def test_parity_holds_across_a_realistic_day(self):
        """Praveen's actual shape: five assigned, one of them closed."""
        tickets = ["WO-035491534", "WO-035454195", "WO-035444535", "WO-035362112", "WO-035451081"]
        self._sync(tickets)

        closed = Case.objects.get(external_ref="WO-035451081")
        for step in ("accept", "start_travel", "reached", "start_work", "complete"):
            self.engineer_client.post(f"/api/cases/{closed.id}/{step}/")

        self.assertEqual(len(self._engineer_cases()), 5, "five assigned upstream, five here")

    def test_email_match_onto_a_loginless_row_is_reported_as_unreachable(self):
        """email/phone stay authoritative, but a case nobody can open is flagged."""
        Employee.objects.create(
            employee_name="Sivaraj", branch="Vellore", salary=0, email="sivaraj@example.com"
        )
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-6",
                        "title": "y",
                        "engineer_name": "Sivaraj",
                        "engineer_email": "sivaraj@example.com",
                        "status": "assigned",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(res.data["unreachable_engineers"], ["Sivaraj"])


class DutyAndLiveTrackingTests(TestCase):
    """Duty is a state the engineer DECLARES. The live board must follow that
    declaration, not the phone's signal — an engineer whose phone stopped
    reporting is still on duty, just not visible."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ops", password="x", role="admin", is_staff=True
        )
        self.engineer_user = User.objects.create_user(
            username="praveen", password="x", role="employee"
        )
        self.engineer = Employee.objects.create(
            user=self.engineer_user, employee_name="Praveen S", branch="Chennai", salary=0
        )
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.eng = APIClient()
        self.eng.force_authenticate(self.engineer_user)

    def _live(self):
        res = self.admin_client.get("/api/tracking/live/")
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def _ping(self, lat, lon, accuracy=10, minutes_ago=0):
        res = self.eng.post(
            "/api/tracking/ping/",
            {"latitude": lat, "longitude": lon, "accuracy": accuracy},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        if minutes_ago:
            ping = LocationPing.objects.latest("id")
            ping.timestamp = timezone.now() - timedelta(minutes=minutes_ago)
            ping.save(update_fields=["timestamp"])
        return res.data

    # -- the core promise -----------------------------------------------------

    def test_start_duty_puts_the_engineer_on_the_live_board(self):
        self.assertEqual(self._live(), [], "nobody on duty yet")

        res = self.eng.post("/api/tracking/start_duty/")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["on_duty"])

        rows = self._live()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["engineer_name"], "Praveen S")
        self.assertTrue(rows[0]["on_duty"])
        # On duty but no fix yet: listed, with no position for the map to plot.
        self.assertIsNone(rows[0]["latitude"])
        self.assertTrue(rows[0]["stale"])

    def test_engineer_stays_on_duty_when_the_phone_stops_reporting(self):
        """The whole point of a declared duty: a locked phone must not read as
        'went home'."""
        self.eng.post("/api/tracking/start_duty/")
        self._ping(13.08, 80.27, minutes_ago=25)

        rows = self._live()
        self.assertEqual(len(rows), 1, "must NOT disappear from the board")
        self.assertTrue(rows[0]["on_duty"])
        self.assertTrue(rows[0]["stale"], "position is old")
        self.assertGreaterEqual(rows[0]["last_seen_minutes"], 24)
        # The last known position is still there to show where they were.
        self.assertAlmostEqual(rows[0]["latitude"], 13.08, places=4)

    def test_a_fresh_ping_clears_the_stale_flag(self):
        self.eng.post("/api/tracking/start_duty/")
        self._ping(13.08, 80.27, minutes_ago=25)
        self.assertTrue(self._live()[0]["stale"])

        self._ping(13.09, 80.28)
        row = self._live()[0]
        self.assertFalse(row["stale"])
        self.assertEqual(row["last_seen_minutes"], 0)

    def test_end_duty_removes_them_from_the_board(self):
        self.eng.post("/api/tracking/start_duty/")
        self._ping(13.08, 80.27)
        self.assertEqual(len(self._live()), 1)

        res = self.eng.post("/api/tracking/end_duty/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["on_duty"])
        self.assertEqual(self._live(), [])

    def test_pinging_without_starting_duty_does_not_put_you_on_the_board(self):
        """Membership is the declared duty, not the presence of GPS data."""
        self._ping(13.08, 80.27)
        self.assertEqual(self._live(), [])

    # -- distance -------------------------------------------------------------

    def test_distance_covers_this_duty_and_ignores_earlier_travel(self):
        # Travel from a previous shift, before today's duty starts.
        self._ping(13.00, 80.00)
        self._ping(13.50, 80.00)
        old = LocationPing.objects.all()
        for p in old:
            p.timestamp = timezone.now() - timedelta(hours=3)
            p.save(update_fields=["timestamp"])

        self.eng.post("/api/tracking/start_duty/")
        # ~11.1 km apart at this latitude (0.1 degree of latitude).
        self._ping(13.00, 80.00)
        self._ping(13.10, 80.00)

        row = self._live()[0]
        self.assertGreater(row["distance_km"], 10)
        self.assertLess(row["distance_km"], 12, "must not include the earlier shift")

    def test_noisy_fixes_do_not_inflate_the_distance(self):
        self.eng.post("/api/tracking/start_duty/")
        self._ping(13.00, 80.00, accuracy=10)
        self._ping(13.90, 80.00, accuracy=5000)  # a wild, low-confidence jump
        self._ping(13.00, 80.00, accuracy=10)

        self.assertEqual(self._live()[0]["distance_km"], 0.0)

    # -- robustness -----------------------------------------------------------

    def test_start_duty_twice_does_not_open_two_sessions(self):
        self.eng.post("/api/tracking/start_duty/")
        self.eng.post("/api/tracking/start_duty/")
        self.assertEqual(DutySession.objects.filter(ended_at__isnull=True).count(), 1)
        self.assertEqual(len(self._live()), 1)

    def test_end_duty_when_not_on_duty_is_a_no_op(self):
        res = self.eng.post("/api/tracking/end_duty/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["on_duty"])

    def test_duty_endpoint_lets_the_app_resume_after_a_reload(self):
        self.eng.post("/api/tracking/start_duty/")
        res = self.eng.get("/api/tracking/duty/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["on_duty"])
        self.assertIsNotNone(res.data["started_at"])

    def test_a_forgotten_session_is_auto_closed(self):
        """One missed Stop Duty must not show someone on duty for days."""
        session = DutySession.objects.create(engineer=self.engineer)
        session.started_at = timezone.now() - timedelta(
            hours=DutySession.MAX_DURATION_HOURS + 1
        )
        session.save(update_fields=["started_at"])

        self.assertEqual(self._live(), [])
        session.refresh_from_db()
        self.assertIsNotNone(session.ended_at)
        self.assertTrue(session.auto_closed)

    def test_branch_scoped_staff_only_see_their_own_branch(self):
        other_user = User.objects.create_user(username="vel", password="x", role="employee")
        other = Employee.objects.create(
            user=other_user, employee_name="Vellore Engineer", branch="Vellore", salary=0
        )
        DutySession.objects.create(engineer=other)
        self.eng.post("/api/tracking/start_duty/")

        self.assertEqual(len(self._live()), 2, "full-access admin sees both")

        # Scoping comes from allowed_sections, not assigned_branch alone — the
        # default sections grant "All", which is why this must be set explicitly.
        scoped = User.objects.create_user(
            username="vellore-admin",
            password="x",
            role="hr",
            assigned_branch="Vellore",
            allowed_sections={"attendance": ["Vellore"]},
        )
        client = APIClient()
        client.force_authenticate(scoped)
        res = client.get("/api/tracking/live/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([r["engineer_name"] for r in res.data], [other.employee_name])

    def test_an_engineer_cannot_read_the_live_board(self):
        res = self.eng.get("/api/tracking/live/")
        self.assertEqual(res.status_code, 403)
