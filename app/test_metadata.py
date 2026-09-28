import unittest
from datetime import timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from metadata import video_metadata
from index_backend import Backend
from metadata import gps_label


class GPSTests(unittest.TestCase):
    def test_camera_placeholder_is_not_a_location(self):
        self.assertIsNone(gps_label({1: 'S', 2: (0, 0, 0), 3: 'W', 4: (0, 0, 0), 9: 'V'}))
        self.assertIsNone(gps_label({1: 'N', 2: (0, 0, 0), 3: 'E', 4: (0, 0, 0)}))

    def test_valid_coordinates_keep_hemisphere(self):
        self.assertEqual(gps_label({1: 'S', 2: (12, 30, 0), 3: 'W', 4: (45, 15, 0), 9: 'A'}),
                         '12.500°S 45.250°W')
        self.assertIsNone(gps_label({1: 'N', 2: (95, 0, 0), 3: 'E', 4: (45, 0, 0)}))


class VideoMetadataTests(unittest.TestCase):
    def probe(self):
        return dict(format=dict(duration='12.5', tags={
            'creation_time': '2026-09-28T10:00:00Z',
            'com.apple.quicktime.make': 'Apple',
            'com.apple.quicktime.model': 'iPhone 16 Pro Max',
            'com.apple.quicktime.location.ISO6709': '+44.0000+026.0000/',
        }), streams=[dict(codec_type='video', width=2160, height=3840, avg_frame_rate='30000/1001'),
                     dict(codec_type='audio')])

    def test_capture_fields_use_existing_probe(self):
        value = video_metadata(self.probe(), timezone=timezone.utc)
        self.assertEqual(value['sortDate'], '20260928100000')
        self.assertEqual(value['camera'], 'Apple iPhone 16 Pro Max')
        self.assertEqual(value['details'], ['4K · 30 fps', 'Apple iPhone 16 Pro Max'])
        self.assertEqual(value['duration'], 12.5)
        self.assertEqual(value['location'], '+44.0000+026.0000/')

    def test_missing_video_is_reported_as_metadata_error(self):
        self.assertIn('No video stream', video_metadata(dict(streams=[]))['error'])

    def test_backend_probes_once_without_temporary_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)/'artifacts'
            with patch('index_backend.configure_cache_environment'):
                backend = Backend(artifacts)
            with patch.object(backend, 'run_command', return_value=SimpleNamespace(stdout=json.dumps(self.probe()))) as command:
                result = backend.probe(Path(directory)/'clip.mp4', 'video')
            self.assertEqual(command.call_count, 1)
            self.assertEqual(result['metadata']['duration'], 12.5)
            self.assertEqual(result['speech_times'], [0])
            self.assertFalse(artifacts.exists())


if __name__ == '__main__':
    unittest.main()
