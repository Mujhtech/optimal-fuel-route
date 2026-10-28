from decimal import Decimal
from unittest import TestCase

from planner.accounting import InitialFuelPrice, fuel_accounting, initial_price_quote
from planner.optimization import FuelCandidate, FuelPlan, FuelStop, optimize_fuel
from planner.providers import Location


class FuelAccountingTests(TestCase):
    def test_short_trip_charges_only_consumed_starting_fuel(self):
        plan = optimize_fuel(300, [])
        cost = fuel_accounting(plan, 50, InitialFuelPrice(Decimal("3.20"), "user_supplied"))
        self.assertEqual(cost["total_cost_usd"], "96.00")
        self.assertEqual(cost["fuel_purchases_cost_usd"], "0.00")
        self.assertEqual(cost["initial_fuel_consumed_gallons"], 30)
        self.assertFalse(cost["initial_price_is_estimate"])

    def test_long_trip_separates_initial_inventory_from_purchases(self):
        plan = optimize_fuel(700, [FuelCandidate("A", 400, Decimal("3"))])
        cost = fuel_accounting(plan, 50, InitialFuelPrice(Decimal("4"), "user_supplied"))
        self.assertEqual(cost["total_cost_usd"], "260.00")
        self.assertEqual(cost["fuel_purchases_cost_usd"], "60.00")
        self.assertEqual(cost["initial_fuel_consumed_cost_usd"], "200.00")

    def test_empty_initial_tank_does_not_require_a_benchmark(self):
        plan = optimize_fuel(100, [FuelCandidate("origin", 0, Decimal("3"))], initial_fuel_gallons=0)
        cost = fuel_accounting(plan, 0, None)
        self.assertEqual(cost["total_cost_usd"], "30.00")
        self.assertEqual(cost["fuel_purchases_cost_usd"], "30.00")

    def test_zero_distance_does_not_require_price(self):
        self.assertEqual(fuel_accounting(optimize_fuel(0, []), 50, None)["total_cost_usd"], "0.00")

    def test_unknown_initial_price_is_nearest_actual_station_quote(self):
        stations = [
            {"external_id": "far", "latitude": 41, "longitude": -87, "price_per_gallon": Decimal("2")},
            {"external_id": "near", "latitude": 40, "longitude": -87, "price_per_gallon": Decimal("4")},
        ]
        quote = initial_price_quote(Location(40, -87), stations)
        self.assertEqual(quote.price, Decimal("4"))
        self.assertEqual(quote.station_id, "near")
        self.assertEqual(quote.distance_miles, 0)
        self.assertTrue(quote.estimated)

    def test_explicit_price_needs_no_station_data(self):
        quote = initial_price_quote(Location(40, -87), [], 3.25)
        self.assertEqual(quote.price, Decimal("3.25"))
        self.assertFalse(quote.estimated)

    def test_missing_initial_price_is_not_treated_as_free(self):
        with self.assertRaises(ValueError):
            initial_price_quote(Location(40, -87), [])
        with self.assertRaises(ValueError):
            fuel_accounting(optimize_fuel(300, []), 50, None)

    def test_rounding_sums_displayed_consumption_components(self):
        plan = optimize_fuel(700, [FuelCandidate("A", 400, Decimal("3.00733333"))])
        cost = fuel_accounting(plan, 50, InitialFuelPrice(Decimal("3.0001"), "user_supplied"))
        self.assertEqual(Decimal(cost["total_cost_usd"]),
                         Decimal(cost["initial_fuel_consumed_cost_usd"]) + Decimal(cost["en_route_consumed_cost_usd"]))

    def test_consumed_purchase_preserves_exact_half_cent_charge(self):
        candidate = FuelCandidate("A", 0, Decimal("1.5"))
        gallons = float(Decimal("0.003333333333333333333333333333"))
        exact_cost = Decimal("0.004999999999999999999999999999")
        stop = FuelStop(candidate, gallons, exact_cost, 0, gallons)
        plan = FuelPlan((stop,), exact_cost, gallons, gallons, 0)
        costs = fuel_accounting(plan, 0, None)
        self.assertEqual(costs["fuel_purchases_cost_usd"], "0.00")
        self.assertEqual(costs["en_route_consumed_cost_usd"], "0.00")
