import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from planner.data.geocoding import brand, census_candidates, fetch_atp, match_atp, match_osm, nearby_postal_cities, normalize_city, parse_census, source_city


def station(station_id="1", name="Pilot #123", city="Austin", state="TX", address="I-35 EXIT 1"):
    return SimpleNamespace(external_id=station_id, name=name, city=city, state=state, address=address)


def poi(poi_id=123, brand_name="Pilot", city="Austin", state="TX", ref="123", longitude=-97.7):
    return {"type": "node", "id": poi_id, "lat": 30.2, "lon": longitude,
            "tags": {"amenity": "fuel", "brand": brand_name, "addr:city": city, "addr:state": state, "ref": ref}}


class GeocodingTests(SimpleTestCase):
    def test_exact_brand_ref_match_has_source(self):
        result = match_osm([station()], [poi()])
        self.assertEqual(result["1"], (30.2, -97.7, "https://www.openstreetmap.org/node/123"))

    def test_different_state_ref_is_not_accepted(self):
        self.assertEqual(match_osm([station()], [poi(state="OK")]), {})

    def test_same_store_number_different_city_is_rejected(self):
        self.assertEqual(match_osm([station()], [poi(city="Dallas")]), {})

    def test_established_city_abbreviations_are_equivalent(self):
        self.assertEqual(normalize_city("St. Louis"), normalize_city("Saint Louis"))
        self.assertEqual(normalize_city("Ft Worth"), normalize_city("Fort Worth"))
        self.assertEqual(normalize_city("Mt Vernon"), normalize_city("Mount Vernon"))
        self.assertNotEqual(normalize_city("North Austin"), normalize_city("Austin"))

    def test_source_full_address_city_is_parsed_without_geographic_guessing(self):
        self.assertEqual(source_city({"addr:full": "100 Main St, Austin TX (I-35, Exit 1)", "addr:state": "TX"}), "Austin")
        self.assertEqual(source_city({"addr:full": "100 Main St, Austin, TX 78701", "addr:state": "TX"}), "Austin")
        self.assertEqual(source_city({"addr:full": "100 Main St, Austin TX", "addr:state": "OK"}), "")
        self.assertEqual(source_city({"addr:full": "Somewhere near Austin", "addr:state": "TX"}), "")

    def test_conflicting_store_number_is_not_city_matched(self):
        self.assertEqual(match_osm([station()], [poi(ref="456")]), {})

    def test_city_brand_match_requires_unique_both_sides(self):
        stations = [station(), station("2", name="Pilot #456")]
        self.assertEqual(match_osm(stations, [poi(ref="")]), {})
        self.assertEqual(match_osm([station()], [poi(ref=""), poi(124, ref="", longitude=-97.8)]), {})

    def test_duplicate_node_and_way_same_site_accepted(self):
        self.assertIn("1", match_osm([station()], [poi(), poi(124, longitude=-97.70001)]))

    def test_generic_ta_prefix_does_not_change_local_business_identity(self):
        self.assertEqual(brand("TA SEYMOUR TRAVEL CENTER"), "TA")
        self.assertNotEqual(brand("TAYLOR TRUCK STOP"), "TA")

    def test_census_does_not_geocode_highway_exits_as_street_addresses(self):
        highway = station()
        street = station("2", address="123 Main Street")
        self.assertEqual(census_candidates([highway, street]), [street])

    def test_census_requires_exact_match_and_allowed_id(self):
        text = '"1","123 Main Street","Match","Exact","123 MAIN ST","-97.7,30.2","1","L"\n"2","Road","Match","Non_Exact","OTHER","-97.8,30.3"\n"3","Road","Match","Exact","OTHER","-97.8,30.3"\n'
        result = parse_census(text, {"1", "2"})
        self.assertEqual(set(result), {"1"})
        self.assertEqual(result["1"][:2], (30.2, -97.7))

    def test_atp_pilot_network_rebrand_matches_exact_store_identity(self):
        feature = {"type": "Feature", "id": "atp123", "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
                   "properties": {"ref": "123", "brand": "ONE9", "amenity": "fuel", "addr:country": "US", "addr:state": "TX", "addr:city": "Austin", "website": "https://locations.pilotflyingj.com/store/123"}}
        result = match_atp([station()], [feature])
        self.assertEqual(result["1"], (30.2, -97.7, "https://locations.pilotflyingj.com/store/123"))
        feature["properties"]["addr:state"] = "OK"
        self.assertEqual(match_atp([station()], [feature]), {})

    def test_atp_diesel_stop_need_not_have_amenity_tag(self):
        feature = {"id": "loves123", "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
                   "properties": {"ref": "123", "brand": "Love's", "fuel:diesel": "yes", "addr:country": "US", "addr:state": "TX", "addr:city": "Austin"}}
        self.assertIn("1", match_atp([station(name="LOVES TRAVEL STOP #123")], [feature]))
        feature["properties"]["addr:country"] = "CA"
        self.assertEqual(match_atp([station(name="LOVES TRAVEL STOP #123")], [feature]), {})

    def test_full_address_chain_location_matches_verified_city(self):
        feature = {"id": "road-ranger", "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
                   "properties": {"ref": "100 Main St, Austin TX (I-35, Exit 1)", "brand": "Road Ranger", "amenity": "fuel",
                                  "addr:country": "US", "addr:state": "TX", "addr:full": "100 Main St, Austin TX (I-35, Exit 1)"}}
        self.assertIn("1", match_atp([station(name="ROAD RANGER #123")], [feature]))
        self.assertEqual(match_atp([station(name="ROAD RANGER #123", city="Dallas")], [feature]), {})

    def test_nonfuel_unknown_shop_is_excluded(self):
        feature = {"id": "shop", "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
                   "properties": {"ref": "123", "brand": "Some Shop", "shop": "convenience", "addr:country": "US", "addr:state": "TX", "addr:city": "Austin"}}
        self.assertEqual(match_atp([station(name="Some Shop #123")], [feature]), {})

    def test_atp_closed_station_is_not_matched(self):
        feature = {"id": "closed", "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
                   "properties": {"ref": "123", "brand": "Pilot", "amenity": "fuel", "end_date": "2025-01-01",
                                  "addr:country": "US", "addr:state": "TX", "addr:city": "Austin"}}
        self.assertEqual(match_atp([station()], [feature]), {})

    def test_exact_official_store_id_can_use_corroborated_nearby_postal_city(self):
        feature = {"id": "pilot123", "geometry": {"type": "Point", "coordinates": [-97.7, 30.35]},
                   "properties": {"ref": "123", "brand": "Pilot", "amenity": "fuel",
                                  "addr:country": "US", "addr:state": "TX", "addr:city": "Pflugerville"}}
        result = match_atp([station()], [feature])
        self.assertEqual(result["1"][:2], (30.35, -97.7))
        feature["properties"]["addr:city"] = "Dallas"
        self.assertEqual(match_atp([station()], [feature]), {})

    def test_nearby_city_evidence_cannot_replace_a_conflicting_store_number(self):
        feature = {"id": "pilot456", "geometry": {"type": "Point", "coordinates": [-97.7, 30.35]},
                   "properties": {"ref": "456", "brand": "Pilot", "amenity": "fuel",
                                  "addr:country": "US", "addr:state": "TX", "addr:city": "Pflugerville"}}
        self.assertEqual(match_atp([station()], [feature]), {})

    def test_wordpress_record_id_is_not_mistaken_for_public_store_number(self):
        feature = {"id": "post18456", "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
                   "properties": {"ref": "18456", "brand": "Allsup's", "shop": "convenience", "@spider": "allsups_yesway_us",
                                  "addr:country": "US", "addr:state": "TX", "addr:city": "Austin"}}
        self.assertIn("1", match_atp([station(name="ALLSUPS #123")], [feature]))
        feature["properties"]["@spider"] = "unknown_provider"
        self.assertEqual(match_atp([station(name="ALLSUPS #123")], [feature]), {})

    def test_wordpress_namespace_fallback_remains_ambiguous_with_two_stations(self):
        feature = {"id": "post18456", "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
                   "properties": {"ref": "18456", "brand": "Allsup's", "shop": "convenience", "@spider": "allsups_yesway_us",
                                  "addr:country": "US", "addr:state": "TX", "addr:city": "Austin"}}
        self.assertEqual(match_atp([station(name="ALLSUPS #123"), station("2", name="ALLSUPS #124")], [feature]), {})

    def test_atp_cached_source_requires_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "pilot_flying_j.geojson").write_text(json.dumps({"features": [{"id": "cached"}]}))
            with patch("requests.get") as get:
                features = fetch_atp(directory, "test/1.0", spiders=["pilot_flying_j"])
            get.assert_not_called()
            self.assertEqual(features, [{"id": "cached"}])

    def test_invalid_cached_provider_payload_fails_before_geocoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pilot_flying_j.geojson"
            for payload in ([], {"features": [None]}, {"features": "invalid"}):
                path.write_text(json.dumps(payload))
                with self.subTest(payload=payload), self.assertRaises(CommandError):
                    fetch_atp(directory, "test/1.0", spiders=["pilot_flying_j"])

    def test_provider_html_error_is_not_treated_as_valid_census_response(self):
        with self.assertRaises(CommandError):
            parse_census("<html>Provider error</html>", {"1"})
