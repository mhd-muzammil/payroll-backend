from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('employees', '0009_task_activity_log_task_checklist'),
    ]

    operations = [
        migrations.AddField(
            model_name='employee',
            name='emp_code',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
