"""Which straight lines between two fixes count as kilometres.

Kilometres are never stored. Every screen works them out again from the fixes
each time it is read (views._trail_km), so changing the rule changes every day
ever recorded -- including bill cycles people have already been paid on. That
is why there are two rules here and a moment between them.

THE OLD RULE. A fix carrying `after_gap` was the first after the phone stopped
tracking, and the one straight line ending at it was skipped. It skipped too
much: the app flagged every reload and every restart Android made of the
tracker, so an engineer the phone closed the app on lost the road they were
carried along. It also skipped too little, because the flag was often lost on
the way, and a drive from Logout to the next Login then counted as work. And it
could take back what it had already counted: a flagged fix arriving late,
behind newer ones, turned a counted line into a skipped one -- a day replayed
from production went from 30.34 km to 23.35 that way.

THE NEW RULE counts every straight line except two.

  LOGIN. A line from a fix before a Login to a fix after it is the trip to
  work, not work. Read off the session row rather than a flag: the row is
  written before any fix after the Login can arrive, so a line this skips is
  skipped from the moment it exists, and one it counts is never taken back.
  Each fix is on one side of each Login, the same side for every line it is
  in -- a fix read as after the Login going in and before it coming out took
  back the line out of a fix held over a quick Logout and Login. When a fix
  after the Login lies where the line starts, the Login was there and the
  drive to it was nothing: the line counts from that fix.

  LOCATION SWITCHED OFF. The engineer turned it off, and the user's rule is
  that only that stretch goes ("gps off panna vidu, matha ellam aaganum").
  Skipped only when the phone said so in time: its gps_off was here before the
  first fix after it arrived, and that fix arrived before every later one.
  Word that turns up after a line was counted cannot take it back.

Everything else is counted as the straight line, the least they can have
travelled: a restart, the app closed, the network gone, Location found already
off when the app opened.

The CUTOVER is when the new rule began: KM_RULE_FROM when the environment says,
otherwise the moment migration 0019 was applied. A line both of whose fixes had
arrived before it is read the old way, byte for byte, so a closed day keeps the
number it closed on -- from the flags that had arrived before it too: a flag
an old phone delivers after the cutover would take back a line counted
before it.
"""

import datetime
import logging
import os
from bisect import bisect_left, bisect_right
from collections import defaultdict

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import DutySession, PhoneEvent

logger = logging.getLogger(__name__)

# The migration that brought the new rule in. When it was applied is the
# cutover, unless KM_RULE_FROM says otherwise.
RULE_MIGRATION = ("cases", "0019_phoneevent_dutysession_last_contact_at")

# How far a phone's clock may run either way of ours before a Login stops
# recognising its first fix. pings.py refuses a fix more than this far ahead.
CLOCK_SKEW = datetime.timedelta(minutes=5)

# A fix from before received_at existed cannot say when it arrived. Two minutes
# past when it was taken stands in for it -- the same "it was not held" line
# QUEUED_THRESHOLD_MINUTES draws.
UNRECORDED_ARRIVAL = datetime.timedelta(minutes=2)

# How far either side of the fixes Logins and events are loaded. Wider than
# any skew the rule allows for, so nothing it could use is ever left out.
LOAD_MARGIN = datetime.timedelta(minutes=10)

# What the km rule reads. The board also needs gps_on, to know when GPS came
# back; asking for both in one query costs nothing.
RULE_EVENT_KINDS = ("gps_off", "tracker_stopped")
STATE_EVENT_KINDS = ("gps_off", "gps_on", "tracker_stopped")

# The migration's applied time, once found. A value that was not there yet is
# not kept, so a process started before migrate ran still finds it later.
_applied = None


def trail_order(ping):
    """The one order a trail is read in: time, then id.

    Time alone left ties to the database. A phone that delivers a held stretch
    stamps it with the moment the WebView woke up, so a dozen kilometres can
    share one second, and two reads of the same rows put them in two orders
    and got two distances. The id breaks the tie the same way every time.
    """
    return (ping.timestamp, getattr(ping, "id", None) or 0)


def arrival(item):
    """When we got it. A row from before received_at existed answers with when
    it was taken."""
    received = getattr(item, "received_at", None)
    return received if received is not None else item.timestamp


def _parse_moment(raw):
    try:
        when = parse_datetime(raw)
        if when is None:
            day = parse_date(raw)
            if day is None:
                return None
            when = datetime.datetime.combine(day, datetime.time.min)
    except ValueError:
        return None
    if timezone.is_naive(when):
        when = timezone.make_aware(when)
    return when


def _migration_applied():
    global _applied
    if _applied is None:
        from django.db.migrations.recorder import MigrationRecorder

        app, name = RULE_MIGRATION
        _applied = (
            MigrationRecorder.Migration.objects.filter(app=app, name=name)
            .values_list("applied", flat=True)
            .first()
        )
    return _applied


