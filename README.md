# Optimal Fuel Route API

Django **6.1.2**, Python 3.13, and Docker. Accepts US start/finish locations and returns a driving route, cost-effective fuel purchases, total trip fuel cost, GeoJSON, and an interactive map. Vehicle assumptions are fixed at **500 miles**, **10 MPG**, and a **50 US gallon** tank.

## Run

```sh
docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000). No API keys or registration are needed. Startup applies migrations and imports the supplied prices and bundled station coordinates **without network calls**. The named Docker volume preserves SQLite data, provider caches, and maps.

```sh
curl --fail-with-body http://localhost:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{
    "start": {"latitude": 41.8781, "longitude": -87.6298},
    "finish": {"latitude": 40.7128, "longitude": -74.0060}
  }'
```

Place names also work:

```json
{
  "start": "Chicago, IL",
  "finish": "New York, NY",
  "initial_fuel_gallons": 50,
  "initial_fuel_price_per_gallon": 3.25
}
```

Only `start` and `finish` are required. Each accepts a place name or named latitude/longitude fields. USA validation uses a bundled Census boundary, including Alaska and Hawaii; it requires no reverse-geocoding call. Returned GeoJSON coordinates use **longitude, latitude** order.

## Response and costs

- `route`: verified road distance in miles and duration in seconds.
- `map`: GeoJSON FeatureCollection containing the **single** full route geometry, endpoints, and numbered fuel markers. Fuel markers reference the station entries by number and ID.
- `fuel_stops`: station names, addresses, source coordinates, mile markers, quoted USD/gallon prices, gallons to purchase, costs, and fuel on arrival/departure.
- `total_cost_usd`: the estimated value of **fuel consumed during the trip**, including consumed starting inventory.
- `fuel_purchases_cost_usd`: planned purchases at stations along the route, separately from starting inventory.
- `fuel`: consumption and inventory quantities, consumed-cost breakdown, and the starting-price source.
- `map_url`: interactive map on the same host; locally served Leaflet and Census state outlines require **no external tiles or CDN**.
- `metadata`: request counts, cache status, recovery details, coordinate coverage, and exclusions.

The vehicle starts full by default. Starting fuel is consumed first (FIFO). A 300-mile trip consumes 30 gallons; if the starting price is $3/gallon, its fuel cost is **$90**, even though no new fuel purchase is required. The unused 20 gallons are not charged to the trip.

Supply `initial_fuel_price_per_gallon` when known. Otherwise, the API estimates that price from the **nearest located station in the supplied list** and exposes `initial_price_is_estimate`, `initial_price_station_id`, and `initial_price_distance_miles`. This is a quoted-price benchmark, not a claim that fuel was bought at that station. A remote benchmark can be inappropriate; an explicit starting price removes that uncertainty. No unknown starting inventory is treated as free.

`initial_fuel_gallons` accepts 0–50. An empty initial tank requires a priced station reachable at the origin. Zero-distance trips consume no fuel and need no price benchmark. Prices are assumed to be comparable USD per US gallon; source prices are a snapshot, not live quotes.

All currency arithmetic uses `Decimal`. Each station's purchase charge and consumed-inventory component are rounded to cents using `ROUND_HALF_UP`. The total sums the displayed consumed-cost components. Gallons are displayed to six decimal places, with calculations performed before display rounding.

## Routing budget and algorithm

| Request | OSRM routing calls | Separate geocoding calls |
| --- | ---: | ---: |
| Cold coordinate trip needing no purchases | 1 | 0 |
| Cold coordinate trip with purchases | normally 2; at most 3 | 0 |
| Cold place-name trip | normally 1–2; at most 3 | up to 2 |
| Identical cached request | 0 | 0 |
| Map display | 0 | 0 |

A fully cold text request can therefore use four external calls normally, or five when a repair is needed; **routing itself never exceeds three calls**. Coordinates avoid geocoding entirely. No station-by-station network lookups occur in the request path.

1. Request one complete initial driving route from OSRM.
2. Project located stations onto its full geometry using a vectorized spatial index and a one-mile lateral corridor. Looping routes can encounter the same station more than once.
3. Apply the exact fixed-route fuel policy: reach the first cheaper station within the real **500-mile** range; otherwise fill the tank or buy only enough to finish. Starting fuel is never artificially reduced. Sorting is O(n log n); the next-cheaper calculation and fuel scan are O(n).
4. Request a single itinerary through all chosen stops and recalculate purchases using its actual road-leg distances.
5. If a selected station is inaccessible or a real leg exceeds range, propose an alternative or bridge stop locally and verify it in the third call. If recalculation drops a purchase, that remaining call can instead remove unnecessary waypoints. Optional cleanup failures preserve an already verified feasible result.
6. Return an explicit error when no plan can be verified within the budget. Bounded recovery does not prove that every alternative itinerary has been exhausted.

Purchases are optimal **for the verified ordered itinerary**. Station selection is a corridor heuristic over the located subset. The API does not claim a global minimum over all roads or unlocated stations. Road-snapped waypoints more than 0.25 miles from a station are rejected; short forecourt/driveway movements are not modeled. OSRM uses the ordinary driving profile, without truck-specific restrictions. Both endpoints must be US locations; transit through another country is not prohibited.

See [DESIGN.md](DESIGN.md) for the proof, invariants, recovery policy, and architecture decisions.

The read-only HTTP helper `scripts/demo_request.py` prints concise responses instead of dumping full geometry. For example, run `docker compose exec -T api python scripts/demo_request.py long --repeat` against the running application.

## Data completeness and preparation

The original `fuel-prices-for-be-assessment.csv` is preserved. Of its 8,151 rows, 620 non-US rows are excluded and 905 duplicate US rows are merged by OPIS ID, keeping the lowest listed price. With no observation timestamps, this is a declared duplicate policy, not a newest-price claim. All **6,626 unique US stations** are imported transactionally.

The input has no latitude/longitude columns, and almost all addresses describe highway exits. The bundled coordinate snapshot contains **2,744 verified station points (41.41%)**, expanded from 1,936. **3,882 stations remain unresolved** and are excluded from spatial planning; the API cannot guarantee coverage or the cheapest station among those excluded records.

Coordinates come from documented public chain-locator snapshots and OpenStreetMap POIs. Ambiguous identities and conflicting store numbers are rejected. Census place references can corroborate names near an actual station, but **city centers are never substituted for station coordinates**.

- [LOCATION_SOURCES.md](planner/data/LOCATION_SOURCES.md): sources, matching rules, and licensing.
- [coverage_report.json](planner/data/coverage_report.json): per-state coverage and input/output SHA-256 hashes.
- [unresolved_stations.csv](planner/data/unresolved_stations.csv): every unresolved OPIS ID.
- `GET /api/coverage/`: current database coverage. Every route response also reports exclusions.

This incomplete source-location coverage is a material limitation, intentionally exposed instead of hidden with approximate station positions. Imports upsert records and do not delete previously imported stations. Identical reimports preserve cache validity; price, address, and coordinate changes publish a new dataset revision.

To add authoritative positions, supply:

```csv
external_id,latitude,longitude,source
12345,41.123,-87.456,https://example.com/verified-station
```

```sh
# Import a portable coordinate CSV already available inside the container.
docker compose exec api python manage.py import_fuel_prices \
  fuel-prices-for-be-assessment.csv --locations /data/station_locations.csv

