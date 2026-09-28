import math
import unittest
from model_config import PIPELINE
from sampling import last_frame_time, sample_times


class SamplingTests(unittest.TestCase):
    def test_existing_pipeline_and_midpoint_sampling_remain_compatible(self):
        self.assertEqual(PIPELINE, 'siglip2-75de2d55-whisper-a4aaeec0-1s-ro-chunk30-v1')
        self.assertEqual(sample_times(2.2, 1), [.5, 1.5, 2.1])
        self.assertEqual(sample_times(.2, 1), [.1])

    def test_nonfinite_or_nonpositive_inputs_are_rejected(self):
        for bad in (0, -1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                sample_times(1, bad)
            with self.assertRaises(ValueError):
                sample_times(bad, 1)

    def test_video_end_excludes_longer_audio_and_handles_unknown_frame_rate(self):
        probe = dict(format=dict(duration='20'), streams=[dict(codec_type='video', duration='10', avg_frame_rate='25/1')])
        self.assertAlmostEqual(last_frame_time(probe), 9.959)
        probe['streams'][0]['avg_frame_rate'] = '0/0'
        self.assertAlmostEqual(last_frame_time(probe), 9.899)


if __name__ == '__main__':
    unittest.main()
