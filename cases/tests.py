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

    def _sync_for(self, tickets, plan_date):
        """One sync tick that says which plan day it speaks for."""
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "plan_date": plan_date.isoformat(),
                "cases": [
                    {
                        "external_ref": t,
                        "title": f"Service call ({t})",
                        "engineer_name": "Praveen",
                        "status": "assigned",
                    }
                    for t in tickets
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def test_only_todays_plan_reaches_the_engineer(self):
        """Yesterday's calls must not sit on today's list."""
        yesterday = timezone.localdate() - timedelta(days=1)
        self._sync_for(["WO-OLD-1", "WO-OLD-2"], yesterday)
        self.assertEqual(self._engineer_cases(), [], "yesterday's plan is not today's list")

        self._sync_for(["WO-NEW-1"], timezone.localdate())
        refs = {c["external_ref"] for c in self._engineer_cases()}
        self.assertEqual(refs, {"WO-NEW-1"})

    def test_a_call_carried_into_today_keeps_showing(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self._sync_for(["WO-CARRIED"], yesterday)
        self.assertEqual(self._engineer_cases(), [])

        # Still open, so today's plan carries it again.
        self._sync_for(["WO-CARRIED"], timezone.localdate())
        refs = {c["external_ref"] for c in self._engineer_cases()}
        self.assertEqual(refs, {"WO-CARRIED"})

    def test_yesterdays_calls_age_out_even_if_the_sync_stops(self):
        """The date does the work, so a stopped sync cannot leave stale cases on
        an engineer's screen — the mirror pass never has to run."""
        yesterday = timezone.localdate() - timedelta(days=1)
        self._sync_for(["WO-STALE"], yesterday)
        case = Case.objects.get(external_ref="WO-STALE")
        # Still flagged in-plan, because nothing ever retracted it.
        self.assertTrue(case.in_current_plan)
        self.assertEqual(self._engineer_cases(), [])

    def test_a_case_created_in_payroll_by_hand_is_always_shown(self):
        Case.objects.create(
            customer_name="Walk-in",
            title="Manual case",
            assigned_to=self.engineer,
            status="assigned",
        )
        refs = [c["title"] for c in self._engineer_cases()]
        self.assertEqual(refs, ["Manual case"], "no plan owns it, so no date hides it")

    def test_a_malformed_plan_date_does_not_lose_the_batch(self):
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "plan_date": "2026-13-45",
                "cases": [
                    {"external_ref": "WO-1", "title": "a", "engineer_name": "Praveen", "status": "assigned"}
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["assigned"], 1)

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
        # The engineer's client stands in for the phone app. The admin's stays a
        # browser: reading the board from one is right, reporting a position
        # from one is the bug this header exists to stop.
        self.eng.credentials(HTTP_X_PAYROLL_CLIENT="app")

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


class EngineerRosterTests(TestCase):
    """The board you pick an engineer from. Unlike /live it holds EVERYONE, so
    someone who has finished their shift can still be opened and reviewed."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ops", password="x", role="admin", is_staff=True
        )
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(self.admin)

        self.on_duty = self._engineer("On Duty Ravi", "Chennai")
        self.checked_out = self._engineer("Checked Out Kumar", "Chennai")
        self.absent = self._engineer("Absent Suresh", "Chennai")

    def _engineer(self, name, branch):
        user = User.objects.create_user(
            username=name.replace(" ", "-").lower(), password="x", role="employee"
        )
        return Employee.objects.create(
            user=user, employee_name=name, branch=branch, salary=0, status="active"
        )

    def _roster(self, **params):
        res = self.client_admin.get("/api/tracking/roster/", params)
        self.assertEqual(res.status_code, 200, res.data)
        return {row["engineer_name"]: row for row in res.data}

    def test_the_board_resolves_by_email_the_way_case_dispatch_does(self):
        """The bug this exists for: a ticket reached the engineer because Payroll
        matched their EMAIL, while the board asked by name only, could not choose
        between the namesakes, and reported nobody on duty while they were out."""
        self.on_duty.email = "ravi@example.com"
        self.on_duty.phone = "9000012345"
        self.on_duty.save(update_fields=["email", "phone"])
        # A second person with the same first name makes the name alone useless.
        self._engineer("On Duty Ravi Kumar", "Chennai")
        DutySession.objects.create(engineer=self.on_duty)

        by_name_only = self.client_admin.post(
            "/api/tracking/roster/", {"names": ["On Duty"]}, format="json"
        )
        self.assertEqual(by_name_only.status_code, 200, by_name_only.data)
        self.assertEqual(
            [r["state"] for r in by_name_only.data],
            ["unmatched"],
            "the name alone cannot choose between the two, and must not guess",
        )

        with_keys = self.client_admin.post(
            "/api/tracking/roster/",
            {"engineers": [{"name": "On Duty", "email": "ravi@example.com"}]},
            format="json",
        )
        self.assertEqual(with_keys.status_code, 200, with_keys.data)
        row = with_keys.data[0]
        self.assertEqual(row["payroll_name"], "On Duty Ravi")
        self.assertEqual(row["state"], "on_duty", "the email resolves it, as it does for cases")
        self.assertEqual(row["engineer_name"], "On Duty", "the caller's spelling is kept")

    def test_the_board_resolves_by_phone_too(self):
        self.on_duty.phone = "9000067890"
        self.on_duty.save(update_fields=["phone"])
        DutySession.objects.create(engineer=self.on_duty)

        res = self.client_admin.post(
            "/api/tracking/roster/",
            {"engineers": [{"name": "Nobody By That Name", "phone": "9000067890"}]},
            format="json",
        )
        self.assertEqual(res.data[0]["state"], "on_duty")
        self.assertEqual(res.data[0]["payroll_name"], "On Duty Ravi")

    def test_everyone_is_on_the_board_whatever_their_state(self):
        DutySession.objects.create(engineer=self.on_duty)
        closed = DutySession.objects.create(engineer=self.checked_out)
        closed.ended_at = timezone.now()
        closed.save(update_fields=["ended_at"])

        roster = self._roster()
        self.assertEqual(roster["On Duty Ravi"]["state"], "on_duty")
        self.assertEqual(roster["Checked Out Kumar"]["state"], "checked_out")
        self.assertEqual(roster["Absent Suresh"]["state"], "absent")
        self.assertEqual(len(roster), 3, "nobody is left off the board")

    def test_an_engineer_who_finished_their_shift_can_still_be_opened(self):
        """The reason this endpoint exists: /live drops them, and then their day
        is unreachable."""
        session = DutySession.objects.create(engineer=self.checked_out)
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])

        self.assertEqual(self.client_admin.get("/api/tracking/live/").data, [])
        self.assertIn("Checked Out Kumar", self._roster())

    def test_the_board_carries_the_numbers_needed_to_read_a_row(self):
        session = DutySession.objects.create(engineer=self.checked_out)
        session.started_at = timezone.now() - timedelta(hours=2)
        session.ended_at = timezone.now()
        session.save(update_fields=["started_at", "ended_at"])
        for lat in (13.00, 13.09):
            LocationPing.objects.create(
                engineer=self.checked_out, latitude=lat, longitude=80.0, accuracy=10
            )

        row = self._roster()["Checked Out Kumar"]
        self.assertGreaterEqual(row["duty_minutes"], 119)
        self.assertGreater(row["distance_km"], 9)
        self.assertIsNotNone(row["duty_ended_at"])
        self.assertIsNotNone(row["latitude"], "their last known position")

    def test_a_checked_out_engineer_is_never_flagged_as_no_signal(self):
        """Nobody expects a phone to report after the shift ended."""
        session = DutySession.objects.create(engineer=self.checked_out)
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])

        self.assertFalse(self._roster()["Checked Out Kumar"]["stale"])

    def test_an_on_duty_engineer_with_no_recent_fix_is_flagged(self):
        DutySession.objects.create(engineer=self.on_duty)
        ping = LocationPing.objects.create(
            engineer=self.on_duty, latitude=13.0, longitude=80.0, accuracy=10
        )
        ping.timestamp = timezone.now() - timedelta(minutes=30)
        ping.save(update_fields=["timestamp"])

        row = self._roster()["On Duty Ravi"]
        self.assertTrue(row["stale"])
        self.assertGreaterEqual(row["last_seen_minutes"], 29)

    def test_an_earlier_day_shows_that_day_state(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        session = DutySession.objects.create(engineer=self.checked_out)
        session.started_at = timezone.now() - timedelta(days=1, hours=3)
        session.ended_at = timezone.now() - timedelta(days=1)
        session.save(update_fields=["started_at", "ended_at"])

        self.assertEqual(
            self._roster(date=yesterday.isoformat())["Checked Out Kumar"]["state"], "checked_out"
        )
        self.assertEqual(self._roster()["Checked Out Kumar"]["state"], "absent")

    def test_an_open_session_wins_over_an_earlier_finished_one(self):
        first = DutySession.objects.create(engineer=self.on_duty)
        first.started_at = timezone.now() - timedelta(hours=5)
        first.ended_at = timezone.now() - timedelta(hours=4)
        first.save(update_fields=["started_at", "ended_at"])
        DutySession.objects.create(engineer=self.on_duty)  # back on duty now

        self.assertEqual(self._roster()["On Duty Ravi"]["state"], "on_duty")

    def test_branch_scoped_staff_see_only_their_branch(self):
        other = self._engineer("Vellore Vishnu", "Vellore")
        DutySession.objects.create(engineer=other)

        scoped = User.objects.create_user(
            username="vellore-hr",
            password="x",
            role="hr",
            assigned_branch="Vellore",
            allowed_sections={"attendance": ["Vellore"]},
        )
        client = APIClient()
        client.force_authenticate(scoped)
        res = client.get("/api/tracking/roster/")
        self.assertEqual([r["engineer_name"] for r in res.data], ["Vellore Vishnu"])

    def test_an_engineer_cannot_read_the_board(self):
        client = APIClient()
        client.force_authenticate(self.on_duty.user)
        self.assertEqual(client.get("/api/tracking/roster/").status_code, 403)

    # -- driven by the caller's engineer register ------------------------------

    def _roster_for(self, names, **params):
        res = self.client_admin.post(
            "/api/tracking/roster/", {"names": names, **params}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        return {row["engineer_name"]: row for row in res.data}

    def test_asking_by_name_returns_a_row_per_name(self):
        DutySession.objects.create(engineer=self.on_duty)

        roster = self._roster_for(["On Duty Ravi", "Absent Suresh"])
        self.assertEqual(set(roster), {"On Duty Ravi", "Absent Suresh"})
        self.assertEqual(roster["On Duty Ravi"]["state"], "on_duty")
        self.assertEqual(roster["Absent Suresh"]["state"], "absent")

    def test_duty_survives_a_name_the_alias_table_resolves(self):
        """The bug this replaced: matching on the caller's side lost the duty
        state for any name only the alias table could resolve, so an engineer
        standing in a customer's shop read as off duty."""
        EngineerAlias.objects.create(external_name="Ravi", employee=self.on_duty)
        DutySession.objects.create(engineer=self.on_duty)

        row = self._roster_for(["Ravi"])["Ravi"]
        self.assertTrue(row["matched"])
        self.assertEqual(row["state"], "on_duty")
        self.assertEqual(row["engineer_id"], self.on_duty.id)
        # Their register spelling is echoed back; the matched record is alongside.
        self.assertEqual(row["payroll_name"], "On Duty Ravi")

    def test_a_shorter_register_name_still_matches(self):
        praveen = self._engineer("Praveen S", "Chennai")
        DutySession.objects.create(engineer=praveen)

        row = self._roster_for(["Praveen"])["Praveen"]
        self.assertTrue(row["matched"])
        self.assertEqual(row["state"], "on_duty")

    def test_a_name_nobody_answers_to_comes_back_as_a_row(self):
        """Not dropped: this is the same gap that makes their cases get skipped,
        and a board that quietly omits them hides it."""
        roster = self._roster_for(["Nobody At All", "On Duty Ravi"])

        self.assertEqual(set(roster), {"Nobody At All", "On Duty Ravi"})
        gap = roster["Nobody At All"]
        self.assertFalse(gap["matched"])
        self.assertEqual(gap["state"], "unmatched")
        self.assertIsNone(gap["engineer_id"])

    def test_an_ambiguous_name_is_reported_rather_than_guessed(self):
        self._engineer("Vijayakumar A", "Chennai")
        self._engineer("Vijayakumar B", "Chennai")

        row = self._roster_for(["Vijayakumar"])["Vijayakumar"]
        self.assertFalse(row["matched"], "two namesakes must never be guessed between")

    def test_the_same_name_asked_twice_yields_one_row(self):
        roster = self._roster_for(["On Duty Ravi", "on duty ravi"])
        self.assertEqual(len(roster), 1)

    def test_branch_scope_still_applies_when_asking_by_name(self):
        other = self._engineer("Vellore Vishnu", "Vellore")
        DutySession.objects.create(engineer=other)
        scoped = User.objects.create_user(
            username="chennai-hr",
            password="x",
            role="hr",
            assigned_branch="Chennai",
            allowed_sections={"attendance": ["Chennai"]},
        )
        client = APIClient()
        client.force_authenticate(scoped)

        res = client.post(
            "/api/tracking/roster/", {"names": ["Vellore Vishnu", "On Duty Ravi"]}, format="json"
        )
        rows = {r["engineer_name"]: r for r in res.data}
        self.assertFalse(rows["Vellore Vishnu"]["matched"], "out of branch is not resolved")
        self.assertTrue(rows["On Duty Ravi"]["matched"])

    def test_inactive_employees_are_left_off(self):
        gone = self._engineer("Left The Company", "Chennai")
        gone.status = "inactive"
        gone.save(update_fields=["status"])

        self.assertNotIn("Left The Company", self._roster())


class EngineerDayViewTests(TestCase):
    """The "what did they actually do today" view: route, distance, time on duty,
    where they stood still and for how long, and a readable timeline."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ops", password="x", role="admin", is_staff=True
        )
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)

        self.engineer_user = User.objects.create_user(
            username="praveen", password="x", role="employee"
        )
        self.engineer = Employee.objects.create(
            user=self.engineer_user, employee_name="Praveen S", branch="Chennai", salary=0
        )
        self.today = timezone.localdate()
        self.base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)

    def _ping(self, lat, lon, minutes, accuracy=10, case=None):
        """A fix at `minutes` past the start of the day."""
        return LocationPing.objects.create(
            engineer=self.engineer,
            case=case,
            latitude=lat,
            longitude=lon,
            accuracy=accuracy,
            timestamp=self.base + timedelta(minutes=minutes),
        )

    def _day(self, **params):
        query = {"engineer": self.engineer.id, **params}
        res = self.admin_client.get("/api/tracking/day/", query)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    # -- stops ---------------------------------------------------------------

    def test_standing_still_for_a_while_is_reported_as_a_stop(self):
        # Same spot, 09:00 to 09:30 — a customer visit.
        for minute in range(0, 31, 5):
            self._ping(13.0000, 80.0000, minute)

        day = self._day()
        self.assertEqual(day["stop_count"], 1)
        stop = day["stops"][0]
        self.assertGreaterEqual(stop["minutes"], 30)
        self.assertAlmostEqual(stop["latitude"], 13.0, places=4)

    def test_moving_through_traffic_is_not_a_stop(self):
        """Junctions and signals must not read as visits."""
        for i in range(12):
            # ~550 m apart each fix, five minutes apart: continuously moving.
            self._ping(13.0000 + i * 0.005, 80.0000, i * 5)

        self.assertEqual(self._day()["stop_count"], 0)

    def test_a_brief_pause_is_not_a_stop(self):
        # Four minutes in one place — a red light, not a job.
        self._ping(13.0000, 80.0000, 0)
        self._ping(13.0000, 80.0000, 4)
        self._ping(13.0500, 80.0000, 20)

        self.assertEqual(self._day()["stop_count"], 0)

    def test_gps_wander_while_parked_still_counts_as_one_stop(self):
        """A phone indoors drifts tens of metres; that is one visit, not many."""
        for i, minute in enumerate(range(0, 31, 5)):
            # Jitter of ~0.0004 degrees is roughly 45 m.
            self._ping(13.0000 + (i % 2) * 0.0004, 80.0000 + (i % 3) * 0.0003, minute)

        day = self._day()
        self.assertEqual(day["stop_count"], 1, "one visit, not one per wobble")

    def test_two_separate_visits_are_two_stops(self):
        for minute in range(0, 31, 5):
            self._ping(13.0000, 80.0000, minute)
        # Drive 5 km away, then sit again.
        for minute in range(60, 91, 5):
            self._ping(13.0450, 80.0000, minute)

        day = self._day()
        self.assertEqual(day["stop_count"], 2)
        self.assertLess(day["stops"][0]["arrived_at"], day["stops"][1]["arrived_at"])

    def test_a_stop_names_the_case_the_engineer_was_attending(self):
        case = Case.objects.create(
            customer_name="Ramesh",
            title="Service call",
            assigned_to=self.engineer,
            status="working",
            external_ref="WO-1",
        )
        for minute in range(0, 31, 5):
            self._ping(13.0000, 80.0000, minute, case=case)

        stop = self._day()["stops"][0]
        self.assertEqual(stop["case_number"], case.case_number)

    def test_noisy_fixes_do_not_invent_a_stop(self):
        self._ping(13.0000, 80.0000, 0, accuracy=5000)
        self._ping(13.0000, 80.0000, 30, accuracy=5000)

        day = self._day()
        self.assertEqual(day["stop_count"], 0)
        self.assertEqual(day["points"], [])

    # -- the day as a whole ---------------------------------------------------

    def test_the_day_reports_distance_duty_time_and_the_route(self):
        session = DutySession.objects.create(engineer=self.engineer)
        session.started_at = self.base
        session.ended_at = self.base + timedelta(hours=3)
        session.save(update_fields=["started_at", "ended_at"])

        self._ping(13.0000, 80.0000, 0)
        self._ping(13.0450, 80.0000, 60)  # ~5 km
        self._ping(13.0900, 80.0000, 120)  # ~5 km more

        day = self._day()
        self.assertEqual(day["engineer_name"], "Praveen S")
        self.assertGreater(day["total_km"], 9)
        self.assertLess(day["total_km"], 11)
        self.assertEqual(day["duty_minutes"], 180)
        self.assertEqual(len(day["points"]), 3, "the route the map draws")
        self.assertIsNotNone(day["first_seen"])
        self.assertIsNotNone(day["last_seen"])

    def test_the_timeline_reads_in_the_order_the_day_happened(self):
        session = DutySession.objects.create(engineer=self.engineer)
        session.started_at = self.base
        session.ended_at = self.base + timedelta(hours=4)
        session.save(update_fields=["started_at", "ended_at"])

        for minute in range(30, 61, 5):
            self._ping(13.0000, 80.0000, minute)

        events = self._day()["events"]
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "duty_start")
        self.assertEqual(types[-1], "duty_end")
        self.assertIn("stop", types)
        # Chronological, always.
        moments = [e["at"] for e in events]
        self.assertEqual(moments, sorted(moments))

    def test_case_milestones_appear_on_the_timeline(self):
        Case.objects.create(
            customer_name="Ramesh",
            title="Service call",
            assigned_to=self.engineer,
            status="completed",
            external_ref="WO-1",
            assigned_at=self.base,
            started_at=self.base + timedelta(minutes=30),
            reached_at=self.base + timedelta(minutes=50),
            completed_at=self.base + timedelta(minutes=90),
        )

        labels = [e["label"] for e in self._day()["events"]]
        self.assertIn("Left for the call", labels)
        self.assertIn("Reached the site", labels)
        self.assertIn("Completed the call", labels)

    def test_an_auto_closed_duty_says_so(self):
        session = DutySession.objects.create(engineer=self.engineer)
        session.started_at = self.base
        session.ended_at = self.base + timedelta(hours=17)
        session.auto_closed = True
        session.save(update_fields=["started_at", "ended_at", "auto_closed"])

        labels = [e["label"] for e in self._day()["events"]]
        self.assertIn("Auto-closed (no Stop Duty)", labels)

    def test_an_earlier_day_can_be_read_back(self):
        yesterday = self.today - timedelta(days=1)
        LocationPing.objects.create(
            engineer=self.engineer,
            latitude=13.0,
            longitude=80.0,
            accuracy=10,
            timestamp=timezone.now() - timedelta(days=1),
        )

        self.assertEqual(len(self._day(date=yesterday.isoformat())["points"]), 1)
        self.assertEqual(len(self._day()["points"]), 0, "today is separate")

    def test_a_quiet_day_answers_with_zeroes_rather_than_an_error(self):
        day = self._day()
        self.assertEqual(day["total_km"], 0.0)
        self.assertEqual(day["duty_minutes"], 0)
        self.assertEqual(day["stops"], [])
        self.assertEqual(day["events"], [])
        self.assertIsNone(day["first_seen"])

    # -- who may read it ------------------------------------------------------

    def test_an_engineer_cannot_read_the_day_view(self):
        client = APIClient()
        client.force_authenticate(self.engineer_user)
        res = client.get("/api/tracking/day/", {"engineer": self.engineer.id})
        self.assertEqual(res.status_code, 403)

    def test_staff_from_another_branch_cannot_read_it(self):
        scoped = User.objects.create_user(
            username="vellore-hr",
            password="x",
            role="hr",
            assigned_branch="Vellore",
            allowed_sections={"attendance": ["Vellore"]},
        )
        client = APIClient()
        client.force_authenticate(scoped)
        res = client.get("/api/tracking/day/", {"engineer": self.engineer.id})
        self.assertEqual(res.status_code, 403)

    def test_a_bad_engineer_or_date_is_rejected_cleanly(self):
        self.assertEqual(
            self.admin_client.get("/api/tracking/day/", {"engineer": "abc"}).status_code, 400
        )
        self.assertEqual(
            self.admin_client.get("/api/tracking/day/", {"engineer": 999999}).status_code, 404
        )
        self.assertEqual(
            self.admin_client.get(
                "/api/tracking/day/", {"engineer": self.engineer.id, "date": "2026-13-45"}
            ).status_code,
            400,
        )


class SyncForAnotherDayLeavesTodayAloneTests(TestCase):
    """A batch only speaks for its own day.

    The auto-sync always sends today. The MANUAL sync endpoint takes its date
    from whoever calls it — and the mirror sweep was scoped to no day at all, so
    a re-sync of an earlier working date cleared in_current_plan on every synced
    case in the table. Every engineer's list emptied at once while OpenCall still
    showed their assignments, and nothing anywhere said why.
    """

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

        self.today = timezone.localdate()
        self.older = self.today - timedelta(days=11)

    def _sync(self, tickets, plan_date):
        body = {
            "plan_date": plan_date.isoformat(),
            "cases": [
                {
                    "external_ref": ticket,
                    "title": f"Service call ({ticket})",
                    "engineer_name": "Praveen",
                    "status": "assigned",
                }
                for ticket in tickets
            ],
        }
        res = self.bot_client.post("/api/cases/bulk_dispatch/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def _engineer_sees(self):
        res = self.engineer_client.get("/api/cases/")
        self.assertEqual(res.status_code, 200, res.data)
        data = res.data
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        return sorted(case["external_ref"] for case in rows)

    def test_a_resync_of_an_older_day_does_not_empty_todays_list(self):
        self._sync(["T-TODAY-1", "T-TODAY-2"], self.today)
        self.assertEqual(self._engineer_sees(), ["T-TODAY-1", "T-TODAY-2"])

        # The call that used to wipe the day.
        self._sync(["T-OLD-1"], self.older)

        self.assertEqual(self._engineer_sees(), ["T-TODAY-1", "T-TODAY-2"])

    def test_an_older_batch_does_not_cancel_todays_calls(self):
        self._sync(["T-TODAY-1"], self.today)
        self._sync(["T-OLD-1"], self.older)

        case = Case.objects.get(external_ref="T-TODAY-1")
        self.assertTrue(case.in_current_plan)
        self.assertNotEqual(case.status, "cancelled")

    def test_the_older_batch_still_lands_it_is_simply_not_todays_plan(self):
        self._sync(["T-OLD-1"], self.older)

        case = Case.objects.get(external_ref="T-OLD-1")
        self.assertEqual(case.assigned_to_id, self.engineer.id)
        self.assertEqual(case.plan_date, self.older)
        self.assertEqual(self._engineer_sees(), [])

    def test_todays_sync_still_drops_a_ticket_that_left_todays_plan(self):
        self._sync(["T-A", "T-B"], self.today)
        self._sync(["T-A"], self.today)

        self.assertEqual(self._engineer_sees(), ["T-A"])
        dropped = Case.objects.get(external_ref="T-B")
        self.assertFalse(dropped.in_current_plan)
        self.assertEqual(dropped.status, "cancelled")

    def test_todays_sweep_never_reaches_back_into_an_earlier_day(self):
        self._sync(["T-OLD-1"], self.older)
        self._sync(["T-TODAY-1"], self.today)

        old_case = Case.objects.get(external_ref="T-OLD-1")
        self.assertTrue(old_case.in_current_plan)
        self.assertNotEqual(old_case.status, "cancelled")

    def test_a_list_already_wiped_comes_back_on_the_next_normal_sync(self):
        """What production is sitting in right now, and whether it self-heals.

        The bad sweep left today's cases with in_current_plan False and their
        status rewritten to cancelled, so the engineer sees nothing. No repair
        script should be needed: the auto-sync re-sends the same Assigned set
        every two minutes, and that has to be enough to bring them back.
        """
        self._sync(["T-TODAY-1", "T-TODAY-2"], self.today)
        # Reproduce the damage exactly as the old sweep left it.
        Case.objects.filter(external_ref__in=["T-TODAY-1", "T-TODAY-2"]).update(
            in_current_plan=False, status="cancelled"
        )
        self.assertEqual(self._engineer_sees(), [])

        # One ordinary tick of the auto-sync.
        self._sync(["T-TODAY-1", "T-TODAY-2"], self.today)

        self.assertEqual(self._engineer_sees(), ["T-TODAY-1", "T-TODAY-2"])
        for ref in ["T-TODAY-1", "T-TODAY-2"]:
            case = Case.objects.get(external_ref=ref)
            self.assertTrue(case.in_current_plan)
            self.assertEqual(case.status, "assigned")
            self.assertEqual(case.plan_date, self.today)

    def test_the_skipped_sweep_is_written_down(self):
        self._sync(["T-TODAY-1"], self.today)
        with self.assertLogs("cases", level="INFO") as captured:
            self._sync(["T-OLD-1"], self.older)
        self.assertIn("plan sweep skipped", " ".join(captured.output))


class CaseNumberPlaceholderTests(TestCase):
    """A blank case number must never block the next case from being created.

    case_number is unique and is built from the pk, so it cannot be known until
    the row exists: save() inserts a placeholder and fills it in immediately
    afterwards. The placeholder used to be the EMPTY STRING — and a unique column
    accepts exactly ONE of those.

    So a single case left holding it, from a crash or a rolled-back request, made
    every later insert fail with

        duplicate key value violates unique constraint "cases_case_case_number_key"
        DETAIL:  Key (case_number)=() already exists.

    which is what took the whole OpenCall sync down: every tick 500ed and no
    engineer's list could be refreshed at all.
    """

    def test_a_case_gets_a_readable_number(self):
        case = Case.objects.create(title="Service call", customer_name="Pradeep")
        self.assertEqual(case.case_number, f"OC-{case.pk:06d}")

    def test_a_case_stuck_on_the_placeholder_does_not_block_the_next_one(self):
        stuck = Case.objects.create(title="Stuck", customer_name="A")
        # Exactly the state production was in: numbered row forced back to blank
        # without going through save().
        Case.objects.filter(pk=stuck.pk).update(case_number="")

        # This is the insert that used to 500 and take the sync with it.
        fresh = Case.objects.create(title="Next", customer_name="B")
        self.assertEqual(fresh.case_number, f"OC-{fresh.pk:06d}")

    def test_two_cases_never_share_a_number(self):
        numbers = {Case.objects.create(title=f"C{i}", customer_name="X").case_number
                   for i in range(25)}
        self.assertEqual(len(numbers), 25)
        self.assertNotIn("", numbers)
        self.assertNotIn(None, numbers)

    def test_clearing_the_number_by_hand_stores_null_not_blank(self):
        # An admin edit that empties the box must not reintroduce the collision.
        case = Case.objects.create(title="Service call", customer_name="Pradeep")
        case.case_number = ""
        case.save()
        case.refresh_from_db()
        self.assertIsNone(case.case_number)

        # And the next case is still fine.
        other = Case.objects.create(title="Another", customer_name="Q")
        self.assertEqual(other.case_number, f"OC-{other.pk:06d}")

    def test_a_dispatch_still_works_with_a_blank_row_in_the_table(self):
        """The end-to-end version: the sync itself must survive it."""
        bot = User.objects.create_user(
            username="opencall-bot", password="x", role="admin", is_staff=True
        )
        engineer_user = User.objects.create_user(
            username="praveen", password="x", role="employee"
        )
        Employee.objects.create(
            user=engineer_user, employee_name="Praveen S", branch="Chennai", salary=0
        )
        stuck = Case.objects.create(title="Stuck", customer_name="A")
        Case.objects.filter(pk=stuck.pk).update(case_number="")

        client = APIClient()
        client.force_authenticate(bot)
        res = client.post(
            "/api/cases/bulk_dispatch/",
            {
                "plan_date": timezone.localdate().isoformat(),
                "cases": [
                    {
                        "external_ref": "T-1",
                        "title": "Service call (T-1)",
                        "engineer_name": "Praveen",
                        "status": "assigned",
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["assigned"], 1)
