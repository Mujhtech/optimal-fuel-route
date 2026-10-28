import json
import logging
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError
from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import cache_control
from django.views.decorators.gzip import gzip_page

from .geography import InvalidLocation
from .providers import ProviderError
from .service import PlanningError, plan_trip
from .service import coverage_summary
from .data.revision import get_dataset_revision

logger = logging.getLogger(__name__)


def error(message, code, status, details=None):
    body = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    response = JsonResponse(body, status=status)
    if status == 503:
        response["Retry-After"] = "5"
    return response


def index(request):
    return render(request, "planner/index.html")


def health(request):
    try:
        snapshot = get_dataset_revision()
        return JsonResponse({"status": "ok", "stations": snapshot.station_count,
                             "located_stations": snapshot.located_count})
    except DatabaseError:
        return error("The station database is unavailable.", "database_unavailable", 503)


@csrf_exempt
@gzip_page
def plan_route(request):
    if request.method != "POST":
        response = error("Use POST with a JSON request body.", "method_not_allowed", 405)
        response["Allow"] = "POST"
        return response
    if request.content_type != "application/json":
        return error("Content-Type must be application/json.", "unsupported_media_type", 415)
    try:
        def reject_constant(value):
            raise ValueError(f"Invalid JSON number: {value}")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate JSON field: {key}")
                result[key] = value
            return result
        payload = json.loads(request.body, parse_constant=reject_constant, object_pairs_hook=unique_object)
        return JsonResponse(plan_trip(payload), json_dumps_params={"separators": (",", ":")})
    except RequestDataTooBig:
        return error("The JSON request exceeds 16 KB.", "request_too_large", 413)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return error("The body must contain valid UTF-8 JSON.", "invalid_json", 400)
    except (PlanningError, ProviderError) as exc:
        return error(str(exc), exc.code, exc.status, getattr(exc, "details", None))
    except InvalidLocation as exc:
        return error(str(exc), "invalid_location", 400)
    except ValueError as exc:
        return error(str(exc), "invalid_request", 400)
    except DatabaseError:
        logger.exception("Station database unavailable")
        return error("The station database is unavailable.", "database_unavailable", 503)


@gzip_page
def route_map(request, plan_id):
    result = cache.get(f"plan:{plan_id}")
    if result is None or "fuel_purchases_cost_usd" not in result:
        return error("This map has expired. Request the route again.", "map_not_found", 404)
    return render(request, "planner/map.html", {"plan": result})


def coverage(request):
    try:
        return JsonResponse({**coverage_summary(get_dataset_revision()),
                             "coordinate_policy": "Verified station points only; unresolved stations are excluded.",
                             "price_source": "fuel-prices-for-be-assessment.csv"})
    except DatabaseError:
        return error("The station database is unavailable.", "database_unavailable", 503)


def schema(request):
    path = Path(settings.BASE_DIR) / "openapi.json"
    return JsonResponse(json.loads(path.read_text()))


@cache_control(public=True, max_age=86400)
@gzip_page
def state_basemap(request):
    path = Path(settings.BASE_DIR) / "planner" / "data" / "us_states_map.geojson"
    return FileResponse(path.open("rb"), content_type="application/geo+json")


@cache_control(public=True, max_age=31536000, immutable=True)
def leaflet_asset(request, asset):
    if asset not in {"leaflet.js", "leaflet.css"}:
        return error("Asset not found.", "not_found", 404)
    path = Path(settings.BASE_DIR) / "planner" / "static" / "planner" / "leaflet-1.9.4" / asset
    return FileResponse(path.open("rb"), content_type="text/css" if asset.endswith(".css") else "application/javascript")
