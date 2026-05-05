import unittest

from apps.core.geo_utils import ZONE_RESOLUTION, get_neighbors, latlng_to_h3


class GeoUtilsTests(unittest.TestCase):
    def test_latlng_to_h3_returns_none_for_invalid_input(self):
        self.assertIsNone(latlng_to_h3(None, 0))
        self.assertIsNone(latlng_to_h3("abc", "def"))
        self.assertIsNone(latlng_to_h3(float("nan"), 0))
        self.assertIsNone(latlng_to_h3(200, 0))  # out of range

    def test_latlng_to_h3_returns_cell_for_valid_input(self):
        cell = latlng_to_h3(-6.7924, 39.2083, ZONE_RESOLUTION)  # Dar es Salaam-ish
        self.assertIsInstance(cell, str)
        self.assertTrue(len(cell) >= 10)

    def test_get_neighbors_returns_empty_for_invalid_cell(self):
        self.assertEqual(get_neighbors("", 1), [])
        self.assertEqual(get_neighbors("not_a_cell", 1), [])

    def test_get_neighbors_includes_origin(self):
        cell = latlng_to_h3(-6.7924, 39.2083, ZONE_RESOLUTION)
        neighbors = get_neighbors(cell, 1)
        self.assertIn(cell, neighbors)
        self.assertGreaterEqual(len(neighbors), 1)

