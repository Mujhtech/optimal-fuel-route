"""Minimum-cost fuel purchases on a fixed, ordered route.

For a constant consumption rate, the greedy rule is exact: buy enough to
reach the first cheaper station within range; otherwise fill the tank (or
buy only enough to finish). Initial fuel is inventory, so its acquisition
cost is deliberately outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


@dataclass(frozen=True)
class FuelCandidate:
    key: str
    mile: float
    price: Decimal
    station: Any = None
    offset_miles: float = 0.0


@dataclass(frozen=True)
class FuelStop:
    candidate: FuelCandidate
    gallons: float
    cost: Decimal
    arrival_fuel_gallons: float
    departure_fuel_gallons: float


@dataclass(frozen=True)
class FuelPlan:
    stops: tuple[FuelStop, ...]
    total_cost: Decimal
    fuel_purchased_gallons: float
    fuel_consumed_gallons: float
    final_fuel_gallons: float


class InfeasibleRoute(ValueError):
    def __init__(self, start_mile: float, end_mile: float) -> None:
        self.gap_start_mile = start_mile
        self.gap_end_mile = end_mile
        super().__init__(
            f"Insufficient fuel to travel from mile {start_mile:.2f} "
            f"to mile {end_mile:.2f}."
        )


def _number(value: object, label: str) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite number.") from exc


def optimize_fuel(
    distance_miles: float,
    candidates: Iterable[FuelCandidate],
    initial_fuel_gallons: float = 50.0,
    mpg: float = 10.0,
    range_miles: float = 500.0,
) -> FuelPlan:
    """Return optimal purchases, excluding the cost of initial inventory.

    Complexity is O(n log n) including sorting; a monotonic stack identifies
    the first cheaper station in O(n). Prices and fuel arithmetic use
    Decimal, and currency is rounded only by the caller for presentation.
    Stations exactly at the destination are ignored because no purchase is
    necessary there. Stations sharing a mile marker use the cheapest price.
    """
    distance = _number(distance_miles, "distance_miles")
    consumption = _number(mpg, "mpg")
    maximum_distance = _number(range_miles, "range_miles")
    fuel = _number(initial_fuel_gallons, "initial_fuel_gallons")
    if distance < 0 or consumption <= 0 or maximum_distance <= 0:
        raise ValueError("Distance must be nonnegative; mpg and range must be positive.")
    capacity = maximum_distance / consumption
    if not 0 <= fuel <= capacity:
        raise ValueError("Initial fuel must be between zero and tank capacity.")

    prepared: list[tuple[FuelCandidate, Decimal, Decimal]] = []
    for candidate in candidates:
        mile = _number(candidate.mile, "candidate mile")
        price = _number(candidate.price, "candidate price")
        if not 0 <= mile <= distance or price <= 0:
            raise ValueError("Candidate miles must lie on the route and prices must be positive.")
        if mile < distance:
            prepared.append((candidate, mile, price))
    prepared.sort(key=lambda item: (item[1], item[2], str(item[0].key)))
    unique: list[tuple[FuelCandidate, Decimal, Decimal]] = []
    for item in prepared:
        if not unique or unique[-1][1] != item[1]:
            unique.append(item)

    # A zero-price destination makes the same rule handle the final purchase.
    positions = [item[1] for item in unique] + [distance]
    prices = [item[2] for item in unique] + [Decimal(0)]
    next_cheaper = [len(unique)] * len(unique)
    stack: list[int] = []
    for index in range(len(positions) - 1, -1, -1):
        while stack and prices[stack[-1]] >= prices[index]:
            stack.pop()
        if index < len(unique) and stack:
            next_cheaper[index] = stack[-1]
        stack.append(index)

    stops: list[FuelStop] = []
    previous = Decimal(0)
    total_cost = Decimal(0)
    total_purchased = Decimal(0)
    tolerance = Decimal("0.000000001")
    for index, position in enumerate(positions):
        required = (position - previous) / consumption
        if required > fuel + tolerance:
            raise InfeasibleRoute(float(previous), float(position))
        fuel = max(Decimal(0), fuel - required)
        if index == len(unique):
            break

        cheaper_distance = positions[next_cheaper[index]] - position
        if cheaper_distance <= maximum_distance:
            desired = cheaper_distance / consumption
        else:
            desired = min(capacity, (distance - position) / consumption)
        purchase = max(Decimal(0), desired - fuel)
        if purchase > tolerance:
            cost = purchase * prices[index]
            stops.append(
                FuelStop(
                    candidate=unique[index][0],
                    gallons=float(purchase),
                    cost=cost,
                    arrival_fuel_gallons=float(fuel),
                    departure_fuel_gallons=float(fuel + purchase),
                )
            )
            total_cost += cost
            total_purchased += purchase
            fuel += purchase
        previous = position

    return FuelPlan(
        stops=tuple(stops),
        total_cost=total_cost,
        fuel_purchased_gallons=float(total_purchased),
        fuel_consumed_gallons=float(distance / consumption),
        final_fuel_gallons=float(fuel),
    )
