"""Offline station matching and explicit, cacheable preparation-time API calls.

No function here is called by the trip API. City centers are never used as fuel
station coordinates. Ambiguous matches are deliberately left unresolved.
"""

import csv
import gzip
import io
import json
import math
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from django.core.management.base import CommandError

from planner.data.importing import US_STATES, coordinate_pair

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = '[out:json][timeout:90];area["ISO3166-1"="US"][admin_level=2]->.us;nwr[amenity=fuel][brand~"Love|Pilot|Flying J|TravelCenters|Petro|Speedway|Allsup|Quarles|Quick Fuel|Racetrac|Stripes",i](area.us);out center tags;'
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
ATP_URL = "https://data.alltheplaces.xyz/runs/latest/output"
ATP_SPIDERS = (
    "pilot_flying_j", "loves_us", "travelcenters_of_america_us", "kwik_trip",
    "caseys_general_store_us", "circle_k", "quiktrip_us", "maverik",
    "sheetz", "rutters_us", "thorntons_us", "cefco_us", "road_ranger_us",
    "speedway_us", "allsups_yesway_us", "cenex", "shell", "chevron_us",
    "marathon_petroleum_us", "sunoco_us", "phillips_66_conoco_76", "sinclair_us", "valero",
)


def normalize(value):
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def normalize_city(value):
    """Expand established city-name abbreviations, without fuzzy place matching."""
    words = re.sub(r"[^A-Z0-9]+", " ", value.upper()).split()
    expanded = {"ST": "SAINT", "FT": "FORT", "MT": "MOUNT"}
    return "".join(expanded.get(word, word) for word in words)


def source_city(properties):
    """Recover a city only when a structured full postal address contains it."""
    if properties.get("addr:city"):
        return properties["addr:city"]
    full = properties.get("addr:full", "")
    # Official Road Ranger and Cenex locators provide full addresses rather
    # than separate cities: '100 Main St, City, TX 75001' or '..., City TX (...)'.
    matched = re.search(r",\s*([^,]+?)\s*,?\s+([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:\s*\(|\s*$)", full)
    if matched and matched[2] == str(properties.get("addr:state", "")).upper():
        return matched[1].strip()
    return ""


@lru_cache(maxsize=1)
def _city_reference():
    """Official Census place references corroborate names, never locate stations."""
    path = Path(__file__).with_name("postal_city_reference.json.gz")
    return json.loads(gzip.decompress(path.read_bytes()))


def nearby_postal_cities(first, second, state, point):
    """Corroborate a postal-city difference for an otherwise exact store ID.

    Both named places must be unambiguous in the same state and their official
    representative points must be within 15 miles of the actual station point.
    The returned station coordinates always remain the locator/OSM POI.
    """
    reference = _city_reference()
    anchors = [reference.get(state + ":" + normalize_city(city), []) for city in (first, second)]
    return all(len(values) == 1 and distance_meters(point, values[0]) <= 24140.16 for values in anchors)


def brand(value):
    name = normalize(value)
    if re.match(r"^TA(?:\s|#|$)", value.upper()) or name == "PETRO":
        return "PETRO" if name == "PETRO" else "TA"
    for prefix, canonical in (
        ("LOVES", "LOVES"), ("FLYINGJ", "FLYINGJ"), ("PILOT", "PILOT"),
        ("MRFUEL", "MRFUEL"), ("XPRESS", "XPRESS"), ("PRIDE", "PRIDE"),
        ("PETROSTOPPING", "PETRO"),
        ("TAEXPRESS", "TA"), ("TRAVELCENTERSOFAMERICA", "TA"),
        ("TATRAVEL", "TA"), ("TASEYMOUR", "TA"),
        ("CIRCLEK", "CIRCLEK"), ("KWIKTRIP", "KWIKTRIP"),
        ("KWIKSTAR", "KWIKSTAR"), ("QUIKTRIP", "QUIKTRIP"),
        ("SPEEDWAY", "SPEEDWAY"), ("CASEYS", "CASEYS"),
        ("MAVERIK", "MAVERIK"), ("ROADRANGER", "ROADRANGER"),
        ("ALLSUPS", "ALLSUPS"), ("STRIPES", "STRIPES"),
        ("THORNTONS", "THORNTONS"), ("SHEETZ", "SHEETZ"),
        ("KUMGO", "KUMGO"), ("RACETRAC", "RACETRAC"),
        ("CEFCO", "CEFCO"), ("RUTTERS", "RUTTERS"),
        ("SHELL", "SHELL"), ("MARATHON", "MARATHON"),
        ("CHEVRON", "CHEVRON"), ("EXXON", "EXXON"),
        ("MOBIL", "MOBIL"), ("SUNOCO", "SUNOCO"),
        ("QUICKFUEL", "QUICKFUEL"), ("QUARLES", "QUARLES"),
        ("ONENINE", "ONE9"), ("ONE9", "ONE9"), ("AMERICASTRAVEL", "ONE9"),
    ):
        if name.startswith(prefix):
            return canonical
    return name


def store_number(value):
    match = re.search(r"#\s*(\d+)", value)
    return str(int(match.group(1))) if match else None


