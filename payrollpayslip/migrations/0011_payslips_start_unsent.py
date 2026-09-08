"""Nothing is sent until somebody sends it -- the old slips included.

0010 stamped every existing payslip as sent, so that nobody lost a month they
had already read. The office asked for the opposite: their words were that
slips they never sent are still sitting on the employee's screen, and that has
to stop. So every slip goes back to private and the Send button is the only
way onto an employee's page from here.

What this means, said plainly because it is not a small thing: the morning
after this deploys, every employee's Payslips page is EMPTY -- including months
they have already read -- until the office sends each one. Reversing the
migration does not undo it either; the dates are gone, not hidden.

Written as its own migration rather than by editing 0010, because 0010 may
already have run.
"""

from django.db import migrations


def unsend_everything(apps, schema_editor):
    Payslip = apps.get_model("payrollpayslip", "Payslip")
    Payslip.objects.exclude(sent_at__isnull=True).update(sent_at=None)


def noop(apps, schema_editor):
    """There is nothing to restore: the dates this cleared are not kept anywhere.

    Declared so the migration is reversible in the sense Django asks about --
    running backwards leaves the column exactly as this left it.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("payrollpayslip", "0010_payslip_sent_at"),
    ]

    operations = [
        migrations.RunPython(unsend_everything, noop),
    ]
