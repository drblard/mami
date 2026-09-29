# Mami — completion tracker

Updated: 2026-09-29. Checkboxes mean verified outcomes, not merely implemented code.
Detailed measurements/history: [SCALING.md](SCALING.md).
Code review: [CODE_REVIEW.md](CODE_REVIEW.md). Queue contract: [PIPELINE.md](PIPELINE.md).

**Current work:** acceptance passed and the persistent-search release is live.
Final maintenance-queue patch `prototype-20260929T090709977496Z` passes 143 Mac
Python tests and signing; its activation is next.
**Live app:** `prototype-20260929T081741386944Z` (until patch activation is recorded).
**Baseline checkpoint:** `9f00bbe`; subsequent commits continue this tracker.
New search/atlas features are candidates.

## Verified foundations

- [x] Separate authoritative personal data and personal-only backups; verify migration/restore.
- [x] Independent import visibility, preview and AI queues; fast real-DJI preview checks.
- [x] Persistent projection and vector generations; real-process insert/replace/delete,
      compaction, generation switch and restart checks on the earlier candidate.
- [x] Native 50× browsing/search run: 723,700 files / 5.83M vectors, paged loading.
- [x] Offline atlas fixture: 82 frames → 3 sheets, ~86% smaller; protected retirement.
- [x] Native atlas crop rendering and stale-frame-reference recovery.
- [x] Independent text/visual services; 256-query 50× test: 35.6 ms median / 58 ms p95.
- [x] Projection v6 facet migration and failure/retry tests on Linux.

## 1. Finish correctness and recovery

- [x] Complete worker review: bounded request writes as well as replies, malformed
      protocol, startup/query timeout, EOF, descendant cleanup and restart exhaustion.
  - [x] Visual transport Linux checks: blocked writes, partial replies/EOF, invalid
        JSON/shapes, size bounds, controllable startup deadline, exact restart budget.
  - [x] Isolated process-group cleanup, including an encoder child retaining a pipe.
  - [x] Combined queries keep transcript results after visual recovery is exhausted.
  - [x] Repeat native/UI behavior with these fixes on the Mac: combined visual failure
        preserves 60 transcript hits, and speech-only recovery passes.
- [x] Complete atlas backfill review: queue priority, cancellation, publication
      interruption, retries, restart and retained/orphan generation handling.
  - [x] Yield before publication for stop, active editor or newly pending previews.
  - [x] Publication rollback/retry preserves raw frames; directory entries are fsynced.
  - [x] Worker EOF exits; exactly three failed attempts persist; repaired frames retry.
  - [x] Frame inserts/updates/deletes invalidate stale completion/error/retirement state.
  - [x] Retirement requires exact manifest/source/projection crop and timestamp parity.
  - [x] Registered generations retain current + one superseded pack; stale projection
        references pin old packs, and altered/unknown files are retained for review.
        Publication-orphan cleanup/retry tests pass locally; Mac suite is running.
- [x] Validate v6 counts/filtering/page boundaries and locked-grid arrivals natively.
- [x] Repeat actual process lifecycle checks with the latest schema and split workers.
- [x] Validate rapid typing/cancellation, visual failure with speech available,
      off-page results and preview refresh in the native UI.
  - [x] Verify the new per-query native metadata-cache bound.

## 2. Close performance and quality gates

- [x] Measure native v6 first-page/text readiness (goal <1 s): 76 ms at 50×.
- [x] Measure cold startup after an authorized macOS disk-buffer purge (not reboot):
      process launch → text 1.30 s; → visual 3.19 s, including diagnostic preflight.
- [x] Run full-path 50× latency and exact-search distinct-file recall together,
      including camera/date/shape/kind/favorite filters and narrow scopes.
      Final 144-query pass: 100% recall in all six scopes, ~70 ms overall p95.
      Earlier day-filter failure is retained in the evidence log; exact small-scope
      scoring fixed it. Real-library comparison also reached 100% recall (~33 ms p95).
- [x] Verify typing-to-layout through SwiftUI debounce/latest-query cancellation:
      ~209 ms at 50×; 107 ms on the offline single-clip fixture (warm caches).
- [x] Resolve memory gate: [RELEASE.md](RELEASE.md) documents the format's memory floor
      and the revised 3.5 GiB steady / 7 GiB transient budget on the 64 GiB Mac.
- [x] Measure sustained search cache growth and generation-swap peaks: 4,096 queries,
      35 ms median / 41 ms p95, ~3 GiB steady and 5.57 GiB visual-process peak.
      Kernel counters replace intrusive vmmap sampling; earlier timing tails retained.
- [x] Finish overlay/deletion boundary checks: tombstones are bounded, empty
      replacement generations work, and pending maintenance is checked every 5 s.
- [x] Validate import/indexing/editing contention in an agreed idle test window;
      do not benchmark against her active editing session.
      CapCut present + controlled DJI decode/import/previews/actual inference:
      speech p95 <2 ms, combined p95 26–39 ms. No project-editing gestures were used.

## 3. Finish SSD preview acceptance

- [x] Verify first/middle/last packed scrub frames in the latest native offline run:
      82 cached frames, unavailable original, three native samples decoded.
- [x] Representative DJI backfill: six clips / 159 frames, 9.07 MB → 1.57 MB;
      packing 0.65 s total, copied raw caches safely retired, originals untouched.
- [x] Preview availability while AI paused: six real imports visible by 1.45 s;
      first thumbnails ~1 s, full-range scrub 2.7–3.8 s. Restart/retry checks pass.
- [x] Crop-aware legacy rollback passes search, offline packed scrubbing and stale
      cache-reference reconnection (`081741386944Z-fallback`).

## 4. Release

- [x] Compare live-derived projection against current live results and state:
      17,257 distinct assets match; exact ranking and personal-state parity pass.
- [x] Complete [CODE_REVIEW.md](CODE_REVIEW.md) implementation/integration review.
- [x] Build/sign a fresh immutable release; complete native catalog/UI/transport checks.
- [x] Record migration/rollback steps and retained builds in [RELEASE.md](RELEASE.md).
- [x] Activate only after acceptance; verify live workers, matched catalog/projection
      and all personal tables (16,906 Photos verification rows, selection and roots).
- [ ] Record the deployed revision and close this tracker with evidence.

## Next loop

Live backfill completed **4,467 packs** with no pending pack/retirement work in the
verified migration snapshot. The final patch materializes pending/obsolete queues
so maintenance does not repeatedly scan all completed assets/generations. Schema
migration took 29 ms on the snapshot; indexed query plans and worker idle exit pass.
The unchanged native executable was reused only after exact Swift-source checksums
matched the fully tested build (14 Swift tests); the patch passes 143 Mac Python tests.

Work through local correctness/recovery first, test each fix, and push checkpoints.
Then resume the Mac-gated checks when it is available. Keep failed gates and deferred
items unchecked; warm-cache results do not close cold-start acceptance.