def rule_cutover():
    """The moment the new rule began, or None for the new rule everywhere."""
    raw = (os.environ.get("KM_RULE_FROM") or "").strip()
    if raw:
        when = _parse_moment(raw)
        if when is not None:
            return when
        # A typo must not quietly move every closed day onto the new rule.
        logger.warning("KM_RULE_FROM=%r is not an ISO date or time; using migration 0019's", raw)
    return _migration_applied()


def day_bounds(day):
    """The first and the first-after instants of a local calendar day."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min), tz)
    end = timezone.make_aware(
        datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time.min), tz
    )
    return start, end


class TrailContext:
    """What the rule needs besides the fixes: the engineer's Logins, what the
    phone said about Location and about the app being stopped, and where the
    cutover falls.

    Loaded once per request for every engineer on it (load_contexts), so a
    board of thirty engineers costs two queries and not sixty. _trail_km loads
    one itself when nobody hands it one.
    """

    def __init__(self, sessions=(), events=(), cutover=None):
        self.sessions = list(sessions)
        self.events = list(events)
        self.cutover = cutover


def _load(engineer_ids, since, until, kinds):
    sessions = defaultdict(list)
    events = defaultdict(list)
    ids = list(engineer_ids)
    if not ids:
        return sessions, events
    early, late = since - LOAD_MARGIN, until + LOAD_MARGIN
    # Every session that overlaps the span, and every one that began in it
    # however it ended: a Login is what the rule looks for.
    for session in (
        DutySession.objects.filter(engineer_id__in=ids, started_at__lte=late)
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gte=early) | Q(started_at__gte=early))
        .order_by("started_at", "id")
    ):
        sessions[session.engineer_id].append(session)
    found = PhoneEvent.objects.filter(engineer_id__in=ids, at__gte=early, at__lte=late)
    if kinds is not None:
        found = found.filter(kind__in=kinds)
    for event in found.order_by("at", "id"):
        events[event.engineer_id].append(event)
    return sessions, events


def load_contexts(engineer_ids, since, until, kinds=RULE_EVENT_KINDS):
    """{engineer_id: TrailContext} for fixes between `since` and `until`.

    Two queries whatever the number of engineers. `kinds` widens the events
    for a caller that reads them for something else as well; None is every
    kind.
    """
    cutover = rule_cutover()
    ids = list(engineer_ids)
    sessions, events = _load(ids, since, until, kinds)
    return {
        engineer_id: TrailContext(sessions[engineer_id], events[engineer_id], cutover)
        for engineer_id in ids
    }


def context_for(pings):
    """The context for fixes nobody loaded one for, read off their own span."""
    pings = list(pings)
    if not pings:
        return TrailContext(cutover=rule_cutover())
    moments = [p.timestamp for p in pings] + [p.received_at for p in pings if p.received_at]
    sessions, events = _load(
        {p.engineer_id for p in pings}, min(moments), max(moments), RULE_EVENT_KINDS
    )
    return TrailContext(
        [s for found in sessions.values() for s in found],
        [e for found in events.values() for e in found],
        rule_cutover(),
    )


class SegmentRule:
    """Whether the straight line between two neighbouring kept fixes counts.

    Built once per trail from the FULL list of fixes, not only the ones the
    accuracy and wander filters keep: the evidence about a stretch -- the
    flag, the first fix after Location came back -- is often on a fix those
    filters throw away.
    """

    def __init__(self, pings, context):
        pings = sorted(pings, key=trail_order)
        self.cutover = context.cutover

        # The old rule's evidence, read exactly as it always was: the flags of
        # ALL the fixes, since the first fix after the GPS wakes is the noisiest
        # of the day and the accuracy filter drops it; and by id as well as by
        # time, since a flagged fix sharing its neighbour's second is not
        # strictly after it. Only the flags that had arrived before the
        # cutover, though: the old rule reads lines that were counted before
        # it, and a flag an old phone delivers after it would take one back.
        flags = [
            p
            for p in pings
            if getattr(p, "after_gap", False)
            and (self.cutover is None or arrival(p) < self.cutover)
        ]
        self._resumed_at = [p.timestamp for p in flags]
        self._resumed_ids = {p.id for p in flags if p.id}

        # Each Login, with the latest Logout before it: a fix taken before
        # that Logout was taken on the duty it ended, however late it arrived.
        sessions = defaultdict(list)
        for session in context.sessions:
            sessions[session.engineer_id].append(session)
        self._logins = defaultdict(list)
        for engineer_id, found in sessions.items():
            found.sort(key=lambda s: (s.started_at, s.id or 0))
            for index, session in enumerate(found):
                ended = [
                    earlier.ended_at
                    for earlier in found[:index]
                    if earlier.ended_at is not None and earlier.ended_at <= session.started_at
                ]
                self._logins[engineer_id].append(
                    (session.started_at, max(ended) if ended else None)
                )

        self._stopped = defaultdict(list)
        switched = defaultdict(list)
        for event in context.events:
            if event.kind == "tracker_stopped":
                self._stopped[event.engineer_id].append(event.at)
            elif event.kind == "gps_off" and (event.detail or "").strip() == "switched":
                switched[event.engineer_id].append(event)
        for moments in self._stopped.values():
            moments.sort()

        by_engineer = defaultdict(list)
        for ping in pings:
            by_engineer[ping.engineer_id].append(ping)

        # The switch-offs the phone reported in time, per engineer, by when.
        self._switched_off = defaultdict(list)
        for engineer_id, events in switched.items():
            fixes = by_engineer.get(engineer_id, [])
            stamps = [p.timestamp for p in fixes]
            # The earliest arrival among each fix and every fix after it.
            earliest = [None] * len(fixes)
            running = None
            for index in range(len(fixes) - 1, -1, -1):
                arrived = arrival(fixes[index])
                running = arrived if running is None or arrived < running else running
                earliest[index] = running
            for event in events:
                first = bisect_right(stamps, event.at)
                if first == len(fixes):
                    continue  # nothing after it yet, so no line to skip either
                came = arrival(fixes[first])
                # Said before the first fix after it arrived, and that fix
                # arrived no later than any fix after it. Otherwise a line
                # across the stretch was on a screen as counted at some moment,
                # and taking it back now is the drop the engineer would see.
                if event.received_at <= came and came <= earliest[first]:
                    self._switched_off[engineer_id].append(event.at)
            self._switched_off[engineer_id].sort()

    def _reads_the_old_way(self, prev, cur):
        return self.cutover is not None and max(arrival(prev), arrival(cur)) < self.cutover

    def skips(self, prev, cur):
        """Whether the straight line from prev to cur does not count as it
        stands. Across a Login part of it may: counted_from says which."""
        if self._reads_the_old_way(prev, cur):
            return self._flagged_between(prev, cur)
        return self.crosses_login(prev, cur) or self._location_switched_off(prev, cur)

    def counted_from(self, prev, cur, usable):
        """The fix the counted straight line into `cur` starts from, or None
        when none of the line from `prev` counts.

        `prev` for a line that counts whole. For one across a Login, the
        first of `usable` (the caller's fixes accurate enough to place
        somebody, in trail order) between the two that is after the Login:
        the wander filter keeps no fix within a few metres of `prev`, so a
        Login at the place of the Logout leaves the line from the Logout to
        wherever the app next woke up as all there is -- and the Login was
        where that fix is, not somewhere along the way. With no such fix the
        Login could have been anywhere on the line, and none of it counts.
        """
        if self._reads_the_old_way(prev, cur):
            return None if self._flagged_between(prev, cur) else prev
        login = self._crossed_login(prev, cur)
        if login is None:
            return None if self._location_switched_off(prev, cur) else prev
        low, high = trail_order(prev), trail_order(cur)
        for fix in usable:
            key = trail_order(fix)
            if key <= low or fix.engineer_id != cur.engineer_id:
                continue
            if key >= high:
                break
            if self._after(fix, login):
                return None if self._location_switched_off(fix, cur) else fix
        return None

    def _flagged_between(self, prev, cur):
        return cur.id in self._resumed_ids or any(
            prev.timestamp < when <= cur.timestamp for when in self._resumed_at
        )

    @staticmethod
    def _after(fix, login):
        """Whether the fix was taken after this Login.

        Received once it had begun -- for a row with no received_at, taken no
        more than two minutes before it -- taken no more than CLOCK_SKEW
        before it, since a phone running slow stamps its first fix before the
        Login; and taken after the Logout before it, since a fix held from
        the duty that Logout ended is not the first fix after the next Login,
        however late it arrives.
        """
        started, ended_before = login
        reached = (
            fix.received_at
            if fix.received_at is not None
            else fix.timestamp + UNRECORDED_ARRIVAL
        )
        return (
            reached >= started
            and fix.timestamp >= started - CLOCK_SKEW
            and (ended_before is None or fix.timestamp > ended_before)
        )

    def _crossed_login(self, prev, cur):
        """The latest Login with `cur` after it and `prev` not, or None."""
        crossed = None
        for login in self._logins.get(cur.engineer_id, ()):
            if self._after(cur, login) and not self._after(prev, login):
                crossed = login
        return crossed

    def crosses_login(self, prev, cur):
        """Whether a Login falls between these two fixes: one taken after it
        and one not, each judged the same way whichever end of a line it is."""
        return self._crossed_login(prev, cur) is not None

    def _location_switched_off(self, prev, cur):
        moments = self._switched_off.get(cur.engineer_id)
        if not moments:
            return False
        index = bisect_left(moments, prev.timestamp)
        # Switched off at or after the fix before the stretch, and before the
        # fix after it.
        if index == len(moments) or moments[index] >= cur.timestamp:
            return False
        off = moments[index]
        # When the phone had stopped the app before the switch-off, the silence
        # began with the app, not with Location: the road before the switch-off
        # went unwatched by a stopped app, and counts as any such stretch does.
        # A later switch-off in the same stretch has that stop before it too.
        stopped = self._stopped.get(cur.engineer_id, [])
        found = bisect_left(stopped, prev.timestamp)
        return not (found < len(stopped) and stopped[found] <= off)
