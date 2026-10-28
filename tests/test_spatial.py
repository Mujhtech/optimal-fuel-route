from decimal import Decimal
import unittest

from planner.spatial import haversine_miles, project_stations


def station(key, longitude, latitude, price="3.25"):
    return {
        "external_id": str(key),
        "longitude": longitude,
        "latitude": latitude,
        "price_per_gallon": Decimal(price),
    }


class SpatialProjectionTests(unittest.TestCase):
    def test_mile_markers_follow_geometry_instead_of_straight_line(self):
        coordinates = [[-100, 40], [-99, 40], [-99, 41]]
        first_length = haversine_miles(coordinates[0], coordinates[1])
        second_length = haversine_miles(coordinates[1], coordinates[2])
        candidates = project_stations(coordinates, 300, [station(1, -99, 40.5)])
        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0].mile, (first_length + second_length / 2) / (first_length + second_length) * 300)
        self.assertAlmostEqual(candidates[0].offset_miles, 0)

    def test_corridor_excludes_far_stations(self):
        candidates = project_stations([[-100, 40], [-99, 40]], 60, [station(1, -99.5, 40.005), station(2, -99.5, 40.1)], 1)
        self.assertEqual([item.key for item in candidates], ["1"])
        self.assertAlmostEqual(candidates[0].mile, 30)
        self.assertGreater(candidates[0].offset_miles, 0.3)
        self.assertLess(candidates[0].offset_miles, 0.4)

    def test_station_near_bend_is_only_returned_once(self):
        candidates = project_stations([[-100, 40], [-99, 40], [-99, 41]], 120, [station(1, -99, 40)])
        self.assertEqual(len(candidates), 1)

    def test_route_revisiting_same_station_preserves_both_encounters(self):
        coordinates = [[-100, 40], [-99, 40], [-99, 41], [-100, 41], [-100, 40], [-101, 40]]
        candidates = project_stations(coordinates, 300, [station(1, -100, 40)])
        self.assertEqual(len(candidates), 2)
        self.assertAlmostEqual(candidates[0].mile, 0)
        self.assertGreater(candidates[1].mile, 200)

    def test_adjacent_out_and_back_segments_preserve_separate_encounters(self):
        candidates = project_stations([[-100, 40], [-99, 40], [-100, 40]], 100, [station(1, -99.5, 40)])
        self.assertEqual(len(candidates), 2)
        self.assertAlmostEqual(candidates[0].mile, 25)
        self.assertAlmostEqual(candidates[1].mile, 75)

    def test_missing_coordinates_are_ignored(self):
        candidates = project_stations([[-100, 40], [-99, 40]], 50, [station(1, None, None)])
        self.assertEqual(candidates, [])

    def test_duplicate_geometry_vertices_are_harmless(self):
        candidates = project_stations([[-100, 40], [-100, 40], [-99, 40]], 50, [station(1, -99.5, 40)])
        self.assertAlmostEqual(candidates[0].mile, 25)

    def test_date_line_projection_does_not_create_world_spanning_segment(self):
        candidates = project_stations([[179, 52], [-179, 52]], 100, [station(1, 180, 52), station(2, 0, 52)])
        self.assertEqual([item.key for item in candidates], ["1"])
        self.assertAlmostEqual(candidates[0].mile, 50)

    def test_dense_geometry_preserves_segment_indices_after_vectorized_build(self):
        coordinates = [[-100 + index / 1000, 40] for index in range(1001)]
        candidates = project_stations(coordinates, 100, [station(1, -99.25, 40), station(2, -99.75, 40)])
        self.assertEqual([candidate.key for candidate in candidates], ["2", "1"])
        self.assertAlmostEqual(candidates[0].mile, 25, places=6)
        self.assertAlmostEqual(candidates[1].mile, 75, places=6)

    def test_reversed_date_line_crossing_preserves_encounter_order(self):
        candidates = project_stations([[-179, 52], [179, 52]], 100, [station(1, -179.5, 52), station(2, 179.5, 52)])
        self.assertEqual([candidate.key for candidate in candidates], ["1", "2"])
        self.assertAlmostEqual(candidates[0].mile, 25)
        self.assertAlmostEqual(candidates[1].mile, 75)

    def test_degenerate_route(self):
        self.assertEqual(project_stations([[-100, 40], [-100, 40]], 0, []), [])
        with self.assertRaises(ValueError):
            project_stations([[-100, 40], [-100, 40]], 100, [])
