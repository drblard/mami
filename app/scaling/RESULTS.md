# Scaling prototype results (live)

Updated 2026-09-28. **Experimental; no scaling migration has been deployed.**
Checklist/recovery instructions: [SCALING.md](../SCALING.md).

Run: `ludi:~/mami-lab/benchmarks/scaling-20260928T124400Z/`.
The production app uses `prototype-20260928T180349767667Z` (fast DJI preview pipeline).
The vector-engine migration is not yet deployed.

**Current checkpoint:** verified archive transfers and the float16 build completed.
Its full-scale quality gate failed; results remain retained. The backup growth was
fixed and the Mac has ~1.1 TiB free. New RabitQ/projection experiments live in
`~/mami-lab/benchmarks/scaling-rq-20260928T181500Z/`.

## Dataset and measurement boundaries

| Dataset | Frames | Files | Speech segments |
|---|---:|---:|---:|
| Consistent real snapshot | 116,615 | 14,474 | 23,493 |
| 50× capacity fixture | 5,830,750 | 723,700 | 1,174,650 |

Replica zero retains real vectors; other replicas add seeded noise and renormalize,
preserving temporal clusters without exact vector duplication. Text is repeated
with unique file identities. This tests capacity and pathological posting-list
lengths, not relevance on unseen five-year footage. Both real and synthetic
exact-neighbor comparisons are retained. Existing camera labels have a known
prototype fallback issue; source extraction is corrected for subsequent runs.

These are **fresh-process / potentially warm-OS-cache** measurements, not a cold
reboot, not the integrated app, and not a controlled CapCut editing session.
The existing app/indexer remained open. Small query samples are useful gates,
not statistically robust production p95 estimates.

## Startup / encoder

| Component | Measurement |
|---|---|
| FTS query ready, fresh Python process | 2.5 ms (inside process) |
| LanceDB + Rust tokenizer ready, 50× | 2.16 s (inside process) |
| Native compiled Core ML model, CPU only | 363 ms load; 23 ms first query |
| Native CPU encoder steady state | 9 ms median / 11 ms p95 |
| Native GPU encoder | 533 ms load **plus 2,007 ms first query**; 7 ms median |
| CPU + Neural Engine | ANE compiler fallback; slower, not selected |
| Rust tokenizer standalone | 521 ms load, 0.024 ms median |

Text tower: 282.3M parameters instead of full 375.2M model. FP16 Core ML package:
565 MB. Export vector cosine >=0.9999948 vs reference on 24 queries. Token IDs
match on 24 queries plus seven Unicode/long-query edge cases. Native CPU is the
leading choice; it avoids query GPU contention and Python AI-library imports.

## Text search

At 1.17M transcript segments:

- Naive full-corpus BM25 ordering: **~650 ms p95**, failed.
- FTS5 with 1–4 character prefix indexes and bounded newest-matching-first
  retrieval: **0.8 ms p95** unfiltered; **1.5 ms** camera; **1.3 ms** exact day.
- This ranking differs from BM25. Camera/day are posting-list intersections;
  arbitrary date ranges, filename/label integration and live backfill remain.

## Vector search — do not confuse speed with correctness

