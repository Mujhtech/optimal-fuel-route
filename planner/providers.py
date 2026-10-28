"""Small, cached clients for free routing and endpoint geocoding services.

Each uncached method makes exactly one HTTP request, without implicit retries.
The SQLite rate gate coordinates all Gunicorn workers in one deployment.
"""

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import requests
from django.conf import settings
from django.core.cache import cache

from .geography import InvalidLocation, validate_coordinates

METERS_PER_MILE = 1609.344


class ProviderError(Exception):
    def __init__(self, message, code="provider_unavailable", status=502):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    label: str = ""
    cache_hit: bool = False
    # Coordinate inputs do not make an external geocoding request.
    requests_made: int = 0


@dataclass(frozen=True)
class Route:
    geometry: dict
    distance_miles: float
    duration_seconds: float
    leg_distances_miles: tuple[float, ...]
    snapped_coordinates: tuple[tuple[float, float], ...] = ()
    cache_hit: bool = False
    requests_made: int = 1


def _setting(name, default):
    return getattr(settings, name, default)


def _key(kind, value):
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return f"provider:{kind}:v1:{digest}"


def _rate_limit(service):
    interval = float(_setting("PROVIDER_RATE_INTERVAL_SECONDS", 1.0))
    if interval <= 0:
        return
    path = Path(_setting("PROVIDER_RATE_DB", Path(settings.BASE_DIR) / "var" / "provider_rate.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(path, timeout=5, isolation_level=None) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS rate_slots (service TEXT PRIMARY KEY, next_at REAL NOT NULL)")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT next_at FROM rate_slots WHERE service = ?", (service,)).fetchone()
            now = time.time()
            scheduled = max(now, row[0] if row else now)
            delay = scheduled - now
            if delay > float(_setting("PROVIDER_MAX_QUEUE_SECONDS", 5)):
                connection.execute("ROLLBACK")
                raise ProviderError("The free routing provider is busy. Retry shortly.", "provider_busy", 503)
            connection.execute(
                "INSERT INTO rate_slots(service, next_at) VALUES(?, ?) "
                "ON CONFLICT(service) DO UPDATE SET next_at = excluded.next_at",
                (service, scheduled + interval),
            )
            connection.execute("COMMIT")
    except sqlite3.Error as exc:
        raise ProviderError("Provider request coordination is temporarily unavailable.", "provider_busy", 503) from exc
    if delay:
        time.sleep(delay)


def _request_json(url, params, service):
    _rate_limit(service)
    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": _setting("PROVIDER_USER_AGENT", "OptimalFuelPlanner/1.0 (assignment application)"), "Accept": "application/json"},
            timeout=(3.0, float(_setting("PROVIDER_TIMEOUT_SECONDS", 20))),
            allow_redirects=False,
        )
        if response.status_code == 429:
            raise ProviderError("The free provider rate limit was reached. Retry shortly.", "provider_rate_limited", 503)
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise ProviderError("The routing or geocoding provider is temporarily unavailable.")
        # OSRM returns useful structured NoRoute/NoSegment responses with HTTP400.
        if response.status_code not in (200, 400):
            raise ProviderError("The routing or geocoding provider rejected the request.")
        return response.json()
    except requests.Timeout as exc:
        raise ProviderError("The routing or geocoding provider timed out.", "provider_timeout", 504) from exc
    except (requests.RequestException, ValueError) as exc:
        raise ProviderError("The routing or geocoding provider returned an invalid response.") from exc


