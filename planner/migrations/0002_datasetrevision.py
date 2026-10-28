import uuid

from django.db import migrations, models
from django.db.models import Count, Q


def initialize_revision(apps, schema_editor):
    Station = apps.get_model("planner", "Station")
    DatasetRevision = apps.get_model("planner", "DatasetRevision")
    alias = schema_editor.connection.alias
    counts = Station.objects.using(alias).aggregate(
        total=Count("pk"),
        located=Count("pk", filter=Q(latitude__isnull=False, longitude__isnull=False)),
    )
    DatasetRevision.objects.using(alias).create(pk=1, station_count=counts["total"], located_count=counts["located"])


class Migration(migrations.Migration):
    dependencies = [("planner", "0001_initial")]
    operations = [
        migrations.CreateModel(
            name="DatasetRevision",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("generation", models.PositiveBigIntegerField(default=0)),
                ("station_count", models.PositiveIntegerField(default=0)),
                ("located_count", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"constraints": [
                models.CheckConstraint(condition=models.Q(id=1), name="single_dataset_revision"),
                models.CheckConstraint(condition=models.Q(located_count__lte=models.F("station_count")), name="located_count_within_total"),
            ]},
        ),
        migrations.RunPython(initialize_revision, migrations.RunPython.noop),
        migrations.RemoveIndex(model_name="station", name="station_location_idx"),
        migrations.AddIndex(model_name="station", index=models.Index(fields=["id"], condition=models.Q(latitude__isnull=False, longitude__isnull=False), name="located_station_idx")),
    ]
