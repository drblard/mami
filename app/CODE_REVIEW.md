# Production code review — findings checklist

Reviews of production Swift/Python, packaging and tests: R1–R8 (2026-09-28/29),
R9 (live click crash, 2026-10-01), R10 (deep pass, 2026-10-02). The installed build
is recorded in [RELEASE.md](RELEASE.md); open work is in [REMAINING.md](REMAINING.md).

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
- [x] Installed (`2281c35`): idle CPU fell from ~39% to ~5% with change-history
  Photos passes (measured with `ps` after the launch pass).
- [x] Found by the user: clicking a card image took ~0.5 s, below it was instant. The
  image had both a double-tap and a tap gesture, so every click waited for the
  double-click interval (0.5 s on `ludi`). Replaced by one gesture using the click count.
- [ ] Measure click latency from real use: `InteractionLatency` logs release-to-render
  times without UI automation (SSH accessibility deliberately not granted).
- [x] Owner-approved follow-up: Swift 6 language mode (tools 6.0, no warnings),
  `@Observable` models, `MamiApp.swift` split by responsibility with a table of
  integration checks, consolidated docs, lab-era scripts and experiments removed,
  `release.py` with revision stamping and personal-data audit.
- [ ] Blocking worker readers (`availableData`, `readLine`, semaphores) still run on
  Swift's cooperative pool; move them to dedicated threads if pool starvation shows.
