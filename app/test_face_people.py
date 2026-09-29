import unittest

try:
    import numpy as np
    import face_people
    from face_people import Quality
except ImportError:
    np = None

DIMENSIONS = 512


def unit(vector):
    return vector / np.linalg.norm(vector, axis=-1, keepdims=True)


@unittest.skipIf(np is None, 'NumPy face checks run where the worker runtime is installed')
class FacePeopleTests(unittest.TestCase):
    def setUp(self):
        self.random = np.random.default_rng(20260929)
        self.people = unit(self.random.normal(size=(4, DIMENSIONS)))

    def faces(self, person, count, noise=0.6):
        variation = self.random.normal(size=(count, DIMENSIONS)) * noise / np.sqrt(DIMENSIONS)
        return unit(self.people[person] + variation).astype(np.float32)

    def test_quality_gate_and_ranking(self):
        self.assertTrue(Quality(0.9, 40, 0.8).reliable)
        self.assertFalse(Quality(0.9, face_people.MINIMUM_EYE_DISTANCE - 1, 0.8).reliable)
        self.assertFalse(Quality(face_people.MINIMUM_DETECTION_SCORE - 0.01, 40, 0.8).reliable)
        self.assertFalse(Quality(0.9, 40, face_people.MINIMUM_FRONTALNESS - 0.01).reliable)
        self.assertGreater(Quality(0.9, 64, 0.9).rank, Quality(0.9, 20, 0.9).rank)

    def test_video_tracks_follow_identity_not_time_order(self):
        a, b = self.faces(0, 3), self.faces(1, 3)
        embeddings = np.stack([a[0], b[0], a[1], b[1], a[2], b[2]])
        labels = face_people.tracks(embeddings, [0.5, 0.5, 1.5, 1.5, 2.5, 2.5])
        self.assertEqual(labels, [0, 1, 0, 1, 0, 1])

    def test_representatives_keep_best_faces_per_track(self):
        qualities = [Quality(0.9, eye, 0.9) for eye in (20, 70, 40, 30, 10)]
        self.assertEqual(face_people.representatives([0, 0, 0, 1, 1], qualities, limit=2), [1, 2, 3, 4])

    def test_groups_separate_people_and_leave_strangers_alone(self):
        embeddings = np.concatenate([self.faces(0, 5), self.faces(1, 5), self.faces(2, 1)])
        self.assertEqual(face_people.groups(embeddings), [0] * 5 + [1] * 5 + [2])

    def test_rejected_face_is_never_regrouped_with_its_person(self):
        embeddings = self.faces(0, 6)
        cannot = [(5, index) for index in range(5)]
        self.assertEqual(face_people.groups(embeddings, cannot_link=cannot), [0] * 5 + [1])

    def test_confirmed_faces_stay_together_even_when_dissimilar(self):
        embeddings = np.concatenate([self.faces(0, 3), self.faces(3, 3)])
        self.assertEqual(face_people.groups(embeddings, must_link=[(0, 3)]), [0] * 6)

    def test_mutual_neighbours_are_symmetric_and_thresholded(self):
        embeddings = np.concatenate([self.faces(0, 3), self.faces(1, 1)])
        self.assertEqual(face_people.mutual_neighbours(embeddings, block=2), {(0, 1), (0, 2), (1, 2)})

    def test_suggestions_use_closest_confirmed_face_and_respect_rejections(self):
        son, daughter = self.faces(0, 4), self.faces(1, 4)
        examples = {'son': son[:3], 'daughter': daughter[:3]}
        stranger = self.faces(2, 1)[0]
        faces = np.stack([son[3], daughter[3], stranger, son[3]])
        result = face_people.suggestions(faces, examples, rejected=[(3, 'son')])
        self.assertEqual(result[0][0], 'son')
        self.assertAlmostEqual(result[0][1], float((son[:3] @ son[3]).max()), places=5)
        self.assertEqual(result[1][0], 'daughter')
        self.assertIsNone(result[2])
        self.assertIsNone(result[3])

    def test_ambiguous_match_is_not_suggested(self):
        twin = unit(self.people[0] + self.people[1])
        examples = {'a': unit(self.people[0])[None], 'b': unit(self.people[1])[None]}
        self.assertEqual(face_people.suggestions(twin[None], examples), [None])
        self.assertEqual(face_people.suggestions(twin[None], {}), [None])


if __name__ == '__main__':
    unittest.main()
