from decimal import Decimal
from unittest.mock import Mock

from django.test import SimpleTestCase

from planner.itinerary import ItineraryError, plan_itinerary
from planner.optimization import FuelCandidate
from planner.providers import Location, ProviderError, Route


class ItineraryTests(SimpleTestCase):
    def setUp(self):
        self.start = Location(40, -90, "Start")
        self.finish = Location(40, -75, "Finish")
        self.geometry = {"type": "LineString", "coordinates": [[-90, 40], [-75, 40]]}

    def candidate(self, key, mile, price, longitude=-83):
        station = {"external_id": key, "name": f"Fuel {key}", "latitude": 40,
                   "longitude": longitude, "price_per_gallon": Decimal(str(price))}
        return FuelCandidate(key, mile, Decimal(str(price)), station)

    def route(self, legs, stations=(), *, requests_made=1, snaps=None):
        if snaps is None:
            snaps = tuple([[-90, 40]] + [[candidate.station["longitude"], 40] for candidate in stations] + [[-75, 40]])
        return Route(self.geometry, sum(legs), 36000, tuple(legs), snaps,
                     cache_hit=not requests_made, requests_made=requests_made)

    def assert_conservation(self, plan, initial=50):
        self.assertAlmostEqual(initial + plan.fuel.fuel_purchased_gallons,
                               plan.fuel.fuel_consumed_gallons + plan.fuel.final_fuel_gallons)
        for stop in plan.fuel.stops:
            self.assertGreaterEqual(stop.arrival_fuel_gallons, 0)
            self.assertLessEqual(stop.departure_fuel_gallons, 50)

    def test_short_trip_uses_no_more_routing_calls(self):
        routing = Mock()
        plan = plan_itinerary(self.route([300]), self.start, self.finish, [], routing=routing)
        routing.assert_not_called()
        self.assertEqual(plan.routing_calls, 1)
        self.assertEqual(plan.fuel.fuel_consumed_gallons, 30)
        self.assert_conservation(plan)

    def test_actual_500_mile_range_does_not_force_avoidable_expensive_stop(self):
        expensive = self.candidate("A", 490, 10, -84)
        cheap = self.candidate("B", 499, 1, -83)
        routing = Mock(return_value=self.route([499, 201], [cheap]))
        plan = plan_itinerary(self.route([700]), self.start, self.finish,
                              [expensive, cheap], routing=routing)
        self.assertEqual([stop.candidate.key for stop in plan.fuel.stops], ["B"])
        self.assertEqual(plan.fuel.total_cost, Decimal("20.0"))
        self.assertEqual([point.label for point in routing.call_args.args[0]], ["Start", "Fuel B", "Finish"])
        self.assertEqual(routing.call_count, 1)
        self.assertEqual(plan.routing_calls, 2)
        self.assert_conservation(plan)

    def test_unreachable_first_station_recovers_with_a_reachable_alternative(self):
        cheap = self.candidate("A", 400, 1, -84)
        alternative = self.candidate("B", 450, 3, -83)
        routing = Mock(side_effect=[self.route([505, 205], [cheap]),
                                    self.route([450, 250], [alternative])])
        plan = plan_itinerary(self.route([700]), self.start, self.finish,
                              [cheap, alternative], routing=routing)
        self.assertTrue(plan.recovered)
        self.assertEqual(plan.routing_calls, 3)
        self.assertEqual(routing.call_count, 2)
        self.assertEqual([stop.candidate.key for stop in plan.fuel.stops], ["B"])
        self.assertEqual(plan.fuel.total_cost, Decimal("60.0"))
        self.assert_conservation(plan)

    def test_expanded_middle_leg_inserts_a_previously_unused_bridge_station(self):
        first = self.candidate("A", 400, 1, -86)
        bridge = self.candidate("C", 700, 4, -82)
        last = self.candidate("B", 900, 1, -79)
        routing = Mock(side_effect=[self.route([400, 530, 270], [first, last]),
                                    self.route([400, 310, 240, 270], [first, bridge, last])])
        plan = plan_itinerary(self.route([1200]), self.start, self.finish,
                              [first, bridge, last], routing=routing)
        self.assertEqual(plan.routing_calls, 3)
        self.assertTrue(plan.recovered)
        self.assertEqual([point.label for point in routing.call_args_list[0].args[0]],
                         ["Start", "Fuel A", "Fuel B", "Finish"])
        self.assertEqual([point.label for point in routing.call_args_list[1].args[0]],
                         ["Start", "Fuel A", "Fuel C", "Fuel B", "Finish"])
        self.assertEqual([stop.candidate.key for stop in plan.fuel.stops], ["A", "C", "B"])
        self.assertEqual(plan.route.distance_miles, 1220)
        self.assert_conservation(plan)

    def test_far_station_snap_is_replaced(self):
        cheap = self.candidate("A", 400, 1, -84)
        alternative = self.candidate("B", 450, 3, -83)
        failed = self.route([400, 300], [cheap], snaps=([-90, 40], [-85, 40], [-75, 40]))
        routing = Mock(side_effect=[failed, self.route([450, 250], [alternative])])
        plan = plan_itinerary(self.route([700]), self.start, self.finish,
                              [cheap, alternative], routing=routing)
        self.assertTrue(plan.recovered)
        self.assertEqual(plan.routing_calls, 3)
        self.assertEqual([stop.candidate.key for stop in plan.fuel.stops], ["B"])

    def test_provider_no_route_can_recover_using_one_alternative(self):
        cheap = self.candidate("A", 400, 1, -84)
        alternative = self.candidate("B", 450, 3, -83)
        routing = Mock(side_effect=[ProviderError("No route", "route_not_found", 422),
                                    self.route([450, 250], [alternative])])
        plan = plan_itinerary(self.route([700]), self.start, self.finish,
                              [cheap, alternative], routing=routing)
        self.assertTrue(plan.recovered)
        self.assertEqual(plan.routing_calls, 3)

    def test_no_alternative_returns_original_range_error_without_an_extra_call(self):
        station = self.candidate("A", 400, 1)
        routing = Mock(return_value=self.route([505, 205], [station]))
        with self.assertRaises(ItineraryError) as raised:
            plan_itinerary(self.route([700]), self.start, self.finish, [station], routing=routing)
        self.assertEqual(raised.exception.code, "station_access_exceeds_range")
        self.assertEqual(routing.call_count, 1)

    def test_failed_recovery_stops_at_three_total_calls(self):
        first = self.candidate("A", 400, 1, -84)
        second = self.candidate("B", 450, 3, -83)
        routing = Mock(side_effect=[self.route([505, 205], [first]), self.route([510, 190], [second])])
        with self.assertRaises(ItineraryError) as raised:
            plan_itinerary(self.route([700]), self.start, self.finish, [first, second], routing=routing)
        self.assertEqual(raised.exception.code, "station_access_exceeds_range")
        self.assertEqual(routing.call_count, 2)

    def test_no_purchase_waypoint_is_removed_and_fuel_is_recomputed(self):
        first = self.candidate("A", 400, 2, -84)
        last = self.candidate("B", 650, 1, -80)
        routing = Mock(side_effect=[self.route([350, 100, 200], [first, last]),
                                    self.route([430, 200], [last])])
        plan = plan_itinerary(self.route([700]), self.start, self.finish, [first, last], routing=routing)
        self.assertTrue(plan.cleanup_performed)
        self.assertEqual(plan.routing_calls, 3)
        self.assertEqual(plan.route.distance_miles, 630)
        self.assertEqual(plan.fuel.total_cost, Decimal("13.0"))
        self.assertEqual([c.key for c in plan.visited_candidates], ["B"])
        self.assert_conservation(plan)

    def test_failed_cleanup_preserves_the_already_verified_plan(self):
        first = self.candidate("A", 400, 2, -84)
        last = self.candidate("B", 650, 1, -80)
        routing = Mock(side_effect=[self.route([350, 100, 200], [first, last]),
                                    ProviderError("Timed out", "provider_timeout", 504)])
        plan = plan_itinerary(self.route([700]), self.start, self.finish, [first, last], routing=routing)
        self.assertFalse(plan.cleanup_performed)
        self.assertEqual(plan.routing_calls, 3)
        self.assertEqual(plan.route.distance_miles, 650)
        self.assertEqual(plan.fuel.total_cost, Decimal("15.0"))
        self.assertEqual([c.key for c in plan.visited_candidates], ["A", "B"])
        self.assert_conservation(plan)

    def test_malformed_cleanup_preserves_the_verified_plan(self):
        first = self.candidate("A", 400, 2, -84)
        last = self.candidate("B", 650, 1, -80)
        routing = Mock(side_effect=[self.route([350, 100, 200], [first, last]),
                                    self.route([430, 200], [last], snaps=())])
        plan = plan_itinerary(self.route([700]), self.start, self.finish, [first, last], routing=routing)
        self.assertEqual(plan.route.distance_miles, 650)
        self.assertEqual(plan.routing_calls, 3)
        self.assertFalse(plan.cleanup_performed)

    def test_malformed_verification_is_rejected_without_retry(self):
        station = self.candidate("A", 400, 1)
        for snaps in ((), ([-90, 40], [float("nan"), 40], [-75, 40]),
                      ([-90, 40], [-84], [-75, 40]),
                      ([-90, 40], [True, 40], [-75, 40])):
            with self.subTest(snaps=snaps):
                routing = Mock(return_value=self.route([400, 300], [station], snaps=snaps))
                with self.assertRaises(ItineraryError) as raised:
                    plan_itinerary(self.route([700]), self.start, self.finish, [station], routing=routing)
                self.assertEqual(raised.exception.code, "invalid_upstream_route")
                self.assertEqual(raised.exception.status, 502)
                self.assertEqual(routing.call_count, 1)

    def test_malformed_cached_cleanup_does_not_count_an_external_request(self):
        first = self.candidate("A", 400, 2, -84)
        last = self.candidate("B", 650, 1, -80)
        routing = Mock(side_effect=[self.route([350, 100, 200], [first, last]),
                                    self.route([430, 200], [last], snaps=(), requests_made=0)])
        plan = plan_itinerary(self.route([700]), self.start, self.finish, [first, last], routing=routing)
        self.assertEqual(plan.route.distance_miles, 650)
        self.assertEqual(plan.routing_calls, 2)
        self.assertFalse(plan.cleanup_performed)

    def test_busy_cleanup_without_an_http_request_preserves_exact_count(self):
        first = self.candidate("A", 400, 2, -84)
        last = self.candidate("B", 650, 1, -80)
        routing = Mock(side_effect=[self.route([350, 100, 200], [first, last]),
                                    ProviderError("Queue busy", "provider_busy", 503)])
        plan = plan_itinerary(self.route([700]), self.start, self.finish, [first, last], routing=routing)
        self.assertEqual(plan.routing_calls, 2)
        self.assertEqual(plan.route.distance_miles, 650)

    def test_cached_routes_report_zero_external_calls(self):
        station = self.candidate("A", 400, 1)
        routing = Mock(return_value=self.route([400, 300], [station], requests_made=0))
        plan = plan_itinerary(self.route([700], requests_made=0), self.start, self.finish,
                              [station], routing=routing)
        self.assertEqual(plan.routing_calls, 0)
        self.assertEqual(routing.call_count, 1)

    def test_unreachable_candidate_coverage_does_not_call_provider(self):
        routing = Mock()
        with self.assertRaises(ItineraryError) as raised:
            plan_itinerary(self.route([700]), self.start, self.finish, [], routing=routing)
        self.assertEqual(raised.exception.code, "insufficient_station_coverage")
        routing.assert_not_called()

    def test_same_station_at_two_route_encounters_remains_distinct(self):
        first = self.candidate("A", 300, 1)
        second = self.candidate("A", 800, 1)
        routing = Mock(return_value=self.route([300, 500, 200], [first, second]))
        plan = plan_itinerary(self.route([1000]), self.start, self.finish, [first, second], routing=routing)
        self.assertEqual([stop.candidate.mile for stop in plan.fuel.stops], [300, 800])
        self.assertEqual(len(plan.visited_candidates), 2)
        self.assert_conservation(plan)

    def test_max_waypoints_is_checked_before_provider_call(self):
        first = self.candidate("A", 400, 3)
        second = self.candidate("B", 900, 3)
        routing = Mock()
        with self.assertRaises(ItineraryError) as raised:
            plan_itinerary(self.route([1200]), self.start, self.finish, [first, second], routing=routing, max_stops=1)
        self.assertEqual(raised.exception.code, "too_many_stops")
        routing.assert_not_called()

    def test_provider_timeout_is_not_automatically_retried(self):
        station = self.candidate("A", 400, 1)
        routing = Mock(side_effect=ProviderError("Timed out", "provider_timeout", 504))
        with self.assertRaises(ProviderError):
            plan_itinerary(self.route([700]), self.start, self.finish, [station], routing=routing)
        self.assertEqual(routing.call_count, 1)
