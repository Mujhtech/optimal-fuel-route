"""Value the fuel consumed, keeping purchase cash flow separate from inventory.

Starting fuel is consumed first (FIFO). Its price is either caller supplied or
the nearest located station's quoted price, explicitly labelled as an estimate.
No part of an unused starting tank is charged to the trip's consumption cost.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from .optimization import FuelPlan
from .providers import Location
from .spatial import haversine_miles


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class InitialFuelPrice:
    price: Decimal
    source: str
    station_id: str | None = None
    distance_miles: float | None = None

    @property
    def estimated(self):
        return self.source == "nearest_listed_station"


def initial_price_quote(start: Location, stations: Iterable[dict], supplied_price=None) -> InitialFuelPrice:
    if supplied_price is not None:
        return InitialFuelPrice(Decimal(str(supplied_price)), "user_supplied")
    closest = None
    for station in stations:
        distance = haversine_miles((start.longitude, start.latitude),
                                   (station["longitude"], station["latitude"]))
        rank = (distance, str(station["external_id"]))
        if closest is None or rank < closest[0]:
            closest = (rank, station)
    if closest is None:
        raise ValueError("A starting-fuel price or at least one located priced station is required to value consumed initial fuel.")
    rank, station = closest
    return InitialFuelPrice(Decimal(str(station["price_per_gallon"])), "nearest_listed_station",
                            str(station["external_id"]), rank[0])


def fuel_accounting(plan: FuelPlan, initial_gallons: float, quote: InitialFuelPrice | None) -> dict:
    """Return cents-consistent FIFO consumption cost and planned cash purchases."""
    consumed = Decimal(str(plan.fuel_consumed_gallons))
    initial_used = min(Decimal(str(initial_gallons)), consumed)
    if initial_used and quote is None:
        raise ValueError("Consumed initial fuel requires a price.")
    initial_cost = Decimal(money(initial_used * quote.price)) if quote else Decimal(0)
    remaining_use = consumed - initial_used
    purchased_cost = Decimal(0)
    purchased_used_cost = Decimal(0)
    for stop in plan.stops:
        purchased_cost += Decimal(money(stop.cost))
        gallons = Decimal(str(stop.gallons))
        used = min(remaining_use, gallons)
        # Preserve the optimizer's exact Decimal charge when a whole purchase
        # is consumed. A display-oriented float gallon conversion must not
        # change which side of a half-cent boundary the charge falls on.
        value = stop.cost if gallons - used <= Decimal("0.00000001") else used * stop.candidate.price
        purchased_used_cost += Decimal(money(value))
        remaining_use -= used
    if remaining_use > Decimal("0.00000001"):
        raise ValueError("Fuel accounting does not balance with the planned consumption.")
    return {
        "total_cost_usd": money(initial_cost + purchased_used_cost),
        "fuel_purchases_cost_usd": money(purchased_cost),
        "initial_fuel_consumed_gallons": round(float(initial_used), 6),
        "initial_fuel_consumed_cost_usd": money(initial_cost),
        "en_route_consumed_cost_usd": money(purchased_used_cost),
        "initial_fuel_price_per_gallon_usd": str(quote.price) if quote else None,
        "initial_price_source": quote.source if quote else "not_needed",
        "initial_price_is_estimate": quote.estimated if quote else False,
        "initial_price_station_id": quote.station_id if quote else None,
        "initial_price_distance_miles": round(quote.distance_miles, 3) if quote and quote.distance_miles is not None else None,
    }
