from types import SimpleNamespace
from unittest import TestCase

from planner.data.osm_spatial import enrich_osm_places, match_named_stations


def polygon(properties, west=-100, south=40, east=-90, north=45):
    return {"type": "Feature", "properties": properties, "geometry": {"type": "Polygon", "coordinates": [
        [[west, south], [east, south], [east, north], [west, north], [west, south]],
    ]}}


class OsmSpatialTests(TestCase):
    def setUp(self):
        self.states = [polygon({"abbr": "IA"})]
        self.places = [polygon({"name": "Test City"}, -96, 41, -94, 43)]
        self.station = SimpleNamespace(external_id="123", name="Independent Truck Stop #2", city="Test City", state="IA")
        self.poi = {"type": "node", "id": 42, "lat": 42.1, "lon": -95.1,
                    "tags": {"amenity": "fuel", "name": "Independent Truckstop #2", "brand": "Shell"}}

    def enriched(self, elements=None):
        return enrich_osm_places(elements or [self.poi], self.states, self.places)

    def test_polygons_fill_missing_tags_without_moving_station(self):
        result = self.enriched()[0]
        self.assertEqual(result["tags"]["addr:state"], "IA")
        self.assertEqual(result["tags"]["addr:city"], "Test City")
        self.assertEqual((result["lat"], result["lon"]), (42.1, -95.1))
        self.assertNotIn("addr:state", self.poi["tags"])

    def test_conflicting_state_tag_is_rejected(self):
        self.poi["tags"]["addr:state"] = "NE"
        self.assertEqual(self.enriched(), [])

    def test_outside_country_is_rejected(self):
        self.poi["lat"] = 48
        self.assertEqual(self.enriched(), [])

    def test_exact_independent_name_survives_different_fuel_brand(self):
        match = match_named_stations([self.station], self.enriched())
        self.assertEqual(match["123"], (42.1, -95.1, "https://www.openstreetmap.org/node/42"))

    def test_distinct_pois_in_same_city_are_ambiguous(self):
        other = {**self.poi, "id": 43, "lon": -95.2}
        self.assertEqual(match_named_stations([self.station], self.enriched([self.poi, other])), {})

    def test_two_price_records_with_same_identity_are_ambiguous(self):
        other = SimpleNamespace(**{**vars(self.station), "external_id": "456"})
        self.assertEqual(match_named_stations([self.station, other], self.enriched()), {})

    def test_generic_names_are_not_joined_by_place_alone(self):
        self.station.name = "Shell"
        self.poi["tags"]["name"] = "Shell"
        self.assertEqual(match_named_stations([self.station], self.enriched()), {})

    def test_incompatible_store_numbers_are_rejected(self):
        self.poi["tags"]["ref"] = "3"
        self.assertEqual(match_named_stations([self.station], self.enriched()), {})

    def test_name_without_city_corroboration_is_not_matched(self):
        enriched = enrich_osm_places([self.poi], self.states)
        self.assertEqual(match_named_stations([self.station], enriched), {})

    def test_malformed_point_cannot_become_a_station_match(self):
        for latitude, longitude in ((True, -95.1), (float("nan"), -95.1), (43.6532, -79.3832)):
            with self.subTest(latitude=latitude, longitude=longitude):
                enriched = self.enriched()
                enriched[0]["lat"], enriched[0]["lon"] = latitude, longitude
                self.assertEqual(match_named_stations([self.station], enriched), {})
