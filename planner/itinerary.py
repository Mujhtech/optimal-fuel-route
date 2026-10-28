"""Bounded route verification and repair for fixed-route fuel planning.

The fuel optimizer is exact for a known ordered itinerary. Station selection is
based on the initial route's projected mile markers, so the selected stops must
be checked against driving distances. At most two additional routing requests
are made here: one verification and, when useful, one repair or cleanup.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
from typing import Callable, Iterable

from django.conf import settings

from .optimization import FuelCandidate, FuelPlan, InfeasibleRoute, optimize_fuel
from .providers import Location, ProviderError, Route, get_route
from .spatial import haversine_miles


class ItineraryError(Exception):
    def __init__(self, message, code, status=422, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


@dataclass(frozen=True)
class ItineraryPlan:
    route: Route
    fuel: FuelPlan
    routing_calls: int
    visited_candidates: tuple[FuelCandidate, ...] = ()
    recovered: bool = False
    cleanup_performed: bool = False


@dataclass(frozen=True)
class _Verified:
    route: Route
    fuel: FuelPlan
    originals: tuple[FuelCandidate, ...]
    actual: tuple[FuelCandidate, ...]


class _UnusableItinerary(Exception):
    def __init__(self, error, originals, route=None, actual=(), bad_keys=(), gap_index=None):
        super().__init__(str(error))
        self.error = error
        self.originals = tuple(originals)
        self.route = route
        self.actual = tuple(actual)
        self.bad_keys = frozenset(bad_keys)
        self.gap_index = gap_index


def _token(candidate):
    # The same station may be passed twice on a looping route. Its original
    # chainage, rather than station ID alone, identifies a route encounter.
    return candidate.key, candidate.mile


def _selected(fuel):
    return tuple(stop.candidate for stop in fuel.stops)


def _coverage_error(exc):
    return ItineraryError(
        "The located stations do not provide a reachable fuel plan for this route.",
        "insufficient_station_coverage",
        details={"gap_start_mile": round(exc.gap_start_mile, 3),
                 "gap_end_mile": round(exc.gap_end_mile, 3)},
    )


def _invalid_route(message="Routing provider returned an incomplete itinerary."):
    return ItineraryError(message, "invalid_upstream_route", 502)


def _finite_number(value):
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


def _verify(route, originals, initial):
    legs = route.leg_distances_miles
    if (len(legs) != len(originals) + 1
            or not _finite_number(route.distance_miles)
            or route.distance_miles < 0
            or any(not _finite_number(leg) or leg < 0 for leg in legs)
            or abs(sum(legs) - route.distance_miles) > 0.01):
        raise _invalid_route()
    snaps = route.snapped_coordinates
    if len(snaps) != len(originals) + 2:
        raise _invalid_route("Routing provider returned incomplete waypoint locations.")
    for point in snaps:
        if (not isinstance(point, (tuple, list)) or len(point) != 2
                or any(not _finite_number(value) for value in point)
                or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
            raise _invalid_route("Routing provider returned invalid waypoint locations.")
    bad_keys = []
    maximum_snap = float(getattr(settings, "MAX_STATION_SNAP_MILES", 0.25))
    for candidate, snapped in zip(originals, snaps[1:-1]):
        station = candidate.station
        if haversine_miles([station["longitude"], station["latitude"]], snapped) > maximum_snap:
            bad_keys.append(candidate.key)
    if bad_keys:
        error = ItineraryError(
            "A selected station could not be snapped to a nearby routable road.",
            "station_access_unverified",
            details={"station_ids": sorted(set(bad_keys))},
        )
        raise _UnusableItinerary(error, originals, route, bad_keys=bad_keys)
    actual = []
    marker = 0.0
    for index, candidate in enumerate(originals):
        marker += legs[index]
        actual.append(FuelCandidate(candidate.key, min(marker, route.distance_miles), candidate.price,
                                    candidate.station, candidate.offset_miles))
    try:
        fuel = optimize_fuel(route.distance_miles, actual, initial_fuel_gallons=initial)
    except InfeasibleRoute as exc:
        positions = [candidate.mile for candidate in actual] + [route.distance_miles]
        gap_index = min(range(len(positions)), key=lambda index: abs(positions[index] - exc.gap_end_mile))
        error = ItineraryError(
            "Road access to the selected fuel stops exceeds the vehicle's available range.",
            "station_access_exceeds_range",
            details={"gap_start_mile": round(exc.gap_start_mile, 3),
                     "gap_end_mile": round(exc.gap_end_mile, 3)},
        )
        raise _UnusableItinerary(error, originals, route, actual, gap_index=gap_index) from exc
    return _Verified(route, fuel, tuple(originals), tuple(actual))


def _request_route(start, finish, selected, routing, initial, max_stops):
    if len(selected) > max_stops:
        raise ItineraryError("This itinerary exceeds the routing provider's waypoint budget.", "too_many_stops")
    waypoints = [Location(latitude=c.station["latitude"], longitude=c.station["longitude"],
                          label=c.station["name"]) for c in selected]
    try:
        route = routing([start, *waypoints, finish])
    except ProviderError as exc:
        if exc.code not in {"route_not_found", "location_off_road"}:
            raise
        raise _UnusableItinerary(exc, selected) from exc
    try:
        return _verify(route, selected, initial)
    except ItineraryError as exc:
        # Optional cleanup must retain exact request counts even when a cached
        # Route is malformed and cannot replace the already verified result.
        exc.requests_made = route.requests_made
        raise


def _optimize_original(distance, candidates, initial):
    try:
        return optimize_fuel(distance, candidates, initial_fuel_gallons=initial)
    except InfeasibleRoute:
        return None


def _alternative_without(distance, candidates, initial, selected, banned=()):
    """Choose a complete alternative locally, without provider lookups.

    A station whose access fails is excluded at every encounter. When the
    provider cannot identify the failing waypoint, try omitting each selected
    station in turn and retain the cheapest feasible projected alternative.
    """
    selections = []
    omitted_sets = [frozenset(banned)] if banned else [frozenset((candidate.key,)) for candidate in selected]
    previous = tuple(_token(candidate) for candidate in selected)
    for omitted in dict.fromkeys(omitted_sets):
        remaining = [candidate for candidate in candidates if candidate.key not in omitted]
        fuel = _optimize_original(distance, remaining, initial)
        if fuel is None:
            continue
        choice = _selected(fuel)
        if tuple(_token(candidate) for candidate in choice) != previous:
            selections.append((fuel.total_cost, len(choice), tuple(_token(c) for c in choice), choice))
    return min(selections, default=(None, None, None, None))[-1]


def _with_bridge(base_distance, candidates, initial, failure):
    """Project observed leg expansion onto all original candidates.

    Including stations that did not initially require a purchase lets a long
    actual leg acquire an intermediate fuel stop. These interpolated markers
    are only used to propose the bounded repair; the third route supplies the
    real distances before any result is returned.
    """
    if not failure.route or not failure.actual:
        return None
    anchors = [(0.0, 0.0)]
    anchors.extend((original.mile, actual.mile)
                   for original, actual in zip(failure.originals, failure.actual))
    anchors.append((base_distance, failure.route.distance_miles))
    by_position = dict(anchors)
    positions = sorted(by_position)
    adjusted = []
    originals = {}
    for candidate in candidates:
        index = max(0, min(bisect_right(positions, candidate.mile) - 1, len(positions) - 2))
        left, right = positions[index:index + 2]
        fraction = (candidate.mile - left) / (right - left) if right > left else 0
        marker = by_position[left] + fraction * (by_position[right] - by_position[left])
        proposal = FuelCandidate(candidate.key, marker, candidate.price, candidate.station, candidate.offset_miles)
        adjusted.append(proposal)
        originals[id(proposal)] = candidate
    fuel = _optimize_original(failure.route.distance_miles, adjusted, initial)
    if fuel is None:
        return None
    selected = tuple(originals[id(stop.candidate)] for stop in fuel.stops)
    if tuple(map(_token, selected)) == tuple(map(_token, failure.originals)):
        return None
    return selected


def _repair(base, candidates, initial, failure):
    if failure.bad_keys:
        return _alternative_without(base.distance_miles, candidates, initial,
                                    failure.originals, failure.bad_keys)
    # If the first selected station is beyond the starting fuel's reach, a
    # direct alternative may be reachable even though this station's detour is
    # not. Interpolating along the failed detour would incorrectly penalize it.
    if failure.gap_index == 0 and failure.originals:
        alternative = _alternative_without(base.distance_miles, candidates, initial,
                                           failure.originals, (failure.originals[0].key,))
        if alternative is not None:
            return alternative
    if failure.gap_index is not None:
        bridge = _with_bridge(base.distance_miles, candidates, initial, failure)
        if bridge is not None:
            return bridge
    return _alternative_without(base.distance_miles, candidates, initial, failure.originals)


def plan_itinerary(
    base_route: Route,
    start: Location,
    finish: Location,
    candidates: Iterable[FuelCandidate],
    initial_fuel_gallons: float = 50.0,
    *,
    routing: Callable | None = None,
    max_stops: int | None = None,
) -> ItineraryPlan:
    """Return a verified fuel plan using at most three total route calls.

    The caller has already requested ``base_route``. This function invokes
    ``routing`` at most twice, even when a repair or cleanup fails. It does not
    claim global optimality across alternative roads or unlocated stations.
    """
    routing = get_route if routing is None else routing
    max_stops = int(getattr(settings, "MAX_FUEL_STOPS", 40) if max_stops is None else max_stops)
    candidates = tuple(sorted(candidates, key=lambda c: (c.mile, c.price, c.key)))
    try:
        initial_plan = optimize_fuel(base_route.distance_miles, candidates,
                                     initial_fuel_gallons=initial_fuel_gallons)
    except InfeasibleRoute as exc:
        raise _coverage_error(exc) from exc
    selected = _selected(initial_plan)
    calls = base_route.requests_made
    if not selected:
        return ItineraryPlan(base_route, initial_plan, calls)
    try:
        verified = _request_route(start, finish, selected, routing, initial_fuel_gallons, max_stops)
        calls += verified.route.requests_made
    except _UnusableItinerary as failure:
        # An upstream rejection or unusable uncached verification still spent
        # its one request. A returned Route carries exact cache-aware counts.
        calls += failure.route.requests_made if failure.route else 1
        repaired = _repair(base_route, candidates, initial_fuel_gallons, failure)
        if repaired is None:
            raise failure.error from failure
        try:
            verified = _request_route(start, finish, repaired, routing, initial_fuel_gallons, max_stops)
        except _UnusableItinerary as final_failure:
            raise final_failure.error from final_failure
        calls += verified.route.requests_made
        return ItineraryPlan(verified.route, verified.fuel, calls, verified.actual, recovered=True)

    actual_to_original = {id(actual): original for original, actual in zip(verified.originals, verified.actual)}
    purchased = tuple(actual_to_original[id(stop.candidate)] for stop in verified.fuel.stops)
    if len(purchased) < len(selected):
        try:
            cleaned = _request_route(start, finish, purchased, routing, initial_fuel_gallons, max_stops)
            calls += cleaned.route.requests_made
        except _UnusableItinerary as failure:
            calls += failure.route.requests_made if failure.route else 1
        except (ProviderError, ItineraryError) as exc:
            calls += getattr(exc, "requests_made", 0 if exc.code == "provider_busy" else 1)
        else:
            if (cleaned.fuel.total_cost, cleaned.route.distance_miles) <= (verified.fuel.total_cost, verified.route.distance_miles):
                return ItineraryPlan(cleaned.route, cleaned.fuel, calls, cleaned.actual, cleanup_performed=True)
    return ItineraryPlan(verified.route, verified.fuel, calls, verified.actual)
