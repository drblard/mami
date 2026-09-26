import unittest
from search_worker import speech_hits, combine_hits


class SpeechSearchTests(unittest.TestCase):
    def test_format_scope_is_applied_before_speech_limit(self):
        frames = {f'file-{i}': [dict(path=f'file-{i}', timestamp=0, frame='cached')] for i in range(80)}
        segments = [(path, dict(text='capre', start=0)) for path in frames]
        allowed = {'file-79'}
        self.assertEqual([h['path'] for h in speech_hits('capre', segments, frames, allowed)], ['file-79'])
        self.assertEqual(speech_hits('capre', segments, frames, set()), [])

    def test_both_fuses_ranks_and_preserves_spoken_moment(self):
        visual = [{'path': 'visual-only', 'score': .9, 'timestamp': 1},
                  {'path': 'shared', 'score': .3, 'timestamp': 50}]
        spoken = [{'path': 'shared', 'score': 999, 'timestamp': 12.4, 'evidence': 'Dunăre'},
                  {'path': 'speech-only', 'score': 2, 'timestamp': 3, 'evidence': 'Dunăre'}]
        hits = combine_hits(visual, spoken)
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0]['path'], 'shared')
        self.assertEqual(hits[0]['timestamp'], 12.4)
        self.assertEqual(hits[0]['evidence'], 'Dunăre')
        self.assertLess(hits[0]['score'], 1)

    def test_both_handles_one_empty_source_and_caps_results(self):
        hits = [{'path': f'file-{i}', 'timestamp': i} for i in range(80)]
        self.assertEqual([h['path'] for h in combine_hits(hits, [])], [h['path'] for h in hits[:60]])
        self.assertEqual(combine_hits([], []), [])

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
