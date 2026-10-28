# Verification and benchmark results

Measured October 28, 2026 with Python 3.13.3, Django 6.1.2 and Shapely 2.1.2 on the local development host. The database held 6,626 US stations, including 2,744 located stations. All figures below are observations, not latency or throughput guarantees.

## Offline components

Reproduce after generating live responses, or supply saved response JSON files:

```sh
python scripts/benchmark.py --compare-legacy --iterations 7
```

The script performs **zero external HTTP calls**. Seven-run medians use the same geometry and station records for both spatial implementations, and assert that every candidate result is identical.

| Component | Chicago–New York | Los Angeles–New York |
| --- | ---: | ---: |
| Route geometry vertices |13,376|34,359|
| Candidate encounters |43|126|
| Previous individual segment constructors |90.78 ms|191.07 ms|
| Vectorized spatial projection |54.00 ms|91.48 ms|
| Duplicate-geometry compact JSON baseline |611,970 bytes|1,601,215 bytes|
| Single-geometry compact JSON |307,224 bytes|804,094 bytes|
| Gzip level 6 body |93,755 bytes|244,075 bytes|
| JSON serialization |5.45 ms|15.43 ms|
| Cached API, identity encoding |20.35 ms|54.65 ms|
| Cached API, gzip encoding |30.38 ms|82.34 ms|

The full-resolution route geometry is preserved. Deduplication cuts JSON size by approximately 49.8%; gzip further reduces transfer size with additional CPU cost. The cache benchmark uses the production-style shared file backend, explicitly forbids provider calls, and includes the hot-path database lookup, map republication, and response serialization.

Dataset revision lookup: **0.134 ms**, exactly **one indexed primary-key query**, with **no station-table query**. SQLite EXPLAIN reports an index search on `planner_datasetrevision`. The previous hot-path station aggregation was a full scan; counts are now computed only when publishing a data update.

## Opt-in live checks

```sh
python scripts/verify_live.py
```

These checks contacted the real free OSRM provider using coordinate inputs (zero geocoding calls). They verified fuel conservation, physical tank capacity, purchase and consumed-cost totals, map access, cache hits, and map-eviction recovery.

| Trip | Road distance | Purchases | Routing calls | First response | Cached repeat |
| --- | ---: | ---: | ---: | ---: | ---: |
| Chicago–New York |791.323 mi|1|2|2.306 s|24 ms|
| Los Angeles–New York |2,801.412 mi|9|2|2.376 s|59 ms|
| New York–Philadelphia |94.293 mi|0|1|0.628 s|6 ms|

The short trip reports **$37.71 consumed fuel** and **$0.00 route purchases**, demonstrating that starting inventory is valued without charging unused fuel. The cross-country trip reports **$947.02 consumed fuel** and **$708.07 route purchases**. Starting prices in these examples are explicitly identified nearest-listed-station estimates, so amounts depend on that snapshot and assumption.

Provider response time and route availability vary. These examples establish successful integrations, not p95/p99 latency or load capacity. Public-service rate limits govern cold-request throughput. Deterministic tests exercise the acceptable third routing call for repairs and cleanup without sending artificial workloads to public services.

## Automated verification

The test suite runs without external HTTP and covers optimizer-oracle comparisons, station identities, geographic validation, source integrity, FIFO accounting, actual-leg recovery, call ceilings, currency rounding, transactional data revisions, cache eviction, and concurrency. Four simultaneous identical requests are tested against real file locks/cache and must share one upstream call and plan ID.

Use the final Docker image for the authoritative test run:

```sh
docker compose run --rm api python manage.py test -v 2
```

Live-response files and local caches live under `DATA_DIR` (`var/` locally, `/data` in Docker) and are intentionally excluded from the build context.
