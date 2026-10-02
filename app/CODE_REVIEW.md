# Production code review — live checklist

Started 2026-09-28 at the user's request. **Completed and deployed 2026-09-29.**

Follow-up implemented and validated: standard Mac storage, self-contained packaging
and on-demand workers. Acceptance and limitations: [MAC_APP.md](MAC_APP.md).
Scope: production Swift/Python, packaging and tests (~8.3k lines before this pass).
Scaling experiments remain isolated in `app/scaling/`; their results are not a
substitute for production correctness. Current live build:
`prototype-20260929T134121462682Z` (real signed app, Application Support storage,
lazy search and idle worker release). 16 Swift and 156 Python tests pass on the Mac;
native UI, lifecycle including paused/retry wake-up, and lab-independent checks pass.

## Review approach

- [x] Inventory responsibilities, dependencies and existing tests.
- [x] Review persistence, migrations, backup publication and retention.
- [x] Review worker lifecycle, protocol framing, cancellation and shutdown.
- [x] Review imports / verified deletion and Photos handoff.
- [x] Review search/indexing boundaries and resource use.
- [x] Review UI state and avoid unnecessary full-catalog work.
- [x] Verify fixes with focused tests and native integration checks.
- [x] Record measurement limitations, recovery and final deployed revision in RELEASE.md.

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

### R9 — maintenance workers read exit status before the exit is observed (live crash)

- **Evidence:** both live crashes (2026-09-30 14:18 and 2026-10-01 11:24) were
  `SwiftUI/Button.swift: Incorrect actor executor assumption` on a click, long after
  the real failure. Unified logs show `-[NSConcreteTask terminationStatus]: task still
  running` from `SearchMaintenance.finished` at 13:50:39 and 19:05:15. Output EOF
  arrives before Foundation observes the exit; AppKit swallowed the exception after
  it unwound a main-actor job, so imports/indexing stopped until the next click
  crashed the app (about 16 hours without processing in the second case).
- [x] Wait for the observed exit (then SIGTERM/SIGKILL) before reading termination
  state in `SearchMaintenance`; never read it while running (`ProcessExit`).
- [x] Same EOF-before-exit wait in `ModelDownloads`, which falsely reported
  successful downloads as protocol failures after terminating them.
- [x] `NSApplicationCrashOnExceptions` so any future main-thread exception crashes
  at its source with a backtrace instead of silently stopping work.
- [x] Maintenance worker stderr uses `AppDiagnostics.workerErrorOutput`, not the
  app error log.
- [x] `--worker-pipe-test` reproduces EOF 0.5 s before exit (statuses 0 and 3) and
  checks exact status/failure/retry state. It aborts with the live
  `NSException` against the old logic and passes with the fix on `ludi`
  (25 Swift / 202 Python tests pass).
- [x] Installed `prototype-20261001T083445972243Z` on 2026-10-01 (RELEASE.md); it
  started and resumed index, face and Photos-import work.

### R10 — 2026-10-02 deep pass: responsiveness, deletion follow-ups, worker contention

- **Evidence (UI lag):** idle main thread was free (sample: 2,783/2,875 waiting), so
  lag came from work triggered per change: every worker `changed` event refreshed
  the grid and republished identical data; every card observed the whole
  annotation/selection models and got new closures, so all visible cards redrew;
  the grid re-filtered (allocating a tag set per item) and re-sorted pages that
  already arrive in capture order; favorite/selection/people saves ran SQLite and
  `flock(catalog.lock)` on the main thread, behind worker transactions and backups.
- [x] Grid refresh is throttled (`CatalogUpdates.changed(after:)`, ≤2/s) and
  publishes only changed values; `MediaCard` takes values and is `.equatable()`.
- [x] Browsing skips the redundant client sort; filtering allocates nothing per item.
- [x] Annotation, selection, people edits and media-root registration save off the
  main thread, in order, showing the change at once and undoing it on failure.
- [x] Lane log writes moved off the main thread.
- [x] Single `Window` scene, models owned by the App, shutdown in the app delegate
  (⌘N previously created a second library whose selection overwrote the first).
