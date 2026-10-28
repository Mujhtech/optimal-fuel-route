"""Find nearby stations without issuing station-by-station routing requests.

Route chainage uses cumulative great-circle segment lengths, scaled to the
road distance returned by the routing provider. A spatial tree limits work
to segments in a small station envelope. Distances to the road are local
straight-line estimates, not a claim that a station is road-accessible.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Iterable, Sequence

from shapely import linestrings
from shapely.geometry import box
from shapely.strtree import STRtree

from .optimization import FuelCandidate

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(first: Sequence[float], second: Sequence[float]) -> float:
    """Distance between two GeoJSON-order [longitude, latitude] points."""
    first_lon, first_lat = map(math.radians, first)
    second_lon, second_lat = map(math.radians, second)
    half_lat = math.sin((second_lat - first_lat) / 2)
    half_lon = math.sin((second_lon - first_lon) / 2)
    value = half_lat**2 + math.cos(first_lat) * math.cos(second_lat) * half_lon**2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, max(0.0, value))))


def _field(station: Any, name: str, default: Any = None) -> Any:
    return station.get(name, default) if isinstance(station, dict) else getattr(station, name, default)


def project_stations(
    coordinates: Sequence[Sequence[float]],
    distance_miles: float,
    stations: Iterable[Any],
    corridor_miles: float = 1.0,
) -> list[FuelCandidate]:
    """Return stations within a lateral corridor, ordered along the route.

    A station can occur more than once when the route passes it again.
    Adjacent matching segments are consolidated into a single encounter.
    No city-centroid fallback is used for stations without coordinates.
    """
    if not math.isfinite(distance_miles) or distance_miles < 0:
        raise ValueError("Route distance must be finite and nonnegative.")
    if not math.isfinite(corridor_miles) or corridor_miles <= 0:
        raise ValueError("Corridor width must be finite and positive.")
    if len(coordinates) < 2:
        raise ValueError("Route geometry must contain at least two points.")
    points: list[tuple[float, float]] = []
    for coordinate in coordinates:
        if len(coordinate) < 2:
            raise ValueError("Each route coordinate needs a longitude and latitude.")
        longitude, latitude = float(coordinate[0]), float(coordinate[1])
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise ValueError("Route coordinates must be finite.")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("Route coordinates are out of bounds.")
        # Unwrap date-line crossings to keep segment envelopes small.
        if points:
            longitude += round((points[-1][0] - longitude) / 360) * 360
        if not points or (longitude, latitude) != points[-1]:
            points.append((longitude, latitude))
    if len(points) < 2:
        if distance_miles == 0:
            return []
        raise ValueError("A nonzero route needs nonzero geometry.")
    segment_lengths = [haversine_miles(a, b) for a, b in zip(points, points[1:])]
    cumulative = [0.0]
    for length in segment_lengths:
        cumulative.append(cumulative[-1] + length)
    scale = distance_miles / cumulative[-1]
    # The Shapely ufunc constructs all segments in one native call instead of
    # paying Python object-constructor overhead for every road vertex.
    segments = linestrings(list(zip(points, points[1:])))
    tree = STRtree(segments)
    route_center_lon = (min(p[0] for p in points) + max(p[0] for p in points)) / 2
    lat_envelope = math.degrees(corridor_miles / EARTH_RADIUS_MILES) * 1.01
    candidates: list[FuelCandidate] = []
    for station in stations:
        latitude, longitude = _field(station, "latitude"), _field(station, "longitude")
        if latitude is None or longitude is None:
            continue
        latitude, longitude = float(latitude), float(longitude)
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            continue
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        longitude += round((route_center_lon - longitude) / 360) * 360
        cos_lat = max(0.001, math.cos(math.radians(latitude)))
        lon_envelope = lat_envelope / cos_lat
        matches = tree.query(
            box(
                longitude - lon_envelope,
                latitude - lat_envelope,
                longitude + lon_envelope,
                latitude + lat_envelope,
            )
        )
        encounters: list[tuple[int, float, float]] = []
        for raw_index in matches:
            index = int(raw_index)
            start, end = points[index], points[index + 1]
            # A local tangent plane avoids longitude degrees being treated
            # as the same physical length as latitude degrees.
            x0 = math.radians(start[0] - longitude) * cos_lat
            y0 = math.radians(start[1] - latitude)
            dx = math.radians(end[0] - start[0]) * cos_lat
            dy = math.radians(end[1] - start[1])
            denominator = dx * dx + dy * dy
            if denominator == 0:
                continue
            fraction = min(1.0, max(0.0, -(x0 * dx + y0 * dy) / denominator))
            projected = (
                start[0] + fraction * (end[0] - start[0]),
                start[1] + fraction * (end[1] - start[1]),
            )
            offset = haversine_miles((longitude, latitude), projected)
            if offset <= corridor_miles:
                mile = (cumulative[index] + fraction * segment_lengths[index]) * scale
                encounters.append((index, mile, offset))
        encounters.sort()
        groups: list[list[tuple[int, float, float]]] = []
        for encounter in encounters:
            if (
                groups
                and encounter[0] <= groups[-1][-1][0] + 1
                and encounter[1] - groups[-1][-1][1] <= 2 * corridor_miles * scale + 1e-8
            ):
                groups[-1].append(encounter)
            else:
                groups.append([encounter])
        key = _field(station, "external_id", _field(station, "id"))
        if key is None:
            raise ValueError("Located stations need an external_id or id.")
        price = Decimal(str(_field(station, "price_per_gallon")))
        for group in groups:
            _, mile, offset = min(group, key=lambda encounter: (encounter[2], encounter[1]))
            candidates.append(
                FuelCandidate(
                    key=str(key),
                    mile=min(distance_miles, max(0.0, mile)),
                    price=price,
                    station=station,
                    offset_miles=offset,
                )
            )
    return sorted(candidates, key=lambda candidate: (candidate.mile, candidate.price, candidate.key))