# Optional offline preparation; defaults persist under DATA_DIR=/data.
docker compose exec api python manage.py geocode_stations --provider atp

docker compose exec api python manage.py geocode_stations --provider osm
```

Preparation downloads are cached; `--refresh` explicitly replaces provider caches. The command writes portable coordinates, unresolved records, and a coverage report under `/data` by default. `--output`, `--report`, and `--coverage-report` can override those paths. `--provider census` submits only actual numbered street addresses in a batch and accepts exact matches; highway-exit descriptions are excluded. Successful bulk coordinate updates publish one cache revision; failures leave database coordinates unchanged.

The bundled snapshot is sufficient for the verified example routes, and no preparation is required to start the app. To ship additional verified coordinates in a new image, copy the exported coordinate CSV into `planner/data/station_locations.csv`, regenerate its coverage artifacts, and rebuild.

## Verification

```sh
docker compose run --rm api python manage.py test -v 2
```

The tests use no external services. They cover the optimizer against an independent exhaustive oracle, exact range boundaries, expensive-detour regression, alternate/bridge recovery, three-call bounds, conservation, FIFO costs, rounding, invalid inputs, coordinate identity, dataset publication/rollback, independent map eviction, tiny-cache culling, and simultaneous identical requests.

```sh
# Optional real-provider integration check; saves responses under var/.
python scripts/verify_live.py

