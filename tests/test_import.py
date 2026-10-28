import csv
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from planner.models import Station
from planner.data.importing import coordinate_pair
from planner.data.revision import get_dataset_revision
from planner.models import DatasetRevision


class FuelImportTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "prices.csv"

    def prices(self, rows):
        with self.path.open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Retail Price"])
            writer.writerows(rows)
        return self.path

    def test_duplicate_minimum_precise_price_and_canadian_filter(self):
        path = self.prices([
            ["1", "Pilot #1", "I-35 EXIT 1", "Austin", "TX", "3.12345678"],
            ["1", "Pilot alias", "I-35 EXIT 1", "Austin", "TX", "3.11345678"],
            ["2", "Canada", "Highway 1", "Toronto", "ON", "1.20"],
        ])
        call_command("import_fuel_prices", str(path), verbosity=0)
        call_command("import_fuel_prices", str(path), verbosity=0)
        self.assertEqual(Station.objects.count(), 1)
        station = Station.objects.get()
        self.assertEqual(station.price_per_gallon, Decimal("3.11345678"))
        self.assertIsNone(station.latitude)

    def test_invalid_row_does_not_partially_import(self):
        self.prices([
            ["1", "Valid", "Street 1", "Austin", "TX", "3.20"],
            ["2", "Invalid", "Street 2", "Austin", "TX", "NaN"],
        ])
        with self.assertRaises(CommandError):
            call_command("import_fuel_prices", str(self.path))
        self.assertEqual(Station.objects.count(), 0)

    def test_import_publishes_one_revision_and_noop_preserves_it(self):
        self.prices([[str(i), "Station", "Street 1", "Austin", "TX", "3.20"] for i in range(100)])
        before = DatasetRevision.objects.get(pk=1).generation
        call_command("import_fuel_prices", str(self.path), verbosity=0)
        imported = get_dataset_revision()
        self.assertEqual(imported.station_count, 100)
        self.assertEqual(DatasetRevision.objects.get(pk=1).generation, before + 1)
        call_command("import_fuel_prices", str(self.path), verbosity=0)
        self.assertEqual(get_dataset_revision(), imported)

    def test_failed_import_preserves_revision(self):
        before = get_dataset_revision()
        self.prices([["1", "Invalid", "Street 1", "Austin", "TX", "NaN"]])
        with self.assertRaises(CommandError):
            call_command("import_fuel_prices", str(self.path))
        self.assertEqual(get_dataset_revision(), before)

    def test_duplicate_identifier_with_different_location_is_rejected(self):
        self.prices([
            ["1", "Station", "Street 1", "Austin", "TX", "3.20"],
            ["1", "Station", "Street 2", "Dallas", "TX", "2.20"],
        ])
        with self.assertRaises(CommandError):
            call_command("import_fuel_prices", str(self.path))
        self.assertFalse(Station.objects.exists())

    def test_supplemental_coordinates_preserved_on_idempotent_import(self):
        self.prices([["1", "Pilot #1", "I-35 EXIT 1", "Austin", "TX", "3.20"]])
        locations = Path(self.directory.name) / "locations.csv"
        locations.write_text("external_id,latitude,longitude,source\n1,30.2672,-97.7431,verified source\n")
        call_command("import_fuel_prices", str(self.path), locations=str(locations), verbosity=0)
        first_updated = Station.objects.get().updated_at
        call_command("import_fuel_prices", str(self.path), verbosity=0)
        station = Station.objects.get()
        self.assertEqual((station.latitude, station.longitude), (30.2672, -97.7431))
        self.assertEqual(station.location_source, "verified source")
        self.assertEqual(station.updated_at, first_updated)

    def test_changed_address_invalidates_old_coordinates(self):
        Station.objects.create(external_id="1", name="Old", address="Old Address", city="Austin", state="TX", price_per_gallon="3.20", latitude=30.2, longitude=-97.7, location_source="old")
        self.prices([["1", "New", "New Address", "Austin", "TX", "3.20"]])
        call_command("import_fuel_prices", str(self.path), verbosity=0)
        station = Station.objects.get()
        self.assertIsNone(station.latitude)
        self.assertEqual(station.location_source, "")

    def test_infinite_coordinates_rejected_before_writes(self):
        self.prices([["1", "Station", "Address", "Austin", "TX", "3.20"]])
        locations = Path(self.directory.name) / "locations.csv"
        locations.write_text("external_id,latitude,longitude\n1,nan,-97.7\n")
        with self.assertRaises(CommandError):
            call_command("import_fuel_prices", str(self.path), locations=str(locations))
        self.assertFalse(Station.objects.exists())

    def test_canadian_supplement_rejected_even_with_us_station_state(self):
        self.prices([["1", "Station", "Address", "Austin", "TX", "3.20"]])
        locations = Path(self.directory.name) / "locations.csv"
        locations.write_text("external_id,latitude,longitude\n1,43.6532,-79.3832\n")
        with self.assertRaises(CommandError):
            call_command("import_fuel_prices", str(self.path), locations=str(locations))
        self.assertFalse(Station.objects.exists())

    def test_boolean_coordinates_are_not_treated_as_numbers(self):
        with self.assertRaises(CommandError):
            coordinate_pair(True, -97.7, "test")

    def test_actual_supplied_file_imports_all_unique_us_station_ids(self):
        source = Path(__file__).resolve().parents[1] / "fuel-prices-for-be-assessment.csv"
        if not source.exists():
            self.skipTest("Assignment source CSV is not present")
        call_command("import_fuel_prices", str(source), verbosity=0)
        self.assertGreater(Station.objects.count(), 5000)
        self.assertFalse(Station.objects.filter(state="ON").exists())
