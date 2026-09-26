# Mami native prototype

SwiftUI macOS browser backed by the existing local experiments. The application
reads the original media, cached JPEGs, and index; all deployments are created
under `~/mami-lab/apps` with a unique timestamp.

## Implemented

- Lazy media grid for all 498 files, video/photo filters, Reveal in Finder.
- Persistent offline SigLIP process: the model stays loaded between searches.
- Up to 60 distinct files per visual query, starting at each file's best frame.
- Hover scrubbing from one-second JPEGs, off-main-thread decoding, a 192 MiB
  decoded-image cache, and neighboring-frame prefetch.
- Search results default to a ±8-second neighborhood; disable the switch to
  scrub the whole clip. One-second previews are discrete frames, not 30 fps video.
- Click a displayed moment to play the original with AVPlayer at that timestamp.
- Romanian spoken-word search with accent-insensitive, whole-word matching.
  All requested words must occur in one transcript segment. Results display
  automatic transcript evidence and seek to the segment's start. This is lexical
  retrieval, not semantic translation or full phrase matching across segments.
- Offline cached browsing, with an explicit error if an original is unavailable.
- Large centered search, ⌘F search focus, capture-date sorting, and two-line
  capture metadata cards. Invalid camera GPS placeholders are excluded.
- Always-visible playback controls with a 100 ms position observer, ±5-second
  seeking (← / →), mute (M), and scrub/resume. Space toggles playback; Escape
  closes the preview. Previous/next results use ⌘← / ⌘→. Transport shortcuts are
  suspended while the capture-info or tags/place popover is open.
- Favorites, manual tags and place labels, and local label/favorites filters.
  Current labels and append-only edit history are stored transactionally in SQLite.
- Persistent media catalog with capture metadata, content IDs and cached-frame
  references. Existing JSON annotation history is imported once and preserved.
