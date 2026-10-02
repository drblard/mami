# Mami app

A native macOS (SwiftUI) library for the family's photos and videos. It imports
from DJI cameras and iCloud Photos, makes everything searchable by what is shown
and what is said, recognises people, and hands originals to CapCut by drag and drop.
It runs on `ludi` (MacBook Pro M1 Max) from `~/Applications/Mami.app`.

This file describes the current system. Live status: [RELEASE.md](RELEASE.md)
(installed build, rollback), [REMAINING.md](REMAINING.md) (open work),
[CODE_REVIEW.md](CODE_REVIEW.md) (review findings). Detailed dated logs from the
build-up (September 2026) are in git history up to commit `2281c35`.

## Using it

- **Browse:** the grid pages through the library newest first, with filters for
  kind, shape, favourites, tags/places, people, date range and camera, and a grid
  lock that holds the view while new media arrives. Hovering a video scrubs through
  SSD previews; double-click or Space opens the preview (originals for playback).
- **Search:** one field searches visuals and Romanian speech together (or either
  alone); dates in the query ("in June") become a date filter. Focusing the field
  starts the search worker, so its visual model (~2–3 s to load) is usually ready by
  the time the query is typed; it stays loaded 10 minutes after the last search
  (60 s while CapCut is in active use).
- **Select and hand off:** click, ⌘-click, then drag originals into CapCut, or
  collect them in the Selected clips panel (B) and drag them all at once.
- **Delete:** with media selected, the trash button, ⌘⌫ or the context menu's
  **Move to Trash** (preview too). Originals go to the macOS Trash; Put Back in
  Finder restores them and they are indexed again.
- **People:** name face groups in the People sheet, then filter by person
  ([FACES.md](FACES.md)).
- **Imports:** DJI cards offload automatically when connected (verified copies;
  optional source removal after verification; optional eject). iCloud Photos
  imports newest first from the Mac's Photos library. Settings (⌘,) holds both.
- `?` shows all keyboard shortcuts.

## How it works

The app is Swift; the heavy work runs in bundled Python worker processes that the
app starts on demand and retires when idle. Workers talk JSON lines over pipes with
bounded reads and deadlines (`MamiCore/LineReader.swift`).

| Lane | Worker | Does |
|---|---|---|
| Import | `import_media.py`, `photos_batch.py` | Verified copy into Originals, then immediate catalog publication |
| Previews | `index_worker.py --role preview` | First thumbnail, then scrub frames ([PIPELINE.md](PIPELINE.md)) |
| AI index | `index_worker.py --role index` | SigLIP 2 frame embeddings, Whisper large-v3-turbo transcripts |
| Faces | `face_worker.py` | InsightFace `antelopev2` detection, embeddings, grouping |
| Search | `search_worker.py` → `packed_search_worker.py` | 4-bit GPU vector scan with exact re-rank; speech word postings |
| Maintenance | `search_sync.py`, `vector_sync.py`, `preview_cache_worker.py` | Search projection, vector generations, packed preview sheets |

Imports outrank previews, which outrank AI indexing and faces; AI work also waits
while CapCut is in active use ([PIPELINE.md](PIPELINE.md) is the contract).

**iCloud Photos:** PhotoKit only sees what macOS has synced into the System Photo
Library, and that sync lags by hours unless the Photos app is running, so Mami keeps
Photos open and hidden while automatic import is on (`PhotosSync.swift`). Passes
follow the library's persistent change history (`PhotosChanges.swift`): a full pass
at launch, after failures and every 6 h; otherwise only changed assets, starting
10 s after a library change. Imported resources are remembered in
`photos_import_history`, so Photos items are never fetched twice.

**Deletion:** `MediaDeletion.swift` moves originals to the Trash, then
`media_deletion.py` records `deleted_media` in the personal store and removes the
generated catalog/index state; search, vectors, previews and faces follow through
their change triggers. Imports skip recorded content while it is absent from the
library. Failed recording puts the originals back.

**UI responsiveness:** models are `@Observable`; grid cards are value-driven and
`.equatable()`; catalog refreshes are throttled (`CatalogUpdates`) and publish only
changes; personal-data saves run off the main thread and are undone on failure.
Real clicks are timed from release to rendered result (`InteractionLatency.swift`,
`interaction-latency.log`; named actions include gesture delays). Card images use one
click gesture: a separate double-click gesture held every click for 0.5 s.

## Data

| What | Where | Backed up |
|---|---|---|
| Originals | `~/Media/Originals/` (`DJI-Pocket-4P/`, `iCloud/`) | Yes |
| Personal data (authoritative) | `~/Library/Application Support/Mami/Personal/user.sqlite` | Yes, plus snapshots in `Personal/Backups/user-state/` |
| Generated catalog, search, previews, faces | `…/Mami/Derived/` | No (rebuildable) |
| Indexing models | `…/Mami/Models/` | No (re-downloadable in Settings) |
| Logs | `~/Library/Caches/Mami/` (`app-errors.log`, `lane-events.log`, `interaction-latency.log`) | No |
| Photos staging | `~/Media/Incoming/.mami-photos/` | Transient: released after each verified import |

Personal tables: annotations (favourites, tags, place) and their history, the clip
selection, Photos import history, media roots, people, face labels, people history
and deletions (`user_store.PERSONAL_TABLES`, mirrored in Swift). Everything keys on
content identity (`sha256:` of the original), so relocated originals reconnect.
Personal backups exclude generated state. Recovery: [BACKUP.md](BACKUP.md).

## Development

Development happens on Linux; the Mac (`ssh ludi`) builds and runs the app.

```bash
cd app
python3 verify.py --python-only     # Python tests + MamiCore Swift tests (Swift 6.4 if installed)
mise x ruff@0.16.9 -- ruff check ..  # lint (rules in ruff.toml)
```

- The `Mami` target needs AppKit, so only `MamiCore` builds on Linux. On the Mac,
  `verify.py` (no flag) runs the full Swift and Python suites.
- Swift 6 language mode with complete concurrency checking (`Package.swift` tools 6.0).
  Swift Testing is pinned to 6.2 because the Command Line Tools lack Xcode's
  `_TestingInterop`.
- Integration checks are launch flags on the app (`IntegrationCheck.swift`, e.g.
  `--catalog-test DIR`, `--media-deletion-test DIR`, `--ui-test DIR`); each writes
  to a new directory. `check_mac_installation.py` runs the installation set against
  a bundle on isolated data.
- Tests and checks use isolated catalogs (`MAMI_CATALOG`, `MAMI_SUPPORT_ROOT`); an
  overridden catalog never writes backups into the live library.
- Before Mac builds or tests, check that nobody is editing in CapCut.

## Releasing

```bash
cd app
python3 release.py             # build, test, package, sign and check a new snapshot
python3 release.py --install   # …then install it and relaunch Mami
```

`release.py` requires a clean commit and records it in the bundle
(`MamiSourceRevision` in Info.plist). See [RELEASE.md](RELEASE.md) for what it checks,
rollback and compatibility notes.
