import unittest
from metadata import gps_label


class GPSTests(unittest.TestCase):
    def test_camera_placeholder_is_not_a_location(self):
        self.assertIsNone(gps_label({1: 'S', 2: (0, 0, 0), 3: 'W', 4: (0, 0, 0), 9: 'V'}))
        self.assertIsNone(gps_label({1: 'N', 2: (0, 0, 0), 3: 'E', 4: (0, 0, 0)}))

    def test_valid_coordinates_keep_hemisphere(self):
        self.assertEqual(gps_label({1: 'S', 2: (12, 30, 0), 3: 'W', 4: (45, 15, 0), 9: 'A'}),
                         '12.500°S 45.250°W')
        self.assertIsNone(gps_label({1: 'N', 2: (95, 0, 0), 3: 'E', 4: (45, 0, 0)}))


if __name__ == '__main__':
    unittest.main()
