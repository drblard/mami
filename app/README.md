# Mami native prototype

SwiftUI macOS browser backed by the existing local experiments. The application
reads the original media, cached JPEGs, and index; all deployments are created
under `~/mami-lab/apps` with a unique timestamp.

## Persistent local signing

Packaging now requires a persistent code-signing identity rather than ad-hoc
signatures, whose designated requirement changes with every build. Run
`~/mami-lab/.venv/bin/python signing.py` once on the Mac to create a dedicated
keychain and a ten-year local code-signing certificate. It preserves existing
identity material on subsequent runs. The printed `security add-trusted-cert`
command must run interactively in Terminal on the Mac: macOS requires the user
to approve certificate trust. Trust is scoped to code signing in the user's
trust settings, not general TLS trust or the system trust store.

Private material stays in `~/mami-lab/signing` (mode 0700): a dedicated keychain,
encrypted PKCS#12 recovery copy, and a mode-0600 password file. Keep that directory
in the Mac's protected backup; don't commit it or generate a replacement certificate
for each build. Packaging unlocks only this keychain, pins the designated requirement
to the certificate fingerprint plus the existing bundle ID, and verifies the signed
bundle. It records public signing details beside the bundle and never falls back
to ad-hoc signing. Signing briefly adds the dedicated keychain to the search list
(codesign requires this even with `--keychain`), serializes signing operations,
then restores the original list in a `finally` block.

This identity is for this local Mac workflow, not notarized public distribution.
Switching from the old ad-hoc identity requires Photos approval once more. Permission
retention must then be tested across two different signed builds before being marked
verified. As of 2026-09-26, interactive trust approval is complete. The signed build
`prototype-20260926T170319871474Z` is open; `prototype-20260926T170515331283Z` is ready
for the update test after the user grants Photos access to the signed identity.
Both strictly verify against the same certificate-pinned requirement and have
different signature hashes; the latter deployment records this in
`signing-cross-build-check.json`. Photos permission retention is now verified:
after approval on the first build, the second resumed fetching automatically
at 20:10:57 +0300 on 2026-09-26 (PID 83748), with no new permission request.

All native worker launches set `PYTHONDONTWRITEBYTECODE=1`: Python cache files inside
`Contents/Resources` invalidate the sealed app. Native integration checks (including
search and real-media import) passed on the first build, followed by successful strict
signature verification. Its signature also remained valid after opening the real
library/indexer. When manually importing modules from a signed bundle for diagnostics,
use `python -B` or the same environment variable to keep the bundle immutable.

Agreed next priorities: finish signing/permission retention, then physical DJI
verified-cleanup checks and `.LRF` handling, then English semantic retrieval over
Romanian speech. Initial import/indexing continues in the background; NFS/HDD
archive support is deferred until closer to the storage purchase.

### Worker-pipe crash fix (2026-09-26)

The macOS launchd log confirmed that Mami PID 63898 exited on SIGPIPE at
18:16:06 +0300 while opening a preview during Photos import. Preview lifecycle
events send busy/idle commands to workers; a finished batch can close stdin
before the UI has consumed its final output and cleared the pipe. All worker
command writes now use descriptor-local `F_SETNOSIGPIPE`, returning a catchable
EPIPE instead of terminating the app. Scheduling updates to an already-finished
import batch are harmless; its final stdout and exit status still determine the
result. Child process signal handling is unchanged.

Build `prototype-20260926T152137745992Z` passed `--worker-pipe-test` (live command
delivery plus 100 writes after the reader closes) and the native integration
suite, including previews and real-media Photos transfers. It also includes the
permanently allocated Settings transfer-status rows.

## Implemented

Usability pass verified in `prototype-20260926T160933527036Z/ui-check`: additive
selection, selection-scoped previews, B basket shortcut, date parser/boundaries and
pre-limit date search, grid-lock arrival/refresh, plus existing native integration
checks. Final deployed bundle is `prototype-20260926T161509876400Z`; identical Swift
sources reuse the verified compiled executable (recorded in `native-build-provenance.txt`).
It also forces full-range JPEG output for limited-range edited video thumbnails,
retries an empty end-of-video sample half a second earlier, and treats subprocess
termination during Quit as resumable work rather than an indexing error. 32 Python
tests passed on Linux and macOS, including quit-during-FFmpeg recovery. The affected
live jobs were repaired with saved inference checkpoints; no indexing errors remained
after repair. New-build Photos access still requires user approval.

