# Scaling to 50× the library

**Status: accepted and released 2026-09-29.** This records the target, the design
decisions it produced and the evidence. The step-by-step log is in git history
(`app/SCALING.md` up to `2281c35`).

## Target

50× the September 2026 library: ~5.8 million frame vectors (768 dimensions, ~17 GiB
uncompressed) and ~1.2 million transcript rows, with cold startup under 5 s,
near-instant search while indexing and editing, and browsing, search and scrubbing
that work with originals offline. Search data and previews live on the Mac SSD.

## Decisions

- **GPU quantized scan with exact re-rank:** an MLX 4-bit groupwise quantized
  matrix scan over all vectors, then exact FP32 scoring of the top 4,096 candidates
  and per-file maxima for distinct-video results. Packed vectors are format 4,
  immutable generations selected by `current.json`, with reader leases. Embedded
  LanceDB (PQ, then IVF-SQ) was evaluated first: PQ queries took 0.4–1.9 s at full
  scale, and IVF-SQ reached 99% mean top-60-file recall; the quantized scan reached
  100% exact recall on the reference queries and was chosen. A full FP16 scan needed
  ~9 GB and was rejected. For small filtered scopes, scoring is exact throughout.
- **SQLite search projection** (`Derived/Search/search.sqlite`, schema 6): keyset
  paging, FTS5 transcripts, facet tables maintained in the same transaction as file
  changes, fed by catalog change triggers (`search_store.py`, `search_sync.py`).
- **Native text encoder** (Core ML export of the SigLIP 2 text tower) so the first
  query does not wait for Python model loading.
- **Personal data split** into `user.sqlite`, the only store backed up.
- **Packed preview sheets** (atlas format 1) for grid and scrubbing; full-resolution
  playback reads originals.
- **Memory budget revised** from a provisional 2 GiB: 4-bit weights alone are
  ~2.09 GiB at this scale. Accepted: 3.5 GiB steady search footprint, 7 GiB during
  an idle-time generation swap, on the 64 GiB Mac.

## Acceptance (measured on `ludi`, M1 Max, 64 GiB)

| Criterion | Target | Measured | Scope |
|---|---|---|---|
| Cold launch to usable visual search | < 5 s | 3.19 s | After disk-cache purge (not reboot) |
| Visual query p95 at 50× | < 100 ms | 71.9 ms (144 queries, 6 scopes, 100% file recall) | Warm cache, synthetic 50× capacity |
| Sustained visual queries | — | p95 40.6 ms over 4,096 queries; ~3 GiB steady, 5.57 GiB peak in swap | Synthetic 50×, kernel counters |
| Text query p95 | < 20 ms | Met | Warm |
| Typing to results | ~100–200 ms | 196 ms (includes 120 ms debounce) | Native UI |
| Offline originals | Works | Browsing, search and scrubbing pass with originals disconnected | Isolated fixture |
| Contention | Works | Imports, decode, inference and search pass with CapCut present | Controlled load, not the user's projects |
| New import readiness | — | 6 DJI clips catalogued in 1.45 s; first thumbnails ~1 s later; full scrub coverage 2.7–3.8 s | SSD to SSD, controlled load |

Real-library search quality was compared against exact search on the live
vectors; the synthetic 50× corpus measures capacity, not search quality. Cold
first queries are slower than the steady-state p95 figures.
