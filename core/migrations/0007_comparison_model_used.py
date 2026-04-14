from django.db import migrations, models
import datetime


def backfill_model_used(apps, schema_editor):
    Comparison = apps.get_model("core", "Comparison")
    today = datetime.date(2026, 4, 14)
    Comparison.objects.filter(created_at__date=today).update(model_used="gpt-5.4-nano")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_add_llmmatchup"),
    ]

    operations = [
        migrations.AddField(
            model_name="comparison",
            name="model_used",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.RunPython(backfill_model_used, migrations.RunPython.noop),
    ]