- Change-aware, versioned SQLite snapshots, integrity checking, backup status,
  and a restore utility. See [catalog backups](#catalog-backups-and-backblaze).
- SHA-256 content identities, with compatibility for earlier path-keyed labels.
  `build_catalog.py --inventory INVENTORY` fingerprints originals read-only and
  writes a fresh catalog plus per-file checkpoints. It reports exact duplicates.

This is a usable search/browser prototype, not the complete media manager.
The SQLite catalog is seeded from a fixed experiment index. Resumable ingestion,
automatic relocation discovery, named people, collections, camera imports,
duplicate review, archive migration and media-backup management remain
to be implemented. Content identities let labels follow an unchanged file once
its new path is included in a rebuilt catalog; moving files is not yet detected
automatically. Checkpoints are preserved but fingerprint runs do not yet resume.
Import requirement: skip camera `.LRF` proxy files. A future explicit cleanup
action should handle existing `.LRF` files; current tools do not delete media.
Visual results remain approximate; returning 60 neighbors does not mean 60
confirmed matches. The Python environment and model cache must remain installed.

## Build from Linux / run on Mac

```bash
python app/deploy.py
```

The command prints a fresh directory on the Mac. Build and package there:

```bash
cd /Users/ludi/mami-lab/apps/prototype-TIMESTAMP
caffeinate -is swift build -c release
~/mami-lab/.venv/bin/python package.py \
  --index /Users/ludi/mami-lab/runs/visual-repaired-20260925T180807825809Z \
  --speech /Users/ludi/mami-lab/runs/speech-20260925T151338631656Z \
  --inventory /Users/ludi/mami-lab/runs/inventory-20260925T150632007122Z/inventory.json \
  --catalog /Users/ludi/mami-lab/catalog/fingerprints-20260926T052452141336Z/catalog.json
Mami.app/Contents/MacOS/Mami --self-test
open Mami.app
```

Packaging creates a new `Mami.app` and refuses to reuse an existing bundle.
The bundle is ad-hoc signed for local development. No App Store or developer
account is needed for this local build.

`--self-test` checks media loading, frame decoding, original-video frame seeks,
muted AVPlayer playback, and three queries through the real Swift/Python protocol.
`--ui-test NEW_DIRECTORY` creates an AppKit-hosted SwiftUI window, exercises
browsing/search/reset, Romanian speech search, append-only annotation persistence,
capture metadata coverage, and initial/seek transport positions, and saves
app-view images. Add `--verify-playing` when launched via Launch Services to
check continuous position updates, pause, paused scrubbing and playing scrubbing.
Neither test changes the original media. UI smoke tests do not substitute for
hands-on evaluation of pointer responsiveness and playback controls.

`--catalog-test NEW_DIRECTORY` tests no-change dump suppression, no-op saves,
legacy migration, transaction rollback, restore, failed-backup retry, and restored
branches that reuse a numeric revision. Test fixtures are preserved.

Configuration is bundled as `Contents/Resources/configuration.json` and may be
overridden with `MAMI_INDEX`, `MAMI_SPEECH`, `MAMI_PYTHON`, and `MAMI_WORKER`.
Python loads only the pinned cached model (`HF_HUB_OFFLINE=1`). Stdout is reserved
for JSON-lines responses; diagnostic/model output goes to stderr.

## Catalog backups and Backblaze

- Live database: `~/mami-lab/catalog/database/catalog.sqlite`.
- Versioned snapshots: **`~/mami-lab/backups/catalog/`** — include this directory
  in Backblaze. Mami shows the latest completed snapshot time and a Show backups
  button. A local snapshot does not indicate that Backblaze has uploaded it.
- Snapshot format: standalone **`.sqlite` database files**, produced with the
  SQLite backup API, rather than SQL-text dumps or raw copies of a live file.
  They contain media records, capture metadata, frame references, current labels,
  annotation history and migration bookkeeping. Original media, cached frame
  images, embeddings, transcripts and model weights remain external files.

Database triggers maintain a revision and random change token. Only actual row
changes advance them: opening, browsing, searching, unchanged index seeding and
identical label saves do not create another snapshot. The token prevents a
restored database branch from colliding with an earlier numeric revision.
Saves schedule a snapshot after a two-second quiet period; startup and a minute
timer retry pending revisions. Orderly app termination flushes pending work.
After an unexpected exit, the next launch retries any committed but unsnapshotted
revision. The current snapshot is about 1.9 MB for 498 media records.

Catalog operations and snapshots share an in-process lock plus a filesystem lock.
Completed snapshots pass `PRAGMA integrity_check` and are synchronized before a
JSON receipt is atomically published. Interrupted outputs without a receipt are
not treated as completed. Every successful changed revision gets a new file;
old snapshots, journals, and staging outputs are retained. No automatic pruning
is performed. The app reports backup errors and retries while it is running.

To restore, choose a `.sqlite` file named by a completed `.json` receipt, and use
a **new** output directory. This verifies SQLite integrity, expected schema,
record counts and a SHA-256 byte-for-byte copy. It refuses existing destinations:

```bash
~/mami-lab/.venv/bin/python restore_catalog.py \
  ~/mami-lab/backups/catalog/catalog-IDENTITY-rREVISION-UUID.sqlite \
  --to ~/mami-lab/catalog/restored-TIMESTAMP
```

To try that restored catalog, quit Mami and launch the bundle with the environment
override (substitute its deployed path):

```bash
open -n --env MAMI_CATALOG="$HOME/mami-lab/catalog/restored-TIMESTAMP" Mami.app
```

This selects the restored copy for that launch and leaves the original live
catalog intact. Media/index files must also be available for full search and
playback. Database records can still be read when the source index is unavailable;
cached-image browsing requires the referenced frame files.

## Measured on the M1 Max

Initial component/integration test, 25 September 2026:

- 498 files / 7,128 cached images loaded.
- 122 thumbnail loads: cold-to-app-cache p95 ~1.85 ms; warm p95 ~0.001 ms.
  The OS disk cache may already be warm; this is not pointer-to-screen latency.
- Three original-video frame seeks: 33–128 ms (AVAssetImageGenerator).
- Persistent-process search round trips: first query ~108 ms, next ~11–13 ms.

The CLT SDK exports both a `State` macro and property wrapper; `ViewState` is a
type alias selecting the existing property wrapper without requiring the missing
SwiftUI macro plugin in Command Line Tools.

## Current deployment / playback verification

Latest bundle:
`~/mami-lab/apps/prototype-20260926T065946500677Z/Mami.app`.
It is open on the Mac. Its `ui-check` passed browsing, visual/speech search, reset,
metadata coverage, SQLite annotation persistence/history, initial/seek position
refresh without hover, and AppKit-dispatched left/right-arrow and M shortcuts.
It also verified that all 498 relocated originals are readable with SHA-256
identities. The current GUI continuous-playback recheck stalled while macOS
reported `CGSSessionScreenIsLocked = true` and no active display/user assertion;
the direct AVPlayer test also stalled at rate 1. Recheck with the Mac unlocked.
The catalog integration suite passed in build `20260926T062538347737Z`, including
snapshot restoration and restored-branch revision collision checks. The real
498-file snapshot was restored and byte-verified in that build's
`restored-real-catalog` directory. Reopening the unchanged library in subsequent
builds left exactly one completed snapshot, at revision 498.
Metadata extraction covered 498 files with zero errors and zero valid locations
in the fields checked. The fingerprint run read 35,795,948,434 bytes in 19.2 s,
with zero failures and no byte-identical duplicates (disk cache may be warm).

The previous GUI-launched self-test **passed** after the user accepted the access
prompt: playback advanced from 5 s to 6.855 s and all three search checks passed.
Its log is `prototype-20260925T203549474366Z/gui-self-test.log`.
SSH-launched playback clocks can stall despite rate 1, so continuous playback
checks must run through Launch Services.

The GUI check in build `20260926T052919395839Z` (`--verify-playing`) **passed** after the renewed Desktop
access wait. It verified continuous transport updates without hovering, pause,
scrubbing while paused, and automatic resume after scrubbing during playback.
Logs `gui-ui-test.log` / `gui-ui-errors.log` and screenshots in `gui-ui-check`
are inside that deployment. Ad-hoc-signed rebuilds may prompt for Desktop
access again. The app is not installed into `/Applications`; all earlier bundles
and results are preserved.

## Originals relocated to ~/Media (26 September 2026)

Originals now live under `~/Media/Originals/DJI-Pocket-4P/2026/YYYY-MM-DD`.
`~/Media/Originals/Mami-iPhone-16-Pro-Max` is reserved for the phone.
The read-only `relocate_catalog.py` scan matched all 498 original SHA-256 values,
with zero missing files, new content or read failures. At scan time there were
68 byte-identical pairs in the accidental `2026-09-Sep` folder and `2026-09-18`.
The user is handling removal of the mistaken folder; Mami references only the
properly dated copies. The renamed ` .MP4` now resolves to
`2026-09-18/DJI_20260918115310_0032_D.MP4` by matching content.

The report is `~/mami-lab/catalog/relocation-20260926T065754403542Z/relocations.json`.
It is a historical scan, not a live inventory. Package using `--relocations` and
`--metadata` pointing at a previous bundle's `Contents/Resources/metadata.json`
instead of re-reading the obsolete inventory paths. Index/transcript logical
keys and cached frame paths stay unchanged; the new bundle maps playback/photo
URLs and Finder targets to verified locations. SQLite rows are updated in place,
preserving labels and preventing duplicate catalog entries. The database has
498 rows at the new paths and a verified automatic snapshot at revision 996.
Older bundles still carry obsolete paths; launch the current bundle above.
