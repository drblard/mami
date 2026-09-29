# Mami scaling work — live checklist

Last updated: 2026-09-29. Status: **Persistent-search integration and remaining acceptance checks in progress**.

## Current checkpoint (read this before the historical log)

**Mac work paused:** user reports his wife is actively working on the computer.
No agent build/benchmark processes remained in the process check; only the live
Mami app and its normal workers were present. Continue Linux-side work. Recheck
activity/permission before resuming Mac tests or deployment changes.

- Pushed milestones include `5dfd46c` (production/refactor), `e77d4f5` (experiments),
  `6e943d7` (search lifecycle) and `3919865` (packed previews / native 50× browsing).
  SSH signing briefly failed; GitHub push succeeded using existing `gh` HTTPS
  credentials without changing the remote. SSH to `ludi` is working again.
- Live app: `prototype-20260928T180349767667Z`, with verified fast native previews.
- Latest built search candidate: `prototype-20260929T060819943554Z`; 14 Swift /
  117 Python tests and native schema-v5 50× checks pass. Current source is v6;
  its Mac build and native integration are pending the activity pause.
- Active isolated run: `~/mami-lab/benchmarks/persistent-native-20260929/`.
  Projection + managed vectors built. Native Library ready in **1.399 s**, loading
  100 summaries; three combined queries **19–21 ms** (warm OS cache possible).
  Real process lifecycle checks passed: live insert visible **0.480 s**, replace,
  delete, reinsert, hot generation switch, restart and transcript updates. All 60
  queries during compaction succeeded, maximum **6.64 ms**. Personal DB unchanged.
- Remaining: native full-path startup/results, update/compaction integration,
  50× full-path and resource checks, packed SSD previews/offline tests, final
  review and deployment. Do not read historical "in progress" entries as the
  current deployment state.

### Latest local checkpoint before Mac pause

- Search coordination now serves SQLite text independently of visual initialization
  in a separate process; bounded visual restart recovery is covered by tests.
- 50× process test: text ready 46 ms, visuals 0.918 s; 256 varied queries median
  35.6 ms / p95 58.0 ms, maximum 185 ms. Combined measured private footprint rose
  from ~2.83 GiB to ~2.95 GiB during bounded cache warmup; no long-duration leak
  claim is inferred from this short run. Native app overhead was investigated next.
- Apple SQLite camera enumeration took 2.5 s despite Python's faster plan. Added
  transactionally maintained camera/day/shape/kind counts and projection v5→v6
  migration. Staged runtime and both test fixtures were upgraded successfully;
  50× migration took 11 s once. Native v6 validation is pending and must wait.
- Current local checks: 121 Python tests, 118 passed and 3 Mac/MLX skips.
  Latest v6/independent-readiness changes are not deployed to the live app.
- Failure-injection checks cover interrupted new-schema creation and v5→v6
  migration: rollback preserves the old version/data, and reopening retries
  successfully. Failed initialization explicitly closes the SQLite connection.
- Resume validation using a fresh signed candidate after the Mac is available.
  Recheck native facet counts, progressive readiness, process lifecycle and offline
  scrub decoding. Keep cold-cache, memory-budget and contention acceptance open.
  Prepared live-derived data is at `catalog/search-runtime-20260929/`; it has not
  been activated. Migration changes only rebuildable projection state. Older v5
  candidates cannot read v6: retain their matching fixture or rebuild a separate
  compatible projection for rollback; do not restore personal data for this change.

### 2026-09-29 — packed preview and UI validation checkpoint

- Added immutable JPEG atlas packing for completed AI jobs, crop-aware native
  rendering and bounded decoded-sheet caching. Cropped images are detached from
  full-sheet storage so individual cache entries cannot retain unaccounted atlases.
- Raw-frame retirement requires unchanged owned-cache signatures, verified sheet
  checksums, current source references and projection visibility. Originals, numeric
  embeddings, legacy experiment paths and modified files do not qualify.
- Tests cover offline packing, exact timestamps/vector preservation, portrait
  geometry, publication races, corrupt-sheet rejection and safe retirement.
  Linux now has an isolated NumPy/Pillow check environment at
  `/home/steevel/mami-lab/checks/.venv`; 108 tests run there with only 3 MLX skips.
- Candidate `prototype-20260929T044434702777Z` passed 14 Swift / 108 Python tests,
  signing and native catalog checks. The compiler flagged NSCache's missing
  Sendable annotation; a narrowly scoped, documented thread-safe wrapper fixes
  that warning in source, pending the next compile.
- New native-scale fixture build: `~/mami-lab/benchmarks/native-50x-20260929/`.
  It includes 5.83M vectors, 723,700 namespaced media summaries, 1.17M transcript
  segments and references to cached frames. It is synthetic capacity data, not
  evidence of relevance on unseen footage. Source originals remain untouched.
- The crop-aware expanded native UI check and the 50× native Library check are
  next. Packed previews and the new search path are still not activated live.
- The 50× native fixture completed: 5,830,750 vectors, 723,700 media summaries,
  1,174,650 transcript segments. The first expanded native run stopped in its
  color fixture: AppKit rejected named NSColor values for the bitmap. Replaced
  these with explicit device-RGB colors; a direct AppKit check confirms the fix.
  No startup performance claim is taken from that failed run.
