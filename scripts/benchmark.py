"""Repeatable offline projection, response-size, query, and cache benchmarks.

Usage:
    .venv/bin/python scripts/benchmark.py
    .venv/bin/python scripts/benchmark.py --iterations 11 var/my-route.json

Reads saved route responses and the local station database. It never contacts
an external provider. Cold-provider latency and load capacity are deliberately
outside this benchmark. Output is one JSON record per measured component.
"""

import argparse
from copy import deepcopy
import gzip
import json
import os
from pathlib import Path
import platform
from statistics import median
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def emit(record):
    print(json.dumps(record, sort_keys=True))


def samples(operation, iterations):
    durations = []
    result = None
    for _ in range(iterations):
        started = perf_counter()
        result = operation()
        durations.append((perf_counter() - started) * 1000)
    return result, round(median(durations), 3)


def route_geometry(response):
    geometry = response.get("route", {}).get("geometry")
    if geometry is not None:
        return geometry
    for feature in response.get("map", {}).get("features", []):
        if feature.get("properties", {}).get("kind") == "route":
            return feature["geometry"]
    raise ValueError("Response has no route geometry")


def single_geometry_response(response, geometry):
    result = deepcopy(response)
    result.setdefault("route", {}).pop("geometry", None)
    collection = result.setdefault("map", {"type": "FeatureCollection", "features": []})
    route_features = [feature for feature in collection["features"]
                      if feature.get("properties", {}).get("kind") == "route"]
    if not route_features:
        collection["features"].insert(0, {"type": "Feature", "geometry": geometry,
                                           "properties": {"kind": "route"}})
    return result


def encoded(response):
    return json.dumps(response, separators=(",", ":")).encode()


def benchmark_response(path, stations, iterations, compare_legacy):
    from shapely.geometry import LineString
    from planner.spatial import project_stations

    response = json.loads(path.read_text())
    geometry = route_geometry(response)
    if geometry.get("type") != "LineString":
        raise ValueError("Route must be a GeoJSON LineString")
    coordinates = geometry["coordinates"]
    distance = response["route"]["distance_miles"]
    operation = lambda: project_stations(coordinates, distance, stations)
    # Warm native library initialization before measuring repeated CPU work.
    operation()
    candidates, projection_ms = samples(operation, iterations)
    single = single_geometry_response(response, geometry)
    duplicate = deepcopy(single)
    duplicate["route"]["geometry"] = geometry
    payload, serialization_ms = samples(lambda: encoded(single), iterations)
    baseline = encoded(duplicate)
    result = {"component": "route", "file": str(path), "vertices": len(coordinates),
              "located_stations": len(stations), "candidate_encounters": len(candidates),
              "projection_median_ms": projection_ms,
              "single_geometry_json_bytes": len(payload),
              "duplicate_geometry_json_bytes": len(baseline),
              "geometry_deduplication_reduction_percent": round(100 * (1 - len(payload) / len(baseline)), 2),
              "gzip_level_6_bytes": len(gzip.compress(payload, compresslevel=6, mtime=0)),
              "serialization_median_ms": serialization_ms}
    if compare_legacy:
        def legacy_projection():
            with patch("planner.spatial.linestrings", side_effect=lambda segments: [LineString(segment) for segment in segments]):
                return operation()
        legacy, legacy_ms = samples(legacy_projection, iterations)
        if legacy != candidates:
            raise AssertionError("Vectorized projection changed candidate results")
        result["legacy_projection_median_ms"] = legacy_ms
        result["candidate_results_identical"] = True
    emit(result)
    return single


def benchmark_revision(iterations):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from planner.data.revision import get_dataset_revision
    from planner.models import DatasetRevision

    get_dataset_revision()
    with CaptureQueriesContext(connection) as queries:
        _, elapsed = samples(get_dataset_revision, iterations)
    if len(queries) != iterations or any("planner_station" in query["sql"] for query in queries):
        raise AssertionError("Revision lookup must use one singleton-row query per call")
    emit({"component": "dataset_revision", "median_ms": elapsed,
          "queries_per_lookup": len(queries) / iterations,
          "station_table_queries": 0,
          "database_vendor": connection.vendor,
          "query_plan": DatasetRevision.objects.filter(pk=1).values("token", "station_count", "located_count").explain()})


