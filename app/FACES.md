# People: face recognition, tagging and filtering

Status: **installed; live backfill running** (2026-09-29). Build
`prototype-20260929T204729959318Z` (revision `56ce062`) is installed at
`~/Applications/Mami.app`; the face lane started automatically and is indexing the
current library newest first. Measured facts are marked as such below.
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
- The published detector declares 640×640 output shapes despite dynamic inputs,
  which CoreML rejects. Fixed-size square copies (640, 1920) are generated once
  from the verified original into `Derived/Faces/Engine` (with CoreML caches).
- **Measured (M1 Max):** CPU detection 749 ms at 1920 px and 90 ms/face recognition;
  CoreML GPU 48 ms and 5 ms/face. An ANE (`ALL`) recognizer prediction **hung
  indefinitely** during the first feasibility run, so both models use `CPUAndGPU`
  (equally fast), and the worker has a per-frame heartbeat watchdog.
- Apple Vision was considered: it detects faces well but exposes no identity
  embedding, so it cannot tag people by itself.

### Coverage

- **Photos:** decode the original (macOS ImageIO, orientation applied, primary
  HEIF image) at up to 4096 px, not the 640 px preview.
- **Multi-scale detection (measured):** at 1920 px, close-up faces (selfies, DJI
  vlogging) exceed the detector's anchor range and are missed. On the first 409
  sample files: preview 640 px found 419 faces, 1920 px alone 264, and 640+1920
  merged with NMS **461**. Production detects at 640 and 1920 and merges.
- **Videos:** decode the original at ~1080 px and sample one frame per second,
  reusing the existing timestamps. Faces of one person that are close in time are
  grouped into a short track, and only the sharpest, most frontal faces per track
  are kept. This avoids hundreds of near-identical faces per clip.
- Quality scores (detector confidence, face size, blur, pose) are recorded, so
  tiny or blurry faces can be shown but excluded from automatic matching.

### Throughput and quality (measured)

Rerun on `ludi` (M1 Max, CoreML GPU, idle/locked Mac, warm OS cache possible):
deterministic sample of 400 photos and 40 videos (1,621 video seconds) from
Originals, read-only; results in `~/mami-lab/benchmarks/faces-20260929/run2/`.

| | preview 640 px | multi-scale 640+1920 (chosen) |
|---|---|---|
| faces found | 1,412 | **1,725** (+22%) |
| reliable faces | 916 | **1,076** (+17%) |
| seconds per photo (incl. decode) | 0.31 | 0.46 |
| seconds per video second | 0.19 | 0.26 |
| projected current library (12,566 photos, 27.2 h video) | 6.2 h | **8.6 h** |

The 2 TB pre-2026 import is not measured; at the same per-item cost it would be
roughly 3–4 days of background work (an extrapolation, not a measurement).
Grouping 299 reliable representatives gave 41 groups of ≥2; visual review of the
contact sheet found every group to be one person, and all 84 faces of the largest
group (photos and videos across dates) the same person. Grouping errs towards
splitting (e.g. one video's track forming its own group); naming a person then
suggests the rest. This is a small sample of mostly one family, not a benchmark
of recognition accuracy across ages; confirmation remains the user's decision.

### Data ownership

- **Generated (catalog/Derived):** face detections, boxes, keypoints, quality,
  embeddings and automatic clusters. They can be rebuilt; they never trigger
  personal backups.
- **Personal (`user.sqlite`):** `people`, `face_labels` (confirmed/rejected, one
  confirmation per face) and `people_history`. Labels are keyed by asset,
  timestamp and normalized box (6 decimals), not embedding copies: the pinned
  model regenerates embeddings, and regenerated faces are matched by overlap
  (IoU ≥ 0.5, ±0.25 s), so labels survive a full face-index rebuild while keeping
  personal backups small. Changes bump the personal revision (normal backups).
- **Generated (`Derived/Faces/faces.sqlite`):** jobs, faces, embeddings, groups and
  assignments, plus `Crops/` thumbnails for representative faces. Separate from
  `catalog.sqlite` to avoid growth and writer-lock contention.
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

`face_worker.py` owns a separate durable queue mirrored from catalog assets whose
previews are complete. It yields while preview or AI-search work is pending
([PIPELINE.md](PIPELINE.md)), waits during active CapCut use, persists pause,
requeues on quit (no attempt spent) and bounds failures at 3 attempts. A face
that yields no heartbeat for 120 s fails its asset and restarts the worker.
Progress appears as a third footer lane (“Faces”).

## Checklist

- [x] Pinned models (archive + file SHA-256), verified atomic install via Settings'
      model download; isolated benchmark environment and sample on `ludi`.
- [x] Feasibility benchmark rerun (GPU units, checkpointed): final timings,
      projected library duration and visual group-purity review (above).
  - [x] First run: resolution comparison (above). It hung in an ANE prediction
        at 430/440 files; results were in memory only. Rerun saves checkpoints.
- [x] Generated face schema and queue; worker with pause, retry, stop/requeue,
      upstream yielding and stall watchdog (Python tests, fake extractor).
- [x] Personal people/label tables, exact scoped undo, history, revision bump
      (native `--catalog-test` check written; Mac run pending).
- [x] Grouping (mutual nearest neighbours, confirmed faces linked), suggestions
      (closest confirmed face, margin), rejections honored, track propagation;
      exact tests on synthetic identities and on the store.
- [x] Correction actions: deselect then name, add/move to person, not-this-person,
      confirm suggestions, rename, merge, remove person, multi-step undo.
- [ ] Split a mixed group at a stricter threshold (deselect-and-name covers the
      common case; dedicated split view not implemented yet).
- [ ] Face boxes in the media preview; click a face to name it.
- [x] People filter (media with all selected people) and People review sheet.
- [x] Native build/tests on `ludi` (21 Swift / 191 Python, catalog + people store
      check), isolated end-to-end `--people-test` on 36 real assets, signed release
      with the ONNX-enabled runtime (`~/mami-lab/runtime-env-faces`, runtime tree
      `prototype-20260929T202434970792Z/runtime-faces`).
  - [x] The end-to-end check found and fixed: text-bound `HAVING` comparison (no
        groups), read-only WAL reader failure after worker exit, CoreML writing to
        the protocol descriptor, a dirty-fixture false failure (now guarded) and
        blank controls over a transparent sheet background.
- [x] Installed; personal-table digests unchanged; people tables added (empty).
      Face model installed through the verified installer. Previous app retained
      at `~/Applications/.Mami-install-eb63285b24a34e4195fa8a03b155f745.app`.
- [ ] Live backfill of the current library completes; record measured duration.
- [ ] User acceptance: name people, correct groups, People filter (physical use).
