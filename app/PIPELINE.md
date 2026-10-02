# Import, preview and search readiness

Status: **in production** since 2026-09-29. This is the contract the import,
preview, AI and face lanes follow.

Measured with CapCut present, video decoding and 50× search (controlled load, SSD
to SSD): six real DJI clips catalogued by 1.45 s; first thumbnails ~1 s after
publication; full-range scrub coverage 2.7–3.8 s. AI stayed paused until previews
finished. Large-DJI-over-USB latency has not been measured.

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

## Lanes and priority

| Lane | Queue (in `catalog.sqlite`) | Waits for |
|---|---|---|
| Import | journal per destination (`.mami-imports/journal.sqlite`) | — |
| Previews | `preview_jobs` (first thumbnail → coarse → fine) | — |
| AI index | `index_jobs` (`mami_index_work` view) | Completed previews; no pending preview work |
| Faces | `face_jobs` in `Derived/Faces/faces.sqlite` | Previews and AI index work (`mami_preview_work`, `mami_index_work`) |
| Preview packing | `preview_pack_pending` | Pending previews; active editing |

- New imports are prioritised in the preview queue (`priority=1`), newest first.
- Inference lanes (AI index, faces) and preview packing yield while CapCut is the
  frontmost app with input in the last 60 s.
- Each lane is a separate process that the app starts when work exists and retires
  when idle; a worker that reaches a healthy idle state earns back its restart budget.
- Workers read queue state without the writer lock; only writes take
  `catalog.lock` (`index_store.readonly` vs `connection`).

The step-by-step implementation log is in git history (`app/PIPELINE.md` up to `2281c35`).
