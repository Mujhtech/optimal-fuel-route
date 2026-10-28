from decimal import Decimal
import random
import unittest

from planner.optimization import FuelCandidate, InfeasibleRoute, optimize_fuel


def candidate(key, mile, price):
    return FuelCandidate(str(key), mile, Decimal(str(price)))


class FuelOptimizationTests(unittest.TestCase):
    def test_short_route_uses_initial_inventory_without_purchase(self):
        plan = optimize_fuel(400, [])
        self.assertEqual(plan.total_cost, Decimal(0))
        self.assertEqual(plan.fuel_consumed_gallons, 40)
        self.assertEqual(plan.final_fuel_gallons, 10)
        self.assertEqual(plan.stops, ())

    def test_only_buys_enough_to_reach_cheaper_station(self):
        plan = optimize_fuel(
            900,
            [candidate("expensive", 400, 5), candidate("cheap", 600, 2)],
        )
        self.assertEqual([stop.candidate.key for stop in plan.stops], ["expensive", "cheap"])
        self.assertEqual([stop.gallons for stop in plan.stops], [10, 30])
        self.assertEqual(plan.total_cost, Decimal("110"))
        self.assertEqual(plan.final_fuel_gallons, 0)

    def test_fills_at_cheap_station_to_skip_expensive_station(self):
        plan = optimize_fuel(
            1000,
            [candidate("cheap", 300, 2), candidate("expensive", 600, 5), candidate("last", 800, 3)],
        )
        self.assertEqual([stop.candidate.key for stop in plan.stops], ["cheap", "last"])
        self.assertEqual([stop.gallons for stop in plan.stops], [30, 20])
        self.assertEqual(plan.total_cost, Decimal("120"))

    def test_equal_price_does_not_create_unnecessary_stops(self):
        plan = optimize_fuel(500, [candidate(1, 0, 3), candidate(2, 200, 3)], 0)
        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(plan.stops[0].candidate.key, "1")
        self.assertEqual(plan.stops[0].gallons, 50)

    def test_shared_location_uses_cheapest_price_and_deterministic_id(self):
        plan = optimize_fuel(
            600,
            [candidate("c", 400, 4), candidate("b", 400, 2), candidate("a", 400, 2)],
        )
        self.assertEqual(plan.stops[0].candidate.key, "a")
        self.assertEqual(plan.total_cost, Decimal("20"))

    def test_initial_empty_tank_can_buy_at_origin(self):
        plan = optimize_fuel(200, [candidate(1, 0, "3.00733333")], 0)
        self.assertEqual(plan.total_cost, Decimal("60.14666660"))
        self.assertEqual(plan.stops[0].gallons, 20)

    def test_exact_range_is_reachable(self):
        plan = optimize_fuel(1000, [candidate(1, 500, 3)])
        self.assertEqual(plan.stops[0].arrival_fuel_gallons, 0)
        self.assertEqual(plan.stops[0].gallons, 50)

    def test_gap_fails_with_the_missing_interval(self):
        with self.assertRaises(InfeasibleRoute) as context:
            optimize_fuel(1100, [candidate(1, 500, 3)])
        self.assertEqual(context.exception.gap_start_mile, 500)
        self.assertEqual(context.exception.gap_end_mile, 1100)

    def test_empty_tank_without_origin_station_is_infeasible(self):
        with self.assertRaises(InfeasibleRoute):
            optimize_fuel(100, [candidate(1, 10, 3)], 0)

    def test_station_at_finish_never_causes_a_purchase(self):
        plan = optimize_fuel(500, [candidate(1, 500, 1)])
        self.assertEqual(plan.stops, ())

    def test_zero_distance(self):
        self.assertEqual(optimize_fuel(0, []).final_fuel_gallons, 50)

    def test_invalid_values_are_rejected(self):
        for values in [(float("nan"), 50), (-1, 50), (10, 51), (10, -1), (10, True)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                optimize_fuel(values[0], [], initial_fuel_gallons=values[1])
        for invalid in [candidate(1, -1, 3), candidate(1, 101, 3), candidate(1, 1, 0)]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                optimize_fuel(100, [invalid])

    def test_random_cases_match_independent_exhaustive_fuel_state_oracle(self):
        """Small integer-gallon trips compare against all possible purchases."""
        rng = random.Random(828)
        for case in range(100):
            distance_units = rng.randrange(2, 20)
            positions = sorted(rng.sample(range(distance_units), rng.randrange(1, min(distance_units, 8))))
            prices = [rng.randrange(1, 7) for _ in positions]
            initial = rng.randrange(6)
            fuel_states = {initial: Decimal(0)}
            previous = 0
            for position, price in list(zip(positions, prices)) + [(distance_units, None)]:
                travelled = position - previous
                arrived = {fuel - travelled: cost for fuel, cost in fuel_states.items() if fuel >= travelled}
                if price is None:
                    fuel_states = arrived
                    break
                after_purchase = {}
                for fuel, cost in arrived.items():
                    for purchase in range(6 - fuel):
                        final_fuel = fuel + purchase
                        final_cost = cost + purchase * price
                        after_purchase[final_fuel] = min(after_purchase.get(final_fuel, Decimal("Infinity")), final_cost)
                fuel_states = after_purchase
                previous = position
            candidates = [candidate(i, p * 10, price) for i, (p, price) in enumerate(zip(positions, prices))]
            with self.subTest(case=case):
                if not fuel_states:
                    with self.assertRaises(InfeasibleRoute):
                        optimize_fuel(distance_units * 10, candidates, initial, mpg=10, range_miles=50)
                else:
                    plan = optimize_fuel(distance_units * 10, candidates, initial, mpg=10, range_miles=50)
                    self.assertEqual(plan.total_cost, min(fuel_states.values()))
                    self.assertTrue(all(stop.departure_fuel_gallons <= 5 for stop in plan.stops))
