# Mami — completion tracker

Updated: 2026-09-29. Checkboxes mean verified outcomes, not merely implemented code.
Detailed measurements/history: [SCALING.md](SCALING.md).
Code review: [CODE_REVIEW.md](CODE_REVIEW.md). Queue contract: [PIPELINE.md](PIPELINE.md).

**Current work:** Mac validation resumed after the user confirmed editing is done;
foreground is `loginwindow`, idle over 11 minutes. Fresh candidate
`prototype-20260929T065032418935Z` is building/testing against isolated fixtures.
**Live app:** `prototype-20260928T180349767667Z` (fast previews).
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

- [ ] Complete worker review: bounded request writes as well as replies, malformed
      protocol, startup/query timeout, EOF, descendant cleanup and restart exhaustion.
  - [x] Visual transport Linux checks: blocked writes, partial replies/EOF, invalid
        JSON/shapes, size bounds, controllable startup deadline, exact restart budget.
  - [x] Isolated process-group cleanup, including an encoder child retaining a pipe.
  - [x] Combined queries keep transcript results after visual recovery is exhausted.
  - [x] Repeat native/UI behavior with these fixes on the Mac: combined visual failure
        preserves 60 transcript hits, and speech-only recovery passes.
- [ ] Complete atlas backfill review: queue priority, cancellation, publication
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
- [ ] Run full-path 50× latency and exact-search distinct-file recall together,
      including camera/date/shape/kind/favorite filters and narrow scopes.
      First 144-query pass: all/camera/shape/kind/favorite-style scopes had 100%
      recall; day minimum was 83.3%. Increased filtered reranking; repeat pending.
- [x] Verify typing-to-layout through SwiftUI debounce/latest-query cancellation:
      ~209 ms at 50×; 107 ms on the offline single-clip fixture (warm caches).
- [x] Resolve memory gate: [RELEASE.md](RELEASE.md) documents the format's memory floor
      and the revised 3.5 GiB steady / 7 GiB transient budget on the 64 GiB Mac.
- [x] Measure sustained search cache growth and generation-swap peaks: 4,096 queries,
      35 ms median / 41 ms p95, ~3 GiB steady and 5.57 GiB visual-process peak.
      Kernel counters replace intrusive vmmap sampling; earlier timing tails retained.
- [x] Finish overlay/deletion boundary checks: tombstones are bounded, empty
      replacement generations work, and pending maintenance is checked every 5 s.
- [ ] Validate import/indexing/editing contention in an agreed idle test window;
      do not benchmark against her active editing session.

## 3. Finish SSD preview acceptance

- [x] Verify first/middle/last packed scrub frames in the latest native offline run:
      82 cached frames, unavailable original, three native samples decoded.
- [ ] Run representative backfill/recovery and measure final storage/cache bounds.
- [ ] Confirm preview availability while AI is paused and across new imports/restarts.
- [ ] Verify rollback compatibility after packed frames replace raw cached JPEGs.

## 4. Release

- [ ] Compare the prepared live-derived projection against current live results and state.
- [ ] Complete [CODE_REVIEW.md](CODE_REVIEW.md) outstanding checks.
- [ ] Build/sign a fresh immutable release; run native checks after the Mac pause ends.
- [ ] Record exact migration/rollback steps and required retained data/builds.
- [ ] Activate only after acceptance; verify live import/search/preview/personal data.
- [ ] Record the deployed revision and close this tracker with evidence.

## Next loop

Latest candidate: `prototype-20260929T081741386944Z`: **14 Swift / 142 Python tests
pass on the Mac**, plus native catalog, worker shutdown, scoped browsing, offline
scrub, visual-failure and latest-query checks. Latest 50× typing-to-layout: 196 ms.
Live app has not yet been switched; contention, representative backfill and fallback
validation are next.

Work through local correctness/recovery first, test each fix, and push checkpoints.
Then resume the Mac-gated checks when it is available. Keep failed gates and deferred
items unchecked; warm-cache results do not close cold-start acceptance.
