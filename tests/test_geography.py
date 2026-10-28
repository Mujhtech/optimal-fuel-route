from django.test import SimpleTestCase

from planner.geography import InvalidLocation, is_in_usa, validate_coordinates


class GeographyTests(SimpleTestCase):
    def test_valid_locations_include_alaska_and_hawaii(self):
        for lat, lon in [(40.7128, -74.006), (34.0522, -118.2437), (47.6062, -122.3321), (25.7617, -80.1918), (61.2181, -149.9003), (21.3099, -157.8581), (38.9072, -77.0369)]:
            with self.subTest(latitude=lat, longitude=lon):
                self.assertTrue(is_in_usa(lat, lon))

    def test_bounding_box_does_not_accidentally_include_canada_or_mexico(self):
        for lat, lon in [(43.6532, -79.3832), (49.2827, -123.1207), (19.4326, -99.1332), (32.5149, -117.0382), (0, 0), (51.5072, -0.1276)]:
            with self.subTest(latitude=lat, longitude=lon):
                self.assertFalse(is_in_usa(lat, lon))

    def test_invalid_coordinate_types_and_values(self):
        for lat, lon in [(True, -74), (40, False), ("40.7", -74), (None, -74), (float("nan"), -74), (40, float("inf")), (91, -74), (40, -181), (43.6532, -79.3832)]:
            with self.subTest(latitude=lat, longitude=lon):
                with self.assertRaises(InvalidLocation):
                    validate_coordinates(lat, lon)
