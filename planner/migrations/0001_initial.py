from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Station",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=32, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("address", models.CharField(max_length=255)),
                ("city", models.CharField(max_length=128)),
                ("state", models.CharField(max_length=2)),
                ("price_per_gallon", models.DecimalField(decimal_places=8, max_digits=10)),
                ("latitude", models.FloatField(blank=True, null=True)),
                ("longitude", models.FloatField(blank=True, null=True)),
                ("location_source", models.CharField(blank=True, max_length=500)),
                ("geocoding_attempted", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [models.Index(fields=["latitude", "longitude"], name="station_location_idx")],
                "constraints": [
                    models.CheckConstraint(condition=models.Q(price_per_gallon__gt=0), name="positive_fuel_price"),
                    models.CheckConstraint(condition=(models.Q(latitude__isnull=True, longitude__isnull=True) | models.Q(latitude__isnull=False, longitude__isnull=False)), name="coordinate_pair_required"),
                ],
            },
        )
    ]
