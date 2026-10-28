"""Publish station-data changes once, then read cache generations in O(1).

Normal Station saves/deletes publish automatically. Code using bulk_create,
bulk_update, or QuerySet.update must call bump_revision in the same transaction
after route-relevant data changes. Imports and coordinate preparation do so.
"""

from dataclasses import dataclass
from uuid import uuid4

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from planner.models import DatasetRevision, Station


@dataclass(frozen=True)
class DatasetSnapshot:
    token: str
    station_count: int
    located_count: int


def _snapshot(record):
    return DatasetSnapshot(str(record["token"]), record["station_count"], record["located_count"])


def get_dataset_revision(using="default") -> DatasetSnapshot:
    """One indexed primary-key lookup in the normal request path."""
    try:
        record = DatasetRevision.objects.using(using).values("token", "station_count", "located_count").get(pk=1)
    except DatasetRevision.DoesNotExist:
        # Also supports a flushed database and an initial empty deployment.
        return bump_revision(using=using)
    return _snapshot(record)


def bump_revision(using="default") -> DatasetSnapshot:
    """Atomically publish a new token and counts after a batch of changes.

    Counts are computed only on writes. Lock the singleton before counting so
    concurrent PostgreSQL writers cannot overwrite counts from an older view.
    The UUID also prevents process-cache collisions between separate databases
    or transactions that roll back a numeric generation.
    """
    with transaction.atomic(using=using):
        revisions = DatasetRevision.objects.using(using)
        revisions.get_or_create(pk=1)
        revisions.select_for_update().get(pk=1)
        counts = Station.objects.using(using).aggregate(
            total=Count("pk"),
            located=Count("pk", filter=Q(latitude__isnull=False, longitude__isnull=False)),
        )
        token = uuid4()
        revisions.filter(pk=1).update(
            token=token,
            generation=F("generation") + 1,
            station_count=counts["total"],
            located_count=counts["located"],
            updated_at=timezone.now(),
        )
        return DatasetSnapshot(str(token), counts["total"], counts["located"])
