import unittest
from gpu_activity import QuietWindow, utilization


class GPUActivityTests(unittest.TestCase):
    def test_multiple_devices_and_missing_telemetry(self):
        self.assertIsNone(utilization([{}]))
        self.assertIsNone(utilization({'PerformanceStatistics': {'Device Utilization %': -1}}))
        self.assertEqual(utilization([{'PerformanceStatistics': {'Device Utilization %': 0}},
                                     {'children': [{'PerformanceStatistics': {'Renderer Utilization %': 67}}]}]), 67)

    def test_load_burst_restarts_quiet_window(self):
        window = QuietWindow()
        self.assertFalse(window.observe(0, 0))
        self.assertFalse(window.observe(0, 1))
        self.assertFalse(window.observe(85, 2))
        for now in (3, 4, 5): self.assertFalse(window.observe(0, now))
        self.assertTrue(window.observe(0, 6))

    def test_unavailable_and_stale_readings_never_mean_idle(self):
        window = QuietWindow()
        window.observe(0, 0)
        self.assertFalse(window.observe(None, 1))
        self.assertFalse(window.observe(0, 2))
        self.assertFalse(window.observe(0, 20))
        self.assertFalse(window.observe(15, 21))


if __name__ == '__main__':
    unittest.main()