- Transcription explicitly selects conventional audio instead of FFmpeg's
  automatic highest-channel-count choice, excluding unsupported `apple_apac`.
  iPhone spatial recordings retain their original tracks; only the temporary
  transcription WAV uses the stereo companion. Verified on `IMG_4325.MOV` in
  `prototype-20260926T153155087439Z/audio-check`; its saved job completed on retry.

- Lazy media grid for all 498 files, video/photo filters, Reveal in Finder.
- Persistent offline SigLIP process: the model stays loaded between searches.
- Up to 60 distinct files per visual query, starting at each file's best frame.
- Search defaults to **Visuals & speech**: visual and Romanian spoken-word results are merged
  using reciprocal ranks, with duplicate files combined. Speech evidence and its
  exact segment timestamp are retained when a file matches spoken words. This
  does not imply both searches matched the same moment. Visual-only and spoken-
  word-only modes remain available.
- Search scope is beside the search field, with explicit **Visuals only** and
  **Speech only** options. Shape is controlled by **Vertical** and **Horizontal**
  toggle buttons. Turn the active button off to show every shape; square and unknown
  remain visible in the unfiltered grid. Classification reads display-oriented cached preview headers off the UI
  thread, including EXIF orientation, so it works without reading originals.
  It describes source shape, not platform eligibility or automatic cropping.
  One-pixel square-preview rounding is tolerated. The current library contains
  357 vertical and 141 horizontal files. Shape is applied before search's 60-file
   cap in both visual and speech retrieval. The **Device** filter also applies before
   that cap, using the original's import-device folder or camera metadata for Photos
   imports. Unknown devices remain selectable. Other browsing filters combine with it;
    **Clear filters** preserves search content and scope, removing any date phrase.
   Camera metadata reads Apple QuickTime make/model for videos and ImageIO TIFF
   make/model for photos (including HEIC). iCloud assets without a camera model,
   such as screenshots or some edited exports, appear as **iCloud · Device
   unavailable** rather than being guessed to come from a phone. Import history
   identifies iCloud provenance even with a custom destination.
- Hover scrubbing from one-second JPEGs, off-main-thread decoding, a 192 MiB
  decoded-image cache, and neighboring-frame prefetch.
- Search results default to a ±8-second neighborhood; disable the switch to
   scrub the whole clip. Hold Command while hovering to temporarily invert that
   range, including when the pointer is stationary. One-second previews are discrete frames, not 30 fps video.
- Click a displayed moment to play the original with AVPlayer at that timestamp.
- Romanian spoken-word search with accent-insensitive, whole-word matching.
  All requested words must occur in one transcript segment. Results display
  automatic transcript evidence and seek to the segment's start. This is lexical
  retrieval, not semantic translation or full phrase matching across segments.
- Offline cached browsing, with an explicit error if an original is unavailable.
- Large centered search, ⌘F search focus, capture-date sorting, and two-line
  capture metadata cards. Invalid camera GPS placeholders are excluded.
- Clicking a card's details/padding selects it; clicking its media opens the preview.
  Command-click anywhere on a card toggles additive selection without opening it.
  Space previews a multiple selection using only those items, in grid order.
  **B** adds highlighted files to Selected clips, or removes them when all are already
  present. In a preview B toggles the displayed item. Text entry keeps normal typing.
  In the listing, Up/Down move by rows
  using the current adaptive column count (including sidebar/resizing changes),
  preserving the column where possible; Left/Right move by one item.
  With a preview open, only Left/Right move through items, updating
  an open preview. The selection stays highlighted and scrolls into view. Text entry
  and popovers retain normal keyboard behavior. Escape or the outside backdrop closes
  the preview; previous/next header buttons are removed.
