from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_comparison_model_used"),
    ]

    operations = [
        migrations.AddField(
            model_name="author",
            name="mlaib_record_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="author",
            name="mlaib_elo",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="work",
            name="mlaib_record_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="work",
            name="mlaib_elo",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
