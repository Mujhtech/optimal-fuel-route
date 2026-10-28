"""Concise, real-HTTP requests for the Loom demo; no external Python packages.

Run against the running app, for example:
    docker compose exec -T api python scripts/demo_request.py long --repeat

The first request can already be cached. This script never clears caches or
changes application data. Repeating means sending the same successful request
again, not retrying a failed provider request.
"""
import argparse
import json
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


PRESETS = {
    "long": {
        "start": {"latitude": 34.0522, "longitude": -118.2437},
        "finish": {"latitude": 40.7128, "longitude": -74.0060},
        "initial_fuel_price_per_gallon": 4.00,
    },
    "short": {
        "start": {"latitude": 40.7128, "longitude": -74.0060},
        "finish": {"latitude": 39.9526, "longitude": -75.1652},
        "initial_fuel_price_per_gallon": 3.00,
    },
    "invalid": {
        "start": {"latitude": 43.6532, "longitude": -79.3832},
        "finish": {"latitude": 40.7128, "longitude": -74.0060},
    },
}


def base_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("Use an HTTP(S) base URL without a query or fragment.")
    return value.rstrip("/")


def request_once(url, payload):
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers={
        "Content-Type": "application/json", "Accept": "application/json",
    })
    started = perf_counter()
    try:
        with urlopen(request, timeout=100) as response:
            status, body = response.status, response.read()
    except HTTPError as response:
        status, body = response.code, response.read()
    elapsed = round(perf_counter() - started, 3)
    try:
        result = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"HTTP {status}: the server returned non-JSON content; check the app logs.") from exc
    if not isinstance(result, dict):
        raise ValueError(f"HTTP {status}: the server returned an unexpected JSON response.")
    return status, result, elapsed


def summarize(result, status, elapsed, public_url, max_stops):
    summary = {"status": status, "elapsed_seconds": elapsed}
    if status != 200:
        summary["error"] = result.get("error", {"message": "Unexpected HTTP error; inspect the application logs."})
        return summary
    try:
        stops = result["fuel_stops"]
        metadata, fuel = result["metadata"], result["fuel"]
        summary.update({
            "distance_miles": result["route"]["distance_miles"],
            "fuel_stop_count": len(stops),
            "total_cost_usd": result["total_cost_usd"],
            "fuel_purchases_cost_usd": result["fuel_purchases_cost_usd"],
            "initial_price_source": fuel["initial_price_source"],
            "initial_fuel_price_per_gallon_usd": fuel["initial_fuel_price_per_gallon_usd"],
            "initial_price_is_estimate": fuel["initial_price_is_estimate"],
            "routing_api_calls": metadata["routing_api_calls"],
            "geocoding_api_calls": metadata["geocoding_api_calls"],
            "result_cache_hit": metadata["result_cache_hit"],
            "map_url": public_url + result["map_url"],
            "stop_rows": [{key: stop[key] for key in (
                "number", "name", "state", "mile_marker", "gallons_purchased",
                "cost_usd", "arrival_fuel_gallons", "departure_fuel_gallons",
            )} for stop in stops[:max_stops]],
        })
        if len(stops) > max_stops:
            summary["more_stops_on_map"] = len(stops) - max_stops
    except (KeyError, TypeError) as exc:
        raise ValueError("The success response does not match this app's API contract; check the base URL and deployed version.") from exc
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preset", nargs="?", choices=PRESETS, default="long")
    parser.add_argument("--repeat", action="store_true", help="Send the identical successful request twice to show caching.")
    parser.add_argument("--base-url", type=base_url, default="http://127.0.0.1:8000", help="Server URL reachable from this process.")
    parser.add_argument("--public-base-url", type=base_url, default="http://localhost:8000", help="Browser URL used in the printed map link.")
    parser.add_argument("--max-stops", type=int, default=3, help="Maximum stop rows to print (default: 3); the map shows all stops.")
    options = parser.parse_args(argv)
    if options.max_stops < 0:
        parser.error("--max-stops must be nonnegative")

    for index in range(2 if options.repeat else 1):
        try:
            status, result, elapsed = request_once(options.base_url + "/api/route/", PRESETS[options.preset])
            summary = {"preset": options.preset, "request": index + 1,
                       **summarize(result, status, elapsed, options.public_base_url, options.max_stops)}
        except (URLError, TimeoutError, OSError) as exc:
            print(json.dumps({"error": "Unable to reach the API.", "detail": str(exc),
                              "action": "Run docker compose up -d --build, then confirm --base-url points to the running server."}, indent=2))
            return 1
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            return 1
        print(json.dumps(summary, indent=2))
        if options.preset == "invalid" and status == 200:
            print(json.dumps({"error": "Expected the non-US preset to be rejected with HTTP 400; check the deployed version."}, indent=2))
            return 1
        if status != 200:
            error = result.get("error")
            expected_invalid = options.preset == "invalid" and status == 400 and isinstance(error, dict) and error.get("code") == "invalid_location"
            return 0 if expected_invalid else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