- Crops now carry original preview dimensions to preserve shape classification.
  Visual/speech replies and on-demand frame lookup retain crop metadata; stale
  raw cache references reconnect through the projection after retirement.
- Transcript scope filtering now uses date/camera FTS postings and SQL predicates,
  avoiding construction of a whole-library path set on every filtered keystroke.
- Derived schemas are projection v5 / packed generation v4. They remain candidate
  formats; the live app still uses the earlier validated fast-preview build.
- Explicit RGB native crop checks now pass (colors, dimensions, stale-reference
  recovery and invalid bounds). The 50× Library run then reported a SQLite open
  error. Query-plan inspection exposed a full-catalog temporary sort in descending
  paging. Removed the redundant expression and added an indexed oldest-first order;
  fixture timings: first 100 summaries **0.33 ms**, count **8.9 ms**, camera facets
  **89.9 ms**. Added query-plan regression coverage and contextual SQLite errors.
  The full native run must be repeated; this is not yet a passing startup result.
- Contextual errors identified a separate Apple-SQLite behavior: `PRAGMA
  user_version` on a read-only, clean WAL file fails with CANTOPEN/ENOENT when
  WAL/SHM sidecars are absent. A temporary read-write connection can initialize
  those files without changing rows. Added that specific recovery path (no creation
  of missing main databases), persistent sidecar policy and a native clean-WAL
  regression test. The paging-index fix remains independently necessary.
- Candidate `prototype-20260929T052429315066Z` passes **14 Swift / 112 Python
  tests**, signing, native crop/projection checks and the complete native 50×
  Library/UI run. Readiness **4.377 s**, first page **100 items**; three combined
  searches **132–178 ms**. Screenshot reviewed: correct paged grid/counts and
  controls. Warm OS caches are possible; this is not a reboot measurement.
- Startup is within the five-second target in that run. Combined-query latency
  still needs tightening toward the 100 ms backend/100–200 ms typing target.
- Active offline atlas check: `benchmarks/atlas-offline-20260929/`, using isolated
  copies of real cached frames with an intentionally unavailable original URL.
- Offline atlas check passed: **82 real cached frames → 3 sheets**, **4,635,693
  bytes → 644,659 bytes**, packed in **0.297 s**. All 82 owned fixture raw frames
  were retired only after projection/checksum validation; embedding references
  stayed unchanged. The original URL is deliberately unmounted; the live cache
  and originals were untouched. Native rendering/search on this fixture is next.
- Native offline fixture now passes: ready **0.983 s**, three searches **15.6–17.5
  ms**, packed-frame crop/reconnection checks and screenshot. Its only original
  URL remains unavailable. Added explicit start/middle/end cached-frame decode
  observations to the next native check for stronger scrub coverage.
- Profiling the 50× path identified avoidable SQLite page churn alongside storage
  cache effects. Added explicit bounded query caches (projection 64 MiB, row map
  32 MiB). A same-process diagnostic measured visual scoring/reranking ~19–21 ms,
  encoder ~13–16 ms and transcript lookup ~0.1–2.7 ms after warming; full native
  validation must still be repeated and reported separately from this diagnostic.

### 2026-09-29 — native startup and lifecycle milestone

- Results: `persistent-native-20260929/native-check/result.json` and the fixture's
  `lifecycle-*/result.json`. Source catalog, projection, vectors and injected fixture
  assets are isolated from the live library; no original media changed.
- Added reproducible `check_persistent_lifecycle.py`, including search while a new
  generation builds, active-reader generation switching and process restart.
- Native startup check now also exercises next-page loading, scoped filtering and
  a SwiftUI grid screenshot. Automatic Photos/camera imports are centrally disabled
  for integration-check launch modes. This expanded UI check still needs its next run.

**Active work:** [production code review](CODE_REVIEW.md) and 50× search validation.
The [import → previews → AI pipeline](PIPELINE.md) and review fixes are live in
`prototype-20260928T173844873061Z`, after 14 Swift / 79 Python checks, native
catalog/UI integration and a real-codec fixture. Large-DJI timing remains open.

Latest live build: **`prototype-20260928T180349767667Z`**. Native batched decoding
reduced the six real DJI clip test from 121.45 s to 8.57 s; thumbnails ~2 s and
full-range scrub coverage 3.32–4.36 s after catalog publication. Details/caveats in
`PIPELINE.md`. All 14 Swift/79 Python tests, JPEG/timestamp/overwrite bounds,
real FFmpeg fallback and signature verification passed before activation.
Run: `~/mami-lab/benchmarks/scaling-20260928T124400Z/`. See the latest activity entry.
**Migration is gated on visual-search quality + latency + memory together.**

## Goal and scope

Design for **50× the current indexed library** (~5.8 million frame vectors at
768 dimensions, ~17 GiB uncompressed), with cold startup under 5 seconds,
preferably substantially faster, and near-instant search while indexing and
editing. Browsing, search and storyboard scrubbing must work with originals
offline. Search indexes and compact previews live on the Mac SSD.

The existing UI, SQLite catalog, import verification, annotations and playback
remain the foundation. This is an incremental search/storage refactor.

## Resume here after an interruption

1. Read this file, `app/README.md`, and `git status --short`.
2. Connect with `ssh ludi` (the previous alias was `mami-mac`).
3. Check running processes before launching workers or benchmarks; do not duplicate
   a long-running job. Read the retained run's status/log files.