- **Capture date** opens a visual date filter with Today, Yesterday, Last 7 days,
  Last 30 days and This month presets. Rolling-day presets include today; This month
  selects the full calendar month. Presets apply immediately. Custom choices are
  drafts until Apply; Cancel or dismiss leaves the active filter unchanged.
  Day/Week modes select a single date or its locale-defined calendar week. Month(s)
  selects one whole month, or a span between two clicked months (including across
  years); Year selects the whole calendar year. Custom range uses first/last day
  clicks on two adjacent month calendars. Reverse selections normalize automatically,
  and a first click can be applied as a single day/month. Year jumping and previous/
  next navigation avoid stepping through years month by month. The filter button
  shows the actual selected dates; Clear/All dates removes the manual constraint.
  Both endpoints are included. Search recognizes English month
  names/abbreviations with an optional year (`goats in September`, `goats in Sep 2025`),
  `in 2025`, `on 2026-09-01`, and `from 2026-09-01 to 2026-09-30` (also `between … and …`).
  An omitted year means the current year. The interpreted range is displayed; date-only
  queries browse all matching items. Date constraints intersect manual filters and apply
  before the 60-result search cap. Undated media are excluded when a date filter is active.
  `prototype-20260926T191151652402Z` passed native integration and date-filter checks
  for presets, leap day, DST, calendar weeks, single/multiple months, years and
  reversed custom ranges. Its `ui-check/date-filter.png` retains the rendered picker;
  the composited image was visually inspected. This signed build is deployed.
  Follow-up `prototype-20260926T193816925890Z` makes the entire day/month/year
  cell clickable and gives quick presets full-width blue-hover clickable rows.
  Switching modes resets the draft to today's day/calendar week/month/year and
  navigates to the current month, ready for Apply without another click. The default
  month does not anchor a later custom month span. Native integration and explicit
  current-period mode-switch checks passed; the signed update is deployed.
  `prototype-20260926T195034391717Z` bounds the picker to the earliest valid
  capture date in the full local catalog through today (independent of search
  and other active filters). Earlier/future day cells, months and years are hidden;
  navigation and the year chooser respect those bounds. Presets and whole-period
  selections are clipped to the same interval. Native integration passed and the
  signed build is running. Bounds expand as older media enters the catalog.
- **Lock grid** freezes the browsing catalog until Refresh or unlock. A permanent row
  shows how many new library items are waiting, without moving the grid vertically.
  Refresh retains the lock and reruns current filters/search; the pending count covers
  new library items, not a prediction of semantic matches. Lock is per window/session.
- Video duration is larger and semibold. The **Import media…** label is constant;
  a small activity dot indicates work without resizing the button.
- Preview fills the available window with a small dismissible backdrop. Fit mode
  preserves aspect ratio and does not enlarge beyond 100% original pixels; a 100%
  control enables native-pixel inspection with scrolling. The title shows zoom percent,
  accounting for Retina scale and display rotation. Photos decode at original size.
- Always-visible playback controls retain play/pause, ±5-second buttons, mute (M),
  and scrub/resume. Space and arrows are reserved for browsing in the preview.
  The indexing progress area and search spinner reserve space to avoid layout shifts.
- Favorites, manual tags and place labels, and local label/favorites filters.
  **Tags & places** opens a checklist with type-ahead suggestions; Return selects
  the first suggestion. Selected labels match any of the chosen tags/places.
  Current labels and append-only edit history are stored transactionally in SQLite.
- Persistent media catalog with capture metadata, content IDs and cached-frame
  references. Existing JSON annotation history is imported once and preserved.
