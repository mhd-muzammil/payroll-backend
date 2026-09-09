"""Every trip already punched gets its row.

The day board reads a case's punch times to show where somebody was, and from
now on it reads them out of CaseVisit -- because a second dispatch of the same
call overwrites the columns on the Case. Without this, every arrival and
departure recorded before today would have no visit row to be read from, and
the first re-dispatch of an old call would take that day's entries with it.

One row per case that has been checked into or out of, dated by the punch
itself (local time -- a punch at 00:30 IST belongs to that day, not to the
previous one in UTC). Cases nobody ever punched get no row: there is nothing to
record, and their columns are still read directly.

Reversing this deletes only the rows it created. It cannot be told apart from a
visit written since by hand, so it deletes every visit whose times match its
case exactly, and leaves anything else alone.
"""

from django.db import migrations


def visits_for_existing_punches(apps, schema_editor):
    Case = apps.get_model("cases", "Case")
    CaseVisit = apps.get_model("cases", "CaseVisit")

    # Local dates, the way every reader of these stamps groups them. The
    # migration runs under the project's timezone, so localtime() here is IST.
    from django.utils import timezone

    rows = []
    punched = Case.objects.exclude(reached_at=None, completed_at=None).only(
        "id",
        "assigned_to_id",
        "assigned_at",
        "reached_at",
        "completed_at",
        "punch_in_lat",
        "punch_in_lon",
        "punch_in_accuracy",
        "punch_out_lat",
        "punch_out_lon",
        "punch_out_accuracy",
        "resolution_notes",
    )
    for case in punched.iterator(chunk_size=500):
        # The day of the arrival, or of the departure when there is no arrival
        # (an older case moved by the four-step buttons could be completed
        # without a reached stamp).
        anchor = case.reached_at or case.completed_at
        day = timezone.localtime(anchor).date()
        rows.append(
            CaseVisit(
                case_id=case.id,
                engineer_id=case.assigned_to_id,
                plan_date=day,
                assigned_at=case.assigned_at,
                checked_in_at=case.reached_at,
                checked_out_at=case.completed_at,
                punch_in_lat=case.punch_in_lat,
                punch_in_lon=case.punch_in_lon,
                punch_in_accuracy=case.punch_in_accuracy,
                punch_out_lat=case.punch_out_lat,
                punch_out_lon=case.punch_out_lon,
                punch_out_accuracy=case.punch_out_accuracy,
                resolution_notes=case.resolution_notes or "",
            )
        )
        if len(rows) >= 500:
            CaseVisit.objects.bulk_create(rows, ignore_conflicts=True)
            rows = []
    if rows:
        CaseVisit.objects.bulk_create(rows, ignore_conflicts=True)


def remove_the_rows_this_made(apps, schema_editor):
    """Only the ones that still say exactly what their case says."""
    CaseVisit = apps.get_model("cases", "CaseVisit")
    for visit in CaseVisit.objects.select_related("case").iterator(chunk_size=500):
        case = visit.case
        if (
            visit.checked_in_at == case.reached_at
            and visit.checked_out_at == case.completed_at
        ):
            visit.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0016_casevisit"),
    ]

    operations = [
        migrations.RunPython(visits_for_existing_punches, remove_the_rows_this_made),
    ]