4. Continue the first unchecked item in the active phase. Record commands,
   artifacts, outcomes and decisions below as work advances.
5. Never edit a signed deployed bundle. Build fresh deployments and preserve old
   builds, media, catalog and experimental artifacts.

### Starting state

- Current live build: `~/mami-lab/apps/prototype-20260928T121844652245Z/Mami.app`.
- Catalog: `~/mami-lab/catalog/database/catalog.sqlite`.
- Originals: `~/Media/Originals` (original files are read-only to this work).
- Existing generated frames/vectors: `~/mami-lab/index-artifacts`.
- Python: `~/mami-lab/.venv/bin/python`.
- Source working tree already has uncommitted search/memory fixes from this
  conversation. These are intentional; preserve them.
- Current search is ~49 ms median / 75 ms p95 on ~116k frames, but ~21 s cold.
- Measured startup: imports 2.51 s, model 1.54 s, incremental corpus 15.60 s
  (109k vector opens alone 13.24 s), text encoder warmup 0.12 s.
- Background MLX free-buffer cache is capped at 256 MiB; the prior 22 GiB worker
  was restarted cleanly. Existing indexing checkpoints remain valid.

## Acceptance criteria

- [ ] Cold app startup / usable search <5 s on the target Mac.
- [ ] First page and text search goal <1 s.
- [ ] Text query p95 <20 ms; visual query p95 <100 ms at 50×.
- [ ] Typing-to-results roughly 100–200 ms including debounce and rendering.
- [ ] Visual ranking quality measured against exact search, including filters
      and distinct-video results (not just nearest-frame recall).
- [ ] Search memory bounded; initial budget ~2 GiB, measured separately from
      background inference and native UI.
- [ ] New/uncompacted arrivals, deletions, recovery and maintenance remain fast.
- [ ] Browsing/search/storyboard scrubbing function with originals disconnected.
- [ ] Real concurrent imports/transcription/CapCut checks pass.

## Phase 1 — architecture and performance prototype

- [x] Record the plan, current deployment and recovery instructions.
- [x] Inspect existing data layouts and available Mac resources/dependencies.
- [x] Create isolated, reproducible benchmark tooling and persistent run state.
- [x] Evaluate embedded LanceDB persistence, indexed/filtered queries, updates
      and restart behavior on real existing embeddings.
- [ ] Benchmark representative 50× data; document synthetic-data limitations.
  - [x] Build 5,830,750 unique perturbed frame vectors with bounded batches.
  - [x] Build/test 1,174,650 transcript rows.
  - [ ] Meet latency/quality/memory gates together.
- [ ] Measure search quality against exact search on real data.
- [x] Evaluate text-only encoder startup (Core ML if practical) and latency.
- [x] Measure compact thumbnail/storyboard budget on representative footage.
- [ ] Select engine/formats using results; record any failed targets honestly.

## Phase 2 — persistent search layer

- [x] Separate authoritative personal data into `user.sqlite`; back up only that store.
  - [x] Implement verified atomic migration, independent revisions, Python-worker routing,
        managed backup retention and protection against stale legacy writes.
  - [x] Pass native migration/restore/retention and UI integration checks.
  - [x] Deploy and verify live personal-data parity and backup size.
- [ ] SQLite FTS5 transcripts/text and indexed metadata queries.
  - [x] Implement/test derived SQLite projection, keyset paging and scoped FTS5.
  - [x] Transactional event/cursor replay, delete/replacement and restore-branch detection.
  - [x] Real-catalog isolated build and query timings.
  - [ ] Legacy index/transcript seeding and native app integration.
- [ ] Persistent vector index built from existing embeddings.
- [ ] Durable, idempotent update queue with delete/replacement handling.
- [ ] Resumable backfill, incremental updates and automatic index maintenance.
- [ ] Restart/recovery and schema/version migration tests.

## Phase 3 — app startup and query integration

- [ ] Paged catalog loading; remove corpus-wide work from startup.
- [ ] Immediate text search; independently warmed visual query encoder.
- [ ] Concurrent queries, cancellation/latest-query semantics and result fusion.
- [ ] Correct metadata filtering and distinct-video ranking.
- [ ] Bounded caches and native end-to-end latency checks.

## Phase 4 — SSD previews

- [ ] Define versioned content-addressed thumbnails and packed storyboards.
- [ ] Generate during import/indexing, sharing decode work where possible.
- [ ] Backfill/reuse existing frames with durable completeness tracking.
- [ ] Use SSD previews for grid/scrubbing; optional evictable playback proxies.
- [ ] Offline-originals acceptance checks and measured storage budget.

## Phase 5 — migration and release

- [ ] Compare replacement against existing live-library results.
- [ ] Validate concurrent editing/indexing, maintenance, interrupted work,
      filters, missing drives, new arrivals and deletions.
- [ ] Package/sign/test a new immutable build.
- [ ] Switch live app only after required checks pass; preserve rollback build.
- [ ] Record final deployment, remaining limitations and maintenance behavior.
- [ ] Retire redundant artifacts only after explicit retention review.

## Decisions and activity log

### 2026-09-28 — start

- User approved staged implementation and requested continuously updated
  checkbox documentation that survives machine reboots.