def distance_meters(a, b):
    # For deduplicating two OSM features representing the same forecourt.
    latitude = math.radians((a[0] + b[0]) / 2)
    return 111195 * math.hypot(a[0] - b[0], (a[1] - b[1]) * math.cos(latitude))


def unique_poi(candidates):
    if not candidates:
        return None
    first = candidates[0]
    # A node and way often describe the same site. Distinct nearby stations are
    # not collapsed unless their coordinates are within the same small forecourt.
    return first if all(distance_meters(first["coordinates"], item["coordinates"]) < 50 for item in candidates[1:]) else None


def match_osm(stations, elements, corroborate_postal_cities=False):
    """Match only unique chain/ref or chain/city/state identities.

    The city fallback requires uniqueness in BOTH the supplied price data and
    the OSM extract. Thus two Shells in the same town remain unresolved.
    """
    by_ref, by_place = defaultdict(list), defaultdict(list)
    for element in elements:
        tags = element.get("tags", {})
        if tags.get("amenity") != "fuel":
            continue
        point = element.get("center", element)
        try:
            coords = coordinate_pair(point.get("lat"), point.get("lon"), "OSM feature")
        except CommandError:
            continue
        identity = brand(tags.get("brand") or tags.get("name") or "")
        if not identity:
            continue
        state, city = tags.get("addr:state", "").upper(), normalize_city(tags.get("addr:city", ""))
        ref = tags.get("ref", "")
        number = str(int(ref)) if re.fullmatch(r"\d+", ref) else store_number(tags.get("name", ""))
        poi = {"coordinates": coords, "state": state, "city": city, "number": number,
               "source": element.get("source") or f"https://www.openstreetmap.org/{element['type']}/{element['id']}"}
        if number:
            by_ref[(identity, number)].append(poi)
        if state in US_STATES and city:
            by_place[(identity, state, city)].append(poi)
    source_by_ref, source_by_place = defaultdict(list), defaultdict(list)
    for station in stations:
        identity = brand(station.name)
        number = store_number(station.name)
        if number:
            source_by_ref[(identity, number)].append(station)
        source_by_place[(identity, station.state, normalize_city(station.city))].append(station)
    matches = {}
    for station in stations:
        identity = brand(station.name)
        number = store_number(station.name)
        matched = None
        key = (identity, number)
        if number and len(source_by_ref[key]) == 1:
            matched = unique_poi(by_ref[key])
            if matched:
                state_conflict = matched["state"] and matched["state"] != station.state
                city_conflict = matched["city"] and matched["city"] != normalize_city(station.city)
                corroborated = (corroborate_postal_cities and matched["state"] == station.state
                                and nearby_postal_cities(station.city, matched["city"], station.state, matched["coordinates"]))
                if state_conflict or (city_conflict and not corroborated):
                    matched = None
        if matched is None:
            key = (identity, station.state, normalize_city(station.city))
            if len(source_by_place[key]) == 1:
                candidate = unique_poi(by_place[key])
                if candidate and not (number and candidate["number"] and number != candidate["number"]):
                    matched = candidate
        if matched:
            matches[station.external_id] = (*matched["coordinates"], matched["source"])
    return matches


def pilot_network(identity):
    return "PILOTNETWORK" if identity in {"PILOT", "FLYINGJ", "ONE9", "MRFUEL", "XPRESS", "PRIDE"} else identity


def match_atp(stations, features):
    """Reuse strict POI identity matching for CC0 official-locator snapshots.

    Pilot, Flying J and ONE9 share the locator's numeric store-ID namespace.
    Rebrands within that network may therefore match a legacy price-file name;
    the store ID, state and city must still agree.
    """
    mapped_stations = []
    for station in stations:
        identity = pilot_network(brand(station.name))
        number = store_number(station.name)
        mapped_stations.append(SimpleNamespace(external_id=station.external_id,
            name=identity + (f" #{number}" if number else ""), city=station.city, state=station.state))
    elements = []
    for feature in features:
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if properties.get("addr:country", "").upper() != "US" or properties.get("end_date"):
            continue
        if geometry.get("type") != "Point" or len(geometry.get("coordinates", [])) < 2:
            continue
        identity = pilot_network(brand(properties.get("brand") or properties.get("name") or ""))
        known_convenience = identity in {"ALLSUPS", "YESWAY"} and properties.get("shop") == "convenience"
        if not known_convenience and properties.get("amenity") != "fuel" and not any(properties.get(key) == "yes" for key in ("fuel:diesel", "fuel:HGV_diesel", "fuel:gasoline")):
            continue
        if not identity:
            continue
        tags = dict(properties)
        tags["addr:city"] = source_city(properties)
        tags["amenity"] = "fuel"
        tags["brand"] = identity
        tags["ref"] = str(tags.get("ref", ""))
        # The upstream Allsup's spider explicitly uses its WordPress post ID,
        # rather than the public store number in the assessment. Do not compare
        # these numeric namespaces. Unique brand/city/state matching still
        # requires a single source and assessment station in that place.
        if properties.get("@spider") == "allsups_yesway_us":
            tags["ref"] = ""
        if re.fullmatch(r"\d+_fuel", tags["ref"]):
            tags["ref"] = tags["ref"].removesuffix("_fuel")
        source = properties.get("website") or properties.get("@source_uri") or "https://www.alltheplaces.xyz/"
        elements.append({"type": "atp", "id": feature.get("id", ""), "source": source,
                         "lat": geometry["coordinates"][1], "lon": geometry["coordinates"][0], "tags": tags})
    return match_osm(mapped_stations, elements, corroborate_postal_cities=True)


