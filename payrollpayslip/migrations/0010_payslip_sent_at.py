"""A payslip is private until somebody sends it.

Generating one used to publish it: the month payroll ran, every employee could
open a slip nobody had checked. `sent_at` is when the office released it, and
null until they do.

Every slip that ALREADY exists is stamped as sent. They are visible to their
employees today, and this migration must not take away last month's payslip
from somebody who has already read it. Only slips generated from here on start
private.
"""

from django.db import migrations, models
from django.utils import timezone


def mark_existing_as_sent(apps, schema_editor):
    Payslip = apps.get_model("payrollpayslip", "Payslip")
    Payslip.objects.filter(sent_at__isnull=True).update(sent_at=timezone.now())


def unmark(apps, schema_editor):
    """Reversing this puts the column back to null, which the field drop wants.

    Nothing is lost by it: the field itself goes with the reverse of the
    AddField below.
    """
    Payslip = apps.get_model("payrollpayslip", "Payslip")
    Payslip.objects.update(sent_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("payrollpayslip", "0009_payslip_special_work_days_payslip_special_work_pay"),
    ]

    operations = [
        migrations.AddField(
            model_name="payslip",
            name="sent_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(mark_existing_as_sent, unmark),
    ]
