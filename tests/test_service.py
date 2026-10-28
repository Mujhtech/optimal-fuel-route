import json
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from planner.models import Station
from planner.optimization import FuelCandidate
from planner.providers import Route
from planner.service import located_stations


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class VerifiedItineraryTests(TestCase):
    def setUp(self):
        cache.clear()
        located_stations.cache_clear()
        self.start = [-90, 40]
        self.finish = [-75, 40]
        self.geometry = {"type": "LineString", "coordinates": [self.start, self.finish]}
        self.payload = {
            "start": {"latitude": self.start[1], "longitude": self.start[0]},
            "finish": {"latitude": self.finish[1], "longitude": self.finish[0]},
            "initial_fuel_price_per_gallon": 3,
        }

    def candidate(self, key, mile, price, longitude):
        row = Station.objects.create(
            external_id=str(key),
            name=f"Fuel {key}",
            address="100 Main Street",
            city="Columbus",
            state="OH",
            latitude=40,
            longitude=longitude,
            price_per_gallon=Decimal(str(price)),
        )
        data = {
            name: getattr(row, name)
            for name in ("external_id", "name", "address", "city", "state", "latitude", "longitude", "price_per_gallon")
        }
        return FuelCandidate(str(key), mile, Decimal(str(price)), data)

    def post_plan(self, candidates, base_distance, final_legs, snaps=None):
        base = Route(self.geometry, base_distance, 36000, (base_distance,))
        if snaps is None:
            snaps = tuple([self.start] + [[c.station["longitude"], c.station["latitude"]] for c in candidates] + [self.finish])
        via = Route(self.geometry, sum(final_legs), 36500, tuple(final_legs), snaps)
        with (
            patch("planner.service.project_stations", return_value=candidates),
            patch("planner.service.get_route", side_effect=[base, via]) as upstream,
        ):
            response = self.client.post("/api/route/", json.dumps(self.payload), content_type="application/json")
        self.assertEqual(upstream.call_count, 2)
        return response

    def test_actual_first_leg_exceeding_range_is_rejected(self):
        station = self.candidate("A", 400, 3, -83)
        response = self.post_plan([station], 700, (505, 205))
        self.assertEqual(response.status_code, 422, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "station_access_exceeds_range")
        self.assertEqual(error["details"], {"gap_start_mile": 0, "gap_end_mile": 505})

    def test_actual_final_leg_exceeding_range_is_rejected(self):
        station = self.candidate("A", 400, 3, -83)
        response = self.post_plan([station], 700, (400, 505))
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(response.json()["error"]["code"], "station_access_exceeds_range")

    def test_far_station_road_snap_is_rejected(self):
        station = self.candidate("A", 400, 3, -83)
        response = self.post_plan([station], 700, (400, 310), snaps=(self.start, [-82.5, 40], self.finish))
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(response.json()["error"]["code"], "station_access_unverified")

    def test_range_reserve_falls_back_to_a_reachable_498_mile_station(self):
        station = self.candidate("A", 498, 3, -83)
        response = self.post_plan([station], 700, (498, 202))
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(result["fuel_stops"][0]["mile_marker"], 498)
        self.assertEqual(result["fuel_stops"][0]["arrival_fuel_gallons"], 0.2)
        self.assertEqual(result["fuel_stops"][0]["gallons_purchased"], 20)
        self.assertEqual(result["fuel_purchases_cost_usd"], "60.00")
        self.assertEqual(result["total_cost_usd"], "210.00")

    def test_missing_station_snap_cannot_bypass_access_verification(self):
        station = self.candidate("A", 400, 3, -83)
        response = self.post_plan([station], 700, (400, 310), snaps=())
        self.assertEqual(response.status_code, 502, response.content)
        self.assertEqual(response.json()["error"]["code"], "invalid_upstream_route")

    def test_multiple_fuel_stops_recomputed_from_actual_leg_distances(self):
        stations = [self.candidate("A", 400, 5, -84), self.candidate("B", 600, 2, -80)]
        response = self.post_plan(stations, 900, (420, 205, 310))
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(result["route"]["distance_miles"], 935)
        self.assertEqual(result["fuel"]["consumed_gallons"], 93.5)
        self.assertEqual(result["fuel"]["purchased_gallons"], 43.5)
        self.assertEqual(result["fuel"]["remaining_gallons"], 0)
        self.assertEqual(result["fuel_purchases_cost_usd"], "124.50")
        self.assertEqual(result["total_cost_usd"], "274.50")
        self.assertEqual([s["mile_marker"] for s in result["fuel_stops"]], [420, 625])
        self.assertEqual([s["gallons_purchased"] for s in result["fuel_stops"]], [12.5, 31])
        self.assertEqual([s["arrival_fuel_gallons"] for s in result["fuel_stops"]], [8, 0])
        self.assertEqual([s["departure_fuel_gallons"] for s in result["fuel_stops"]], [20.5, 31])
        self.assertEqual(result["metadata"]["routing_api_calls"], 2)
        self.assertEqual(len(result["map"]["features"]), 5)
        self.assertEqual(result["fuel"]["initial_gallons"] + result["fuel"]["purchased_gallons"],
                         result["fuel"]["consumed_gallons"] + result["fuel"]["remaining_gallons"])

    def test_total_equals_rounded_purchase_line_items_plus_starting_inventory(self):
        stations = [self.candidate("A", 400, "3.00733333", -84), self.candidate("B", 600, "2.12345678", -80)]
        self.payload["initial_fuel_price_per_gallon"] = 3.0001
        response = self.post_plan(stations, 900, (400, 200, 300))
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual([s["cost_usd"] for s in result["fuel_stops"]], ["30.07", "63.70"])
        self.assertEqual(result["fuel"]["initial_fuel_consumed_cost_usd"], "150.01")
        self.assertEqual(result["total_cost_usd"], "243.78")
        en_route = sum(Decimal(stop["cost_usd"]) for stop in result["fuel_stops"])
        self.assertEqual(Decimal(result["total_cost_usd"]), en_route + Decimal(result["fuel"]["initial_fuel_consumed_cost_usd"]))
