"""Grouping unnamed faces and suggesting people from confirmed examples.

Embeddings are L2-normalized, so dot products are cosine similarities. Nothing
here reads or writes storage; callers pass the user's confirmations and
rejections, which always take precedence over automatic decisions.
"""
from dataclasses import dataclass

import numpy as np

# Faces too small, blurry or turned away give unreliable identities. They stay
# visible in the media but do not seed groups or automatic suggestions.
MINIMUM_EYE_DISTANCE = 16.0
MINIMUM_DETECTION_SCORE = 0.6
MINIMUM_FRONTALNESS = 0.25
# Grouping links two faces only when they are mutual close neighbours above
# this similarity, which avoids chaining different people through lookalikes.
GROUP_SIMILARITY = 0.5
GROUP_NEIGHBOURS = 10
# Consecutive faces of one person within a single video are one track.
TRACK_SIMILARITY = 0.55
TRACK_REPRESENTATIVES = 3
# A suggestion needs a strong match to some confirmed face of that person and
# a clear margin over every other person.
SUGGEST_SIMILARITY = 0.45
SUGGEST_MARGIN = 0.08
SIMILARITY_BLOCK = 4096


@dataclass(frozen=True)
class Quality:
    score: float
    eye_distance: float
    frontalness: float

    @property
    def reliable(self):
        return (self.score >= MINIMUM_DETECTION_SCORE and self.eye_distance >= MINIMUM_EYE_DISTANCE
                and self.frontalness >= MINIMUM_FRONTALNESS)

    @property
    def rank(self):
        """Higher is a better example face for display and grouping."""
        return self.score * min(1.0, self.eye_distance / 64.0) * self.frontalness


def _check(embeddings):
    embeddings = np.asarray(embeddings, dtype=np.float32)
    if embeddings.ndim != 2:
        raise ValueError('Embeddings must be a two-dimensional array')
    return embeddings


def tracks(embeddings, times):
    """Group one video's faces into per-person tracks (greedy, time-ordered).

    Returns a track number for each face. A face joins the most similar
    existing track if above TRACK_SIMILARITY, otherwise starts a new one.
    """
    embeddings = _check(embeddings)
    order = sorted(range(len(embeddings)), key=lambda index: (times[index] is None, times[index] or 0, index))
    labels = [-1] * len(embeddings)
    centres = []
    for index in order:
        if centres:
            similarity = np.asarray(centres) @ embeddings[index]
            best = int(np.argmax(similarity))
            if similarity[best] >= TRACK_SIMILARITY:
                labels[index] = best
                merged = centres[best] + embeddings[index]
                centres[best] = merged / np.linalg.norm(merged)
                continue
        labels[index] = len(centres)
        centres.append(embeddings[index].copy())
    return labels


def representatives(labels, qualities, limit=TRACK_REPRESENTATIVES):
    """Indices of the best-quality faces in each track."""
    chosen = []
    for label in sorted(set(labels)):
        members = [index for index, value in enumerate(labels) if value == label]
        members.sort(key=lambda index: (-qualities[index].rank, index))
        chosen.extend(members[:limit])
    return sorted(chosen)


def mutual_neighbours(embeddings, similarity=GROUP_SIMILARITY, neighbours=GROUP_NEIGHBOURS, block=SIMILARITY_BLOCK):
    """Edges (i, j), i < j, between mutual k-nearest neighbours above `similarity`."""
    embeddings = _check(embeddings)
    count = len(embeddings)
    k = min(neighbours, count - 1)
    if k <= 0:
        return set()
    nearest = np.empty((count, k), dtype=np.int64)
    for start in range(0, count, block):
        scores = embeddings[start:start + block] @ embeddings.T
        rows = np.arange(scores.shape[0])
        scores[rows, rows + start] = -np.inf
        candidates = np.argpartition(-scores, k - 1, axis=1)[:, :k]
        keep = np.take_along_axis(scores, candidates, axis=1) >= similarity
        nearest[start:start + len(scores)] = np.where(keep, candidates, -1)
    neighbour_sets = [set(row[row >= 0].tolist()) for row in nearest]
    return {(i, j) for i, row in enumerate(neighbour_sets) for j in row if i < j and i in neighbour_sets[j]}


def groups(embeddings, cannot_link=(), must_link=()):
    """Connected components of mutual-neighbour edges, respecting user constraints.

    must_link pairs (e.g. faces confirmed as the same person) are always joined;
    an edge is skipped if it would join two faces in any cannot_link pair.
    Returns a group number per face; numbers are ordered by first member.
    """
    embeddings = _check(embeddings)
    parent = list(range(len(embeddings)))
    members = {index: {index} for index in range(len(embeddings))}
    # Per group: faces it may never be joined with (union of members' rejections).
    forbidden = {index: set() for index in range(len(embeddings))}
    for a, b in cannot_link:
        forbidden[a].add(b); forbidden[b].add(a)
    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def join(a, b, force):
        a, b = root(a), root(b)
        if a == b or (not force and not forbidden[a].isdisjoint(members[b])):
            return
        if len(members[a]) < len(members[b]):
            a, b = b, a
        parent[b] = a
        members[a] |= members.pop(b)
        forbidden[a] |= forbidden.pop(b)
    for a, b in must_link:
        join(a, b, True)
    edges = mutual_neighbours(embeddings)
    for a, b in sorted(edges, key=lambda edge: -float(embeddings[edge[0]] @ embeddings[edge[1]])):
        join(a, b, False)
    labels, numbers = [], {}
    for index in range(len(embeddings)):
        labels.append(numbers.setdefault(root(index), len(numbers)))
    return labels


def suggestions(faces, examples, rejected=()):
    """Suggest a person for each face from confirmed example embeddings.

    `examples` maps person -> array of confirmed embeddings; `rejected` holds
    (face index, person) pairs the user said are wrong. Returns, per face,
    (person, similarity) or None. Matching uses each person's closest confirmed
    face so people remain recognizable across ages and appearances.
    """
    faces = _check(faces)
    blocked = set(rejected)
    people = sorted(examples)
    if not people or not len(faces):
        return [None] * len(faces)
    best = np.full((len(faces), len(people)), -np.inf, dtype=np.float32)
    for column, person in enumerate(people):
        confirmed = _check(examples[person])
        if len(confirmed):
            best[:, column] = (faces @ confirmed.T).max(axis=1)
    result = []
    for index, row in enumerate(best):
        allowed = [(float(score), person) for score, person in zip(row, people) if (index, person) not in blocked]
        allowed.sort(key=lambda item: (-item[0], item[1]))
        if not allowed or allowed[0][0] < SUGGEST_SIMILARITY:
            result.append(None)
            continue
        runner_up = allowed[1][0] if len(allowed) > 1 else -1.0
        result.append((allowed[0][1], allowed[0][0]) if allowed[0][0] - runner_up >= SUGGEST_MARGIN else None)
    return result
