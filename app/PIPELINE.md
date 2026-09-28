# Import, preview and search readiness

Status: **implementation in progress**. Requested after the four-video DJI offload.

**Live milestone:** `prototype-20260928T180349767667Z` is running with native
batched previews (activation confirmed). The prior queue-separation activation
confirmed generated schema v2 and separate preview/index/search processes. At
activation 15,684 preview jobs were complete, 1,060 pending/running; 15,682 AI jobs
were complete, 1,062 queued. No error jobs were reported. Existing checkpoints
were preserved. Large-DJI latency and further scaling remain open measurements.

## User-visible contract

1. **Imported / playable:** after byte verification and publication in Originals,
   register the media directly in the catalog. Do not wait for a full-library scan,
   a model to load, or another video's AI job. Existing filters/grid lock still apply.
2. **Preview-ready:** a separate durable preview queue creates a first thumbnail
   for each arrival, then coarse full-range scrub samples, then finer previews.
   New imports take priority over the older backlog. Original playback is already available.
3. **Search-ready:** the AI queue consumes completed previews and produces visual
   embeddings/transcripts. Its delay/failure must not hide playable media or block previews.

The importer, preview worker and AI worker have separate process/queue ownership.
Copy verification and optional source-removal checks remain unchanged. Regenerated
queue state stays in `catalog.sqlite`, not personal backups.

## Checklist

- [x] Extract shared queue schema/transactions; migrate old jobs into preview/index queues.
- [x] Publish verified imports immediately, including duplicate/resume cases.
- [x] Run preview work independently from inference, with first-thumbnail priority.
- [x] Publish coarse/fine scrub progress; add timestamp-based native scrub selection.
- [x] Yield AI work while preview work is pending; reuse existing checkpoints.
- [x] Implement separate preview/AI progress and readiness in the native app (Mac checks pending).
- [ ] Test four arrivals appearing before any AI work, preview-before-AI ordering,
      restart/retry, source mutation, and no premature source cleanup.
  - [x] Python queue/import/recovery tests pass on Linux and the Mac.
- [ ] Measure actual import-to-catalog and import-to-preview timings on the Mac.
  - [x] Four short real-codec fixture clips: all catalogued by 0.294 s;
    all thumbnails 0.345 s and complete scrub previews 0.636 s after workers start,
    with AI paused and zero inference units. Sources retained. Larger DJI timing pending.
- [x] Validate/sign a fresh build before activation.

## Implementation checkpoint

### Real DJI responsiveness run — active

- Source: existing `Originals/DJI-Pocket-4P/2026/2026-09-28` clips, read-only.
- Isolated output: `prototype-20260928T173844873061Z/dji-responsiveness-check/`.
- Tool: `check_dji_responsiveness.py`; durable per-file observations in
  `progress.json`, final measurements in `result.json`, separate worker logs.
- Test starts preview/index processes before importing; AI is persistently paused.
  It measures publication, first thumbnail, full-range coarse scrub coverage and
  completed dense previews per clip, and verifies unchanged source signatures.
- This is an SSD-to-SSD repeat import, not a camera USB-speed measurement.
  Native UI visibility and actual continuous playback are separate checks.
- Mac activity checked immediately before starting: lock screen, idle >100 minutes.
- Read-only AVFoundation validation passed for all six current DJI clips (4.1–48.7 s).
  Start/middle/end frame decoding took 12–19 ms after the first 176 ms decode.
  Results: `dji-playback-readiness.json` beside the build. This confirms playable
  original codecs and seeking, not continuous playback/UI-render latency.
- Real import run completed: six clips / 579,128,334 source bytes, all catalogued
  within 1.272 s from test start (SSD-to-SSD; not USB and not native grid timing).
  Per-clip first thumbnails: 1.95–6.18 s after catalog publication; full-range scrub
  coverage: 13.56–55.66 s; completed dense previews: 13.56–120.62 s. AI units: zero.
  All source signatures unchanged. **Functional pass, preview latency still too slow.**
- Next optimization: measure single-pass video sampling versus one FFmpeg process
  and seek per frame. Preserve timestamp/checkpoint semantics and first-thumbnail
  priority; do not call these timings an acceptable finished UX.
