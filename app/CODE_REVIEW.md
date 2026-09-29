# Production code review — live checklist

Started 2026-09-28 at the user's request. **In progress.**
Scope: production Swift/Python, packaging and tests (~8.3k lines before this pass).
Scaling experiments remain isolated in `app/scaling/`; their results are not a
substitute for production correctness. Current live build:
`prototype-20260928T180349767667Z` (review fixes, separate preview/AI queues and
validated native batch preview decoding).

## Review approach

- [x] Inventory responsibilities, dependencies and existing tests.
- [ ] Review persistence, migrations, backup publication and retention.
- [ ] Review worker lifecycle, protocol framing, cancellation and shutdown.
- [x] Review imports / verified deletion and Photos handoff.
- [ ] Review search/indexing boundaries and resource use.
- [ ] Review UI state and avoid unnecessary full-catalog work.
- [ ] Verify fixes with focused tests and native integration checks.
- [ ] Record remaining issues and the final deployed revision.

## Findings and work

### R1 — retention policy and tests contain magic numbers / depend on wall time

- **Evidence:** `Catalog.retainUserBackups` embedded thresholds and formatted dates;
  `CatalogChecks.swift` allowed a range of retained counts instead of verifying files.
- [x] Introduce a named, pure `BackupRetentionPolicy` in `MamiCore`.
- [x] Inject one timestamp into snapshot creation and retention; use UTC calendar
  windows rather than approximating a year as a fixed number of days.
- [x] Replace the loose integration count with an exact expected set of files.
- [x] Add focused Swift unit tests for recency, each retention tier, cutoffs,
  equal timestamps, other identities and clock rollback.
- [x] Run the unit/integration tests on the Mac.
  - [x] All 14 Swift unit tests pass on the Mac.
  - [x] Refreshed native catalog, personal-store and UI integration checks pass.

### R2 — test catalog overrides still use production backup directories

- **Evidence:** `Catalog.standard` honors `MAMI_CATALOG` for the database but always
  supplies the live `~/mami-lab/backups/catalog` path.
- [x] Make overridden catalogs default to their own backup directories.
- [x] Add a configuration-isolation regression check (also legacy annotations/artifacts).

### R3 — storage and scheduling responsibilities are mixed

- **Evidence:** `Catalog.swift` contains catalog operations, migrations, backup
  filesystem I/O, the SQLite wrapper and the observable backup scheduler.
- [x] Extract cohesive storage/scheduling components without widening the public API.
- [x] Name scheduling policy values and use a monotonic clock for elapsed-time limits.
- [x] Consolidate duplicate SQLite backup logic and validate required state rows.
- [x] Copy only explicitly owned personal tables during migration, rather than
  copying the entire generated cache and relying on a deletion list.

### R4 — search worker transport has unbounded waits

- **Evidence:** `SearchWorker.readLine` uses blocking `availableData` without a
  deadline; `stop` waits indefinitely. Startup/protocol failures can leave a child
  process allocated. Existing cancellation drops stale results but cannot stop a hang.
- [x] Add bounded line framing/deadlines and explicit failed-worker cleanup; tests added.
- [x] Bound shutdown while preserving graceful EOF for healthy workers.

### R5 — production inference imports the experiment driver

- **Evidence:** `search_worker.py` / `index_backend.py` import model pins and sampling
  helpers from `feasibility/lab.py`, which also changes environment variables on import.
- [x] Move production pins/cache settings and sampling helpers into production modules.
- [x] Keep pipeline identity tied to the selected model revisions.
- [x] Verify sampling/pipeline compatibility in Python tests and real-model Mac checks.

### R6 — speech tests mostly exercise a superseded implementation

- **Evidence:** `speech_hits` is no longer called by the worker; real queries use
  `Snapshot.spoken`, while several existing tests still exercise the old linear scan.
- [x] Consolidate the actual speech-search implementation into a NumPy-independent component.
- [x] Move tests onto the production path, covering prefixes, accents, scopes and timestamps.

### R7 — selection loading unnecessarily decodes the full media catalog

- **Evidence:** `ClipSelection.load` calls `catalog.media()` even for an empty or
  tiny selection, duplicating library startup work.
- [x] Query only the selected asset IDs, using the existing asset index.
- [x] Preserve relocation/reconnection behavior and test empty/scoped retrieval.

### R8 — metadata probing does avoidable extra I/O

- **Evidence:** the index backend writes a one-record inventory JSON, then metadata
  extraction invokes ffprobe again despite already having the format/stream data.
- [x] Use a pure probe-to-metadata function; keep file inventory loading as an adapter.
- [x] Verify capture dates, camera labels and error behavior with fixtures.

## Issues already assigned to the scaling work

- Full-catalog browsing/refresh and corpus-sized snapshot rebuilding are not a
  50× solution. Paging and durable incremental indexes remain explicit milestones.
- Import/index progress protocols and active-editor heuristics have different
  lifecycle semantics; do not force them into one generic worker abstraction.
- Programmatic-only AppKit `init(coder:)` failures and best-effort rollback/close
  cleanup are not automatically bugs. Review their contracts rather than replacing
  every `fatalError`, force unwrap or `try?` mechanically.

## Activity log

- Resumed Mac validation: candidates `065032418935Z` and `065824172337Z` passed
  native v6 scoped counts, equal-date/oldest-first pages, grid-lock cutoff and the
  per-query metadata bound. Latest `070426288464Z` passes 14 Swift / 140 Python
  tests after fixing a wall-clock-dependent historical-pruning test (fixed UTC
  fixture time and exact retained sets). No signed bundle was patched.