- Candidate: SQLite/FTS5 + embedded LanceDB IVF-PQ + text-only SigLIP encoder,
  with SSD-resident preview artifacts. Engine/codec choice is provisional until
  measured. Do not call a warm OS-cache benchmark a cold-reboot measurement.
- Current task: inspect resources and design the isolated benchmark harness.

### 2026-09-28 — environment and corpus preparation

- Mac: M1 Max, 10 CPU cores, 64 GiB RAM; ~117 GiB SSD space available. Live
  index artifacts occupy 9.7 GiB. Plan one 50× dataset, retaining at least 50 GiB
  free rather than producing multiple full-size benchmark copies.
- Created separate `~/mami-lab/.venv-scaling` inheriting existing inference
  libraries. Installed LanceDB 0.39.0, PyArrow 25.0.1, coremltools 9.0 there.
  Production `~/mami-lab/.venv` is unchanged.
- Added `app/scaling/common.py` atomic progress/checkpoint/run-lock helpers and
  `corpus.py` consistent SQLite snapshot + resumable vector consolidation.
  Source manifest, row metadata and transcripts are retained with the run.
- Current task: deploy prototype tooling; prepare the isolated real corpus and
  query vectors, then evaluate indexes and text-only encoder independently.

### 2026-09-28 — first retained run

- Run: `~/mami-lab/benchmarks/scaling-20260928T124400Z/`.
- Sources: `source/`; logs: `corpus.log`, `vectors-1x.log`, `encoder.log`.
  Each tool owns a separate `*.lock` and atomic `*-progress.json`.
- Corpus complete: 116,615 vectors, 14,474 distinct files, 23,493 speech segments;
  358,241,280 raw vector bytes. SQLite source snapshot is retained as `source.sqlite`.
- The environment needed an explicit `mami-inference.pth` pointing to the
  production environment's site-packages (a nested venv does not inherit those
  automatically). This reads existing packages without modifying them.
- Extracted SigLIP's text-only tower (282.3M vs 375.2M total parameters). It
  produces **identical** vectors for 24 real-language queries; CPU encoding was
  ~30–46 ms under concurrent work. A Core ML export is the next startup experiment.
- First IVF-PQ index built successfully. Reporting initially failed because
  LanceDB 0.39 returns an `IndexStatistics` dataclass; serialization is corrected.
  Rerunning reuses the committed table/index instead of rebuilding.

### 2026-09-28 — scale and early failures (do not release yet)

- The 50× IVF-PQ table/index finished in 211 s: **5,830,750 vectors / 723,700
  synthetic files**, 18.62 GB on disk, 3.66 GiB peak builder RSS. A tool-side SSH
  timeout occurred at 120 s; the `nohup` job correctly survived and completed.
  Subsequent long waits use no tool timeout. Full result: `vectors-50x-build.json`.
- Naive BM25-ranked FTS5 at 1.17M segments failed the latency target (p95 ~650 ms
  on broad prefixes). A separately retained fast path uses 1–4 character prefix
  indexes, FTS-intersected camera/day scopes, and newest-inserted matches first:
  **p95 0.8 ms unfiltered / 1.5 ms camera / 1.3 ms exact day**. This changes ranking;
  it is not evidence that full-corpus BM25 can be instantaneous. Arbitrary date
  ranges and ranking policy still need acceptance work.
- Initial vector defaults were fast but missed too many exact top-60 files.
  Increasing exact refinement improved real-corpus average file recall to 98.5%
  (worst query 83.3%) at ~68 ms p95 after warmup. Do not approve engine settings
  based on latency alone. 50× query measurements and further quality tuning next.
- Initial prototype camera categories used a `details` fallback that incorrectly
  labels some unknown-device photos with resolution strings. Dominant tested
  category is genuinely iPhone 16 Pro Max. Correct canonical metadata projection
  before production; this prototype is not a migration source for camera labels.
- Core ML export encountered two tooling issues, both corrected in the retained
  scripts: tokenizer length must be explicitly 64; generic Transformers masking
  emits unsupported `aten::new_ones`, so the verified export wrapper uses a fixed
  bidirectional padding mask. Export parity cosine >=0.9999948 over 24 queries.
  Python-package load was 2.90 s; native precompiled-model startup is being measured.
  Neural Engine compilation failed and fell back to CPU; do not claim ANE speed.
- SSD storyboard sample: 48 clips across frame-count quantiles, ~0.55 video hours,
  using only existing cached frames. 256px cells, every 2 s, JPEG quality 65:
  **13.15 MiB/video-hour**, vs WebP **10.16 MiB/hour**; mean grid thumbnail 8.76 KB.
  Extrapolation: ~20.4 GiB scrub JPEGs for 5.83M one-second source frames, plus
  ~5.9 GiB thumbnails for 724k files. Composition/clip lengths change this estimate.
  JPEG is the initial native-friendly candidate; visual inspection remains.
- Current task: finish 50× latency and fresh-native-process encoder checks;
  test vector update/recovery behavior and quantify remaining quality tradeoffs.

### 2026-09-28 — corrected query plans and native encoder

- **Precompiled Core ML CPU-only is the preferred encoder:** fresh native process
  loads in 363 ms, first inference 23 ms, subsequent p50 9 ms / p95 11 ms.
  GPU had a 2 s first-inference penalty; CPU+Neural Engine fell back and was slower.
  Tokenizer Rust runtime loads in 521 ms, encodes in ~0.024 ms, and matches all
  24 saved queries plus seven Unicode/whitespace/long-query edge cases exactly.
  These are process-start, warm-OS-cache measurements, not a reboot test.
