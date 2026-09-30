"""Recompute face groups and person assignments inside one face-index transaction."""
from collections import defaultdict

import numpy as np

import face_people
import faces_store


def _embeddings_for(db, ids):
    if not ids:
        return np.zeros((0, 512), faces_store.EMBEDDING_DTYPE)
    rows = {}
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        marks = ','.join('?' * len(batch))
        rows.update((row['id'], row['embedding']) for row in db.execute(f'SELECT id,embedding FROM faces WHERE id IN ({marks})', batch))
    return np.stack([np.frombuffer(rows[face], dtype=faces_store.EMBEDDING_DTYPE) for face in ids])


def resolve_labels(db, people, labels):
    """Map personal labels onto current faces: ({face: person}, {(face, person)})."""
    confirmed, rejected = {}, set()
    for asset, timestamp, x1, y1, x2, y2, person, verdict in labels:
        if person not in people:
            continue
        face = faces_store.match_label(db, asset, timestamp, (x1, y1, x2, y2))
        if face is None:
            continue
        if verdict == 'confirmed':
            confirmed[face] = person
        elif verdict == 'rejected':
            rejected.add((face, person))
    return confirmed, rejected


def upgrade_same_moment(db, assignments, confirmed, confirmed_vectors, candidate_vectors):
    """Accept suggestions from the same video or photo moment as a user confirmation.

    Only the user's own confirmations are anchors, so automatic matches never
    spread from moment to moment. A second face in the same photo is someone else.
    """
    if not confirmed:
        return
    jobs = {row['asset']: (row['kind'], row['capture_time']) for row in db.execute('SELECT asset,kind,capture_time FROM face_jobs')}
    asset_of = {row['id']: row['asset'] for row in db.execute('SELECT id,asset FROM faces')}
    anchors = defaultdict(list)  # person -> [(asset, kind, time, vector)]
    for face, person in confirmed.items():
        asset = asset_of.get(face)
        kind, time = jobs.get(asset, (None, None))
        anchors[person].append((asset, kind, time, confirmed_vectors[face]))
    for face, (person, source, similarity) in list(assignments.items()):
        if source != 'suggested' or (similarity or 0) >= face_people.AUTOMATIC_SIMILARITY or face not in candidate_vectors:
            continue
        asset = asset_of.get(face)
        kind, time = jobs.get(asset, (None, None))
        nearby = [vector for anchor_asset, anchor_kind, anchor_time, vector in anchors.get(person, ())
                  if (anchor_asset == asset and kind == 'video')
                  or (anchor_asset != asset and kind == 'image' and anchor_kind == 'image' and time is not None
                      and anchor_time is not None and abs(anchor_time - time) <= face_people.SAME_MOMENT_SECONDS)]
        if nearby and max(float(vector @ candidate_vectors[face]) for vector in nearby) >= face_people.MOMENT_SIMILARITY:
            assignments[face] = (person, 'moment', similarity)


def recompute(db, people, labels):
    """Replace face_assignments and face_groups; returns (assigned, grouped) counts."""
    ids, matrix = faces_store.embeddings(db)
    confirmed, rejected = resolve_labels(db, people, labels)
    assignments = {face: (person, 'confirmed', None) for face, person in confirmed.items()}
    confirmed_ids = sorted(confirmed)
    confirmed_embeddings = _embeddings_for(db, confirmed_ids)
    examples = {}
    for person in sorted(set(confirmed.values())):
        rows = [index for index, face in enumerate(confirmed_ids) if confirmed[face] == person]
        examples[person] = confirmed_embeddings[rows]
    candidates = [index for index, face in enumerate(ids) if int(face) not in confirmed]
    rejected_by_face = defaultdict(set)
    for face, person in rejected:
        rejected_by_face[face].add(person)
    blocked = [(position, person) for position, index in enumerate(candidates) for person in rejected_by_face.get(int(ids[index]), ())]
    for position, result in enumerate(face_people.suggestions(matrix[candidates], examples, rejected=blocked)):
        if result is not None:
            assignments[int(ids[candidates[position]])] = (result[0], 'suggested', result[1])
    upgrade_same_moment(db, assignments, confirmed, dict(zip(confirmed_ids, confirmed_embeddings)), dict(zip(map(int, ids), matrix)))
    # Other faces of a video track follow its reviewed or suggested representatives.
    tracks = defaultdict(list)
    for row in db.execute('SELECT id,asset,track FROM faces'):
        tracks[(row['asset'], row['track'])].append(row['id'])
    for members in tracks.values():
        decided = [assignments[face] for face in members if face in assignments]
        if not decided:
            continue
        confirmed_people = [person for person, source, _ in decided if source == 'confirmed']
        person = confirmed_people[0] if confirmed_people else max(decided, key=lambda item: item[2] or 0)[0]
        # A track that is confirmed or automatically matched settles its other
        # frames: they are not sent back for review as weaker suggestions.
        settled = bool(confirmed_people) or any(who == person and (source == 'moment' or (source == 'suggested' and (similarity or 0) >= face_people.AUTOMATIC_SIMILARITY))
                                               for who, source, similarity in decided)
        for face in members:
            if person in rejected_by_face.get(face, ()):
                continue
            current = assignments.get(face)
            if current is None or (settled and current[1] == 'suggested' and current[0] == person
                                   and (current[2] or 0) < face_people.AUTOMATIC_SIMILARITY):
                assignments[face] = (person, 'track', None)
    unassigned = [index for index, face in enumerate(ids) if int(face) not in assignments]
    position = {int(ids[index]): offset for offset, index in enumerate(unassigned)}
    must_link = []
    for members in tracks.values():
        linked = [position[face] for face in members if face in position]
        must_link += list(zip(linked, linked[1:]))
    labels_by_face = face_people.groups(matrix[unassigned], must_link=must_link) if unassigned else []
    first = {}
    for offset, label in enumerate(labels_by_face):
        first.setdefault(label, int(ids[unassigned[offset]]))
    db.execute('DELETE FROM face_assignments')
    db.execute('DELETE FROM face_groups')
    db.executemany('INSERT INTO face_assignments VALUES(?,?,?,?)',
                   [(face, person, source, similarity) for face, (person, source, similarity) in sorted(assignments.items())])
    # Typicality: similarity to the group's average face (outliers are least typical).
    grouped = matrix[unassigned] if unassigned else matrix[:0]
    sums = {}
    for offset, label in enumerate(labels_by_face):
        sums[label] = sums.get(label, 0) + grouped[offset].astype(np.float64)
    centres = {label: total / np.linalg.norm(total) for label, total in sums.items()}
    db.executemany('INSERT INTO face_groups VALUES(?,?,?)',
                   [(int(ids[unassigned[offset]]), first[label], float(grouped[offset] @ centres[label]))
                    for offset, label in enumerate(labels_by_face)])
    # Record the labels too: edits made while no worker ran are recomputed later.
    db.execute('UPDATE face_state SET grouped=? WHERE id=1', (faces_store.face_set(db, faces_store.labels_token(people, labels)),))
    return len(assignments), len(labels_by_face)
