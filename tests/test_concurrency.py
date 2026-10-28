from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase, override_settings

from planner.data.revision import get_dataset_revision
from planner.providers import Route
from planner.service import located_stations, plan_trip


class IdenticalRequestConcurrencyTests(TransactionTestCase):
    def test_simultaneous_requests_share_one_cold_route_call(self):
        # Bootstrap the singleton before threads start; the production hot path
        # performs only reads. TransactionTestCase has no surrounding write lock.
        get_dataset_revision()
        located_stations.cache_clear()
        self.addCleanup(located_stations.cache_clear)
        payload = {"start": {"latitude": 40.7128, "longitude": -74.006},
                   "finish": {"latitude": 39.9526, "longitude": -75.1652},
                   "initial_fuel_price_per_gallon": 3.0}
        geometry = {"type": "LineString", "coordinates": [[-74.006, 40.7128], [-75.1652, 39.9526]]}
        route = Route(geometry, 100, 7200, (100,))
        barrier = threading.Barrier(4)

        def provider(points):
            time.sleep(0.05)  # Ensure the other requests reach the shared lock.
            return route

        def request():
            try:
                barrier.wait(timeout=5)
                return plan_trip(payload)
            finally:
                connections.close_all()

        with tempfile.TemporaryDirectory(prefix="fuel-concurrency-") as directory:
            with override_settings(CACHE_DIR=Path(directory), CACHES={"default": {
                "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                "LOCATION": str(Path(directory) / "responses"),
            }}), patch("planner.service.get_route", side_effect=provider) as upstream:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(lambda _: request(), range(4)))
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(len({result["plan_id"] for result in results}), 1)
        self.assertEqual(sum(not result["metadata"]["result_cache_hit"] for result in results), 1)
        self.assertTrue(all(result["total_cost_usd"] == "30.00" for result in results))