- Lifecycle fixture passed: replayed upsert doesn't duplicate, unindexed arrivals
  are immediately searchable, replacements/deletes work, independent readers can
  refresh, maintenance preserves data, and a reopened table sees committed state.
- Full-scale initial PQ measurements failed: ~0.43 s unfiltered and ~1.9 s camera
  filtered. Query-plan analysis found `maximum_nprobes=all` searched all partitions
  for our off-image-manifold text queries. Fixed probing and scoped exact search
  for small filtered sets avoid that behavior. Camera/day bitmap indexes also
  replace the expensive high-hit-count B-tree prefilter.
- **IVF-SQ is now the leading vector index**, rather than the originally proposed
  aggressive PQ compression. On real data, 128/256 fixed probes + 1024 candidates
  gave 99.0% mean top-60-file recall, 90% worst unfiltered / 95% worst camera,
  at ~11 ms p95. Narrow-day exact search had 100% recall / 3.6 ms p95.
- At 5.83M vectors with SQ and 128/1024 probes: **79 ms p95 unfiltered, 86 ms
  camera-filtered, 32 ms narrow-day**, peak benchmark RSS 1.28 GiB (includes
  benchmark source arrays). Index cache is 512 MiB; metadata cache 128 MiB.
  Full-scale exact-quality checks on difficult queries are running before choosing
  final settings. All previous slower/less-accurate results remain retained.
- Prototype metadata extraction is corrected in source to match native camera
  rules and filter by pipeline version. Existing benchmark rows remain unchanged
  for comparability; migration must use the corrected projection.
- Four recovery/metadata/FTS-expression regression tests pass on Linux.
- Current task: full-scale exact recall, cold process entrypoint, and update-tail
  latency checks; then record the architecture decision and begin persistent-layer work.

### 2026-09-28 — quality gate is still open

- Added [detailed live results](scaling/RESULTS.md), also mirrored into the Mac run
  along with this checklist. Dependency versions are recorded in `environment.txt`.
- Fresh 50× search-side process: text available in 2.5 ms inside Python; vector
  store + tokenizer in 2.16 s; first vector query 257 ms. Native CPU encoder loads
  separately in ~363 ms and can warm concurrently. Full app/reboot tests remain.
- Full-scale exact search found difficult-query recall of **70–72%** with the fast
  IVF-SQ settings. More probes improved quality but exceeded latency targets.
  This is a failed combined quality/latency gate, not an approved replacement.
- Testing USearch 2.26.2 memory-mapped HNSW as an alternative. Real-corpus int8
  graph opens in 3 ms; 8,192 candidates + exact rerank gave **99.6% average / 96.7%
  worst file recall, 24 ms p95**. Full-scale graph build is running with a durable
  checkpoint every five replicas; command and progress are `graph_bench.py --run
  RUN --scale 50 --build`, `graph-50x-progress.json`, and `graph-50x.log`.
  A float16 variant is also being measured on the real corpus before spending
  full-scale build resources on it. Filtering, reranking I/O, and working-set
  residency are required before selecting a graph backend.
- Graph experiments preserve at least 35 GiB SSD headroom to allow atomic index
  checkpoint replacement; production cache budgets have not been changed.
- Recovery/metadata/FTS-expression tests passed on both Linux and the Mac.
- A storyboard sample was visually inspected; portrait subjects remain clear at
  scrub size. No originals were read to create the sample.
- Current task: await full-scale graph build, measure graph accuracy/filters/RSS,
  and select an engine only after its combined performance/quality gate passes.

### 2026-09-28 — graph and browsing checkpoint

- Int8 HNSW 50× build completed: 5,830,750 nodes, 5.34 GB file, 615 s build,
  6.78 GiB peak builder RSS. Search opens the memory map in 144 ms. With 8,192
  candidates and full-precision reranking, queries were **175 ms median / 217 ms
  p95**, peak benchmark RSS 3.45 GiB. This misses the initial latency/RSS targets.
- Real-corpus float16 graph, 4,096 candidates / expansion 8,192: **99.44% average,
  98.33% worst file recall**, 26.5 ms p95. A higher-precision graph may avoid the
  costly full-precision reranking step. Full-scale build now runs as:

  ```sh
  ~/mami-lab/.venv-scaling/bin/python -B RUN/source/graph_bench.py \
    --run RUN --scale 50 --dtype f16 --build
  ```

  Log: `graph-50x-f16.log`; progress: `graph-50x-f16-progress.json`;
  checkpoint: `graph-50x-f16.usearch`. Checkpoint every five replicas, atomic
  replacement. Do not launch another writer if the run lock is held.

- **Next after completion:** run `graph_bench.py --run RUN --scale 50 --dtype f16
  --candidates 4096 --expansion 8192` (without `--rerank`); then exact-quality and
  filtered-search checks with `graph_filter_bench.py --run RUN --scale 50 --dtype
  f16 --candidates 4096 --expansion 8192`. Measure physical footprint as well as RSS: read-only
  mapped pages are reclaimable, unlike the prior GPU cache leak. Do not silently
  relax the documented memory target.
