# People: face recognition, tagging and filtering

Status: **in use** since 2026-09-29; the current library is backfilled (6.6 h,
16,805 assets, zero face failures, measured). Face index schema v4. Open items are
in [REMAINING.md](REMAINING.md); the build-up log is in git history (`app/FACES.md`
up to `2281c35`). Measured facts are marked as such below.
Goal from the user: best achievable accuracy; slower indexing is acceptable. The
current library should finish in days, and the planned ~2 TB of pre-2026 media
must not take weeks. Personal, non-commercial use only.

## Design

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

### Live grouping review and revision (2026-09-30, measured)

The completed live backfill took **6.6 h** for 16,805 assets (93,833 faces, 24,743
representatives, zero failures). Its review list offered 851 groups; the user
rightly doubted that many people. Analysis of a copy of the live index:

- 1,363 of 2,384 groups came mainly from screenshots (PNG), screen recordings or
  saved/shared media without camera metadata: social-media faces, not her footage.
  Near-identical 0.97–0.98 matches were the same avatar/overlay across screenshots.
- 252 groups came from a single clip or photo.
- One person was split across groups; whole-group average similarity ≥ 0.55 joined
  the same person in different looks, while 0.45 pairs were different people.
- A cluster of false detections (goats, chickens, objects, backs of heads) had low
  ArcFace embedding norms: below 15 nearly all non-faces, 15–17 mixed, ≥ 17 faces.

Changes: embedding norm ≥ 17 for grouping/suggestion seeds; a second average-linkage
merge pass (0.55, recomputed averages, rejection veto); per-asset origin (camera /
screen / other, face schema v2 migrated in place); review list limited to groups in
≥ 2 media and mostly from her own camera by default (toggle for saved media),
sorted by media count. Named people are still suggested in all media. Live result:
**133** review groups (was 851); regroup of 11,652 faces takes 2.5 s.

### Review flow and automatic matches (2026-09-30)

User feedback: naming ~35 faces of a group of thousands made the whole group vanish
without explanation (its other faces had become suggestions for that person), and
two places to "approve" felt confusing. Now naming from a group opens the person's
page with an explanation, groups list their least typical faces first (face schema
v3 stores each face's similarity to its group average), and a person's page has
three tabs: **To check**, **Matched automatically** and **Confirmed**.

`FaceIndex.automaticSimilarity = 0.65`: suggestions at least this similar to a
confirmed face count without review. Banded visual review of Lucia's 7,642
suggestions (20 random faces per band): all sampled faces ≥ 0.65 were her (6,779
faces, 89%); 0.60–0.65 had one or two doubtful faces; below 0.55 clear mistakes
appeared (partial faces, a screenshot, a dog). A 20-face sample cannot exclude a
~2% error rate, so rejection stays available on automatic matches. Automatic matches
are derived, not written to the personal store: they follow new confirmations.

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

## Implemented

- Generated face schema/queue; worker with pause, retry, stop/requeue, upstream
  yielding and a stall watchdog; catalog mirrored only when the catalog changes.
- Personal `people`/`face_labels`/`people_history` tables with exact scoped undo.
- Grouping (mutual nearest neighbours), suggestions, rejections, track propagation
  and same-moment matches (a confirmation accepts that person's resembling faces in
  the same clip or photos within 60 s).
- Review: To check / Matched automatically (≥ 0.65) / Confirmed tabs, outliers
  first, Shift-click ranges, ⌘A, ⌘↩ confirm, ⌘⌫ not this person; edits save off the
  main thread and each person page shows what an edit changed.
- People filter (media with all selected people).
