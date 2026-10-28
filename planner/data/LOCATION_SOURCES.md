# Verified station-coordinate snapshot

The price CSV has highway-exit descriptions, names and prices, with no
latitude/longitude columns. `station_locations.csv` supplies actual station
points keyed by the original OPIS IDs. Prices always come from the assessment
file; coordinate sources do not replace prices.

Prepared on **October 28, 2026**. The snapshot currently contains **2,744** located
stations of **6,626** unique US stations (**41.41%**), spanning **47 states**.
This improves the initial 1,936-point snapshot by 808 stations. The remaining
**3,882** stations are retained with missing coordinates and excluded from
spatial optimization. `coverage_report.json` gives state-level completeness,
source-file SHA-256 hashes, and price/coordinate-file checksums.
`unresolved_stations.csv` lists every unresolved OPIS ID and original address.
The snapshot is deliberately incomplete; it does not imply every station in
the price file participates in the optimization or is currently operating.

## Coordinate sources and licensing

**2,509 points** match [AllThePlaces](https://www.alltheplaces.xyz/) snapshots,
released under **CC0 1.0** and derived from chain-published locators. The
[official API](https://github.com/alltheplaces/alltheplaces/blob/master/API.md)
provides `/runs/latest/output/{spider}.geojson`. These sources were downloaded
on October 28, 2026. Actual station/source URLs are preserved in each row.
Raw provider downloads are cached during preparation but are not bundled.

| AllThePlaces dataset | Selected points |
| --- | ---: |
| `pilot_flying_j` | 610 |
| `loves_us` | 542 |
| `kwik_trip` | 337 |
| `circle_k` | 230 |
| `travelcenters_of_america_us` | 186 |
| `maverik` | 117 |
| `caseys_general_store_us` | 84 |
| `quiktrip_us` | 80 |
| `allsups_yesway_us` | 69 |
| `sheetz` | 44 |
| `road_ranger_us` | 39 |
| `rutters_us` | 36 |
| `shell` | 29 |
| `cefco_us` | 23 |
| `thorntons_us` | 22 |
| `chevron_us` | 19 |
| `marathon_petroleum_us` | 15 |
| `sunoco_us` | 11 |
| `phillips_66_conoco_76` | 8 |
| `cenex` | 3 |
| `sinclair_us` | 3 |
| `valero` | 2 |

`speedway_us` was inspected but its current public IDs differ from the
assessment's legacy store-number namespace. The snapshot instead obtains
Speedway matches from OSM's comparable identifiers; conflicting numeric IDs
are never silently treated as matching store numbers.

An additional **235 points** match **OpenStreetMap**, available under
[ODbL 1.0](https://www.openstreetmap.org/copyright), with attribution
**© OpenStreetMap contributors**. These rows retain individual OSM feature
URLs. A node and a way representing one forecourt can be merged only when
within 50 meters; distinct sites remain ambiguous. The merged table retains
OSM attribution; CC0 applies specifically to the AllThePlaces-derived rows.

Two successful, bounded preparation-time Overpass requests retrieved 1,661
and 3,678 POIs respectively, using the same US area and `out center tags`:

```overpass
[out:json][timeout:90];
area["ISO3166-1"="US"][admin_level=2]->.us;
nwr[amenity=fuel][brand~"Love|Pilot|Flying J|TravelCenters|Petro",i](area.us);
out center tags;
```

The second request used the brand expression
`Speedway|Allsup|Quarles|Quick Fuel|Racetrac|Stripes`.
An additional 17 OSM points were corroborated by full-precision Census
state/place polygons, filling missing address tags only when exactly one
containing administrative area existed. Matching still required a unique
comparable chain/store ID or exact independent business name, city and state.
The source coordinates were never moved. The preparation helper and tests
live in `osm_spatial.py`; the optional `--state-boundaries` and
`--place-boundaries` arguments accept the source GeoJSON. Official source:
[2025 Census national place boundaries](https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_place_500k.zip).

Broader country-wide and regional requests timed out and were not treated as
complete extracts. Preparation failures never introduce invented station points.

## Identity and geographic corroboration

1. Normalize known brands and store numbers, then require a unique comparable
   brand/store ID in both source and price-file records. Different states are
   rejected. Pilot, Flying J and ONE9 share the official locator namespace;
   legacy network brands may match that same store identity.
2. Expand established city abbreviations (`St`/`Saint`, `Ft`/`Fort`,
   `Mt`/`Mount`). Parse a city from a source's complete postal address only
   when its explicit state agrees.
3. For an otherwise exact unique official chain/store ID with different postal
   city names, corroborate both names using the **2025 US Census Places
   Gazetteer**. Both names must be unambiguous within the same state, and both
   official representative points must be within 15 miles of the actual locator
   point. Distant city mismatches and conflicting store numbers are rejected.
4. Without comparable store IDs, require a unique brand/city/state identity
   in **both** the assessment data and complete loaded provider data. Multiple
   same-brand stations in a town are left unresolved.
5. The Allsup's upstream spider explicitly sets `ref` to a WordPress post ID,
   rather than its public store number. Only for that documented source is
   the numeric ref excluded from store-number comparisons. The two-sided
   unique brand/city/state rule still applies. See the
   [primary source code](https://github.com/alltheplaces/alltheplaces/blob/master/locations/spiders/allsups_yesway_us.py).
6. Reject closed or non-US source records, ambiguous identities and invalid
   coordinates. Output coordinates always come from actual station points.

`postal_city_reference.json.gz` contains normalized Census place references
solely for identity corroboration. **It is never a source of output station
coordinates**, route waypoints, or fuel-stop positions. It was derived from
[the official 2025 Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.2025.html),
[national places download](https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip).
No city center, estimated highway exit or guessed coordinate substitutes for
an unresolved station.

## Rebuild or extend the snapshot

Import prices first, then prepare coordinates outside serving requests:

```sh
python manage.py geocode_stations --provider atp --cache /data/geocoding/atp \
  --output /data/station_locations.csv --report /data/unresolved_stations.csv \
  --coverage-report /data/station_coverage.json

python manage.py geocode_stations --provider osm --cache /data/geocoding/osm.json \
  --output /data/station_locations.csv --report /data/unresolved_stations.csv \
  --coverage-report /data/station_coverage.json
```

Default ATP preparation downloads the relevant 23 chain files once, retaining
completed files if a later download fails. `--refresh` explicitly retrieves
new snapshots. `--spiders` accepts a comma-separated subset. The OSM command
accepts a complete cached Overpass response and otherwise runs a bounded chain
query. Default downloads and exports live under `DATA_DIR` (`/data` in Docker),
so manual preparation never silently overwrites the bundled snapshot.
Existing verified coordinates are retained; reviewed authoritative
replacements can be supplied through `import_fuel_prices --locations`.

Optional `--provider census` submits only actual numbered street addresses
in one batch and accepts only exact address-range matches. Highway-exit
descriptions are excluded because Census requires structure addresses. See
[the Census API documentation](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html).

Coordinate updates invalidate cached route results through the dataset
revision inside the same database transaction. Docker startup imports the
bundled coordinate table without contacting these services. Route requests
never call AllThePlaces, Overpass, Census or the city reference dataset.
