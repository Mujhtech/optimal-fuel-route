import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.core.management import call_command

from planner.data.geocoding import write_coverage_report
from planner.data.importing import read_locations, read_prices
from planner.management.commands.geocode_stations import Command
from planner.models import Station
from planner.data.revision import get_dataset_revision


class CoverageReportTests(SimpleTestCase):
    def test_missing_locations_are_explicit_in_state_and_total_counts(self):
        stations = [SimpleNamespace(state="TX", latitude=30.2), SimpleNamespace(state="TX", latitude=None),
                    SimpleNamespace(state="OH", latitude=None)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            result = write_coverage_report(path, stations)
            self.assertEqual(result["total_stations"], 3)
            self.assertEqual(result["located_stations"], 1)
            self.assertEqual(result["unresolved_stations"], 2)
            self.assertEqual(result["coverage_percent"], 33.33)
            self.assertEqual(result["by_state"]["TX"], {"stations": 2, "located": 1, "unresolved": 1})
            self.assertEqual(json.loads(path.read_text()), result)

    def test_bundled_points_are_valid_us_locations_and_belong_to_price_file(self):
        prices, _, _ = read_prices(settings.BASE_DIR / "fuel-prices-for-be-assessment.csv")
        locations = read_locations(settings.BASE_DIR / "planner/data/station_locations.csv")
        self.assertTrue(locations)
        self.assertFalse(set(locations) - set(prices))
        self.assertTrue(all(source.startswith("https://") for _, _, source in locations.values()))

    def test_empty_dataset_has_a_defined_coverage_report(self):
        with tempfile.TemporaryDirectory() as directory:
            result = write_coverage_report(Path(directory) / "coverage.json", [])
        self.assertEqual(result["coverage_percent"], 0)
        self.assertEqual(result["total_stations"], 0)

    def test_geocoding_default_exports_are_inside_persistent_data_directory(self):
        parser = Command().create_parser("manage.py", "geocode_stations")
        options = vars(parser.parse_args([]))
        for field in ("output", "report", "coverage_report"):
            self.assertTrue(Path(options[field]).is_relative_to(settings.DATA_DIR))


class GeocodingRevisionTests(TestCase):
    def test_cached_geocoding_updates_revision_once_and_writes_completeness(self):
        Station.objects.create(external_id="1", name="PILOT #123", address="I-35 EXIT 1", city="Austin", state="TX", price_per_gallon="3.00000000")
        before = get_dataset_revision().token
        feature = {"id": "pilot123", "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
                   "properties": {"ref": "123", "brand": "Pilot", "amenity": "fuel",
                                  "addr:country": "US", "addr:state": "TX", "addr:city": "Austin"}}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "pilot_flying_j.geojson").write_text(json.dumps({"features": [feature]}))
            options = {"provider": "atp", "cache": directory, "spiders": "pilot_flying_j", "output": str(base / "points.csv"),
                       "report": str(base / "unresolved.csv"), "coverage_report": str(base / "coverage.json")}
            with patch("requests.get") as upstream:
                call_command("geocode_stations", **options)
                after = get_dataset_revision().token
                self.assertNotEqual(before, after)
                self.assertEqual(Station.objects.get(external_id="1").latitude, 30.2)
                self.assertEqual(json.loads((base / "coverage.json").read_text())["located_stations"], 1)
                call_command("geocode_stations", **options)
                self.assertEqual(get_dataset_revision().token, after)
                upstream.assert_not_called()
