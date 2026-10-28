"""Preparation-only spatial corroboration for actual OpenStreetMap fuel points.

Administrative polygons may fill missing state/city tags. They never provide
station coordinates. Independent business-name matches require exact names and
unambiguous city/state identities on both sides of the join.
"""
from collections import defaultdict
import math
import re

from shapely.geometry import Point, shape
from shapely.strtree import STRtree
from django.core.management.base import CommandError

from .geocoding import normalize, normalize_city, store_number, unique_poi
from .importing import US_STATES, coordinate_pair


def _geometry_index(features, property_names):
    geometries, values = [], []
    for feature in features:
        properties = feature.get("properties") or {}
        value = next((properties.get(key) for key in property_names if properties.get(key)), None)
        if value:
            geometries.append(shape(feature["geometry"]))
            values.append(value)
    return geometries, values, STRtree(geometries)


def _covering(point, geometries, values, tree):
    return {values[int(index)] for index in tree.query(point) if geometries[int(index)].covers(point)}


def enrich_osm_places(elements, state_features, place_features=()):
    """Fill missing OSM address tags from containing administrative polygons.

    Conflicting existing state tags, outside-USA points, and ambiguous polygon
    joins are rejected. Place features should expose ``name`` or ``NAME``.
    Their spatial join is used only when an actual city tag is missing.
    """
    state_shapes, states, state_tree = _geometry_index(state_features, ("abbr", "STUSPS", "STUSAB"))
    place_shapes, places, place_tree = _geometry_index(place_features, ("name", "NAME"))
    result = []
    for element in elements:
        tags = dict(element.get("tags") or {})
        if tags.get("amenity") != "fuel":
            continue
        location = element.get("center", element)
        latitude, longitude = location.get("lat"), location.get("lon")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (latitude, longitude)):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            continue
        point = Point(longitude, latitude)
        containing_states = _covering(point, state_shapes, states, state_tree) & US_STATES
        tagged_state = tags.get("addr:state", "").strip().upper()
        if tagged_state:
            if tagged_state not in containing_states:
                continue
        elif len(containing_states) == 1:
            tagged_state = containing_states.pop()
        else:
            continue
        tags["addr:state"] = tagged_state
        if not tags.get("addr:city") and place_shapes:
            containing_places = _covering(point, place_shapes, places, place_tree)
            if len(containing_places) == 1:
                tags["addr:city"] = containing_places.pop()
        result.append({**element, "tags": tags})
    return result


def _business_name(value):
    value = re.sub(r"#\s*\d+\b", "", value.upper())
    value = re.sub(r"^THE\s+", "", value)
    value = re.sub(r"\b(?:INC|LLC|LTD)\.?$", "", value.strip())
    return normalize(value)


GENERIC_NAMES = frozenset({
    "FUEL", "GAS", "GASSTATION", "FUELSTOP", "TRUCKSTOP", "TRAVELCENTER", "TRAVELPLAZA",
    "SHELL", "BP", "CHEVRON", "PHILLIPS66", "SUNOCO", "EXXON", "MOBIL", "MARATHON",
    "SPEEDWAY", "PILOT", "FLYINGJ", "LOVES", "CIRCLEK", "CASEYS", "QUIKTRIP", "KWIKTRIP",
    "KWIKSTAR", "MAVERIK", "SHEETZ", "THORNTONS", "SINCLAIR", "CITGO", "VALERO", "AMOCO",
})


def match_named_stations(stations, enriched_elements):
    """Match independent businesses without discarding their names for brands.

    An independent truck stop can sell Shell-branded fuel. Comparing only its
    ``brand`` would lose the truck-stop name; this additional exact-name join
    uses the business's name, city and state. Generic chain names are excluded.
    """
    source = defaultdict(list)
    for station in stations:
        key = (_business_name(station.name), station.state, normalize_city(station.city))
        if key[0] and key[0] not in GENERIC_NAMES:
            source[key].append(station)
    pois = defaultdict(list)
    for element in enriched_elements:
        tags = element.get("tags") or {}
        if (tags.get("amenity") != "fuel" or not tags.get("addr:city")
                or tags.get("addr:state", "").upper() not in US_STATES
                or element.get("type") not in {"node", "way", "relation"}):
            continue
        identity = _business_name(tags.get("name", ""))
        if not identity or identity in GENERIC_NAMES:
            continue
        key = (identity, tags.get("addr:state", "").upper(), normalize_city(tags["addr:city"]))
        location = element.get("center", element)
        try:
            latitude, longitude = coordinate_pair(location.get("lat"), location.get("lon"), "OSM business point")
        except CommandError:
            continue
        ref = str(tags.get("ref", ""))
        number = str(int(ref)) if re.fullmatch(r"\d+", ref) else store_number(tags.get("name", ""))
        pois[key].append({"coordinates": (latitude, longitude), "number": number,
                          "source": f"https://www.openstreetmap.org/{element['type']}/{element['id']}"})
    matches = {}
    for key, records in source.items():
        if len(records) != 1:
            continue
        poi = unique_poi(pois[key])
        if poi is None:
            continue
        station = records[0]
        number = store_number(station.name)
        if number and poi["number"] and number != poi["number"]:
            continue
        matches[station.external_id] = (*poi["coordinates"], poi["source"])
    return matches
