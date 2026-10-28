"""Local country validation; no reverse-geocoding request is needed."""

import json
import math
from functools import lru_cache
from numbers import Real
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import prep


class InvalidLocation(ValueError):
    pass


@lru_cache(maxsize=1)
def _us_boundary():
    path = Path(__file__).with_name("data") / "us_boundary.geojson"
    with path.open(encoding="utf-8") as handle:
        return prep(shape(json.load(handle)["geometry"]))


def is_in_usa(latitude: float, longitude: float) -> bool:
    """Check the bundled Census 2025 1:500k boundary, including AK and HI.

    This generalized cartographic boundary is deliberately not buffered into
    neighboring countries. Locations directly on a generalized shoreline or
    international boundary may require a nearby inland coordinate.
    """
    return _us_boundary().covers(Point(longitude, latitude))


def validate_coordinates(latitude, longitude) -> tuple[float, float]:
    for value in (latitude, longitude):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise InvalidLocation("Latitude and longitude must be JSON numbers.")
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidLocation("Latitude and longitude must be finite numbers.")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise InvalidLocation("Latitude or longitude is outside its valid range.")
    latitude, longitude = float(latitude), float(longitude)
    if not is_in_usa(latitude, longitude):
        raise InvalidLocation("Start and finish locations must be within the USA.")
    return latitude, longitude