def benchmark_cache(response, iterations):
    from django.conf import settings
    from django.core.cache import cache
    from django.test import Client, override_settings
    from planner.data.revision import get_dataset_revision
    from planner.service import _cache_key

    # Prime a production-style cache with the saved response. This isolates the
    # warm request path and response serialization from route/geocoding work.
    request = {"start": {"latitude": 41.8781, "longitude": -87.6298},
               "finish": {"latitude": 40.7128, "longitude": -74.0060},
               "initial_fuel_price_per_gallon": 3.0}
    body = json.dumps(request)
    host = next((value for value in settings.ALLOWED_HOSTS if value and value != "*"), "localhost").lstrip(".")
    client = Client(HTTP_HOST=host)
    with tempfile.TemporaryDirectory(prefix="fuel-benchmark-") as directory:
        with override_settings(CACHE_DIR=Path(directory), CACHES={"default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": str(Path(directory) / "responses"),
        }}):
            record = deepcopy(response)
            record.setdefault("metadata", {})

            def request_once(gzip_enabled=False):
                # Read outside the clock when priming; the timed API still does
                # its own normal revision lookup. Reprime if a concurrent import
                # publishes a new version during a developer benchmark run.
                key = _cache_key(request, get_dataset_revision().token)
                cache.set(f"result:{key}", record, 60)
                started = perf_counter()
                result = client.post("/api/route/", body, content_type="application/json",
                                     HTTP_ACCEPT_ENCODING="gzip" if gzip_enabled else "identity")
                duration = (perf_counter() - started) * 1000
                if result.status_code != 200:
                    raise AssertionError(f"Cached API request failed with HTTP {result.status_code}")
                payload = gzip.decompress(result.content) if result.get("Content-Encoding") == "gzip" else result.content
                if not json.loads(payload)["metadata"]["result_cache_hit"]:
                    raise AssertionError("Expected a result-cache hit")
                return duration, len(result.content)

            with patch("planner.service.get_route", side_effect=AssertionError("Benchmark must not call a routing provider")), \
                 patch("planner.service.resolve_location", side_effect=AssertionError("Benchmark must not call a geocoding provider")):
                request_once()
                plain = [request_once() for _ in range(iterations)]
                compressed = [request_once(True) for _ in range(iterations)]
            emit({"component": "cached_api", "fixture_plan_id": record["plan_id"],
                  "iterations": iterations, "routing_calls": 0, "geocoding_calls": 0,
                  "cache_backend": "FileBasedCache", "identity_median_ms": round(median(item[0] for item in plain), 3),
                  "identity_response_bytes": plain[0][1],
                  "gzip_median_ms": round(median(item[0] for item in compressed), 3),
                  "gzip_response_bytes": compressed[0][1]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("responses", nargs="*", type=Path, help="Saved route-response JSON files")
    parser.add_argument("--iterations", type=int, default=7)
    parser.add_argument("--compare-legacy", action="store_true", help="Also time the previous per-segment geometry construction")
    options = parser.parse_args()
    if not 1 <= options.iterations <= 100:
        parser.error("--iterations must be between 1 and 100")
    import django
    django.setup()
    from django.conf import settings
    response_directory = Path(settings.DATA_DIR)
    paths = options.responses or [path for path in (response_directory / "chicago-new-york.json", response_directory / "los-angeles-new-york.json") if path.exists()]
    if not paths:
        parser.error("No saved route responses found; pass a response JSON file or run the separate opt-in live verification first")

    import shapely
    from planner.data.revision import get_dataset_revision
    from planner.models import Station

    snapshot = get_dataset_revision()
    stations = list(Station.objects.filter(latitude__isnull=False, longitude__isnull=False).values(
        "external_id", "latitude", "longitude", "price_per_gallon"))
    emit({"component": "environment", "python": platform.python_version(), "django": django.get_version(),
          "shapely": shapely.__version__, "iterations": options.iterations, "network_calls": 0,
          "total_us_stations": snapshot.station_count, "located_stations": snapshot.located_count})
    benchmark_revision(options.iterations)
    for path in paths:
        single = benchmark_response(path, stations, options.iterations, options.compare_legacy)
        benchmark_cache(single, options.iterations)


if __name__ == "__main__":
    main()