- A 723,700-row summary catalog (796 MB) returns/decode pages of 100 media in
  **0.58 ms first page / 0.44 ms p95**, with no repeated rows over 50 keyset pages.
  This validates paged scalar metadata instead of loading giant frame arrays.
- Temporal representative experiments are retained in `scenes.json`. Aggressive
  thinning loses file-candidate recall; it is not adopted as a silent shortcut.
  Keeping ~48% of frames with 480 file candidates retained 98.3% worst recall
  before ANN errors; stronger thinning was worse.
- Current app and production environment remain on the earlier search/memory fix.
  No source media, production catalog schema, or live search engine was changed
  by the scaling experiments. Phases 2–5 are intentionally still unchecked.

### 2026-09-28 — SSD guard stopped the long build safely

- Float16 HNSW build stopped at the 35 GiB SSD reserve. Last durable checkpoint:
  **40/50 replicas**, 7,857,586,624 bytes. Later in-memory work is disposable;
  rerunning the same command resumes from this checkpoint.
- Mac now has ~35 GiB free; this run occupies ~39 GiB. Swap remains ~1.1 GiB,
  and ~31 GiB RAM is free after the builder exited. The disk-space decrease
  exceeds this run's size; its remaining cause was not established by our samples.
- Linux development computer has ~874 GiB disk space available. Archive only
  our failed-candidate benchmark data there with checksum verification before
  removing its Mac copy. Do not remove originals, production caches, or user files.
- Planned archive location:
  `/home/steevel/mami-lab/benchmarks/scaling-20260928T124400Z/`.
- [x] Archive and verify the completed int8 50× graph (5.34 GB).
- [x] Cache exact-quality baselines and narrow-scope vectors required by the next test.
- [x] Archive and verify the 50× LanceDB experiment (about 22 GiB).
- [x] Resume float16 graph build with the original space reserve intact.
- [x] Run the documented full-scale float16 quality/filter/memory checks (quality failed).

Archive preparation: source SHA-256 manifests cover the 5,343,920,604-byte int8
graph and all 179 files / 23,125,885,449 bytes of the LanceDB experiment. Resumable
rsync transfers are running to the Linux archive. Mac copies will be removed only
after destination hashes/file sets match and the source is verified again.
`reference-cache.json` records 24 exact quality baselines and the small exact-day
vector subset, so subsequent graph checks do not require the large Lance table
to remain on the Mac. Six helper/recovery/archive regression tests pass.
The graph writer now checks temporary checkpoint size as well as the 35 GiB
reserve before writing an atomic replacement. Wait for both archive verifications
and removals before resuming, rather than using the first few recovered GiB.

Archive continuation (after each rsync finishes; do not verify a partial copy):

1. On Linux run `python app/scaling/archive.py verify --root LOCAL_RUN --manifest
   LOCAL_RUN/archive-graph-50x.json --receipt LOCAL_RUN/verified-graph-50x.json`;
   repeat using `archive-lance-50x.json` / `verified-lance-50x.json`.
2. Copy each `verified-*.json` receipt back to the Mac run.
3. On the Mac run `source/archive.py remove --root RUN --manifest
   RUN/archive-graph-50x.json --receipt RUN/verified-graph-50x.json` with the
   scaling Python; repeat for Lance. This rechecks the source before removal.
4. Update the checkboxes and mirror this document, check free space, then resume
   the same float16 build command. Its binary checkpoint remains on the Mac.

### 2026-09-28 — backup growth diagnosed and approved cleanup completed

- Found the continuing growth: `~/mami-lab/backups/catalog` held **~1.1 TB** of
  whole-catalog snapshots, triggered by generated indexing changes. This was an
  application design error, not just benchmark disk usage.
- User explicitly approved pruning redundant historical snapshots while keeping
  recent restore points and distinct manual-data states. Then clarified that
  regenerable data should not be backed up and asked to separate the databases.
- `prune_catalog_backups.py` inventoried **9,238 completed snapshots**, retained
  60 (newest/baseline, hourly/daily/monthly and every distinct manual-data state),
  integrity-checked every retained file, then removed **9,178 redundant snapshots**.
  No unknown/changed candidates were removed. Audit: `backup-prune-review/` in RUN.
  Removed logical bytes: 1,146,230,796,288. Mac now has **~1.1 TiB free**; catalog
  backups fell to **9.4 GiB**. Three cleanup regression tests passed before apply.
- The larger Lance archive transfer finished and passed Linux SHA-256 verification
  over all 179 files. Its reviewed Mac retirement is finishing separately; this
  is no longer needed to resolve the disk emergency.
- `user.sqlite` implementation is now in source:
  - Annotations/history, clip selection, library roots, Photos import ledger and
    legacy-import markers move under the shared writer lock, with row-for-row
    verification and atomic publication. Generated metadata/index tables are removed.
  - Cache and personal revisions are independent. Missing personal data errors
    rather than silently recovering stale legacy copies; a missing cache can be
    recreated using the personal store's identity.
  - Python scanner/Photos workers read/write the personal store after migration.
  - Automatic backups target only `backups/catalog/user-state`; metadata refresh
    no longer schedules them. At most one runs at once; import-driven backups are
    coalesced to one/minute, manual changes debounce for two seconds, Quit flushes.
  - Managed personal-backup retention: newest 64 plus 24 hourly, 30 daily and
    12 monthly windows per identity; annotation edit history remains in each copy.
  - Explicit full diagnostic exports still embed current personal tables and can
    seed a fresh store. Old builds cannot write to migrated legacy personal tables.
