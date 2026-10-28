"""Application orchestration: cached data, bounded routing, and explicit accounting."""
from contextlib import contextmanager
from functools import lru_cache
import fcntl
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache

from .accounting import fuel_accounting, initial_price_quote, money
from .data.revision import get_dataset_revision
from .itinerary import ItineraryError, plan_itinerary
from .models import Station
from .providers import get_route, resolve_location
from .spatial import project_stations

RESULT_VERSION = "v2"


class PlanningError(Exception):
    def __init__(self, message, code="invalid_request", status=400, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def numeric(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanningError(f"{field} must be a number.")
    # Bounds first prevent overflow when validating arbitrarily large JSON ints.
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise PlanningError(f"{field} must be between {minimum} and {maximum}.")
    return float(value)


def validate_request(payload):
    if not isinstance(payload, dict):
        raise PlanningError("The request must be a JSON object.")
    allowed = {"start", "finish", "initial_fuel_gallons", "initial_fuel_price_per_gallon"}
    if unknown := set(payload) - allowed:
        raise PlanningError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    if "start" not in payload or "finish" not in payload:
        raise PlanningError("Both start and finish are required.")
    for name in ("start", "finish"):
        location = payload[name]
        if isinstance(location, str):
            if not location.strip() or len(location) > 300:
                raise PlanningError(f"{name} must contain between 1 and 300 characters.")
        elif isinstance(location, dict):
            if set(location) != {"latitude", "longitude"}:
                raise PlanningError(f"{name} requires latitude and longitude only.")
            numeric(location["latitude"], f"{name}.latitude", -90, 90)
            numeric(location["longitude"], f"{name}.longitude", -180, 180)
        else:
            raise PlanningError(f"{name} must be a US place name or latitude/longitude object.")
    initial = numeric(payload.get("initial_fuel_gallons", 50), "initial_fuel_gallons", 0, 50)
    initial_price = payload.get("initial_fuel_price_per_gallon")
    if initial_price is not None:
        numeric(initial_price, "initial_fuel_price_per_gallon", 0.001, 100)
    return initial, initial_price


@lru_cache(maxsize=4)
def located_stations(revision):
    return tuple(Station.objects.filter(latitude__isnull=False, longitude__isnull=False).values(
        "external_id", "name", "address", "city", "state", "latitude", "longitude",
        "price_per_gallon", "location_source",
    ))


@contextmanager
def request_lock(key):
    """Bounded lock shards coalesce simultaneous requests across workers."""
    directory = Path(settings.CACHE_DIR) / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / key[:2]).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _cache_key(payload, revision):
    configuration = [settings.OSRM_BASE_URL, settings.GEOCODING_BASE_URL,
                     settings.STATION_CORRIDOR_MILES, settings.MAX_FUEL_STOPS,
                     getattr(settings, "MAX_STATION_SNAP_MILES", 0.25),
                     getattr(settings, "MAX_ROUTE_SNAP_METERS", 1000)]
    return hashlib.sha256(json.dumps([RESULT_VERSION, payload, revision, configuration], sort_keys=True).encode()).hexdigest()


def _cached_plan(key):
    result = cache.get(f"result:{key}")
    if result is None:
        return None
    # Culling can remove the independently stored map. Republish on cache hits.
    cache.set(f"plan:{result['plan_id']}", result, settings.PLAN_CACHE_TIMEOUT)
    return {**result, "metadata": {**result["metadata"], "result_cache_hit": True,
                                   "routing_api_calls": 0, "geocoding_api_calls": 0}}


def coverage_summary(snapshot):
    return {"total_us_stations": snapshot.station_count, "located_stations": snapshot.located_count,
            "unlocated_stations": snapshot.station_count - snapshot.located_count,
            "located_percent": round(100 * snapshot.located_count / snapshot.station_count, 2) if snapshot.station_count else 0}


def plan_trip(payload):
    initial, supplied_price = validate_request(payload)
    snapshot = get_dataset_revision()
    key = _cache_key(payload, snapshot.token)
    with request_lock(key):
        if (existing := _cached_plan(key)) is not None:
            return existing
        start = resolve_location(payload["start"])
        finish = resolve_location(payload["finish"])
        base = get_route([start, finish])
        stations = located_stations(snapshot.token)
        candidates = []
        if base.distance_miles > initial * 10 + 1e-8:
            if not stations:
                raise PlanningError("No geocoded fuel stations are available. Import station coordinates before planning trips that require fuel.",
                                    "station_data_unavailable", 503)
            candidates = project_stations(base.geometry["coordinates"], base.distance_miles,
                                          stations, corridor_miles=settings.STATION_CORRIDOR_MILES)
        try:
            itinerary = plan_itinerary(base, start, finish, candidates, initial,
                                       routing=get_route, max_stops=settings.MAX_FUEL_STOPS)
        except ItineraryError as exc:
            raise PlanningError(str(exc), exc.code, exc.status, exc.details) from exc
        route, fuel = itinerary.route, itinerary.fuel
        quote = None
        if initial > 0 and fuel.fuel_consumed_gallons > 0:
            try:
                quote = initial_price_quote(start, stations, supplied_price)
            except ValueError as exc:
                raise PlanningError(str(exc), "initial_fuel_price_unavailable", 503) from exc
        costs = fuel_accounting(fuel, initial, quote)
        stops = []
        for index, stop in enumerate(fuel.stops, start=1):
            station = stop.candidate.station
            stops.append({"number": index, "station_id": stop.candidate.key, "name": station["name"],
                          "address": station["address"], "city": station["city"], "state": station["state"],
                          "latitude": station["latitude"], "longitude": station["longitude"],
                          "location_source": station.get("location_source", ""),
                          "mile_marker": round(stop.candidate.mile, 3),
                          "price_per_gallon_usd": str(stop.candidate.price),
                          "gallons_purchased": round(stop.gallons, 6), "cost_usd": money(stop.cost),
                          "arrival_fuel_gallons": round(stop.arrival_fuel_gallons, 6),
                          "departure_fuel_gallons": round(stop.departure_fuel_gallons, 6)})
        features = [{"type": "Feature", "geometry": route.geometry, "properties": {"kind": "route"}}]
        for kind, point in (("start", start), ("finish", finish)):
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [point.longitude, point.latitude]},
                             "properties": {"kind": kind, "label": point.label}})
        features.extend({"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["longitude"], s["latitude"]]},
                         "properties": {"kind": "fuel_stop", "number": s["number"], "station_id": s["station_id"]}} for s in stops)
        plan_id = str(uuid4())
        coverage = coverage_summary(snapshot)
        result = {
            "plan_id": plan_id, "map_url": f"/api/plans/{plan_id}/map/",
            "route": {"distance_miles": round(route.distance_miles, 3),
                      "duration_seconds": round(route.duration_seconds, 1)},
            "fuel_stops": stops, "total_cost_usd": costs.pop("total_cost_usd"),
            "fuel_purchases_cost_usd": costs.pop("fuel_purchases_cost_usd"),
            "fuel": {"mpg": 10, "maximum_range_miles": 500, "tank_capacity_gallons": 50,
                     "initial_gallons": initial, "purchased_gallons": round(fuel.fuel_purchased_gallons, 6),
                     "consumed_gallons": round(fuel.fuel_consumed_gallons, 6),
                     "remaining_gallons": round(fuel.final_fuel_gallons, 6), **costs},
            # Full-resolution geometry appears exactly once.
            "map": {"type": "FeatureCollection", "features": features},
            "metadata": {"currency": "USD", "result_cache_hit": False,
                         "routing_api_calls": itinerary.routing_calls,
                         "geocoding_api_calls": start.requests_made + finish.requests_made,
                         "route_recovered": itinerary.recovered,
                         "unused_waypoints_removed": itinerary.cleanup_performed,
                         "candidate_station_count": len(candidates), "station_coverage": coverage,
                         "station_corridor_miles": settings.STATION_CORRIDOR_MILES,
                         "optimization": "Exact minimum-cost purchases on the verified road itinerary; station selection uses located stations near the initial route.",
                         "cost_basis": "FIFO cost of fuel consumed; starting fuel uses an explicit price or a labelled nearest-station quote estimate.",
                         "warnings": [f"{coverage['unlocated_stations']} stations without verified coordinates were excluded."] if coverage["unlocated_stations"] else [],
                         "map_expires_in_seconds": settings.PLAN_CACHE_TIMEOUT,
                         "attribution": "Routing: OSRM / OpenStreetMap contributors (ODbL). Basemap: US Census. Prices: supplied CSV."},
        }
        cache.set(f"result:{key}", result, settings.PLAN_CACHE_TIMEOUT)
        # Publish the map last, after any culling caused by the result write.
        cache.set(f"plan:{plan_id}", result, settings.PLAN_CACHE_TIMEOUT)
        return result
