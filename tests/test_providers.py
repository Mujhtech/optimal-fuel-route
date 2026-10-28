import tempfile
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from planner.geography import InvalidLocation
from planner.providers import Location, ProviderError, _rate_limit, get_route, resolve_location


START = Location(40.7128, -74.006)
FINISH = Location(41.8781, -87.6298)


def route_payload():
    return {
        "code": "Ok",
        "routes": [{"distance": 1609344, "duration": 50000, "geometry": {"type": "LineString", "coordinates": [[-74.006, 40.7128], [-87.6298, 41.8781]]}, "legs": [{"distance": 1609344}]}],
        "waypoints": [{"distance": 5, "location": [-74.006, 40.7128]}, {"distance": 2, "location": [-87.6298, 41.8781]}],
    }


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}, PROVIDER_RATE_INTERVAL_SECONDS=0)
class ProviderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @patch("planner.providers.requests.get")
    def test_route_one_call_and_repeated_request_cached(self, get):
        get.return_value = response(route_payload())
        first = get_route([START, FINISH])
        second = get_route([START, FINISH])
        self.assertEqual(first.distance_miles, 1000)
        self.assertEqual(first.leg_distances_miles, (1000,))
        self.assertEqual(first.requests_made, 1)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.requests_made, 0)
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(get.call_args.kwargs["params"]["overview"], "full")

    @patch("planner.providers.requests.get")
    def test_coordinate_inputs_make_zero_geocoding_calls(self, get):
        location = resolve_location({"latitude": 40.7128, "longitude": -74.006})
        self.assertEqual(location.requests_made, 0)
        get.assert_not_called()

    @patch("planner.providers.requests.get")
    def test_geocoding_is_us_filtered_and_cached(self, get):
        get.return_value = response({"features": [{"properties": {"countrycode": "US", "name": "New York", "state": "New York", "country": "United States"}, "geometry": {"type": "Point", "coordinates": [-74.006, 40.7128]}}]})
        first = resolve_location(" New York,  NY ")
        second = resolve_location("new york, ny")
        self.assertEqual(first.latitude, 40.7128)
        self.assertEqual(first.requests_made, 1)
        self.assertEqual(second.requests_made, 0)
        self.assertTrue(second.cache_hit)
        self.assertEqual(get.call_args.kwargs["params"]["countrycode"], "US")
        get.assert_called_once()

    @patch("planner.providers.requests.get")
    def test_non_us_geocode_rejected_even_if_provider_ignores_filter(self, get):
        get.return_value = response({"features": [{"properties": {"countrycode": "CA"}, "geometry": {"coordinates": [-79.3832, 43.6532]}}]})
        with self.assertRaises(ProviderError) as error:
            resolve_location("Toronto")
        self.assertEqual(error.exception.code, "location_not_found")

    @patch("planner.providers.requests.get")
    def test_timeouts_have_no_retry(self, get):
        get.side_effect = requests.Timeout
        with self.assertRaises(ProviderError) as error:
            get_route([START, FINISH])
        self.assertEqual(error.exception.status, 504)
        get.assert_called_once()

    @patch("planner.providers.requests.get")
    def test_no_route_is_an_actionable_422(self, get):
        get.return_value = response({"code": "NoRoute"}, 400)
        with self.assertRaises(ProviderError) as error:
            get_route([START, FINISH])
        self.assertEqual(error.exception.code, "route_not_found")
        self.assertEqual(error.exception.status, 422)

    @patch("planner.providers.requests.get")
    def test_upstream_rate_limit(self, get):
        get.return_value = response({}, 429)
        with self.assertRaises(ProviderError) as error:
            get_route([START, FINISH])
        self.assertEqual(error.exception.status, 503)

    @patch("planner.providers.requests.get")
    def test_invalid_route_payloads_fail_safely(self, get):
        fixtures = [None, {}, {"code": "Ok", "routes": []}]
        wrong_legs = route_payload()
        wrong_legs["routes"][0]["legs"] = []
        fixtures.append(wrong_legs)
        nan_coordinate = route_payload()
        nan_coordinate["routes"][0]["geometry"]["coordinates"][0][0] = float("nan")
        fixtures.append(nan_coordinate)
        invalid_snap = route_payload()
        invalid_snap["waypoints"][0]["location"] = ["bad", 40]
        fixtures.append(invalid_snap)
        for payload in fixtures:
            with self.subTest(payload=payload):
                get.return_value = response(payload)
                with self.assertRaises(ProviderError):
                    get_route([START, FINISH])

    @patch("planner.providers.requests.get")
    def test_large_waypoint_snap_is_rejected(self, get):
        payload = route_payload()
        payload["waypoints"][0]["distance"] = 10000
        get.return_value = response(payload)
        with self.assertRaises(ProviderError) as error:
            get_route([START, FINISH])
        self.assertEqual(error.exception.code, "location_off_road")

    @patch("planner.providers.requests.get")
    def test_stricter_road_snap_policy_does_not_reuse_permissive_cache(self, get):
        payload = route_payload()
        payload["waypoints"][0]["distance"] = 50
        get.return_value = response(payload)
        self.assertEqual(get_route([START, FINISH]).requests_made, 1)
        with override_settings(MAX_ROUTE_SNAP_METERS=10):
            with self.assertRaises(ProviderError) as error:
                get_route([START, FINISH])
        self.assertEqual(error.exception.code, "location_off_road")
        self.assertEqual(get.call_count, 2)

    def test_input_shape_validation(self):
        for value in [None, True, 17, [], "", "x" * 301, {"latitude": 40.7}, {"latitude": 40.7, "longitude": -74, "extra": True}]:
            with self.subTest(value=value):
                with self.assertRaises(InvalidLocation):
                    resolve_location(value)

    @patch("planner.providers.time.sleep")
    @patch("planner.providers.time.time", return_value=1000)
    def test_rate_gate_reserves_shared_slots(self, current_time, sleep):
        with tempfile.TemporaryDirectory() as directory:
            with override_settings(PROVIDER_RATE_INTERVAL_SECONDS=1, PROVIDER_RATE_DB=f"{directory}/rates.sqlite3"):
                _rate_limit("routing")
                _rate_limit("routing")
                _rate_limit("geocoding")
        sleep.assert_called_once_with(1)