- Linux tests: 64 passed/skipped total (61 pass, 3 NumPy tests explicitly skipped).
  First Mac compile found a missing bound-parameter overload on the SQLite scalar
  helper; fixed. Current candidate `prototype-20260928T145201455233Z` is compiling.
  Do not activate until native migration/restore/retention checks pass.

The int8 graph transfer completed; Linux SHA-256 verification and a second Mac
verification passed. Its Mac copy was removed, with receipt
`archived-graph-50x.usearch.json`. The Mac nevertheless reported only ~28 GiB free
after removal (down from ~35 GiB before the transfer), indicating continuing disk
growth outside this retired artifact. The float16 build remains stopped while
the larger archive transfer runs; investigate disk growth before resuming.

### Continuous work checkpoint — search projection and 50× engine loop

- User requested continuing through all remaining items until implemented/tested.
  Do not mark failed candidates done or require prompts to continue normal work.
- New `search_store.py` + `build_search_store.py` isolate rebuildable search state
  from production writers; source events and mutations commit together. Consumer
  data/cursor commits are atomic, with positive restore-branch detection and
  acknowledged-log compaction. Tests cover seed races, deletion/replay, rollback,
  restart, filters and changed catalog identity. Native integration still pending.
- Isolated real snapshot projected in 4.82 s once; 100-row page 0.57 ms; scoped
  transcript prefix query 0.82 ms. Run `search-store-20260928T182500Z`.
- New 50× RabitQ full scan failed latency (~569 ms p95). PCA-128 GPU candidate
  scan itself is fast after warmup (~15–22 ms), but candidate selection/rerank I/O
  and quality miss the targets. Full-dimension GPU scan is being measured as a
  reference bound before selecting another approximation. It may exceed the
  provisional 2 GiB memory budget; do not silently relax that target.
- Experimental run: `scaling-rq-20260928T181500Z`; all scripts and measurements
  are retained. No new search-engine candidate has been activated.

### GPU quantized scan milestone (not yet approved for release)

- MLX's supported 4-bit groupwise quantized matrix scan + 4,096-candidate exact
  FP32 rerank on 5,830,750 vectors: **64.6 ms median / 87.2 ms p95 / 98.2 ms max**
  across 24 query timings; **100% exact top-60-file recall on all eight difficult
  reference queries**. Private GPU allocations: **2,669 MiB**, cache ~22 MiB.
  Quantized file: 2,798,760,271 bytes. More reference queries/filters still needed.
- This exceeds the initial provisional ~2 GiB budget. It is reported explicitly,
  not counted as a memory-gate pass. A full FP16 GPU scan was ~68 ms warm but
  required ~9 GB and had a 2.9 s first query; rejected as the default approach.
- Two-bit compression lost real-corpus recall (88.3% worst). Three-bit + half
  scales recovered the eight full-scale references but took ~735 ms on a sampled
  query; its less efficient kernel failed latency. Four-bit remains the leading
  measured option. Production integration must enforce explicit cache/allocation
  limits and account for the native text encoder, not only vector weights.
- Native text-encoder service and bounded Python client are now in source behind
  explicit `--native-encoder`; no default/live engine switch. Artifact preparation
  requires recorded export/tokenizer parity and writes checksummed immutable files.
- Persistent SQLite projection now includes legacy transcript/vector seed adapters,
  with matrix-row references instead of reopening baseline per-frame files.
  Projection schema 2 is experimental and has not been installed in production.
  Current local suite: 90 tests, 87 passing and 3 explicit NumPy skips.

### Persistent base generation and complete query references

- Exact references now cover **24 queries × 3 scopes** (unfiltered, dominant
  camera, narrow day) on the 5.83M-vector fixture. Four-bit MLX retained all exact
  top-60 files at 4,096 rerank candidates, but uncached single-threaded FP32 row
  reads caused ~500 ms tails. Stage timings identify disk reranking, not GPU math.
- Added exact top-k sampled-cutoff selection, with a guaranteed global fallback;
  adversarial/tie/sparse-filter tests passed on the Mac. It reduced selection from
  ~46 ms to ~5 ms without changing the selected scores. Bounded parallel candidate
  reads improve storage tails. With 1,024 candidates: unfiltered/camera p95 ~22–23
  ms and 100% reference recall; narrow-day worst recall 83.3%, so that scope keeps
  a larger/exact path. Do not silently use the smaller candidate budget everywhere.
- Native encoder FP16 CPU path narrowly failed the strict cosine gate for one
  query (0.9998688). FP32 export fixes it (minimum 0.99999988); actual persistent
  native client readiness 2.396 s, maximum of 24 query round trips 40.1 ms.
  The FP16 artifact is not approved as the reference-matching production default.
- `packed_vectors.py` implements immutable, manifest-last base generations:
  normalized row metadata, memory-mapped full vectors, quantized weights, bounded
  candidate I/O. Three real-MLX tests passed on the Mac, including exact file ranks,
  scoped filtering, absent source artifacts and rejection of partial generations.
- `packed_search_worker.py` is an **opt-in** integration path combining native text
  encoding, the persistent SQLite projection and packed base vectors. Incremental
  overlays/compaction and native paged browsing remain open before default rollout.