- Full 144-query 50× reference checks initially exposed a distinct-video failure:
  one video's frames exhausted the reranking budget, returning 50 instead of 60
  videos. Exact scoring for small scopes now gives 100% file recall across all
  six tested scopes, overall p95 71.9 ms. This is warm-cache synthetic capacity
  evidence; cold cache and real-library comparison are separate checks.
- Registered atlas-generation cleanup is implemented/tested: current plus one
  superseded generation retained; projection references pin old sheets; exact
  file/digest verification preserves unknown or modified content. Completed
  publication orphans can be reclaimed; unregistered/incomplete scratch remains
  explicitly outside automatic deletion.

- Latest 2026-09-29 local loop: bounded visual request writes (previously only
  reads were bounded), startup deadlines, strict reply framing and terminal restart
  failure. Private process groups reap encoder descendants. Combined search returns
  speech results with an explicit visual error after visual failure. Real subprocess
  tests cover blocked I/O, invalid replies, partial EOF and exact restart exhaustion.
- Atlas checks now cover priority changes during packing, cancellation before
  publication, database publication failure/retry, EOF and persistent retry limits.
  New/deleted frames invalidate stale pack/error markers. Raw retirement compares
  exact manifest/source/projection metadata, not just sheet membership. Failed
  published directories remain retained; bounded orphan cleanup is still open.
- Native per-query metadata retention is limited to the loaded browse window and
  current matches. Native assertions added for that bound and exact v6 scoped counts,
  partial days and oldest-first pages. These need Mac compilation/validation.
  Latest Linux suite: 136 tests, 133 passed / 3 MLX skips; Ruff passes.

- 2026-09-29 local checkpoint: Mac work paused while the user's wife works.
  Independent text/visual processes now have bounded reply framing, query deadlines
  and at most two visual restarts. Local tests exercise pending initialization,
  text availability, protocol continuity and recovery after a child exits.
  Projection v6 maintains browsing facets in the same transaction as file changes.
  Failure-injection tests verify new-schema rollback, v5 migration rollback,
  preserved transcript results and successful retry; failed initialization closes
  its connection. Linux suite: 121 tests, 118 passed / 3 MLX skips.
  Native v6 validation and latest lifecycle integration remain pending. Live build
  remains `prototype-20260928T180349767667Z`; no acceptance gate is closed by these
  local checks.

- 2026-09-28: user asked for best practices throughout and a broader simplification
  pass. Repository guidance added in `AGENTS.md`. Retention extraction started;
  no review build deployed yet. Live data remains in the already verified split store.
- Python review checks: 74 tests, 71 passed locally and 3 NumPy-dependent tests
  explicitly skipped. Worker packaging now uses one manifest with dependency checks.
- Native test setup found CLT lacks XCTest/Testing. Swift Testing 6.4 also links
  Xcode-only `_TestingInterop`; pinned 6.2 instead, which has no such dependency.
  Test infrastructure checks are still pending; do not claim native review tests passed.
- User requested separate import/preview/AI queues; implementation contract and
  acceptance checklist are in `PIPELINE.md`. This also removes the observed
  head-of-line blocking in `Queue.run_job` (preview + embedding interleaved).
- Review changes now include indexed selected-asset lookups, isolated backups/
  legacy-annotation/artifact paths, named retention/scheduling policy, monotonic
  deadlines, bounded worker transport, positive-list personal migration and one
  worker-resource manifest. Python checks currently pass (79 tests, 3 skips).
- Import/source-removal verification and Photos handoff were reviewed. Existing
  verification ordering is preserved; immediate catalog publication happens only
  after successful byte verification. Hard-link cleanup updates the destination
  signature without forcing another source hash.
- Preview scheduling initially exposed a retained SQLite cursor locking the next
  transaction. The cursor is now explicitly closed; recovery/order tests pass.
- Swift Testing 6.2 validation is running in `prototype-20260928T171808235430Z`.
  The production app has not been replaced by these pending review/queue changes.
- Swift Testing now builds with the CLT environment. Its partial-line test exposed
  a blocking Foundation pipe-read behavior in the first deadline implementation.
  Replaced that call with one POSIX read per readiness check; retained the hanging
  test's sample and terminated only its owned test helper. `verify.py` now gives
  checks a wall-clock bound and cleans up their process group on timeout.
- Current validation candidate: `prototype-20260928T173844873061Z`. The previous
  hanging run is recorded as failed, not counted as passing validation.
- That candidate now passes **14 Swift tests / 79 Python tests** on the Mac,
  including the actual partial-line timeout. Real-codec and native UI/catalog
  integration checks are next; no review/queue build has been activated yet.
- Native catalog and personal-store integration passed in the signed candidate:
  migration, exact retention outcomes, generated-data exclusion, personal-only
  restore, missing-store detection, rollback and full-export compatibility.
  Real-codec pipeline and UI checks are running in `pipeline-check/` and `ui-check/`
  under `prototype-20260928T173844873061Z`. Mac activity was checked first: locked
  session, idle for over 90 minutes. Live build remains unchanged.
- Real-codec pipeline and native UI integration now pass; strict signature verification
  passed afterward. Activation of `prototype-20260928T173844873061Z` is in progress,
  after another activity check and confirmation that no import worker was running.
- Activation completed: separate preview/index/search workers confirmed, generated
  schema v2, existing checkpoints retained, no error jobs at activation. Audit:
  `activation-check.json` beside the signed bundle. Remaining review items and
  the 50× search-engine decision are not marked complete by this deployment.
