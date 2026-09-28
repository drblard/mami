"""Pure search matching/ranking logic; no inference or filesystem dependencies."""
from bisect import bisect_left
import re
import unicodedata

RESULT_LIMIT = 60
RECIPROCAL_RANK_OFFSET = 60
WORD_PATTERN = re.compile(r'\w+')


def words(text):
    folded = ''.join(character for character in unicodedata.normalize('NFD', text.casefold())
                     if not unicodedata.combining(character))
    return WORD_PATTERN.findall(folded)


class SpeechIndex:
    def __init__(self, segments, by_path):
        self.frames = {path: sorted(frames, key=lambda frame: frame['timestamp'] or 0)
                       for path, frames in by_path.items() if frames}
        self.times = {path: [frame['timestamp'] or 0 for frame in frames] for path, frames in self.frames.items()}
        self.segments = []
        self.postings = {}
        for path, segment in segments:
            if path not in self.frames:
                continue
            text = segment['text'].strip()
            tokens = words(text)
            index = len(self.segments)
            self.segments.append((path, segment, text, len(tokens)))
            for term in set(tokens):
                self.postings.setdefault(term, set()).add(index)
        self.vocabulary = sorted(self.postings)

    def search(self, query, allowed=None, limit=RESULT_LIMIT):
        terms = words(query)
        if not terms or limit <= 0:
            return []
        prefix = terms.pop() if query[-1:].isalnum() else None
        candidates = [self.postings.get(term, set()) for term in set(terms)]
        if prefix is not None:
            matching = set()
            for index in range(bisect_left(self.vocabulary, prefix), len(self.vocabulary)):
                term = self.vocabulary[index]
                if not term.startswith(prefix):
                    break
                matching.update(self.postings[term])
            candidates.append(matching)
        if not candidates:
            return []
        candidates.sort(key=len)
        matches = candidates[0].intersection(*candidates[1:])
        ranked = sorted((self.segments[index] for index in matches
                         if allowed is None or self.segments[index][0] in allowed),
                        key=lambda row: (row[3], row[0], float(row[1]['start'])))
        hits, seen = [], set()
        term_count = len(set(words(query)))
        for path, segment, text, length in ranked:
            if path in seen:
                continue
            seen.add(path)
            timestamp = float(segment['start'])
            times, frames = self.times[path], self.frames[path]
            insertion = bisect_left(times, timestamp)
            nearby = range(max(0, insertion-1), min(len(frames), insertion+1))
            ordinal = min(nearby, key=lambda index: abs(times[index]-timestamp))
            hits.append({**frames[ordinal], 'timestamp': timestamp, 'evidence': text,
                         'score': term_count / max(1, length)})
            if len(hits) == limit:
                break
        return hits


def combine_hits(visual, spoken, limit=RESULT_LIMIT):
    """Fuse ranks without comparing unrelated model and lexical score scales."""
    merged, scores = {}, {}
    for results in (visual, spoken):
        for rank, hit in enumerate(results, start=1):
            path = hit['path']
            scores[path] = scores.get(path, 0) + 1 / (RECIPROCAL_RANK_OFFSET + rank)
            if path not in merged or hit.get('evidence'):
                merged[path] = hit  # Speech keeps its actual timestamp and excerpt.
    order = sorted(scores, key=lambda path: (-scores[path], path))
    return [{**merged[path], 'score': scores[path]} for path in order[:limit]]
