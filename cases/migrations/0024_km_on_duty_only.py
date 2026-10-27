"""Kilometres only on duty, from the moment this was applied.

Nothing changes in the database. The time this migration was applied is the
duty rule's cutover (cases/km_rule.py, duty_cutover), the way 0019's is the
Login rule's. Never revert it: applied again later, it would move the cutover
and read days already closed a second time.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0023_importedkmday'),
    ]

    operations = []
