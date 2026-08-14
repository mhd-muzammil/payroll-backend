from django.db import migrations

# An earlier release added Case.completion_source and was then reverted: the model
# field and its migration were removed, but production had already applied it, so
# the COLUMN survived as NOT NULL with no database default.
#
# Django only names the columns its model knows about, so every INSERT into
# cases_case omitted this one and Postgres rejected the row:
#
#   NotNullViolation: null value in column "completion_source" violates
#   not-null constraint
#
# That took bulk_dispatch down completely — every sync returned 500 and engineers
# saw no cases at all.
#
# The constraint is RELEASED rather than the column dropped: releasing it fixes
# inserts immediately and keeps whatever was recorded, so nothing is destroyed
# over a mistake in migration housekeeping. A default is set too, so the column
# fills itself in for any writer that does not mention it.


def release_completion_source(apps, schema_editor):
    """Only where the column can exist: it was only ever created on Postgres.

    Written defensively because this migration's whole reason for existing is a
    database that disagreed with the migration history — so it must not assume
    what it will find.
    """
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'cases_case' AND column_name = 'completion_source'
            """
        )
        if cursor.fetchone() is None:
            return
        cursor.execute("ALTER TABLE cases_case ALTER COLUMN completion_source SET DEFAULT ''")
        cursor.execute("ALTER TABLE cases_case ALTER COLUMN completion_source DROP NOT NULL")
        cursor.execute(
            "UPDATE cases_case SET completion_source = '' WHERE completion_source IS NULL"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0007_case_plan_date"),
    ]

    operations = [
        # Reversing would restore the outage, so there is nothing to undo.
        migrations.RunPython(release_completion_source, migrations.RunPython.noop),
    ]