# Offline CPU, response-size, query, and cached-request benchmarks.
python scripts/benchmark.py --compare-legacy
```

Use those commands inside the container, or prefix `python` with `.venv/bin/` locally. The benchmark performs no HTTP requests. Its legacy comparison verifies identical station candidates; external latency, CPU work, response size and warm-cache timings are measured separately. See [BENCHMARKS.md](BENCHMARKS.md) for recorded results and their limits.

`GET /api/schema/` serves the OpenAPI 3.1 contract. `GET /health/` checks the database and reports station counts. Errors use `{"error":{"code":"...","message":"...","details":{}}}`: malformed input/non-US coordinates 400, oversized body 413, unsupported media 415, unverified/unrouteable trip 422, bad upstream 502, missing data/congestion 503, timeout 504, expired map 404. JSON bodies are limited to 16 KB; duplicate fields, non-finite numbers and excessive nesting are rejected.

## Development and deployment

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python manage.py migrate
.venv/bin/python manage.py import_fuel_prices fuel-prices-for-be-assessment.csv --locations planner/data/station_locations.csv
.venv/bin/python manage.py runserver
```

Docker runs Gunicorn as a nonroot user, with a health check and pinned dependencies. Set `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, and an identifying/contact `PROVIDER_USER_AGENT` for deployment; see `.env.example`. SQLite and a shared file cache keep this small static-data assessment self-contained. One indexed singleton-row query identifies the dataset generation; station rows are loaded once per process and revision. Bulk imports publish a revision only when records change.

Result, route and map caches last up to 24 hours; geocoding caches last 30 days. Dataset changes and semantic provider configuration invalidate results. Cache hits restore independently evicted maps. Shared file locks coalesce identical requests across workers. SQLite coordinates the public-provider rate gate, allowing at most one request/second per service with bounded queuing and no HTTP retries. JSON is compact and supports gzip; full route geometry is returned once.

The supplied deployment is one container on one host. For greater throughput, use a privately hosted routing service and then choose shared cache/database infrastructure based on measured workload; free public-provider limits remain the main cold-request constraint.

## Primary references

- [Django downloads](https://www.djangoproject.com/download/): 6.1.2, latest stable verified October 28, 2026.
- [OSRM API](https://project-osrm.org/docs/v5.24.0/api/) and [public-server policy](https://routing.openstreetmap.de/about.html): free, modest use, one request/second, no availability guarantee; configurable `OSRM_BASE_URL`.
- [Photon](https://photon.komoot.io/): free endpoint geocoding; configurable `GEOCODING_BASE_URL`.
- [AllThePlaces](https://www.alltheplaces.xyz/) and [OpenStreetMap](https://www.openstreetmap.org/copyright): prepared station coordinates; source-specific licensing is documented in the manifest.
- [US Census cartographic boundaries](https://www.census.gov/geographies/mapping-files/2025/geo/carto-boundary-file.html): local validation/basemap; provenance in `planner/data/BOUNDARY_SOURCE.md`.
