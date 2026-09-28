import unittest
try:
    import numpy as np
except ImportError:
    np = None
from vector_selection import top_indices


@unittest.skipIf(np is None,'NumPy required for vector selection checks')
class VectorSelectionTests(unittest.TestCase):
    def assert_top(self, scores, count):
        found = top_indices(scores, count)
        expected = np.sort(scores[np.isfinite(scores)])[-count:]
        np.testing.assert_array_equal(np.sort(scores[found]), expected)
        self.assertEqual(len(found),len(set(found.tolist())))

    def test_fast_path_and_sparse_filter_match_exact_selection(self):
        rng = np.random.default_rng(17)
        for count in (1,60,4096):
            scores = rng.normal(size=100000).astype(np.float32)
            self.assert_top(scores,count)
            scores[::2] = -np.inf
            self.assert_top(scores,count)

    def test_adversarial_sampling_and_ties_fall_back_without_losing_hits(self):
        scores = np.zeros(100000,dtype=np.float32)
        scores[1::64] = 10
        self.assert_top(scores,60)
        scores[:] = -np.inf;scores[:5] = 1
        self.assert_top(scores,60)
        scores[:] = 1
        self.assert_top(scores,60)

    def test_empty_inputs(self):
        self.assertEqual(len(top_indices(np.array([]),60)),0)
        self.assertEqual(len(top_indices(np.ones(10),0)),0)


if __name__=='__main__':unittest.main()
