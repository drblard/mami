# People: face recognition, tagging and filtering

Status: **planned** (2026-09-29). Nothing below is implemented or measured yet.
Goal from the user: best achievable accuracy; slower indexing is acceptable. The
current library should finish in days, and the planned ~2 TB of pre-2026 media
must not take weeks. Personal, non-commercial use only.

## Recommended design

### Recognition model

- **InsightFace `antelopev2`**: SCRFD-10G detector with five facial keypoints plus
  the `glintr100` ArcFace ResNet-100 recognizer (512-d embeddings). This is the
  strongest openly available recognizer family. Its weights are licensed for
  non-commercial use, which matches this app. Pin exact files by SHA-256.
- Run both through ONNX Runtime (CoreML execution provider, CPU fallback) in the
  existing bundled Python worker. The detector's keypoints produce the standard
  ArcFace 112×112 alignment, so detection and recognition stay consistent.
- Apple Vision was considered: it detects faces well but exposes no identity
  embedding, so it cannot tag people by itself.

### Coverage

- **Photos:** decode the original, not the 640 px preview. Detect at a long side
  of ~1920 px; tiled detection for very large images if small faces are missed.
  Crop/align each face from the full-resolution original.
- **Videos:** decode the original at ~1080 px and sample one frame per second,
  reusing the existing timestamps. Faces of one person that are close in time are
  grouped into a short track, and only the sharpest, most frontal faces per track
  are kept. This avoids hundreds of near-identical faces per clip.
- Quality scores (detector confidence, face size, blur, pose) are recorded, so
  tiny or blurry faces can be shown but excluded from automatic matching.

### Throughput (estimate, to be measured)

Assumed on the M1 Pro: detection ~15–30 ms per frame and recognition ~10–20 ms per
face, with hardware video decoding. This suggests hours for the current ~17k
assets, and roughly one to three days for 2 TB, depending on video duration.
A feasibility benchmark on a sample of real media must replace these guesses
before anything is scheduled.

### Data ownership

- **Generated (catalog/Derived):** face detections, boxes, keypoints, quality,
  embeddings and automatic clusters. They can be rebuilt; they never trigger
  personal backups.
- **Personal (`user.sqlite`):** people (name, created), confirmed and rejected
  face assignments. Each assignment stores asset ID, timestamp, normalized box
  and a copy of the face embedding (~2 KB), so names survive a full regeneration
  of the face index and can be re-matched without guessing.
- Migration adds new personal tables only. Verified, atomic publication and
  backup/restore follow the existing personal-store rules.

### Matching

- Unnamed faces are clustered conservatively (prefer splitting over merging).
- A person is matched by nearest confirmed examples, not one average face,
  so children growing up over years remain recognizable as more examples are
  confirmed across ages.
- Media are either **confirmed** (a tagged face) or **suggested** (above a
  threshold, not yet reviewed). The filter shows both, marking suggestions.

### User experience

1. **People** view: groups of unnamed faces, largest first. Name a group once
   ("our son"); merge two groups; remove wrong faces.
2. Person page: confirmed media, then suggestions to accept/reject in bulk.
3. **People** filter beside Tags & places: select one or more people, e.g. all
   media with our son, combined with the existing date/kind/device filters.
4. Preview: face boxes with names; click a face to name or correct it.

### Correcting wrong matches

Automatic groups will contain mistakes; correcting them must be quick and permanent.

- **Remove faces:** select one or many faces in a group (click, ⌘/⇧-click, or drag
  a selection box) and choose *Not this person*. They return to unnamed faces.
- **Move faces:** *Move to…* an existing person or *New person* from a selection.
  This splits a mixed group, e.g. two siblings, in one step.
- **Split suggestions:** a group can be re-divided at a stricter similarity, showing
  its sub-groups side by side so a mixed group can be separated at once.
- **Merge:** combine two groups or people that are the same person.
- **Undo:** every naming, removal, move and merge can be undone; changes use the
  existing personal edit history.
- **Corrections are permanent:** a rejection is stored as "this face is not this
  person" in `user.sqlite`. Re-clustering, new imports and full index rebuilds
  must respect confirmations and rejections; a rejected face is never suggested
  for that person again, and a confirmed face never moves automatically.
- Suggestions are ordered least-confident first on request, so likely mistakes
  can be reviewed before they spread to other matches.

### Pipeline priority

Face indexing is a separate durable queue after previews, consistent with
[PIPELINE.md](PIPELINE.md): import visibility and previews stay first, and it
yields to CapCut activity like the AI lane. Progress appears as its own lane.

## Checklist

- [ ] Feasibility: pinned models, isolated benchmark on a real sample (photos and
      DJI video). Measure speed per photo/video-minute, recall of small faces and
      clustering quality; compare preview-frame vs full-resolution coverage.
- [ ] Generated face schema and queue; worker stage with cancellation/EOF/restart tests.
- [ ] Personal people/assignment tables, migration, backup/restore tests.
- [ ] Clustering/matching with exact threshold tests on fixtures, including
      confirmed/rejected constraints surviving re-clustering and a full rebuild.
- [ ] Correction actions: remove, move, new person from selection, split, merge, undo.
- [ ] Native People view, naming/confirmation, preview face boxes and People filter.
- [ ] Native build/tests on `ludi`, isolated fixture run, then signed release.
- [ ] Live backfill of the current library, with progress and measured duration.
