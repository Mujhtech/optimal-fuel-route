from uuid import uuid4

from django.db import models, router, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


class Station(models.Model):
    external_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    price_per_gallon = models.DecimalField(max_digits=10, decimal_places=8)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    location_source = models.CharField(max_length=500, blank=True)
    geocoding_attempted = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["id"], condition=models.Q(latitude__isnull=False, longitude__isnull=False), name="located_station_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(price_per_gallon__gt=0), name="positive_fuel_price"),
            models.CheckConstraint(
                condition=(models.Q(latitude__isnull=True, longitude__isnull=True)
                           | models.Q(latitude__isnull=False, longitude__isnull=False)),
                name="coordinate_pair_required",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"

    def save(self, *args, **kwargs):
        # Keep a normal ORM save and its revision invalidation in the same
        # transaction. Imports use bulk operations and publish one revision.
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            return super().save(*args, **kwargs)


class DatasetRevision(models.Model):
    """One small row changes whenever route-relevant station data changes."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    token = models.UUIDField(default=uuid4, editable=False)
    generation = models.PositiveBigIntegerField(default=0)
    station_count = models.PositiveIntegerField(default=0)
    located_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="single_dataset_revision"),
            models.CheckConstraint(condition=models.Q(located_count__lte=models.F("station_count")), name="located_count_within_total"),
        ]


@receiver(post_save, sender=Station)
@receiver(post_delete, sender=Station)
def station_changed(sender, using, **kwargs):
    # Bulk imports bypass signals deliberately, then call this helper once.
    from planner.data.revision import bump_revision

    bump_revision(using=using)