- [x] Deletion: a record blocks imports only while the content is absent from the
  library (Put Back / rolled-back failure no longer block); Photos receipts for
  deleted content are skipped and released instead of crashing the batch; jobs are
  deleted before frame units; a file trashed mid-scan is not an error; personal
  table list includes people/face labels/deletions for full exports.
- [x] Failed snapshots/migrations remove their unpublished copies (previously one
  orphan full copy per failed minute); import policy files are removed on exit.
- [x] Photos downloads are cancellable and give up after 5 min without data;
  SearchMaintenance/ModelDownloads read worker output to EOF before reaping.
- [x] Photos passes use the persistent change history (full pass at launch, after
  failures, on start-date change and every 6 h) and run 10 s after a library
  change; per-pass redundant history writes for ~17.5k receipts removed. Measured
  before: a full pass kept Mami at ~39% CPU idle (sample, `PhotosImporting.swift:239`).
- [x] Workers read status/pending/unit state without the writer lock; the scan's
  metadata-version filter runs in SQL; the face worker mirrors the catalog only
  when its change token moves (was a full join per face job).
- [x] `verify.py --python-only` also runs MamiCore Swift tests on Linux; numpy-only
  imports no longer break 4 Linux tests. 25 Swift / 212 Python pass on Linux and
  on `ludi`; installation and media-deletion checks pass on the signed bundle.
- [ ] Measure click latency and idle CPU on the installed build (no remote UI
  automation: SSH lacks accessibility access, deliberately not granted).
- [ ] Not done, owner decision: Swift 6 language mode / `@Observable` migration,
  splitting `MamiApp.swift`, consolidating docs, retiring lab-era scripts, a
  scripted release, dedicated threads for blocking worker readers.

## Issues already assigned to the scaling work

- Full-catalog browsing/refresh and corpus-sized snapshot rebuilding are not a
  50× solution. Paging and durable incremental indexes remain explicit milestones.
- Import/index progress protocols and active-editor heuristics have different
  lifecycle semantics; do not force them into one generic worker abstraction.
- Programmatic-only AppKit `init(coder:)` failures and best-effort rollback/close
  cleanup are not automatically bugs. Review their contracts rather than replacing
  every `fatalError`, force unwrap or `try?` mechanically.

## Activity log

- Production-layout follow-up is local-only while the Mac is in use. Standard
  paths, verified no-clobber storage migration, physical-reference relocation,
  separate personal ownership, runtime bundling and on-demand lifecycle changes
  are implemented with 155 Python checks (150 pass / 5 Mac-only skips). Added
  missing-personal-store, interrupted-publication, competing-destination,
  checkpoint-preservation and coordinator-crash regressions. Native compilation,
  signing/relocation validation and real migration remain open in `MAC_APP.md`.
  The reported first-connect DJI issue was not caused by deployment: no changes
  had been installed. Added mount observation, canonical connection keys and
  visible busy deferral for the next native checks; cause remains unconfirmed.

- Final release `prototype-20260929T090709977496Z`, revision `0d43d1f`, is active
  through `~/Applications/Mami.app`. Its unchanged Swift executable matches the
  14-test native build; all 143 Python tests pass on the Mac. Final live audit and
  signed verification pass. All personal digests remain unchanged; 4,467 packs
  are complete with zero pending/error work. A real live query-plan check found
  a cleanup scan; indexed obsolete/pending queues now eliminate completed-library
  rescans, with migration/query-plan/retry tests and a real-catalog migration check.

- Final candidate `prototype-20260929T081741386944Z`: 14 Swift / 142 Python tests,
  complete native catalog/personal-store/UI checks, rapid-query/cache bounds,
  offline scrubbing, visual-failure speech recovery and bounded maintenance shutdown
  pass. Six real DJI backfills and actual import/preview/inference contention pass;
  originals stay unchanged. Crop-aware legacy fallback is independently signed and
  passes after raw caches have been retired. Live activation/audit is the final item.

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