- Candidate `prototype-20260928T183633494048Z` passed 14 Swift / 90 Python tests
  and signing. It is **not activated**. A previous test failure was a macOS /var vs
  /private/var canonical-path assertion, corrected without weakening behavior.
- Current real-corpus generation build: `benchmarks/packed-production-20260928T185000Z/`.
  Source catalog is a retained isolated snapshot; legacy vector/transcript seeding
  is included. Existing live app remains `prototype-20260928T180349767667Z`.

### Incremental vector lifecycle implementation checkpoint

- Packed generations now have reader leases and managed ownership IDs. Publication
  switches one fsynced pointer only after all files and manifest are complete;
  cleanup retains three managed generations and never deletes an active reader's
  generation. A regression test caught/fixed a shadowed lock-handle variable that
  previously prevented pruning from occurring.
- `VectorOverlay` atomically publishes bounded exact deltas over the base: inserts,
  replacements and deletions supersede base assets; failed reads preserve the last
  complete snapshot. Delta refresh runs outside the query path. Full base rebuilds
  are scheduled by `vector_sync.py`; the worker warms a replacement before swapping.
  Initial compaction scheduling is provisional and requires workload validation.
- Opt-in native configuration now accepts `MAMI_NATIVE_ENCODER`,
  `MAMI_PACKED_INDEX`, and `MAMI_SEARCH_PROJECTION`; default/live configuration
  remains unchanged. This permits isolated native integration before migration.
- Candidate `prototype-20260928T190528364258Z` is running bounded checks. The local
  suite is 102 tests (Mac-only MLX/NumPy tests skipped on Linux); targeted projection
  and generation-publication checks pass. No latest search build is activated.

### Paged native browsing implementation / current validation

- Native projection reader now pages 100 summaries, supports full-catalog camera,
  shape, kind, date, manual-label/favorite scopes and stable arrival cutoffs for
  locked grids. Frame lists load on hover; off-page search hits resolve through
  the projection. UI refresh preserves the loaded page window instead of resetting
  a scrolled library to its first page on every preview update.
- Projection schema 4 includes cached shape and stable arrival sequence; packed
  generation format 3 carries matching scalar filters. These schemas are still
  isolated candidates, not silently migrated into the active library.
- Search maintenance is app-owned via stdin EOF lifetime; only marked managed
  roots may compact. Active CapCut use defers compaction; unfinished owned scratch
  work is disposable. Source and derived change logs have separate acknowledged
  compaction floors, preserving all retained/active generation cursors.
- Added native projection checks: exact keyset coverage, no duplicates, scopes
  before page limits, grid-lock arrivals and offline/off-page lookups.
- Candidate `prototype-20260928T192800897315Z` passed Swift checks but Python
  caught a callback/state-row name collision in the vector builder. Renamed the
  source checkpoint variable; the fix is included in the next candidate.
- Production Python static checks now use Ruff 0.16.9 (`F` correctness rules),
  passing locally. Obsolete imports were removed; no broad style-only rewrite.
- Current build/check candidate: `prototype-20260928T195607155287Z`. Live app still
  `prototype-20260928T180349767667Z`. Full new-path native/50× and lifecycle checks,
  packed previews and final cold-start/concurrency validation remain open.
- Candidate validation completed: **14 Swift / 103 Python tests pass**, signed
  packaging succeeds, and native projection/catalog/user-store integration passes.
  The projection checks cover exact keyset paging, filters before limits, arrival
  cutoffs, offline frame references and off-page search result resolution.
- User explicitly requested commit/push checkpoints while continuing through the
  remaining work. Source changes are being checkpointed now; this is not a claim
  that the opt-in persistent search path has been rolled out or all targets met.

### 2026-09-28 — live split verified; review and preview priority now active

- Live build: `prototype-20260928T145201455233Z`. Native catalog/user-store/UI
  checks and signing passed before activation. Migration audited every old personal
  row: 16,889 Photos verification records, saved selection, library root and empty
  annotation/history/import-marker tables were preserved. `user.sqlite` and its
  automatic backup are **5,120,000 bytes**, versus **278,253,568 bytes** for the
  generated catalog. User-store integrity passed; no generated tables were present.
  Audit files: `personal-migration-{before,after}.json` beside the deployed bundle.
- Both reviewed large artifacts were SHA-256-verified on Linux, reverified on the
  Mac, and retired there. The original backup-growth problem was separately fixed
  by the approved cleanup and the personal-only backup design.
- Full float16 graph completed (9.82 GB). It opens in ~160 ms and has ~61 ms p95
  raw-query latency, but failed full-scale exact-file recall badly on the difficult
  synthetic queries. Greater exploration also missed the combined latency/quality
  gate. It is **not approved for production**. Private physical footprint ~239 MB;
  RSS includes several GiB of reclaimable mapped pages. Both metrics are retained.
- RabitQ 4-bit full-scan candidate on the real corpus: 99.86% mean / 96.67% worst
  file recall, ~28 ms p95 including initial cache misses. No 50× claim yet.
- User requested a best-practices pass and separate readiness queues after a real
  four-video offload. Follow `CODE_REVIEW.md` and `PIPELINE.md`; broader vector
  migration remains gated. Keep development tests isolated while she edits.
