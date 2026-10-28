import json
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase, override_settings

from planner.models import Station
from planner.providers import Location, ProviderError, Route
from planner.service import located_stations


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class RouteApiTests(TestCase):
    def setUp(self):
        cache.clear()
        located_stations.cache_clear()
        self.payload = {"start": {"latitude": 41.8781, "longitude": -87.6298},
                        "finish": {"latitude": 40.7128, "longitude": -74.0060}}
        self.geometry = {"type": "LineString", "coordinates": [[-87.6298, 41.8781], [-74.006, 40.7128]]}

    def post(self, payload=None):
        return self.client.post("/api/route/", data=json.dumps(self.payload if payload is None else payload), content_type="application/json")

    def test_validation(self):
        invalid = [[], {}, {**self.payload, "initial_fuel_gallons": 51},
                   {**self.payload, "initial_fuel_gallons": 10 ** 400},
                   {**self.payload, "initial_fuel_gallons": True},
                   {**self.payload, "start": {"latitude": "41", "longitude": -87}},
                   {**self.payload, "unrecognized": 1}, {**self.payload, "finish": " "}]
        with patch("planner.service.get_route") as upstream:
            for payload in invalid:
                with self.subTest(payload=payload):
                    self.assertEqual(self.post(payload).status_code, 400)
            upstream.assert_not_called()

    def test_invalid_country_before_routing(self):
        self.payload["start"] = {"latitude": 43.6532, "longitude": -79.3832}
        with patch("planner.service.get_route") as upstream:
            response = self.post()
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_location")
            upstream.assert_not_called()

    def test_media_method_malformed_and_oversized_json(self):
        self.assertEqual(self.client.get("/api/route/").status_code, 405)
        self.assertEqual(self.client.post("/api/route/", data="text", content_type="text/plain").status_code, 415)
        for body in ("{", '{"start":NaN}', '{"start":Infinity}', '{"start":1,"start":2}', '[' * 1100 + '0' + ']' * 1100):
            self.assertEqual(self.client.post("/api/route/", data=body, content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/route/", data='"' + 'a' * 17000 + '"', content_type="application/json").status_code, 413)

    def test_short_route_one_call_cache_and_map(self):
        route = Route(self.geometry, 200, 12000, (200,))
        Station.objects.create(external_id="origin", name="Origin Fuel", address="123 Main", city="Chicago", state="IL",
                               latitude=41.8781, longitude=-87.6298, price_per_gallon=Decimal("3.00"))
        with patch("planner.service.get_route", return_value=route) as upstream:
            response = self.post()
            self.assertEqual(response.status_code, 200, response.content)
            result = response.json()
            self.assertEqual(result["total_cost_usd"], "60.00")
            self.assertEqual(result["fuel_purchases_cost_usd"], "0.00")
            self.assertTrue(result["fuel"]["initial_price_is_estimate"])
            self.assertEqual(result["fuel"]["consumed_gallons"], 20)
            self.assertEqual(result["fuel"]["remaining_gallons"], 30)
            self.assertEqual(result["metadata"]["routing_api_calls"], 1)
            self.assertEqual(result["metadata"]["geocoding_api_calls"], 0)
            cached = self.post().json()
            self.assertTrue(cached["metadata"]["result_cache_hit"])
            self.assertEqual(cached["metadata"]["routing_api_calls"], 0)
            self.assertEqual(self.client.get(result["map_url"]).status_code, 200)
            upstream.assert_called_once()

    def test_optional_initial_fuel_price(self):
        with patch("planner.service.get_route", return_value=Route(self.geometry, 100, 3600, (100,))):
            result = self.post({**self.payload, "initial_fuel_price_per_gallon": 3.20}).json()
        self.assertEqual(result["total_cost_usd"], "32.00")
        self.assertEqual(result["fuel"]["initial_fuel_consumed_cost_usd"], "32.00")
        self.assertFalse(result["fuel"]["initial_price_is_estimate"])

    def test_long_route_verified_distances_cost_and_two_call_budget(self):
        station = Station.objects.create(external_id="A", name="Test Fuel", address="123 Main St", city="Columbus", state="OH",
                                         latitude=39.96, longitude=-83, price_per_gallon=Decimal("3.00000000"))
        base = Route(self.geometry, 700, 36000, (700,))
        via = Route(self.geometry, 710, 36500, (400, 310), ((-87.6298, 41.8781), (-83, 39.96), (-74.006, 40.7128)))
        from planner.optimization import FuelCandidate
        candidate = FuelCandidate("A", 400, Decimal("3"), {"external_id": "A", "name": station.name, "address": station.address,
                                  "city": station.city, "state": station.state, "latitude": station.latitude, "longitude": station.longitude})
        with patch("planner.service.get_route", side_effect=[base, via]) as upstream, patch("planner.service.project_stations", return_value=[candidate]):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(result["route"]["distance_miles"], 710)
        self.assertEqual(result["fuel"]["purchased_gallons"], 21)
        self.assertEqual(result["fuel_purchases_cost_usd"], "63.00")
        self.assertEqual(result["total_cost_usd"], "213.00")
        self.assertEqual(result["fuel_stops"][0]["mile_marker"], 400)
        self.assertEqual(result["fuel_stops"][0]["departure_fuel_gallons"], 31)
        self.assertEqual(len(result["map"]["features"]), 4)
        self.assertNotIn("geometry", result["route"])
        self.assertEqual(result["map"]["features"][0]["geometry"], self.geometry)

    def test_coverage_failure_and_provider_failure(self):
        with patch("planner.service.get_route", return_value=Route(self.geometry, 700, 3600, (700,))):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "station_data_unavailable")
        with patch("planner.service.get_route", side_effect=ProviderError("Timed out", "provider_timeout", 504)):
            self.assertEqual(self.post().status_code, 504)

    def test_map_expiration_health_and_spec(self):
        self.assertEqual(self.client.get(f"/api/plans/{uuid4()}/map/").status_code, 404)
        self.assertEqual(self.client.get("/health/").json()["status"], "ok")
        self.assertEqual(self.client.get("/api/schema/").json()["openapi"], "3.1.0")

    def test_legacy_cost_contract_map_is_not_mislabeled_after_upgrade(self):
        plan_id = str(uuid4())
        cache.set(f"plan:{plan_id}", {"plan_id": plan_id, "total_cost_usd": "0.00"})
        response = self.client.get(f"/api/plans/{plan_id}/map/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "map_not_found")

    def test_map_assets_are_served_locally(self):
        response = self.client.get("/assets/us-states.geojson")
        states = json.loads(b"".join(response.streaming_content))
        self.assertEqual(len(states["features"]), 51)
        self.assertEqual(self.client.get("/assets/leaflet-1.9.4/leaflet.js").status_code, 200)
        self.assertEqual(self.client.get("/assets/leaflet-1.9.4/leaflet.css").status_code, 200)
        self.assertEqual(self.client.get("/assets/leaflet-1.9.4/settings.py").status_code, 404)

    def test_cached_result_recreates_an_evicted_map_without_upstream_calls(self):
        payload = {**self.payload, "initial_fuel_price_per_gallon": 3}
        with patch("planner.service.get_route", return_value=Route(self.geometry, 100, 3600, (100,))) as upstream:
            first = self.post(payload).json()
            cache.delete(f"plan:{first['plan_id']}")
            self.assertEqual(self.client.get(first["map_url"]).status_code, 404)
            repeated = self.post(payload).json()
            self.assertTrue(repeated["metadata"]["result_cache_hit"])
            self.assertEqual(self.client.get(repeated["map_url"]).status_code, 200)
            upstream.assert_called_once()

    def test_changing_provider_settings_invalidates_result_cache(self):
        payload = {**self.payload, "initial_fuel_price_per_gallon": 3}
        with patch("planner.service.get_route", return_value=Route(self.geometry, 100, 3600, (100,))) as upstream:
            self.assertEqual(self.post(payload).status_code, 200)
            with override_settings(OSRM_BASE_URL="https://different.example"):
                second = self.post(payload)
            self.assertEqual(second.status_code, 200)
            self.assertFalse(second.json()["metadata"]["result_cache_hit"])
            self.assertEqual(upstream.call_count, 2)

    def test_price_update_invalidates_full_result_and_station_cache(self):
        station = Station.objects.create(external_id="origin", name="Origin Fuel", address="123 Main", city="Chicago", state="IL",
                                         latitude=41.8781, longitude=-87.6298, price_per_gallon=Decimal("3.00"))
        with patch("planner.service.get_route", return_value=Route(self.geometry, 100, 3600, (100,))) as upstream:
            first = self.post().json()
            self.assertEqual(first["total_cost_usd"], "30.00")
            station.price_per_gallon = Decimal("4.00")
            station.save(update_fields=["price_per_gallon", "updated_at"])
            second = self.post().json()
        self.assertFalse(second["metadata"]["result_cache_hit"])
        self.assertEqual(second["total_cost_usd"], "40.00")
        self.assertNotEqual(second["plan_id"], first["plan_id"])
        self.assertEqual(upstream.call_count, 2)

    def test_first_result_does_not_evict_its_own_map_in_a_tiny_cache(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory, override_settings(CACHES={"default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache", "LOCATION": directory,
            "OPTIONS": {"MAX_ENTRIES": 1, "CULL_FREQUENCY": 1}}}):
            with patch("planner.service.get_route", return_value=Route(self.geometry, 100, 3600, (100,))):
                result = self.post({**self.payload, "initial_fuel_price_per_gallon": 3}).json()
            self.assertEqual(self.client.get(result["map_url"]).status_code, 200)

    def test_gzip_and_compact_response_keep_geometry_single_copy(self):
        import gzip
        payload = {**self.payload, "initial_fuel_price_per_gallon": 3}
        with patch("planner.service.get_route", return_value=Route(self.geometry, 100, 3600, (100,))):
            response = self.client.post("/api/route/", data=json.dumps(payload), content_type="application/json", HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(response["Content-Encoding"], "gzip")
        body = gzip.decompress(response.content)
        self.assertEqual(body.count(b'"LineString"'), 1)
        self.assertNotIn(b'": ', body)

    def test_coverage_endpoint_reports_excluded_stations(self):
        Station.objects.create(external_id="unlocated", name="Unknown", address="I-90", city="Chicago", state="IL", price_per_gallon=3)
        response = self.client.get("/api/coverage/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_us_stations"], 1)
        self.assertEqual(response.json()["unlocated_stations"], 1)