def fetch_atp(cache_directory, user_agent, spiders=ATP_SPIDERS, refresh=False, endpoint=ATP_URL):
    import requests

    cache_directory = Path(cache_directory)
    cache_directory.mkdir(parents=True, exist_ok=True)
    features = []
    for spider in spiders:
        if not re.fullmatch(r"[a-z0-9_]+", spider):
            raise CommandError("Invalid AllThePlaces spider name")
        path = cache_directory / f"{spider}.geojson"
        try:
            if path.exists() and not refresh:
                payload = json.loads(path.read_text())
            else:
                response = requests.get(f"{endpoint.rstrip('/')}/{spider}.geojson", headers={"User-Agent": user_agent}, timeout=(10, 60))
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
                    raise ValueError("missing feature collection")
                path.write_text(json.dumps(payload))
            if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
                raise ValueError("missing feature collection")
            if any(not isinstance(feature, dict) for feature in payload["features"]):
                raise ValueError("invalid feature record")
        except (requests.RequestException, ValueError, OSError) as exc:
            raise CommandError(f"AllThePlaces preparation failed for {spider}; completed downloads remain cached: {exc}") from exc
        features.extend(payload["features"])
    return features


def read_osm_cache(path):
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value.get("elements"), list) or value.get("remark"):
            raise ValueError("missing elements or incomplete Overpass result")
        return value["elements"]
    except (OSError, ValueError, AttributeError) as exc:
        raise CommandError(f"Cannot read OSM cache: {exc}") from exc


def fetch_osm(path, user_agent, endpoint=OVERPASS_URL):
    import requests

    try:
        response = requests.get(endpoint, params={"data": OVERPASS_QUERY}, headers={"User-Agent": user_agent}, timeout=(10, 120))
        response.raise_for_status()
        payload = response.json()
        if payload.get("remark") or not isinstance(payload.get("elements"), list):
            raise ValueError("Overpass returned an incomplete result")
    except (requests.RequestException, ValueError) as exc:
        raise CommandError(f"OSM preparation failed; no coordinates were changed: {exc}") from exc
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))
    return payload["elements"]


def census_candidates(stations):
    # Census requires a structure number; highway-exit descriptions are excluded.
    return [station for station in stations if re.match(r"^\d+\s+[A-Za-z]", station.address)]


def fetch_census(stations, cache_path, user_agent, endpoint=CENSUS_URL):
    import requests

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    for station in stations:
        writer.writerow([station.external_id, station.address, station.city, station.state, ""])
    try:
        response = requests.post(endpoint, files={"addressFile": ("stations.csv", output.getvalue(), "text/csv")},
                                 data={"benchmark": "Public_AR_Current"}, headers={"User-Agent": user_agent}, timeout=(10, 180))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CommandError(f"Census preparation failed; no coordinates were changed: {exc}") from exc
    target = Path(cache_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(response.text)
    return parse_census(response.text, {station.external_id for station in stations})


def parse_census(text, allowed_ids):
    matches = {}
    recognized_response = False
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 3 and row[0] in allowed_ids and row[2] in {"Match", "No_Match", "Tie"}:
            recognized_response = True
        if len(row) < 6 or row[0] not in allowed_ids or row[2] != "Match" or row[3] != "Exact":
            continue
        try:
            lon, lat = row[5].split(",")
            coordinates = coordinate_pair(lat, lon, "Census match")
        except (ValueError, CommandError):
            continue
        matches[row[0]] = (*coordinates, "https://geocoding.geo.census.gov/ (exact address-range match)")
    if allowed_ids and not recognized_response:
        raise CommandError("Census response contains no recognized requested IDs; no stations were changed")
    return matches


def write_locations(path, stations):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as source:
        writer = csv.writer(source)
        writer.writerow(["external_id", "latitude", "longitude", "source"])
        for station in stations:
            if station.latitude is not None:
                writer.writerow([station.external_id, station.latitude, station.longitude, station.location_source])


def write_coverage_report(path, stations):
    """Publish completeness explicitly; missing coordinates are never hidden."""
    by_state = defaultdict(lambda: {"stations": 0, "located": 0})
    for station in stations:
        by_state[station.state]["stations"] += 1
        by_state[station.state]["located"] += station.latitude is not None
    total = sum(value["stations"] for value in by_state.values())
    located = sum(value["located"] for value in by_state.values())
    result = {"total_stations": total, "located_stations": located,
              "unresolved_stations": total - located,
              "coverage_percent": round(100 * located / total, 2) if total else 0,
              "by_state": {state: {**value, "unresolved": value["stations"] - value["located"]}
                           for state, value in sorted(by_state.items())}}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result
