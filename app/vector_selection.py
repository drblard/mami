"""Exact top-k with a sampled cutoff fast path; never approximates the result."""

SAMPLE_STRIDE = 64
CANDIDATE_OVERSAMPLE = 2
MAX_FAST_CANDIDATES_MULTIPLIER = 8


def top_indices(scores, count):
    import numpy as np
    if count <= 0 or len(scores) == 0:
        return np.empty(0, dtype=np.int64)
    count = min(count, len(scores))
    if len(scores) <= count*MAX_FAST_CANDIDATES_MULTIPLIER:
        selected = np.argpartition(scores, -count)[-count:]
        return selected[np.isfinite(scores[selected])]
    sample = scores[::SAMPLE_STRIDE]
    sample_count = min(len(sample), max(1, (count*CANDIDATE_OVERSAMPLE + SAMPLE_STRIDE-1)//SAMPLE_STRIDE))
    cutoff = np.partition(sample, -sample_count)[-sample_count]
    candidates = np.flatnonzero((scores >= cutoff) & np.isfinite(scores))
    if count <= len(candidates) <= count*MAX_FAST_CANDIDATES_MULTIPLIER:
        selected = np.argpartition(scores[candidates], -count)[-count:]
        return candidates[selected]
    # Sampling cannot change correctness: an unhelpful cutoff falls back to
    # the global selection, including highly tied or selectively filtered data.
    selected = np.argpartition(scores, -count)[-count:]
    return selected[np.isfinite(scores[selected])]