- Sequential FFmpeg benchmark on the 48.7-second DJI HEVC clip: 49 preview frames
  in **40.75 s software** versus **6.07 s VideoToolbox hardware decoding**. Outputs
  and results retained in `single-pass-preview-check/` beside the live deployment.
  This uses `fps=1`, whose exact sampling semantics differ from the current midpoint
  checkpoints. Do not replace existing frame/vector references without validating
  timestamp-compatible extraction. Hardware/batched extraction is not deployed yet.
- Follow-up candidate `prototype-20260928T180349767667Z` uses a bounded native
  AVFoundation batch decoder (up to 16 requested midpoint timestamps per process),
  sharing decoder state and retaining the existing FFmpeg fallback for unsupported
  media. Actual returned timestamps must stay within 110 ms of requests; output
  JPEGs are verified before checkpointing. Existing checkpoints are reused.
  Linux checks pass (79 tests, 3 NumPy skips); Mac build/tests/signing are running.
  Repeat the same six-clip fixture before activation; current live build is unchanged.
- Candidate build/signing and all 14 Swift / 79 Python Mac tests passed. The
  identical six-clip responsiveness fixture is now running under
  `prototype-20260928T180349767667Z/dji-responsiveness-check/`; live build unchanged.
- Repeat fixture passed: six clips catalogued by **1.212 s**; thumbnails
  **1.87–2.18 s** after cataloguing; full-range scrub coverage **3.32–4.36 s**;
  dense previews **3.32–7.78 s**. Total **8.57 s**, versus **121.45 s** previously.
  AI remained paused, sources unchanged. This is SSD-to-SSD with real DJI footage,
  not native grid-render timing or USB transfer throughput.
- Verified all 131 generated JPEGs, 640px bound and portrait orientation. Native
  helper rejects overwrites and batches over 16. Injected native failure correctly
  fell back to real FFmpeg and produced a validated JPEG. Strict signature passed.
- Activation of `prototype-20260928T180349767667Z` is in progress after confirming
  locked/idle Mac and no running import. Check `activation-check.json` for completion.
- Activation completed: native app, preview/index/search workers and native batch
  helper confirmed running. Audit saved as `activation-check.json`; no source or
  personal-data migration was required for this preview optimization.

- `index_store.py` owns generated queue schema/transactions and verified admission.
  Index schema v2 adds `preview_jobs` and independent preview pause/retry state.
- `preview_pipeline.py` prioritizes first thumbnails, then coarse full-range samples,
  then round-robin dense batches. Scheduling order is persisted as a sequence,
  so wall-clock changes do not affect fairness.
- `index_worker.py --role preview` performs no inference. `--role index` consumes
  completed previews and yields when preview work is waiting. Standalone `--once`
  retains an explicit combined mode for fixture/tools compatibility.
- Import progress signals catalog publication directly; visibility no longer waits
  for discovery. One content identity remains one logical grid item on reimport.
- Empty-frame media records are playable through their original URL. Coarse scrubbing
  uses timestamps; clip-cache reconnection no longer creates personal backup revisions.
- Five new pipeline tests pass, along with the existing import/queue/recovery tests.
  Current local total: 79 tests (76 pass, 3 explicit NumPy skips).
- `check_media_pipeline.py` is the pending real-codec/process check: four generated
  clips, visible before previews, separate preview process while AI is paused,
  timing report, no AI units and original-fixture retention.
- Candidate `prototype-20260928T173844873061Z` passes 14 Swift and 79 Python tests
  on the Mac. Packaging, real-codec and native UI/catalog checks are pending.
- Signed packaging, native catalog/personal-store checks and the real-codec pipeline
  fixture now pass. Results are retained in `pipeline-check/result.json`. The UI
  check is still running; this candidate has not been activated.
- Native UI integration and strict signature verification passed. Activation is
  in progress with no import in flight; confirm `activation-check.json` before
  considering the new queue schema/workers live.

Mac work must first check CapCut activity. The user reported she left the computer;
the last check found the lock screen and an idle session. Keep integration fixtures
and their backups/artifacts separate from the live library.
