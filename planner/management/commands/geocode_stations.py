import csv
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from planner.data.geocoding import ATP_SPIDERS, census_candidates, fetch_atp, fetch_census, fetch_osm, match_atp, match_osm, parse_census, read_osm_cache, write_coverage_report, write_locations
from planner.models import Station
from planner.data.revision import bump_revision
from planner.data.osm_spatial import enrich_osm_places, match_named_stations


class Command(BaseCommand):
    help = "Prepare station coordinates once using cached AllThePlaces/OSM POIs or Census batch geocoding. Never runs during trip requests."

    def add_arguments(self, parser):
        parser.add_argument("--provider", choices=["atp", "osm", "census"], default="atp")
        parser.add_argument("--cache", help="Cached raw response path; reused without network if present")
        parser.add_argument("--refresh", action="store_true", help="Explicitly replace cached provider response")
        parser.add_argument("--output", default=str(settings.DATA_DIR / "station_locations.csv"), help="Portable located-stations CSV")
        parser.add_argument("--report", default=str(settings.DATA_DIR / "unresolved_stations.csv"), help="CSV of stations still missing coordinates")
        parser.add_argument("--coverage-report", default=str(settings.DATA_DIR / "station_coverage.json"), help="JSON completeness summary per state")
        parser.add_argument("--endpoint", help="Optional compatible private provider endpoint")
        parser.add_argument("--spiders", default=",".join(ATP_SPIDERS), help="AllThePlaces spider names, comma separated")
        parser.add_argument("--state-boundaries", help="Optional full-precision Census state GeoJSON for OSM tag corroboration")
        parser.add_argument("--place-boundaries", help="Optional Census place GeoJSON; requires --state-boundaries")

    def handle(self, *args, **options):
        stations = list(Station.objects.order_by("external_id"))
        if not stations:
            raise CommandError("Import fuel prices before preparing coordinates")
        provider = options["provider"]
        cache = Path(options["cache"] or (settings.DATA_DIR / "geocoding" / ("atp" if provider == "atp" else f"{provider}_stations.{'json' if provider == 'osm' else 'csv'}")))
        user_agent = settings.PROVIDER_USER_AGENT
        if provider == "atp":
            kwargs = {"endpoint": options["endpoint"]} if options["endpoint"] else {}
            features = fetch_atp(cache, user_agent, spiders=options["spiders"].split(","), refresh=options["refresh"], **kwargs)
            matches = match_atp(stations, features)
        elif provider == "osm":
            # Match against all stations so excluding previously located records
            # cannot turn an ambiguous city/chain identity into a unique one.
            if cache.exists() and not options["refresh"]:
                elements = read_osm_cache(cache)
            else:
                kwargs = {"endpoint": options["endpoint"]} if options["endpoint"] else {}
                elements = fetch_osm(cache, user_agent, **kwargs)
            if options["place_boundaries"] and not options["state_boundaries"]:
                raise CommandError("--place-boundaries requires --state-boundaries")
            if options["state_boundaries"]:
                def boundaries(path):
                    if not path:
                        return []
                    try:
                        payload = json.loads(Path(path).read_text())
                        if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
                            raise ValueError("Expected a GeoJSON FeatureCollection")
                        return payload["features"]
                    except (OSError, ValueError) as exc:
                        raise CommandError(f"Cannot read Census boundary file: {exc}") from exc
                elements = enrich_osm_places(elements, boundaries(options["state_boundaries"]), boundaries(options["place_boundaries"]))
            matches = {**match_named_stations(stations, elements), **match_osm(stations, elements)}
        else:
            unresolved = census_candidates([station for station in stations if station.latitude is None and (options["refresh"] or not station.geocoding_attempted)])
            if not unresolved:
                self.stdout.write("No new numbered street addresses need Census geocoding.")
                matches = {}
            elif cache.exists() and not options["refresh"]:
                matches = parse_census(cache.read_text(), {station.external_id for station in unresolved})
            else:
                if len(unresolved) > 10000:
                    raise CommandError("Census batch limit is 10,000; supply a smaller dataset")
                kwargs = {"endpoint": options["endpoint"]} if options["endpoint"] else {}
                matches = fetch_census(unresolved, cache, user_agent, **kwargs)
        changed = []
        now = timezone.now()
        for station in stations:
            if station.latitude is None and station.external_id in matches:
                station.latitude, station.longitude, station.location_source = matches[station.external_id]
                station.geocoding_attempted = True
                station.updated_at = now
                changed.append(station)
        with transaction.atomic():
            if changed:
                Station.objects.bulk_update(changed, ["latitude", "longitude", "location_source", "geocoding_attempted", "updated_at"], batch_size=500)
                bump_revision()
            if provider == "census":
                Station.objects.filter(pk__in=[station.pk for station in unresolved]).update(geocoding_attempted=True)
        write_locations(options["output"], stations)
        write_coverage_report(options["coverage_report"], stations)
        report = Path(options["report"])
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("w", newline="", encoding="utf-8") as source:
            writer = csv.writer(source)
            writer.writerow(["external_id", "name", "address", "city", "state"])
            writer.writerows((station.external_id, station.name, station.address, station.city, station.state) for station in stations if station.latitude is None)
        located = sum(station.latitude is not None for station in stations)
        self.stdout.write(self.style.SUCCESS(f"new_coordinates={len(changed)}; located={located}; unresolved={len(stations)-located}"))
        self.stdout.write(f"Portable coordinates: {options['output']}; unresolved report: {report}")
