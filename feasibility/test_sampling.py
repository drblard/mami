import unittest
from lab import sample_times, last_frame_time


class SamplingTests(unittest.TestCase):
    def test_seek_stays_before_video_end_when_audio_is_longer(self):
        record = {'probe': {'format': {'duration': '2.005333'}, 'streams': [
            {'codec_type': 'video', 'duration': '2.002000', 'avg_frame_rate': '30000/1001'}]}}
        last = last_frame_time(record)
        self.assertLess(last, 59 * 1001 / 30000)
        self.assertGreater(last, 1.96)

    def test_short_clip_has_one_in_bounds_sample(self):
        self.assertEqual(sample_times(.4, 1), [.2])

    def test_partial_final_second_is_covered(self):
        self.assertEqual(sample_times(2.4, 1), [.5, 1.5, 2.2])

    def test_no_gap_exceeds_requested_interval(self):
        for duration in [.01, .4, 1, 1.001, 5.62, 45.74, 309.77]:
            times = sample_times(duration, 1)
            self.assertTrue(all(0 <= t < duration for t in times))
            boundaries = [0, *times, duration]
            self.assertTrue(all(b-a <= 1 for a, b in zip(boundaries, boundaries[1:])))

    def test_invalid_input(self):
        for duration, interval in [(0, 1), (-1, 1), (3, 0), (3, -1), (float('nan'), 1)]:
            with self.assertRaises(ValueError):
                sample_times(duration, interval)


if __name__ == '__main__':
    unittest.main()