| Configuration | Dataset | Latency | Quality / caveat |
|---|---|---|---|
| IVF-PQ defaults | Real | ~10–26 ms | Unacceptably low recall |
| IVF-PQ, broad probes, refine 8 | Real | ~68 ms p95 warm | 98.5% mean / 83.3% worst file recall |
| IVF-PQ, adaptive probing + B-tree filters | 50× | 0.43 s unfiltered / 1.9 s camera | Failed latency |
| IVF-SQ, 128/256 fixed probes, exact rerank | Real | ~11 ms p95 | 99.0% mean; 90% worst / 95% worst camera |
| IVF-SQ, 128/1024 fixed probes + bitmap filters | 50× | 79 ms p95 / 86 ms camera | See hard-query recall below |
| Exact narrow-day subset | 50× | ~32 ms p95 | Avoids ANN false negatives in tiny scopes |
| Memory-mapped HNSW int8 + exact rerank, 8,192 candidates | Real | 24 ms p95 | 99.6% mean / 96.7% worst |
| Same int8 graph + rerank | 50× | 217 ms p95 | 144 ms mmap open; 3.45 GiB peak RSS; fails initial budget |
| HNSW float16, expansion 8,192, 4,096 candidates | Real | 26.5 ms p95 | 99.44% mean / 98.33% worst; full-scale build running |
| HNSW float16, same settings | 50× | ~61 ms p95 | Failed: difficult-query mean file recall ~28%, some zero |
| RabitQ 4-bit, full scan | 50× | ~569 ms p95 | Failed latency; ~7 GiB peak benchmark RSS |
| PCA-128 projected GPU scan + FP16 rerank | 50× | ~220 ms median | Failed: ~85% mean / 40% minimum difficult-query recall |

**IVF-SQ is not yet approved:** full-scale exact comparisons found only 70% recall
for “a person talking to the camera” and 71.7% for “a baby sleeping” at 128 probes.
Increasing probes improves recall but can violate latency/cache targets. Other
queries reached 98–100%. A memory-mapped HNSW alternative (USearch) is being tested
at full scale; filtered retrieval, reranking I/O and residency must also pass.

Query-plan inspection showed adaptive maximum probing visits every partition for
these text queries. Fixed probes prevent accidentally turning ANN into a full
index scan. Bitmap indexes improve high-hit-count categorical prefilters.

Lifecycle tests passed in an isolated LanceDB fixture: idempotent replay, searchable
unindexed arrivals, replacements, deletions, reader refresh, maintenance and reopen.
Full-scale uncompacted-tail and interrupted-build tests remain acceptance gates.

## Paged browsing

The 50× summary projection contains **723,700 media rows in 796 MB**. Fifty keyset
pages of 100 summaries, including JSON decoding, took **0.58 ms for the first page
and 0.44 ms p95**; no row was repeated. This excludes integrated SwiftUI rendering
and a true cold-SSD test. It validates indexed scalar capture dates and summaries
instead of loading all media and their complete frame arrays at launch.

## SSD previews

48 clips across frame-count quantiles, ~0.55 video hours, sampled from existing
SSD JPEGs without opening originals:

- 256px cells, every two seconds, max 100 cells/sheet, JPEG quality 65:
  **13.15 MiB per video-hour**.
- WebP equivalent: **10.16 MiB/hour**, but native codec support/complexity favors
  starting with JPEG.
- Mean 256px grid thumbnail: **8.76 KB**.
- Approximate 50× projection: **20.4 GiB storyboards + 5.9 GiB grid thumbnails**.
  This depends on the actual video/photo mix, durations and visual complexity.
- A retained portrait storyboard was visually inspected: subjects remain clear
  at grid/scrub size; portrait aspect ratio is preserved within padded cells.

No whole-library preview backfill or offline-originals app test has run yet.

## Durable SQLite projection milestone

`search_store.py` now materializes scalar media summaries, per-asset frame rows,
embedding references and FTS5 transcripts from a transactional source change log.
Each consumer batch commits data and its cursor together. Restore-branch anchors
prevent silently reusing a cursor against changed history; acknowledged log entries
can be compacted while preserving the anchor. Incomplete builds resume.

On an isolated copy of the real catalog: **14,474 files / 116,561 frames / 21,868
incremental transcript segments** projected in **4.82 s once**. First 100-item
page **0.57 ms**, prefix transcript query **0.82 ms**. Subsequent launches read
the saved projection rather than rebuild it. Legacy experimental transcripts
still need an explicit seed adapter; these counts do not claim complete parity.
Run: `benchmarks/search-store-20260928T182500Z/`. Not attached to production yet.
