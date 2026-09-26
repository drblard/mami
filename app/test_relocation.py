import unittest
from relocate_catalog import match_paths


class RelocationTests(unittest.TestCase):
    def test_renamed_file_matches_content_and_prefers_date_folder(self):
        previous = {'/old/ .MP4': 'sha256:a', '/old/other.jpg': 'sha256:b'}
        discovered = {'/new/2026-09-Sep/ .MP4': 'sha256:a',
                      '/new/2026-09-18/DJI_renamed.MP4': 'sha256:a',
                      '/new/2026-09-18/other.jpg': 'sha256:different'}
        locations, missing, duplicates = match_paths(previous, discovered)
        self.assertEqual(locations, {'/old/ .MP4': '/new/2026-09-18/DJI_renamed.MP4'})
        self.assertEqual(missing, ['/old/other.jpg'])
        self.assertEqual(len(duplicates), 1)


if __name__ == '__main__':
    unittest.main()
