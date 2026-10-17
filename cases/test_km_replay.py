"""Kilometres never go down, replayed the way the phone delivers a day.

An engineer watched "Travelled today" read 158.2 and then 151.3 a minute later.
Nothing was deleted: the phone delivers its day in pieces and out of order --
live fixes first, a held stretch behind them, the word about Location after the
fix it explains -- and a rule that reads evidence as it turns up can take back a
kilometre it already showed.

KmReplay feeds one engineer's day to the server in the order it ARRIVES:
every fix, batch, event, Login and Logout at the moment it reaches us, through
the endpoints the app posts to, asking GET /api/tracking/duty/ after each one
what today's kilometres are -- the number on the engineer's screen. It is meant
to be reused: build a day with fix() / batch() / event() / login() / logout()
and call run().
"""
import datetime
import itertools
import os
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from cases.models import DutySession, LocationPing
from cases.views import _trail_km
from employees.models import Employee

# A fixed day: every arrival is read at its own moment through a clock the
# endpoints are handed, so a run never depends on when it runs.
DAY = datetime.date(2026, 10, 16)

# km_rule's cutover: before every fix (the new rule) or after every fix (the
# old one), so a replay says which rule it is reading.
NEW_RULE = {"KM_RULE_FROM": "2000-01-01T00:00:00+00:00"}
OLD_RULE = {"KM_RULE_FROM": "2100-01-01T00:00:00+00:00"}

# About a kilometre of latitude.
KM = 0.009


class KmReplay:
    """One engineer's day, delivered to the server one arrival at a time."""

    def __init__(self, engineer, user, day=DAY):
        self.engineer = engineer
        self.day = day
        self.phone = APIClient()
        self.phone.force_authenticate(user)
        self.phone.credentials(HTTP_X_PAYROLL_CLIENT="app")
        self._arrivals = []
        self._order = itertools.count()
        self._keys = itertools.count()

    def at(self, hour, minute=0, second=0):
        return timezone.make_aware(
            datetime.datetime.combine(self.day, datetime.time(hour, minute, second))
        )

    # -- building the day ------------------------------------------------------

    def make_fix(self, taken, latitude, longitude=78.0, after_gap=False, accuracy=8):
        """A fix as the phone sends it, not yet delivered."""
        return {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": accuracy,
            "timestamp": taken.isoformat(),
            "client_key": f"fix-{next(self._keys)}",
            "after_gap": after_gap,
        }

    def fix(self, taken, latitude, longitude=78.0, arrives=None, after_gap=False, accuracy=8):
        """One fix, posted on its own to /ping/ -- the live send."""
        payload = self.make_fix(taken, latitude, longitude, after_gap, accuracy)
        self._add(arrives or taken, "ping", payload)
        return payload

    def batch(self, fixes, arrives):
        """Fixes held on the phone, posted together to /ping/batch/."""
        self._add(arrives, "batch", list(fixes))

    def event(self, kind, at, arrives=None, detail=""):
        self._add(
            arrives or at,
            "events",
            [
                {
                    "kind": kind,
                    "at": at.isoformat(),
                    "client_key": f"event-{next(self._keys)}",
                    "detail": detail,
                }
            ],
        )

    def login(self, at):
        self._add(at, "login", None)

    def logout(self, at):
        self._add(at, "logout", None)

    def _add(self, arrives, kind, payload):
        self._arrivals.append((arrives, next(self._order), kind, payload))

    # -- delivering it ---------------------------------------------------------

    def run(self, test, never_down=True):
        """Deliver everything in arrival order. Returns [(when, what, km)].

        After every arrival the engineer's own duty is read at that moment,
        and with `never_down` the test fails the first time it reads lower
        than the time before -- with the whole history, so the drop can be
        seen in context.
        """
        readings = []
        for arrives, _, kind, payload in sorted(self._arrivals, key=lambda a: (a[0], a[1])):
            with mock.patch("django.utils.timezone.now", return_value=arrives):
                self._deliver(test, kind, payload, arrives)
                response = self.phone.get("/api/tracking/duty/")
            test.assertEqual(response.status_code, 200, response.content[:300])
            km = response.data["today_km"]
            what = f"batch x{len(payload)}" if kind == "batch" else kind
            if never_down and readings:
                test.assertGreaterEqual(
                    km,
                    readings[-1][2],
                    "today_km went down at %s (%s):\n%s"
                    % (
                        timezone.localtime(arrives).strftime("%H:%M:%S"),
                        what,
                        "\n".join(
                            f"  {timezone.localtime(w):%H:%M:%S} {d:12} {k}" for w, d, k in readings
                        ),
                    ),
                )
            readings.append((arrives, what, km))
        return readings

    def _deliver(self, test, kind, payload, arrives):
        if kind == "ping":
            response = self.phone.post("/api/tracking/ping/", payload, format="json")
            test.assertEqual(response.status_code, 201, response.data)
        elif kind == "batch":
            response = self.phone.post(
                "/api/tracking/ping/batch/", {"pings": payload}, format="json"
            )
            test.assertEqual(response.status_code, 201, response.data)
            test.assertEqual(response.data["rejected"], [], response.data)
        elif kind == "events":
            response = self.phone.post("/api/tracking/events/", {"events": payload}, format="json")
            test.assertEqual(response.status_code, 201, response.data)
            test.assertEqual(response.data["rejected"], [], response.data)
        elif kind == "login":
            # Written as start_duty writes it, at the moment it happened. The
            # model's own default reads a clock this test cannot move.
            DutySession.objects.create(engineer=self.engineer, started_at=arrives)
        elif kind == "logout":
            DutySession.objects.filter(engineer=self.engineer, ended_at__isnull=True).update(
                ended_at=arrives
            )


