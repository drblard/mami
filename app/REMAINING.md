# Mami — completion tracker

Updated: 2026-09-29. Checkboxes mean verified outcomes, not merely implemented code.
Detailed measurements/history: [SCALING.md](SCALING.md).
Code review: [CODE_REVIEW.md](CODE_REVIEW.md). Queue contract: [PIPELINE.md](PIPELINE.md).

**Current constraint:** Mac builds, tests, benchmarks and deployment are paused
while the user's wife works. Linux development/tests can continue.
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
  - [ ] Repeat native/UI behavior with these fixes on the Mac.
- [ ] Complete atlas backfill review: queue priority, cancellation, publication
      interruption, retries, restart and retained/orphan generation handling.
  - [x] Yield before publication for stop, active editor or newly pending previews.
  - [x] Publication rollback/retry preserves raw frames; directory entries are fsynced.
  - [x] Worker EOF exits; exactly three failed attempts persist; repaired frames retry.
  - [x] Frame inserts/updates/deletes invalidate stale completion/error/retirement state.
  - [x] Retirement requires exact manifest/source/projection crop and timestamp parity.
  - [ ] Bounded cleanup policy for retained/orphan atlas directories (currently retained).
- [ ] Validate v6 counts/filtering/page boundaries and locked-grid arrivals natively.
- [ ] Repeat actual process lifecycle checks with the latest schema and split workers.
- [ ] Validate rapid typing/cancellation, visual failure with speech available,
      off-page results and preview refresh in the native UI.
  - [ ] Verify the new per-query native metadata-cache bound; regression assertion added.

## 2. Close performance and quality gates

- [ ] Measure native v6 first-page/text readiness (goal <1 s).
- [ ] Measure genuinely cold startup/search (goal <5 s); label cache conditions.
- [ ] Run full-path 50× latency and exact-search distinct-file recall together,
      including camera/date/shape/kind/favorite filters and narrow scopes.
- [ ] Verify typing-to-render latency with debounce, rather than backend time alone.
- [ ] Resolve memory gate: measured search footprint is ~3 GiB, above the original
      provisional ~2 GiB; reduce or explicitly justify the revised budget.
- [ ] Measure sustained cache/overlay/compaction growth and generation-swap peaks.
- [ ] Validate import/indexing/editing contention in an agreed idle test window;
      do not benchmark against her active editing session.

## 3. Finish SSD preview acceptance

- [ ] Verify first/middle/last packed scrub frames in the latest native offline run.
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

Latest local suite: **136 tests: 133 passed, 3 Mac/MLX skips**; Ruff passes.
Native additions await compilation/validation on the Mac; they are not counted as passed.

Work through local correctness/recovery first, test each fix, and push checkpoints.
Then resume the Mac-gated checks when it is available. Keep failed gates and deferred
items unchecked; warm-cache results do not close cold-start acceptance.
