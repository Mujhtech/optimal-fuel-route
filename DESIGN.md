# Design and correctness

## Contract and assumptions

The requested trip is a driving route between US endpoints. A 500-mile range and 10 MPG imply a 50-gallon tank. The tank starts full unless the caller supplies a different initial quantity. Distances are OSRM road distances between snapped waypoints; source points and snapping limits are checked before returning a plan. A station visit is not assumed reachable merely because it is near a route on a map.

`total_cost_usd` values fuel consumed, using FIFO inventory accounting. Starting inventory is priced explicitly by the caller or by an identified nearest-station quote estimate. Only the portion consumed is charged. Planned purchases are a separate cash-flow field. This resolves the otherwise unspecified starting-price assumption without returning free fuel or charging unused inventory.

Money uses decimal source prices, not binary floating-point prices. Currency is rounded only at component presentation. Route geometry/distance is measured data and uses floating point; the core optimizer converts mile markers and fuel calculations to Decimal before buying decisions. Serialized gallon quantities are rounded after planning and never fed back into it.

## Exact fuel optimization on an ordered route

Let tank capacity be 50 gallons, consumption be one gallon per ten miles, and stations be ordered by route mile. At station i:

1. Find the first strictly cheaper station j within 500 miles. Buy enough to reach j, accounting for fuel already in the tank.
2. If none exists, fill the tank, unless the destination needs less fuel.

Why this is optimal: if j exists, buying fuel at i for travel after j can be replaced with a cheaper purchase at j. Before j, every intermediate station costs at least as much as i, so purchasing there cannot improve the cost of reaching j. If no cheaper station is reachable, fuel bought at i can replace equally priced or more expensive fuel before the next refill; filling cannot increase optimal cost. Existing inventory may already exceed the target quantity, in which case buy nothing. A zero-price destination handles the final purchase without buying unnecessary fuel there.

Equal-position candidates use the cheapest station deterministically. A monotonic stack finds the next cheaper station in O(n), avoiding a per-station forward scan. Sorting is O(n log n); the fuel scan is O(n). For any fixed itinerary, initial fuel consumed is constant, so adding its FIFO value does not change which purchases minimize cost.

Tests compare the implementation with an independent exhaustive discrete-fuel dynamic-programming oracle on randomized cases, in addition to exact-boundary, precision, tie, empty-tank and gap examples.

## Station selection and bounded road verification

The optimizer's guarantee applies to a known ordered itinerary, not to every possible road/station combination. Selecting that itinerary is deliberately bounded by the assessment's provider-call budget:

- Call 1 obtains the complete initial route. Local spatial work finds actual station points inside a configurable corridor. Candidate chainage follows cumulative route geometry, normalized to OSRM's distance. A station may have several encounters on a loop.
- Initial optimization uses the true capacity and initial inventory. There is no artificial 496-mile tank or dropped starting fuel.
- Call 2 routes through all selected stops. Actual legs replace projected mile markers, and the fuel optimizer runs again.
- If a station fails access validation, the local proposal excludes it and considers other candidates. A first-leg detour can be replaced with another reachable station. If a long later leg needs a bridge, measured leg expansion is used to propose an additional candidate; these interpolated values are never returned as verified distances.
- Call 3 verifies that repair. If call 2 was already feasible but some visits no longer require a purchase, call 3 can remove those visits instead. Failed optional cleanup retains the feasible verified itinerary, accounting for the attempted call.

At most three routing invocations occur, including the initial route. Cache-aware metadata reports actual upstream calls. No station-by-station directions, matrix, reverse-geocode, or price lookup occurs in a request. A third verification failure is an explicit error; the code does not claim that no route exists anywhere, only that it could not verify one within its available candidates and budget.

Regression tests include A at mile 490/$10 and B at 499/$1 on a 700-mile trip (choose B directly), inaccessible cheap A with reachable B (third-call recovery), actual-leg expansion requiring an unused bridge, repeated station encounters, and cleanup failures that preserve an existing valid result.

## Data integrity and incomplete coordinates

The source has prices but no coordinates. Highway exits and city names do not identify a station point reliably enough for a 500-mile guarantee. Coordinates therefore come from actual public POIs matched using documented store identities and geographic evidence. Ambiguity is retained as missing data. All source prices are imported, but unlocated records are excluded from spatial optimization and counted in every response.

The coordinate manifest, unresolved IDs, per-state counts and checksums make this limitation inspectable. Improving coordinate completeness can change recommended stops and feasibility. The result is not advertised as a global minimum over the entire source file.

Import validation precedes writes, conflicting identities fail explicitly, and a single transaction publishes station changes and a new dataset token. An unchanged reimport does not invalidate caches. A singleton DatasetRevision row supplies one indexed hot-path lookup and cached counts. Bulk write paths call the publication helper; ordinary model saves/deletes invalidate automatically. Rollback and concurrent-request behavior are covered by tests.

## Performance decisions

The request path performs no repeated station lookups. Located rows are cached per process and dataset token. Shapely vectorized segment construction and STRtree queries reduce CPU while preserving candidate identity/order. Full-resolution geometry is necessary for reliable candidate projection and is retained internally; the response includes it exactly once. Compact JSON and gzip reduce transport costs without degrading route geometry.

Caching is split between provider results and complete plans so updated prices can reuse route geometry. Semantic configuration and dataset tokens participate in cache keys. Independent map eviction is repaired by a cached request, and the map is published last to avoid culling it during the same request. Shared locks prevent identical simultaneous requests from multiplying upstream calls. A separate provider gate enforces public-service request rates across Gunicorn workers.

SQLite and local shared files are deliberate for this one-host, small static-dataset assessment. They keep Docker testing reproducible with no credentials or extra services. Larger deployments should measure query/serialization/provider costs before introducing PostgreSQL/Redis or additional workers; public routing quotas do not disappear when backend infrastructure changes.

## Verification limits

Unit/integration tests are deterministic and use no external HTTP. Opt-in live checks verify the actual provider contract, default estimated costs, physical road-leg fuel conservation, map rendering, caching and the request budget. Offline benchmarks isolate CPU, query behavior, response size and warm-cache latency. Two or three live trips are not a load test or a tail-latency guarantee.

Road snapping and a car driving profile do not model access gates, truck restrictions, closures, toll tradeoffs, or every driveway. Input-country boundaries are cartographic approximations; the route can transit another country. These are explicit domain limits, separate from the tested fuel arithmetic and request budget.
