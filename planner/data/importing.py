"""Validated, network-free ingestion of the supplied OPIS CSV."""

import csv
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from planner.models import Station
from planner.geography import is_in_usa
from planner.data.revision import bump_revision

US_STATES = frozenset("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
REQUIRED = {"OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Retail Price"}


def coordinate_pair(latitude, longitude, context):
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        raise CommandError(f"{context}: latitude and longitude must be numbers")
    try:
        lat, lon = float(latitude), float(longitude)
    except (ValueError, TypeError) as exc:
        raise CommandError(f"{context}: latitude and longitude must be numbers") from exc
    # Station state is validated independently. These bounds include Alaska and Hawaii.
    if not (math.isfinite(lat) and math.isfinite(lon) and 18 <= lat <= 72 and (-180 <= lon <= -65 or 170 <= lon <= 180)):
        raise CommandError(f"{context}: coordinates are outside the United States bounds")
    if not is_in_usa(lat, lon):
        raise CommandError(f"{context}: coordinates must be within the United States")
    return lat, lon


def read_locations(path):
    if not path:
        return {}
    result = {}
    try:
        with Path(path).open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            fields = set(reader.fieldnames or [])
            id_key = "external_id" if "external_id" in fields else "OPIS Truckstop ID"
            if not {id_key, "latitude", "longitude"} <= fields:
                raise CommandError("Location CSV requires external_id, latitude, longitude columns")
            for line, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    raise CommandError(f"Location line {line}: malformed CSV row")
                station_id = row[id_key].strip()
                if not station_id or len(station_id) > 32:
                    raise CommandError(f"Location line {line}: invalid station ID")
                lat, lon = coordinate_pair(row["latitude"], row["longitude"], f"Location line {line}")
                source_label = (row.get("source") or "supplemental CSV").strip()
                if len(source_label) > 500:
                    raise CommandError(f"Location line {line}: source exceeds 500 characters")
                value = (lat, lon, source_label)
                if station_id in result and result[station_id] != value:
                    raise CommandError(f"Location line {line}: conflicting coordinates for {station_id}")
                result[station_id] = value
    except OSError as exc:
        raise CommandError(f"Cannot read location CSV: {exc}") from exc
    return result


def read_prices(path):
    """Validate the entire file before any writes; keep the minimum price per OPIS ID."""
    rows, skipped, duplicates = {}, 0, 0
    try:
        with Path(path).open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if not REQUIRED <= set(reader.fieldnames or []):
                missing = sorted(REQUIRED - set(reader.fieldnames or []))
                raise CommandError(f"Price CSV missing columns: {', '.join(missing)}")
            for line, raw in enumerate(reader, start=2):
                if None in raw or any(value is None for value in raw.values()):
                    raise CommandError(f"Price line {line}: malformed CSV row")
                state = (raw.get("State") or "").strip().upper()
                if state not in US_STATES:
                    skipped += 1
                    continue
                row = {}
                for column, field, maximum in (("OPIS Truckstop ID", "external_id", 32), ("Truckstop Name", "name", 255), ("Address", "address", 255), ("City", "city", 128)):
                    value = (raw.get(column) or "").strip()
                    if not value or len(value) > maximum:
                        raise CommandError(f"Price line {line}: {column} must contain 1–{maximum} characters")
                    row[field] = value
                try:
                    price = Decimal((raw.get("Retail Price") or "").strip())
                    if not price.is_finite() or not 0 < price < 100:
                        raise InvalidOperation
                    quantized = price.quantize(Decimal("0.00000001"))
                    if quantized != price:
                        raise InvalidOperation
                except (InvalidOperation, ValueError) as exc:
                    raise CommandError(f"Price line {line}: Retail Price must be positive, below 100, with at most 8 decimal places") from exc
                row.update(state=state, price_per_gallon=quantized)
                lat, lon = (raw.get("latitude") or "").strip(), (raw.get("longitude") or "").strip()
                if lat or lon:
                    lat, lon = coordinate_pair(lat, lon, f"Price line {line}")
                    row.update(latitude=lat, longitude=lon, location_source="price CSV")
                previous = rows.get(row["external_id"])
                if previous:
                    if tuple(previous[field].upper() for field in ("address", "city", "state")) != tuple(row[field].upper() for field in ("address", "city", "state")):
                        raise CommandError(f"Price line {line}: conflicting locations for station {row['external_id']}")
                    duplicates += 1
                    if row["price_per_gallon"] >= previous["price_per_gallon"]:
                        continue
                rows[row["external_id"]] = row
    except OSError as exc:
        raise CommandError(f"Cannot read price CSV: {exc}") from exc
    if not rows:
        raise CommandError("Price CSV contains no valid US stations")
    return rows, skipped, duplicates


def import_prices(path, locations_path=None):
    rows, skipped, duplicates = read_prices(path)
    locations = read_locations(locations_path)
    existing = {station.external_id: station for station in Station.objects.all()}
    created, updated = [], []
    mutable_fields = ["name", "address", "city", "state", "price_per_gallon", "latitude", "longitude", "location_source", "geocoding_attempted"]
    with transaction.atomic():
        imported_at = timezone.now()
        for station_id, values in rows.items():
            station = existing.get(station_id)
            previous_values = None
            if station is None:
                station = Station(external_id=station_id)
                created.append(station)
            else:
                previous_values = tuple(getattr(station, field) for field in mutable_fields)
                # A changed address invalidates a previous geocode unless supplied afresh.
                if (station.address, station.city, station.state) != (values["address"], values["city"], values["state"]):
                    station.latitude = station.longitude = None
                    station.location_source = ""
                    station.geocoding_attempted = False
            for field, value in values.items():
                setattr(station, field, value)
            if station_id in locations:
                station.latitude, station.longitude, station.location_source = locations[station_id]
            if station.latitude is not None:
                station.geocoding_attempted = True
            if previous_values is not None and previous_values != tuple(getattr(station, field) for field in mutable_fields):
                station.updated_at = imported_at
                updated.append(station)
        Station.objects.bulk_create(created, batch_size=500)
        if updated:
            Station.objects.bulk_update(updated, fields=[*mutable_fields, "updated_at"], batch_size=500)
        if created or updated:
            bump_revision()
    return {"created": len(created), "updated": len(updated), "duplicates": duplicates, "non_us_rows": skipped, "located": Station.objects.filter(latitude__isnull=False).count(), "total": Station.objects.count()}