def make_engineer(username, name):
    user = User.objects.create_user(username=username, password="x", role="employee")
    engineer = Employee.objects.create(
        user=user,
        employee_name=name,
        email=f"{username}@example.com",
        role="Field Engineer",
        department="Service",
        branch="Chennai",
        salary=Decimal("20000"),
    )
    return user, engineer


def drops(readings):
    """Every step down in a run, as (before, after)."""
    return [
        (before[2], after[2]) for before, after in zip(readings, readings[1:]) if after[2] < before[2]
    ]


class ReplayTests(TestCase):
    """The orders a day really arrives in. Each reads the new rule, and the
    harness fails the moment today_km goes down."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.engineer = make_engineer("replayer", "Replay Raja")

    def setUp(self):
        patcher = mock.patch.dict(os.environ, NEW_RULE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.day = KmReplay(self.engineer, self.user)

    def _morning(self, day, until_minute=60):
        """Login at nine, a fix every five minutes, a kilometre apart."""
        day.login(day.at(9, 0))
        for step, minute in enumerate(range(5, until_minute + 1, 5)):
            day.fix(day.at(9, 0) + datetime.timedelta(minutes=minute), 11.0 + step * KM)
        return 11.0 + (until_minute // 5 - 1) * KM

    # -- a late batch with a flag on its first fix ----------------------------

    def _restart_while_live(self, day):
        """Android restarted the tracker at 10:02. What it took after that sat
        in its own buffer -- the first one flagged -- while the page went on
        posting the newest fix, and the buffer came in behind them at 10:40."""
        last = self._morning(day)  # 9:05 .. 10:00, live
        held = [
            day.make_fix(day.at(10, 5), last + 1 * KM, after_gap=True),
            day.make_fix(day.at(10, 10), last + 2 * KM),
            day.make_fix(day.at(10, 15), last + 3 * KM),
            day.make_fix(day.at(10, 20), last + 4 * KM),
        ]
        for step, minute in enumerate((25, 30, 35)):
            day.fix(day.at(10, minute), last + (5 + step) * KM)
        day.batch(held, arrives=day.at(10, 40))
        for step, minute in enumerate((45, 50)):
            day.fix(day.at(10, minute), last + (8 + step) * KM)

    def test_a_late_batch_whose_first_fix_is_flagged_never_takes_km_back(self):
        self._restart_while_live(self.day)
        readings = self.day.run(self)
        # The whole road: eleven kilometres to ten o'clock and nine after it,
        # the restart's straight line included -- nothing skipped.
        self.assertAlmostEqual(readings[-1][2], 20.0, delta=0.2)

    def test_the_old_rule_took_it_back(self):
        """The harness can see a drop: the same day read the old way loses
        the line into the flagged fix the moment the buffer lands."""
        with mock.patch.dict(os.environ, OLD_RULE):
            readings = self.day_with(self._restart_while_live).run(self, never_down=False)
        self.assertTrue(drops(readings), readings)

    def day_with(self, build):
        day = KmReplay(self.engineer, self.user)
        build(day)
        return day

    # -- an offline flush across a Logout and a Login -------------------------

    def _flush_across_a_login(self, day):
        """Last fix before Logout at 12:59:50. Twenty kilometres off duty.
        Login at 15:00:02; the first fix after it (flagged) and nine more are
        queued with no network, a live send of the newest gets through at
        15:05, and the queue lands half a minute later."""
        day.login(day.at(9, 0))
        day.fix(day.at(12, 59, 50), 11.0)
        day.logout(day.at(13, 0))
        day.login(day.at(15, 0, 2))
        away = 11.0 + 20 * KM
        queued = [day.make_fix(day.at(15, 0, 20), away, after_gap=True)]
        for step in range(1, 10):
            taken = day.at(15, 0, 20) + datetime.timedelta(seconds=30 * step)
            queued.append(day.make_fix(taken, away + step * 0.3 * KM))
        day.fix(day.at(15, 5), away + 10 * 0.3 * KM)
        day.batch(queued, arrives=day.at(15, 5, 30))
        day.fix(day.at(15, 6), away + 11 * 0.3 * KM)

    def test_an_offline_flush_across_a_login_never_takes_km_back(self):
        self._flush_across_a_login(self.day)
        readings = self.day.run(self)
        # Only the road after the Login: eleven steps of 0.3 km. The twenty
        # kilometres between Logout and Login never counted, at any moment.
        self.assertAlmostEqual(readings[-1][2], 11 * 0.3, delta=0.1)
        self.assertLess(max(k for _, _, k in readings), 5)

    def test_the_old_rule_counted_the_drive_home_then_took_it_back(self):
        with mock.patch.dict(os.environ, OLD_RULE):
            readings = self.day_with(self._flush_across_a_login).run(self, never_down=False)
        self.assertTrue(any(before - after > 15 for before, after in drops(readings)), readings)

    # -- the word about Location arriving after the fix it explains -----------

    def _location_off_while_offline(self, day, event_arrives):
        """Location and data off together at 10:00:05, seven kilometres
        ridden, Location back at 10:40:20. The batch with the first fix after
        it gets through at 10:41:02; the events post goes when it can."""
        last = self._morning(day)
        day.event("gps_off", day.at(10, 0, 5), arrives=event_arrives, detail="switched")
        day.event(
            "gps_on",
            day.at(10, 40, 20),
            arrives=max(event_arrives, day.at(10, 40, 21)),
            detail="switched",
        )
        back = last + 7 * KM
        day.batch(
            [day.make_fix(day.at(10, 40, 30), back, after_gap=True)], arrives=day.at(10, 41, 2)
        )
        day.fix(day.at(10, 41, 31), back + KM)
        day.fix(day.at(10, 46), back + 2 * KM)

    def test_events_failing_then_landing_never_take_km_back(self):
        """The events post failed while the batch went through, so the stretch
        was on the screen as counted. When the gps_off lands it is too late to
        take it back."""
        self._location_off_while_offline(self.day, event_arrives=self.day.at(10, 41, 30))
        readings = self.day.run(self)
        # Eleven kilometres of morning, the seven across, two after.
        self.assertAlmostEqual(readings[-1][2], 20.0, delta=0.3)

    def test_events_that_land_first_leave_the_stretch_out_from_the_start(self):
        self._location_off_while_offline(self.day, event_arrives=self.day.at(10, 0, 6))
        readings = self.day.run(self)
        self.assertAlmostEqual(readings[-1][2], 13.0, delta=0.3)

    def test_events_that_fail_while_the_flagged_fix_gets_through(self):
        """APK 1.5 with data off: gps_off queued, the flagged fix's batch gets
        through first, the event a minute later and the next fix after it."""
        last = self._morning(self.day)
        self.day.event(
            "gps_off", self.day.at(10, 0, 5), arrives=self.day.at(10, 31), detail="switched"
        )
        self.day.batch(
            [self.day.make_fix(self.day.at(10, 30, 10), last + 15 * KM, after_gap=True)],
            arrives=self.day.at(10, 30, 30),
        )
        self.day.fix(self.day.at(10, 31, 1), last + 16 * KM)
        readings = self.day.run(self)
        self.assertAlmostEqual(readings[-1][2], 27.0, delta=0.3)

    # -- fixes sharing one second ---------------------------------------------

    def test_a_stretch_stamped_in_one_second_never_takes_km_back(self):
        """The evidence day: a dozen kilometres all stamped 02:17:43 PM,
        because the WebView stamped them when its delayed callback ran. Half
        of them arrived with the live send, the rest twenty seconds later."""
        last = self._morning(self.day)
        burst = self.day.at(14, 17, 43)
        first_half = [self.day.make_fix(burst, last + n * 2 * KM) for n in (1, 2, 3)]
        second_half = [self.day.make_fix(burst, last + n * 2 * KM) for n in (4, 5, 6)]
        self.day.batch(first_half, arrives=self.day.at(14, 17, 44))
        self.day.batch(second_half, arrives=self.day.at(14, 18, 4))
        self.day.fix(self.day.at(14, 20), last + 13 * KM)
        readings = self.day.run(self)
        self.assertAlmostEqual(readings[-1][2], 11 + 13, delta=0.3)

        # And read in any order, the stored day gives the one answer.
        fixes = list(LocationPing.objects.filter(engineer=self.engineer))
        answers = set()
        for shuffled in (fixes, fixes[::-1], sorted(fixes, key=lambda p: p.latitude)):
            with mock.patch("django.utils.timezone.now", return_value=self.day.at(23)):
                answers.add(_trail_km(shuffled))
        self.assertEqual(answers, {readings[-1][2]})

    # -- a slow phone clock at Login ------------------------------------------

    def test_a_slow_clock_still_finds_the_login(self):
        """Three minutes slow: the first fix after the Login is stamped
        before it, and its arrival says which side of the Login it is on.

        It is on that side for every line it is in, not only the one into
        it: the drive to the Login is left out, and the road out of that
        first fix counts like any other.
        """
        self.day.login(self.day.at(9, 0))
        self.day.fix(self.day.at(12, 56, 50), 11.0, arrives=self.day.at(12, 59, 51))
        self.day.logout(self.day.at(13, 0))
        self.day.login(self.day.at(15, 0, 2))
        away = 11.0 + 20 * KM
        self.day.fix(self.day.at(14, 57, 20), away, arrives=self.day.at(15, 0, 21))
        self.day.fix(self.day.at(15, 2, 20), away + KM, arrives=self.day.at(15, 5, 21))
        self.day.fix(self.day.at(15, 7, 20), away + 2 * KM, arrives=self.day.at(15, 10, 21))
        readings = self.day.run(self)
        # Never the twenty kilometres to the Login, at any moment.
        self.assertLess(max(k for _, _, k in readings), 3)
        self.assertAlmostEqual(readings[-1][2], 2.0, delta=0.1)

    # -- a Logout and a quick Login again --------------------------------------

    def test_a_fix_held_from_before_a_quick_logout_never_takes_km_back(self):
        """Taken at 2:56 while the network was flaky, delivered at 3:01 --
        after a Logout at 2:59 and a Login at 3:00. It was taken on the
        morning's duty, so it is no first fix after the Login: the lines
        either side of it are the morning's road, and the four kilometres
        shown before it landed stay."""
        self.day.login(self.day.at(9, 0))
        self.day.fix(self.day.at(14, 50), 11.0)
        held = self.day.make_fix(self.day.at(14, 56), 11.0 + 3 * KM)
        self.day.fix(self.day.at(14, 58), 11.0 + 4 * KM)
        self.day.logout(self.day.at(14, 59))
        self.day.login(self.day.at(15, 0, 2))
        self.day.batch([held], arrives=self.day.at(15, 1))
        readings = self.day.run(self)
        self.assertAlmostEqual(readings[-1][2], 4.0, delta=0.1)

    def test_the_last_stretch_before_a_quick_logout_counts_when_it_lands_after_the_login(self):
        self.day.login(self.day.at(9, 0))
        self.day.fix(self.day.at(14, 50), 11.0)
        held = [
            self.day.make_fix(self.day.at(14, 56), 11.0 + 3 * KM),
            self.day.make_fix(self.day.at(14, 58), 11.0 + 4 * KM),
        ]
        self.day.logout(self.day.at(14, 59))
        self.day.login(self.day.at(15, 0, 2))
        self.day.batch(held, arrives=self.day.at(15, 1))
        readings = self.day.run(self)
        self.assertAlmostEqual(readings[-1][2], 4.0, delta=0.1)

    def test_fixes_sent_before_a_login_are_never_taken_back_by_it(self):
        """A phone a minute fast stamps its last fixes after the Logout they
        were sent before. Only what arrived after a Login can be the first
        fix after it, so the Login takes nothing back."""
        self.day.login(self.day.at(9, 0))
        for step, minute in enumerate((57, 58, 59)):
            self.day.fix(
                self.day.at(14, minute, 30),
                11.0 + step * KM,
                arrives=self.day.at(14, minute - 1, 30),
            )
        self.day.logout(self.day.at(14, 59))
        self.day.login(self.day.at(15, 0, 2))
        readings = self.day.run(self)
        self.assertAlmostEqual(readings[-1][2], 2.0, delta=0.1)

    # -- a Login where the Logout was ------------------------------------------

    def test_a_login_where_the_logout_was_still_counts_the_road_after_it(self):
        """Logout at the office at one, Login there again at two, one fix a
        few metres away and then nothing: the phone closed the app. Reopened
        thirty kilometres on, at a customer. The Login was where the Logout
        was, so the drive to it is nothing, and the thirty kilometres were
        driven on duty -- counted as the straight line, like any app the
        phone closed."""
        self.day.login(self.day.at(9, 0))
        self.day.fix(self.day.at(12, 58), 11.0 - KM)
        self.day.fix(self.day.at(12, 59), 11.0)
        self.day.logout(self.day.at(13, 0))
        self.day.login(self.day.at(14, 0))
        self.day.fix(self.day.at(14, 0, 30), 11.00005)  # five metres: wander
        self.day.fix(self.day.at(15, 0), 11.0 + 30 * KM)
        self.day.fix(self.day.at(15, 1), 11.0 + 31 * KM)
        readings = self.day.run(self)
        self.assertAlmostEqual(readings[-1][2], 1 + 30 + 1, delta=0.1)

    # -- the cutover -----------------------------------------------------------

    def test_a_flag_landing_after_the_cutover_never_takes_back_a_line_counted_before_it(self):
        """The two fixes either side were here, and counted, before the new
        rule began; a flagged fix taken between them was held on an old
        phone and lands after. Word that turned up after the line was
        counted cannot take it back, under either rule."""
        with mock.patch.dict(os.environ, {"KM_RULE_FROM": self.day.at(12).isoformat()}):
            self.day.login(self.day.at(9, 0))
            self.day.fix(self.day.at(11, 0), 11.0)
            self.day.fix(self.day.at(11, 30), 11.0 + 5 * KM)
            flagged = self.day.make_fix(
                self.day.at(11, 15), 11.0 + 2 * KM, after_gap=True, accuracy=500
            )
            self.day.batch([flagged], arrives=self.day.at(12, 30))
            readings = self.day.run(self)
        self.assertAlmostEqual(readings[-1][2], 5.0, delta=0.1)


class ParityTests(TestCase):
    """The engineer's phone, the board and the day panel say one number.

    A day with a Logout and a Login, Location switched off and back, and a
    late batch -- every rule the kilometres have, at once.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.engineer = make_engineer("parity", "Parity Prabhu")
        cls.office = User.objects.create_user(username="parity-office", password="x", role="admin")

    def setUp(self):
        patcher = mock.patch.dict(os.environ, NEW_RULE)
        patcher.start()
        self.addCleanup(patcher.stop)
        # manage.py loads the real Ola key; the day view must not spend it
        # snapping or naming a test's places.
        ola = mock.patch("cases.olamaps.is_configured", return_value=False)
        ola.start()
        self.addCleanup(ola.stop)

    def test_roster_day_and_duty_agree(self):
        day = KmReplay(self.engineer, self.user)
        day.login(day.at(9, 0))
        for step, minute in enumerate(range(5, 61, 5)):
            day.fix(day.at(9, 0) + datetime.timedelta(minutes=minute), 11.0 + step * KM)
        day.event("gps_off", day.at(10, 2), detail="switched")
        day.fix(day.at(10, 40), 11.0 + 20 * KM, after_gap=True)
        day.event("gps_on", day.at(10, 39), arrives=day.at(10, 40))
        held = [day.make_fix(day.at(10, 45 + n), 11.0 + (21 + n) * KM) for n in range(5)]
        day.fix(day.at(10, 55), 11.0 + 27 * KM)
        day.batch(held, arrives=day.at(11, 5))
        day.fix(day.at(12, 0), 11.0 + 28 * KM)
        day.logout(day.at(12, 1))
        day.login(day.at(15, 0))
        day.fix(day.at(15, 1), 11.5, after_gap=True)
        day.fix(day.at(15, 6), 11.5 + KM)
        readings = day.run(self)

        office = APIClient()
        office.force_authenticate(self.office)
        with mock.patch("django.utils.timezone.now", return_value=day.at(23)):
            duty = day.phone.get("/api/tracking/duty/").data["today_km"]
            roster = office.get("/api/tracking/roster/", {"date": DAY.isoformat()}).data
            panel = office.get(
                "/api/tracking/day/", {"engineer": self.engineer.id, "date": DAY.isoformat()}
            ).data

        board = next(row for row in roster if row["engineer_id"] == self.engineer.id)
        self.assertEqual(board["distance_km"], duty)
        self.assertEqual(panel["total_km"], duty)
        self.assertEqual(readings[-1][2], duty)
        # Eleven kilometres of morning, the batch's road and the hour after
        # Location came back (eight), one after the Login. Not the twenty
        # with Location off, nor the drive to the second Login.
        self.assertAlmostEqual(duty, 11 + 8 + 1, delta=0.3)
