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
        settled = bool(confirmed_people) or any(source == 'suggested' and who == person and (similarity or 0) >= face_people.AUTOMATIC_SIMILARITY
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
    db.execute('UPDATE face_state SET grouped=? WHERE id=1', (faces_store.face_set(db),))
    return len(assignments), len(labels_by_face)
