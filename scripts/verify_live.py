"""Opt-in live integration check: python scripts/verify_live.py.

The regular test suite never uses external services. This check uses real free
providers, makes at most three routing calls per trip, and stores responses in var.
"""
import json
import os
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.test import Client
from django.conf import settings

trips = {
    "chicago-new-york": {"start": {"latitude": 41.8781, "longitude": -87.6298}, "finish": {"latitude": 40.7128, "longitude": -74.0060}},
    "los-angeles-new-york": {"start": {"latitude": 34.0522, "longitude": -118.2437}, "finish": {"latitude": 40.7128, "longitude": -74.0060}},
    "new-york-philadelphia": {"start": {"latitude": 40.7128, "longitude": -74.0060}, "finish": {"latitude": 39.9526, "longitude": -75.1652}},
}
host = next((value for value in settings.ALLOWED_HOSTS if value and value != "*"), "localhost").lstrip(".")
client = Client(HTTP_HOST=host)
for name, payload in trips.items():
    started = perf_counter()
    response = client.post("/api/route/", data=json.dumps(payload), content_type="application/json")
    elapsed = perf_counter() - started
    result = response.json()
    if response.status_code != 200:
        raise SystemExit(f"{name}: HTTP {response.status_code}: {result}")
    assert result["metadata"]["routing_api_calls"] <= 3
    assert result["metadata"]["geocoding_api_calls"] == 0
    from decimal import Decimal
    assert sum((Decimal(stop["cost_usd"]) for stop in result["fuel_stops"]), Decimal(0)) == Decimal(result["fuel_purchases_cost_usd"])
    assert Decimal(result["total_cost_usd"]) == Decimal(result["fuel"]["initial_fuel_consumed_cost_usd"]) + Decimal(result["fuel"]["en_route_consumed_cost_usd"])
    assert result["total_cost_usd"] != "0.00"
    assert "geometry" not in result["route"]
    fuel = result["fuel"]["initial_gallons"]
    previous = 0.0
    for stop in result["fuel_stops"]:
        fuel -= (stop["mile_marker"] - previous) / 10
        assert fuel >= -0.001
        fuel += stop["gallons_purchased"]
        assert fuel <= 50.001
        previous = stop["mile_marker"]
    fuel -= (result["route"]["distance_miles"] - previous) / 10
    assert fuel >= -0.001
    assert client.get(result["map_url"]).status_code == 200
    # Independent eviction must be repaired by a cached request, without routing.
    from django.core.cache import cache
    cache.delete(f"plan:{result['plan_id']}")
    cached_start = perf_counter()
    repeated = client.post("/api/route/", data=json.dumps(payload), content_type="application/json").json()
    cached_elapsed = perf_counter() - cached_start
    assert repeated["metadata"]["routing_api_calls"] == 0
    assert repeated["metadata"]["result_cache_hit"]
    assert client.get(repeated["map_url"]).status_code == 200
    path = Path(settings.DATA_DIR) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result))
    print(json.dumps({"trip": name, "miles": result["route"]["distance_miles"], "stops": len(result["fuel_stops"]),
                      "cost_usd": result["total_cost_usd"], "route_purchases_usd": result["fuel_purchases_cost_usd"],
                      "routing_calls": result["metadata"]["routing_api_calls"],
                      "seconds": round(elapsed, 3), "cached_seconds": round(cached_elapsed, 3), "map": result["map_url"]}))