def resolve_location(value) -> Location:
    """Resolve a USA place name, or validate numeric coordinates locally."""
    if isinstance(value, dict):
        if set(value) - {"latitude", "longitude"} or not {"latitude", "longitude"} <= set(value):
            raise InvalidLocation("A coordinate location must contain latitude and longitude only.")
        latitude, longitude = validate_coordinates(value["latitude"], value["longitude"])
        return Location(latitude, longitude, f"{latitude}, {longitude}")
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise InvalidLocation("Each location must be a place name (1–300 characters) or a coordinate object.")
    query = " ".join(value.split())
    base_url = _setting("GEOCODING_BASE_URL", "https://photon.komoot.io").rstrip("/")
    key = _key("geocode", [base_url, query.casefold()])
    cached = cache.get(key)
    if cached is not None:
        return Location(**cached, cache_hit=True, requests_made=0)
    payload = _request_json(base_url + "/api/", {"q": query, "countrycode": "US", "limit": 5, "lang": "en"}, "geocoding")
    try:
        features = payload["features"]
        if not isinstance(features, list):
            raise TypeError
        for feature in features:
            properties = feature["properties"]
            if str(properties.get("countrycode", "")).upper() != "US":
                continue
            longitude, latitude = feature["geometry"]["coordinates"][:2]
            try:
                latitude, longitude = validate_coordinates(latitude, longitude)
            except InvalidLocation:
                continue
            label = ", ".join(dict.fromkeys(str(properties[field]) for field in ("name", "city", "state", "country") if properties.get(field)))
            result = {"latitude": latitude, "longitude": longitude, "label": label or query}
            cache.set(key, result, timeout=int(_setting("GEOCODE_CACHE_TIMEOUT", 30 * 86400)))
            return Location(**result, requests_made=1)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ProviderError("The geocoding provider returned an invalid response.") from exc
    raise ProviderError(f"No USA location was found for '{query}'. Try a city and state or coordinates.", "location_not_found", 422)


def get_route(points: Sequence[Location]) -> Route:
    """Request one complete driving route, optionally through fuel waypoints."""
    if not 2 <= len(points) <= 100:
        raise InvalidLocation("A route must contain between 2 and 100 waypoints.")
    for point in points:
        validate_coordinates(point.latitude, point.longitude)
    coordinates = ";".join(f"{point.longitude:.7f},{point.latitude:.7f}" for point in points)
    base_url = _setting("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
    key = _key("route", [base_url, coordinates, float(_setting("MAX_ROUTE_SNAP_METERS", 1000))])
    cached = cache.get(key)
    if cached is not None:
        return Route(**cached, cache_hit=True, requests_made=0)
    payload = _request_json(
        f"{base_url}/route/v1/driving/{coordinates}",
        {"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false", "generate_hints": "false"},
        "routing",
    )
    if not isinstance(payload, dict):
        raise ProviderError("The routing provider returned an invalid response.")
    if payload.get("code") in {"NoRoute", "NoSegment"}:
        raise ProviderError("No drivable route exists between these locations.", "route_not_found", 422)
    if payload.get("code") != "Ok":
        raise ProviderError("The routing provider could not calculate this route.")
    try:
        route = payload["routes"][0]
        geometry = route["geometry"]
        if geometry["type"] != "LineString" or len(geometry["coordinates"]) < 2:
            raise ValueError
        for coordinate in geometry["coordinates"]:
            if len(coordinate) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in coordinate):
                raise ValueError
            if not -180 <= coordinate[0] <= 180 or not -90 <= coordinate[1] <= 90:
                raise ValueError
        distance = float(route["distance"])
        duration = float(route["duration"])
        legs = tuple(float(leg["distance"]) / METERS_PER_MILE for leg in route["legs"])
        if not math.isfinite(distance) or distance < 0 or not math.isfinite(duration) or duration < 0:
            raise ValueError
        if len(legs) != len(points) - 1 or any(not math.isfinite(leg) or leg < 0 for leg in legs):
            raise ValueError
        if abs(sum(legs) * METERS_PER_MILE - distance) > max(5, distance * 0.001):
            raise ValueError
        waypoints = payload["waypoints"]
        if len(waypoints) != len(points):
            raise ValueError
        for waypoint in waypoints:
            snapped_distance = float(waypoint["distance"])
            if not math.isfinite(snapped_distance) or snapped_distance < 0:
                raise ValueError
            if snapped_distance > float(_setting("MAX_ROUTE_SNAP_METERS", 1000)):
                raise ProviderError("An input location is too far from a routable road.", "location_off_road", 422)
        snapped = tuple(tuple(waypoint["location"]) for waypoint in waypoints)
        for coordinate in snapped:
            if len(coordinate) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in coordinate):
                raise ValueError
            if not -180 <= coordinate[0] <= 180 or not -90 <= coordinate[1] <= 90:
                raise ValueError
        result = {
            "geometry": geometry,
            "distance_miles": distance / METERS_PER_MILE,
            "duration_seconds": duration,
            "leg_distances_miles": legs,
            "snapped_coordinates": snapped,
        }
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        raise ProviderError("The routing provider returned an invalid response.") from exc
    cache.set(key, result, timeout=int(_setting("ROUTE_CACHE_TIMEOUT", 86400)))
    return Route(**result)
