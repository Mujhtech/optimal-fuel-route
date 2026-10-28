from decimal import Decimal

from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from planner.data.revision import bump_revision, get_dataset_revision
from planner.models import DatasetRevision, Station


class DatasetRevisionTests(TestCase):
    def station(self, external_id="1", located=False):
        return Station.objects.create(
            external_id=external_id, name="Test Fuel", address="123 Main St",
            city="Austin", state="TX", price_per_gallon=Decimal("3.25"),
            latitude=30.2672 if located else None, longitude=-97.7431 if located else None,
        )

    def test_warm_revision_read_is_one_primary_key_query(self):
        before = get_dataset_revision()
        with CaptureQueriesContext(connection) as queries:
            after = get_dataset_revision()
        self.assertEqual(before, after)
        self.assertEqual(len(queries), 1)
        self.assertIn("planner_datasetrevision", queries[0]["sql"])
        self.assertNotIn("planner_station", queries[0]["sql"])

    def test_normal_saves_and_deletes_publish_token_and_counts(self):
        initial = get_dataset_revision()
        first = self.station(located=True)
        one = get_dataset_revision()
        self.assertNotEqual(one.token, initial.token)
        self.assertEqual((one.station_count, one.located_count), (1, 1))
        second = self.station("2")
        two = get_dataset_revision()
        self.assertNotEqual(two.token, one.token)
        self.assertEqual((two.station_count, two.located_count), (2, 1))
        first.price_per_gallon = Decimal("3.00")
        first.save(update_fields=["price_per_gallon", "updated_at"])
        changed = get_dataset_revision()
        self.assertNotEqual(changed.token, two.token)
        self.assertEqual((changed.station_count, changed.located_count), (2, 1))
        second.delete()
        deleted = get_dataset_revision()
        self.assertNotEqual(deleted.token, changed.token)
        self.assertEqual((deleted.station_count, deleted.located_count), (1, 1))

    def test_bulk_change_is_published_once_in_same_transaction(self):
        before = DatasetRevision.objects.get(pk=1).generation
        with transaction.atomic():
            Station.objects.bulk_create([
                Station(external_id=str(i), name="Test", address="123 Main St", city="Austin",
                        state="TX", price_per_gallon=3) for i in range(10)
            ])
            snapshot = bump_revision()
        self.assertEqual(DatasetRevision.objects.get(pk=1).generation, before + 1)
        self.assertEqual((snapshot.station_count, snapshot.located_count), (10, 0))

    def test_rolled_back_station_change_restores_revision(self):
        before = get_dataset_revision()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.station(located=True)
                self.assertNotEqual(get_dataset_revision().token, before.token)
                raise RuntimeError("rollback")
        self.assertEqual(get_dataset_revision(), before)
        self.assertEqual(Station.objects.count(), 0)

    def test_missing_revision_initializes_existing_bulk_data(self):
        self.station(located=True)
        DatasetRevision.objects.all().delete()
        snapshot = get_dataset_revision()
        self.assertEqual((snapshot.station_count, snapshot.located_count), (1, 1))
        self.assertEqual(DatasetRevision.objects.count(), 1)
        with self.assertNumQueries(1):
            self.assertEqual(get_dataset_revision(), snapshot)

    def test_rolled_back_generation_does_not_reuse_process_cache_token(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.station("rolled-back")
                discarded = get_dataset_revision().token
                raise RuntimeError("rollback")
        self.station("committed")
        self.assertNotEqual(get_dataset_revision().token, discarded)