- Change-aware, versioned SQLite snapshots, integrity checking, backup status,
  and a restore utility. See [catalog backups](#catalog-backups-and-backblaze).
- SHA-256 content identities, with compatibility for earlier path-keyed labels.
  `build_catalog.py --inventory INVENTORY` fingerprints originals read-only and
  writes a fresh catalog plus per-file checkpoints. It reports exact duplicates.
- Automatic background scanning of `~/Media/Originals`, persistent Pause/Resume,
  progress, and incremental metadata/frame/embedding/transcription checkpoints.
  Search reloads committed incremental results without restarting its model.

This is a usable search/browser prototype, not the complete media manager.
The SQLite catalog was seeded from a fixed experiment index; new files now enter
the resumable indexing queue. Named people, collections, direct iPhone transfer,
duplicate review, archive migration and media-backup management remain planned.
The scanner reconnects moved files by content when their earlier location is
unavailable. The separate one-off fingerprint experiment does not resume, but
the application's scanner checkpoints hashes per file and reuses unchanged ones.
Scanning ignores camera `.LRF` proxy files. Camera import optionally preserves them
in the hidden `.mami-proxies` tree and can remove them only after independent-copy
verification, using the same keep/skip/remove policies as original media.
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
  --metadata /Users/ludi/mami-lab/apps/prototype-20260926T065946500677Z/Mami.app/Contents/Resources/metadata.json \
  --relocations /Users/ludi/mami-lab/catalog/relocation-20260926T065754403542Z/relocations.json
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

## Verified card and folder imports

**Import media…** accepts a mounted camera card or media folder. Choose its device
folder (DJI/iPhone presets or a custom name), then **Import & verify**. All supported
media in the selected folder tree is considered. The importer copies to
`~/Media/Originals/<device>/<year>/<YYYY-MM-DD>/`, using capture metadata or, when
unavailable, the source's modification date. It records which date source it used.
Direct USB/PTP iPhone transfer is not implemented. For a synced iPhone library,
use the iCloud Photos importer below.

The worker hashes the source, copies in fsynced 4 MiB blocks, rereads the copy and
verifies SHA-256, and confirms the source stat signature is unchanged. Only then
does it atomically publish the destination filename. It checks catalog/import
journal duplicate candidates by rehashing the existing copy before skipping one.
Name collisions preserve the existing file and give the new copy a unique suffix.
Hidden files, symlinks and unsupported types are skipped. `.LRF` proxies are skipped
unless **Include DJI .LRF proxy files** is enabled. When enabled, copies are preserved
under `Originals/.mami-proxies/<device>/<year>/<date>/`, outside the media index;
the proxy's own bytes must be saved and verified before its source can be removed.
Per-file skip/keep/remove applies independently to each listed file.

**Pause**, **Resume**, **Stop**, and background progress are available in the import
sheet. Imports continue while Mami previews or searches media. After a crash
or closing Mami, choose the same source and device folder again: the importer
checks saved partial bytes against the source and appends the remaining bytes.
Damaged partial attempts are retained and replaced with new attempts. The tests
exercise actual process exit without Python cleanup, not just thrown exceptions.

The import journal and staging files live in `Originals/.mami-imports`, ignored by
the scanner. Completed staging files are retained as hard links to published
originals, so they do not consume another full copy of the media. Failed attempts
can consume extra storage and are not automatically removed. The import journal
is separate from catalog snapshots; back up the entire originals tree to retain
it too. Published copies trigger an automatic scan; indexing's own pause state
and GPU activity policy still apply.

Persistent camera preferences live in **Settings (⌘,) → DJI Camera**, including
automatic offload on connection, verified source removal, LRF inclusion and eject.
Automatic offload and eject default on; source removal and LRF inclusion default
off. All four preferences survive app restarts. **Import** remains available for
manual folders, and Settings offers **Review files / exceptions…** and retry.

While Mami is open, external local volumes with the Pocket4P's `MISC/PP-041.db`
and `DCIM` layout are recognized without relying on their volume name. The camera
waits for the shared importer to finish any active Photos batch. One attempt runs
per connection; a reconnect or **Check camera / retry** permits another attempt.
Camera status/errors remain visible in Settings even when Photos subsequently
uses the importer. Disable automatic offload before connecting to review exceptions.
Native scheduling checks cover disabled/busy deferral, one attempt per connection,
reconnect and explicit retry. The tested signed build
`prototype-20260926T183506337829Z` is deployed; the new connection-triggered workflow
still awaits a fresh physical camera connection. Manual verified offload/eject was
already checked on the hardware as recorded below.

Source removal is explicitly selectable in Settings. Per-file exceptions
are **Skip — leave untouched**, **Import & keep**, and **Import & remove**; choices
are remembered per source path. Before removing each file, including duplicates,
the worker full-syncs the independent destination inside Originals, freshly rereads
and SHA-256 hashes both files, commits a verification receipt, checks their full
stat signatures again, and only then unlinks the source. Any mismatch/error retains
the source. SQLite fullfsync and macOS F_FULLFSYNC are used for this boundary.
After a crash, a prior verification receipt never authorizes removal without fresh
verification. This is selective file removal, not device formatting: unselected
`.LRF`, hidden, unsupported and skipped files remain.
The `prototype-20260926T171304898633Z` build adds explicit proxy import
and cleanup controls. 34 Python tests passed on Linux and macOS, including proxy
keep/skip/removal and retention after destination corruption. Native integration
checks passed and the app's signature remained valid afterward.

Camera imports default to **Eject camera/card after successful import**. Only
external source volumes on a different disk from the destination qualify. The
worker rechecks volume UUID, mount point and device identifiers before normal
whole-disk eject. Failed, stopped and empty imports do not eject. Eject failures
report that copies are saved but manual eject is needed. Wait for
**Safe to unplug — device ejected** before disconnecting.

Disconnection retains partial attempts. Reconnect and choose the same source and
device folder to retry: saved bytes are compared with the source before resuming;
source removal always requires fresh verification. Recovery cannot prevent device
filesystem damage from disconnecting during a filesystem write.

Physical DJI checks passed using the user's new MP4, JPG and LRF: skip-photo/keep
video-and-proxy, import-and-keep all three, then verified removal of all three and
successful whole-device eject. Copies remain in Originals and `.mami-proxies`.
Evidence and hashes are saved on the Mac in
`prototype-20260926T181327963381Z/physical-dji-offload-check.json`. This build is
running; Photos fetching resumed and its signature strictly verifies after launch.
36 Python tests passed on Linux/macOS and native integration passed. Mid-copy
disconnect/reconnect recovery was simulated in a regression test; physical cable
removal during transfer has not been tested. English semantic speech retrieval
is the next priority.
The retained `prototype-20260926T100312588736Z/removal-check` fixture passed on
the Mac with real video bytes, F_FULLFSYNC, a durable removal receipt, and keep/
skip/proxy retention. The 33 Python checks include a real process crash after
verification and before unlink, followed by corruption of the destination and a
successful verified recovery to a new destination.

## Cable-free Photos imports

### Newest-first scheduling

Photos downloads use descending PhotoKit creation dates. The asset list refreshes
every 60 seconds between assets during a long pass so newly synced captures can
jump ahead of the historical backlog. Already staged receipts are drained newest
first within the existing bounded staging pipeline; an active download finishes.
Camera files are sorted by capture timestamp (DJI timestamped names, otherwise
capture metadata, with file modification time as fallback).

Index jobs persist capture-time priorities. For Photos, the durable import journal
retains PhotoKit's capture timestamp in the staged source signature even after
staging is removed; this takes precedence over edited-file tags or destination copy
time. Other media uses cached metadata, then modification time as fallback.
Equal timestamps use newest enqueue order. Existing
unfinished jobs have priorities filled during the next scan; completed indexing
is retained. The scheduler chooses the next job afresh, rather than walking an
old snapshot of the backlog. Incoming scan requests are coalesced at 15 seconds;
a 60-second periodic check also discovers arrivals. At a cooperative checkpoint,
older work yields, is reprioritized, and resumes without repeating saved frames,
embeddings or speech chunks. Active model/FFmpeg calls finish before yielding.

`prototype-20260926T185402022252Z` passed 40 Python tests on Linux/macOS and native
integration, including PhotoKit ordering, capture-date precedence over filename/
arrival time, authoritative Photos journal dates, priority persistence and mid-job
arrival without duplicate frames. This signed build is deployed and running.

The old `32 of 16866` Photos message represented position in the entire matching
library, not downloads remaining, and reset when ordering changed. It is replaced
by `newest first · capture date · filename`. A live audit against the 21:48:54
catalog snapshot retained all 4,285 previously completed resource IDs, found 151
new IDs and confirmed none of the latest 30 imported files was previously completed.
Evidence is in `prototype-20260926T184845814982Z/redownload-audit.json`. Filenames
can legitimately repeat: IMG_9823.HEIC exists as distinct March 2 and September 24
Photos resources, each tracked independently.

**Import media… → Enable Photos import…** asks for macOS Photos permission. The
Mac must use the same iCloud-synced System Photo Library as the phone. Apple’s
PhotoKit supplies full original photo/video resources, including Live Photo paired
videos, and downloads cloud-only data using `isNetworkAccessAllowed`. Mami never
calls Photos modification or deletion APIs. Deleting a photo in the synced Photos
library still propagates through iCloud, but an already imported Mami copy is independent.

Configure imports in **Settings (⌘,)**: choose the exact destination directory,
an inclusive **Import from** date, and **Enable automatic import**. The initial
date defaults to January 1 of this year and is persisted; it does not roll forward
in January. The PhotoKit predicate excludes older/undated assets before downloads,
and includes future captures after the configured date. Pending receipts follow
the same date restriction. Changing the date does not remove existing copies;
changing destination affects new imports and does not move existing originals.
Unavailable destinations report an error rather than creating a replacement mount.
While enabled and Mami is open,
checks run every five minutes. PhotoKit reads locally cached originals or downloads
cloud-only bytes. A background producer streams, hashes and rereads each original;
a concurrent consumer copies and verifies published receipts in small batches.
There is no wait for the full library export. Up to eight resources / 512 MiB are
queued or being copied (a single oversized resource is allowed), plus the producer's
current resource. A slow/paused destination applies backpressure to fetching. Existing
staging from earlier versions is drained first and is not part of that new-work bound.
Copies go to
`<chosen directory>/year/date/` (default `~/Media/Originals/iCloud`). Custom
destinations are registered for background indexing; offline roots retain catalog
entries. Completed resources have individual receipts;
interrupted downloads restart that resource. Handled failures release their temporary
download; unreceipted artifacts from abrupt termination are retained. A failed
resource does not prevent other completed downloads from importing. Completed
exports are excluded from future import passes. Formats outside JPG/JPEG, PNG,
HEIC, MP4 and MOV are reported as unsupported and left in Photos.

Exports and receipts live in `~/Media/Incoming/.mami-photos`. After an independent
destination passes SHA-256 verification and macOS full-sync, completion is committed
to the catalog and receipt. Staging is freshly reverified before removal. Redundant
temporary hard links from earlier exporters and the completed destination's staging
link are also released; unrelated/failed artifacts are preserved. No Photos/iCloud
deletion API is used. A crash after history commit retries cleanup without copying;
offline completed originals never trigger a new download. Import errors stop this
pass with receipted bytes retained for retry. Back up Incoming to retain unfinished
downloads. Completed resource IDs, SHA-256 digests
and sizes are also committed to `photos_import_history` in the backed-up catalog,
independently of destination paths. Existing completed JSON receipts migrate into
that history. The exporter consults history before requesting cloud bytes, so moving
an original, disconnecting an archive, deleting staging after completion, or changing
the destination does not trigger re-download. A changed/recreated System Photo
Library with different PhotoKit local identifiers will require identity reconciliation.
The user has enabled real Photos imports. The parallel pipeline's native real-media
fixture, bounded producer/consumer, and 30 Python copy/recovery tests passed on macOS
in `prototype-20260926T144822443381Z/ui-check` (isolated fixture catalog). That build
validated the transfer. The follow-up build `prototype-20260926T145242894829Z`
is deployed and has been observed draining the live staging backlog. It also lets
already-downloaded originals finish transferring if macOS requires renewed Photos
permission after an update. Settings shows fetching and saved-original progress
separately; macOS unified logs expose status/errors under subsystem
`local.mami.prototype`, category `PhotosImport`.

Photos access failures now explicitly report **library import is incomplete**,
with **Allow Photos access…** and a Photos privacy-settings link. A drained staging
queue is not reported as a completed library import. Successful checks state how
many matching Photos library items were actually enumerated.

The `prototype-20260926T150611352384Z` build replaces FFmpeg photo decoding with
native ImageIO (`--image-probe`, `--image-frame`). This fixes HEIC auxiliary-image
filter conflicts without modifying originals. Video/audio errors now expose the
actual FFmpeg diagnostic instead of just a long failed command. Metadata version
upgrades refresh device labels while retaining visual and speech checkpoints.
31 Python tests and native integration checks passed. Native decoding was verified
against `IMG_4343.HEIC` and `IMG_4545.HEIC`; both produced correctly oriented 480×640
previews. `live-metadata-repair.json` in that deployment records repair of 113 live
indexed/failed iCloud items, including iPhone 16 Pro Max and iPhone 13 Pro Max
identification, with zero recorded indexing errors afterward. This build is open.

### NFS / HDD archiving — planned

Managed archiving is not implemented. The next layer should map content identities
to one or more locations and stable storage-volume IDs, distinguish offline storage
from a missing file, and retain local thumbnails/search data. A move must copy,
flush and freshly hash the archive copy, durably commit its catalog location and
recovery receipt, then remove the local original only after verification. Interrupted
moves must resume safely. Playback and CapCut export must request the archive when
offline (or explicitly restore a local working copy). Neither missing-file detection
nor an offline mount should clear Photos import history; re-import needs an explicit
repair action. Until this exists, manual moves do not automatically reconnect the
browser to an arbitrary archive path.

## Selected clips island

Settings/history/grid verification: `prototype-20260926T110320692870Z/ui-check`
passed native row-aware arrow navigation, configurable Photos date boundaries,
Photos history persistence after staging removal and snapshot restore, and the
existing search/preview checks. `settings.png` retains the settings layout.
24 Python import/index-queue tests passed, including exact destination layout,
resumable verification and custom/offline indexing roots. Final checkbox styling
was built in `prototype-20260926T110459411628Z`, which is open on the Mac.

Use **+** on a thumbnail, or **Add to selection** in playback (Command-Shift-S),
to collect originals across searches and filters. The collapsible right-hand island
shows thumbnails, removal controls, context-menu reordering, and a multi-file
**Drag originals to CapCut** handle. It exports full original files, not rendered
subclips. Selection order and content identities survive restarts in the catalog
and its change-aware snapshots. Original paths reconnect after catalog refresh.
Missing originals stop the drag with a visible error rather than silently omitting
files. Native tests verify two independent file pasteboard items, reload, order,
no-op backups, and restoration from a snapshot. The user confirmed successful
bulk dragging from Selected clips into CapCut on 2026-09-26.

Latest native verification: `prototype-20260926T100509862908Z/ui-check` passed
selection persistence/restoration/multi-file pasteboard, all 498 original paths,
shape/search controls, the native import controller (new copy plus duplicate),
and playback seeking/keyboard controls. `library.png` and `import-complete.png`
retain the new island and Photos/import UI. This bundle was opened on the Mac.

The newer `prototype-20260926T104006228706Z/ui-check` additionally passed native
Space open/close, arrow preview navigation, outside-click dismissal, Retina-aware
preview sizing, Photos year-boundary exclusion, and device-scoped search. It retains
`large-preview.png`, `device-filter.png`, and compact-layout screenshots. Mouse tests
dispatch directly through AppKit because posting mouse events while the Mac is
locked replaces synthetic coordinates with the hardware cursor position. This
newer bundle is the one opened on the Mac.

Verification: `prototype-20260926T080506558677Z/ui-check` passed native search,
shape filtering, import controls (one new file plus one verified catalog duplicate),
and seeking/keyboard controls. `prototype-20260926T080009646906Z/import-index-check`
passed byte verification, duplicate re-import, and new-file CPU indexing in an
isolated retained library. Continuous playback passed while unlocked in
`prototype-20260926T074822742894Z/playback-check`; a later continuous-playback check
stalled after the session locked again. Real GPU transcription later passed with
CapCut open in `prototype-20260926T092711536655Z/gpu-resume-check`.
Automated Python checks cover abrupt process exits in both copying and
indexing, pause/resume, damaged partial copies, source mutation, and cache repair.

## Background scanning and indexing

The app starts one worker after the library opens and rescans every five minutes
while running. **Scan now** requests an earlier pass. Supported inputs currently
include MP4, MOV, JPG/JPEG, PNG and HEIC (actual decoding depends on FFmpeg/Pillow).
Hidden files, symlinks and `.LRF` proxies are skipped. No originals are changed.

The progress area shows discovery, file checking, preview creation, visual
indexing and transcription. Discovery has an indeterminate indicator until the
file total is known; subsequent phases show completed/total work. **Pause** is
persisted in SQLite and survives restarting Mami. While a unit is in flight the
UI says it is pausing; it then waits at the next safe boundary. **Resume** uses
the existing checkpoints. File checks cache size, nanosecond mtime/ctime, inode
and device; unchanged scans do not hash the files again or mutate the catalog.

Durable checkpoints are stored in the same catalog (and its snapshots):

- A full SHA-256 hash after each successfully checked file. During hashing,
  pause/foreground requests are checked between 8 MiB reads; a process crash
  may require hashing that one unfinished file again.
- Metadata, each preview frame, each embedding, extracted audio chunks and
  each completed 30-second Romanian transcription chunk.
- Job state and bounded automatic retries; **Retry** keeps successful units.

Artifacts live under `~/mami-lab/index-artifacts` and are fsynced before their
checkpoint commits. Crash-orphaned outputs are retained. On restart, interrupted
jobs return to the queue and valid units are reused. A source changing during
processing invalidates that job rather than publishing mixed-version results.
Completed incremental jobs also audit preview/vector existence during scans.
Missing artifacts are recreated individually; rebuilding a preview reconnects
its existing embedding instead of repeating inference. This audit does not yet
repair the separate legacy experimental index or detect byte-level cache corruption.
Chunked speech can lose context at chunk boundaries; it does not establish an
accuracy improvement over whole-clip transcription.

Work runs out of process at utility QoS with nice(10), single-threaded CPU visual
inference and limited FFmpeg threads. Previews and searches do not send scheduling
commands or pause imports/indexing. Manual Pause and GPU-load checks remain.
This is resource-conscious scheduling,
not a hard guarantee of zero performance impact. `.background` QoS proved too
restrictive for macOS disk I/O in the first native scan test; utility QoS resumed
the 213 saved file checks and completed the 498-file scan.

GPU transcription reads Apple IOAccelerator utilization without elevated privileges.
Before each inference boundary it requires three consecutive seconds below 15%,
using the maximum device/renderer/tiler reading across available devices. Missing
telemetry never means idle; the UI reports that it is waiting. A fresh window after
each chunk prevents Mami's previous GPU burst from being mistaken for an editor.
CapCut can remain open. These driver counters are best-effort, not a supported
per-process export detector; a CPU/media-engine export may show low GPU utilization.
In-flight inference finishes its current chunk before yielding. The serial queue
can still wait at a speech stage while GPU load stays high. Real pinned Whisper
inference and a saved speech checkpoint completed with CapCut open in the retained
`prototype-20260926T092711536655Z/gpu-resume-check` fixture. Finder-launched worker
PATH now includes Homebrew so Whisper can invoke ffmpeg.

The grid refreshes as previews become available. Committed embeddings and speech
segments are loaded by the existing search worker on its next query. Results
already on screen are not automatically reranked. Existing experimental indexes
remain the base corpus; no full reindex of those 498 assets is needed.

`--scan-ui-test NEW_DIRECTORY` verifies the native controller, pause persistence,
no scan writes while paused, resume, and the 498-file baseline scan. The successful
run is in `prototype-20260926T073246953188Z/scan-ui-check`. Isolated fixtures under
`prototype-20260926T071912826752Z/real-index-check` retain real new-file pipeline
checkpoints; `prototype-20260926T073246953188Z/search-refresh-check` verified the
same search process seeing 7,132 then 7,133 samples after a new checkpoint.

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
