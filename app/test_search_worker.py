import unittest
from search_worker import speech_hits


class SpeechSearchTests(unittest.TestCase):
    def test_accents_and_exact_words_with_segment_timestamp(self):
        frames = {'video': [{'path': 'video', 'timestamp': .5, 'frame': 'a'},
                            {'path': 'video', 'timestamp': 8.5, 'frame': 'b'}]}
        segments = [('video', {'text': 'Am ajuns la Dunăre!', 'start': 8.1})]
        hit = speech_hits('dunare', segments, frames)[0]
        self.assertEqual(hit['frame'], 'b')
        self.assertEqual(hit['timestamp'], 8.1)
        self.assertEqual(hit['evidence'], 'Am ajuns la Dunăre!')
        self.assertEqual(speech_hits('dun', segments, frames), [])
        self.assertEqual(speech_hits('dunare capre', segments, frames), [])

    def test_duplicate_file_and_missing_file(self):
        frames = {'video': [{'path': 'video', 'timestamp': .5, 'frame': 'a'}]}
        segments = [('video', {'text': 'capre', 'start': 0}),
                    ('video', {'text': 'alte capre', 'start': 2}),
                    ('missing', {'text': 'capre', 'start': 0})]
        self.assertEqual(len(speech_hits('capre', segments, frames)), 1)
        self.assertEqual(speech_hits('???', segments, frames), [])


if __name__ == '__main__':
    unittest.main()
