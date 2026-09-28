# Mami native prototype

**Active work:** [50× scaling checklist and recovery notes](SCALING.md) ·
[live prototype measurements](scaling/RESULTS.md).

## Import readiness pipeline (2026-09-28)

Verified imports are registered directly in the catalog, before preview or AI
processing. Videos can play from their saved original immediately. Separate
preview and AI workers have independent durable queues and pause controls.
Imported arrivals get thumbnails first, then full-range coarse scrub frames,
then denser previews in bounded batches. Scrubbing selects by actual timestamp.
AI consumes completed preview checkpoints and yields while previews are waiting.

The signed candidate `prototype-20260928T173844873061Z` passed 14 Swift tests,
79 Python tests, native catalog/personal-store and UI integration, and a four-clip
real-codec pipeline check with AI paused. Short generated clips were catalogued
by 0.294 s; preview workers produced all thumbnails in 0.345 s and scrub frames
in 0.636 s. These fixture timings are not a large-DJI throughput guarantee.
See [PIPELINE.md](PIPELINE.md) for acceptance progress and deployment status.

This changes generated index schema to version 2. Do not run older workers against
the migrated live catalog. Explicit full exports strip the new preview queue and
use index schema 1 for compatible recovery; existing frame/AI checkpoints remain.

## Personal-data storage and backups (migration candidate, 2026-09-28)

`catalog/database/user.sqlite` is authoritative for annotations and their history,
saved clip selection/order, media-root configuration, Photos resource/digest
verification history, and legacy-import markers. `catalog.sqlite` holds generated
media metadata and indexing state. Stable content identities reconnect edits when
the generated catalog is rebuilt. Legacy personal tables in the cache are frozen
compatibility copies; writing them from an old build is rejected after migration.

Migration holds the existing catalog writer lock, uses SQLite's backup API into
a temporary database, removes generated tables, vacuums, compares every retained
table to the source, integrity-checks/fsyncs, then atomically publishes `user.sqlite`.
An ownership/version marker prevents mixing libraries or silently recreating lost
personal data from stale cache copies. A missing cache can be initialized with the
personal store's identity.

Automatic backups cover **only the personal store**, under
`backups/catalog/user-state`. Generated indexing updates neither change its revision
nor schedule backups. Manual changes debounce for two seconds; import-driven
backups coalesce to at most one/minute, one backup runs at a time, and orderly Quit
flushes pending personal changes. Retention keeps the newest 64 plus hourly points
for 24 hours, daily points for 30 days, and monthly points for a year per identity.
Annotation edit history is included in each backup. Originals require their own
media backup; search vectors, transcripts and previews are regenerated as needed.

`restore_catalog.py SNAPSHOT --to NEW_DIRECTORY` recognizes both historical full
catalog snapshots and new personal snapshots. Personal restore creates `user.sqlite`
only; opening with `MAMI_CATALOG=NEW_DIRECTORY` initializes the cache separately.
Explicit full diagnostic exports still embed up-to-date personal tables for
compatibility. Do not simply launch an older build against a migrated live store;
use an explicit full export/restored directory for a compatible rollback.

The old scheduler backed up the whole cache after indexing changes, accumulating
~1.1 TB. With user approval, 9,178 redundant snapshots were removed after verifying
all 60 retained restore/manual-state representatives. The plan, verification and
deletion results are retained under
`benchmarks/scaling-20260928T124400Z/backup-prune-review/`. Unknown or changed files
are preserved by the cleanup tool. Full exports are no longer automatic.

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
- Hover scrubbing from one-second JPEGs, serial off-main-thread decoding with
  autorelease pools, shared in-flight requests, and a strictly costed 96 MiB /
  256-entry LRU thumbnail cache. Neighboring frames and two nearby grid rows are
  prefetched. Offscreen cards release displayed images; scrub requests debounce
  for 35 ms to avoid decoding every crossed frame during fast pointer movement.
- Search results default to a ±8-second neighborhood; disable the switch to
   scrub the whole clip. Hold Command while hovering to temporarily invert that
   range, including when the pointer is stationary. One-second previews are discrete frames, not 30 fps video.
- Double-click a displayed moment to play the original with AVPlayer at that timestamp.
- Romanian spoken-word search with accent-insensitive, whole-word matching.
  All requested words must occur in one transcript segment. Results display
  automatic transcript evidence and seek to the segment's start. This is lexical
  retrieval, not semantic translation or full phrase matching across segments.
- Offline cached browsing, with an explicit error if an original is unavailable.
- Large centered search, ⌘F search focus, capture-date sorting, and two-line
  capture metadata cards. Invalid camera GPS placeholders are excluded.
- Clicking anywhere on a card selects it; double-clicking its media opens the preview.
  Command-click anywhere on a card toggles additive selection without opening it.
  Native file drags from a selected card include all highlighted cards. Starting
  a drag on an unselected card replaces the selection and drags only that file.
  Drag sessions export original file URLs with copy semantics. Hover outlines
  distinguish the card under the pointer from the persistent selection.
  Preview navigation beeps at either boundary without recreating the preview or
  player; its header shows the current position and total (including selection-only
  previews). Search updates after a 250 ms typing debounce; superseded queued
  worker requests are cancelled before execution.
  Lock grid, arrivals/Refresh and indexing progress/Pause/Resume live in a pinned
  bottom status bar. Frequent import/indexing progress no longer invalidates the
  whole browser. Catalog-update chatter, On this Mac and CapCut-can-stay-open copy
  are removed. Filters use aligned rows, a media/shape group and trailing sort.
  `prototype-20260927T043305729764Z` passed native integration, drag-selection
  policy checks, preview identity checks at both boundaries and cache eviction
  bounds. The layout was reviewed from a native snapshot. Physical grid-to-CapCut
  dragging and subjective trackpad scrolling still need hands-on verification.
  Drag correction `prototype-20260927T190932713275Z`: native hit testing previously
  used only `visibleRect`, which AppKit can extend beyond a non-clipping card's
  bounds. The native regression reproduced both A and B claiming a press on B.
  Hit testing now requires both bounds and visibleRect; one shared router chooses
  a single owner and captures its provider on mouse-down, clearing ownership before
  a native drag session begins. An AppKit regression selects an image, presses a
  video after a stale image press, and verifies only the video's original URL is
  returned even if SwiftUI replaces the provider before dragging. Native integration
  and strict signing passed; this corrected build is running. End-to-end CapCut
  drop confirmation remains a hands-on check.
  Follow-up `prototype-20260927T055807759699Z` hides the Show/Camera picker labels,
  calls the date picker **Date**, aligns sorting at the right of the lower filter
  row, removes static search/keyboard hints, and removes the footer's fixed-height
  bottom gap. `?` (outside text entry) or the question-mark toolbar button opens
  a transient shortcuts popover, dismissed with Escape/Done/outside click. Browser
  and Settings use en_US display locale for comma-grouped numbers. Native checks
  and strict signature verification passed; the update is open.
  Footer correction `prototype-20260927T060840761999Z` reserves a 22-point control
  row, a 4-point progress track and one 18-point detail row in every state. Errors
  replace GPU details instead of adding another row; the Retry control retains
  its layout slot while hidden/disabled. This keeps the grid/footer boundary fixed
  without extra padding beneath the message. Release build/signature checks passed.
  `prototype-20260927T061134750268Z` additionally shows persistent queue totals in
  the existing footer row: **N left · M indexed**, plus failed-job count when needed.
  Left includes queued/running/error jobs; indexed counts completed queue jobs,
  not separately seeded baseline indexes. SQL counts refresh at most every two
  seconds during ordinary progress and on forced phase boundaries. The per-file
  progress indicator remains separate. All 41 Python tests passed on Linux/macOS,
  including active/error/completed count transitions; signed release is deployed.
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

Packaging creates a new `Mami.app`, refuses to reuse an existing bundle, and uses
the persistent local signing identity described above. It does not fall back to
ad-hoc signing. No App Store account is needed for this local build.

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

## Personal-data backups and Backblaze

- Authoritative personal database: `~/mami-lab/catalog/database/user.sqlite`.
- Versioned snapshots: **`~/mami-lab/backups/catalog/user-state/`** — include this directory
  in Backblaze. Completed JSON receipts record snapshot times; the footer reports
  backup errors. A local snapshot does not indicate that Backblaze has uploaded it.
- Snapshot format: standalone **`.sqlite` database files**, produced with the
  SQLite backup API, rather than SQL-text dumps or raw copies of a live file.
  They contain labels, edit history, saved selection/order, library roots and import
  verification records. Generated media metadata, indexing checkpoints, transcripts,
  images, embeddings and model weights are excluded. Historical full snapshots in
  the parent directory are still supported for restoration.

Database triggers maintain a revision and random change token. Only actual row
changes advance them: opening, browsing, searching, unchanged index seeding and
identical label saves do not create another snapshot. The token prevents a
restored database branch from colliding with an earlier numeric revision.
Manual saves schedule a snapshot after a two-second quiet period; import-driven
changes coalesce to one/minute. Startup and a minute timer retry pending personal
revisions. Indexing changes do not advance this store. Orderly app termination flushes pending work.
After an unexpected exit, the next launch retries any committed but unsnapshotted
revision.

Catalog operations and snapshots share an in-process lock plus a filesystem lock.
Completed snapshots pass `PRAGMA integrity_check` and are synchronized before a
JSON receipt is atomically published. Interrupted outputs without a receipt are
not treated as completed. Every successful changed revision gets a new file;
managed personal snapshots follow the bounded retention policy above. Unknown or
incomplete outputs are preserved. The app reports backup errors and retries while it is running.

To restore, choose a `.sqlite` file named by a completed `.json` receipt, and use
a **new** output directory. This verifies SQLite integrity, expected schema,
record counts and a SHA-256 byte-for-byte copy. It refuses existing destinations:

```bash
~/mami-lab/.venv/bin/python restore_catalog.py \
  ~/mami-lab/backups/catalog/user-state/catalog-IDENTITY-rREVISION-UUID.sqlite \
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
and active-editor policy still apply.

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

### 2026-09-27 memory incident

The overnight native process (PID 16145) reached a confirmed **95.6 GiB physical
footprint**, with 89.7 GiB swapped, after almost eight hours. Its vmmap and stack
sample are retained at `~/mami-lab/memory-incident-20260927-{vmmap,sample}.txt`.
Normal quit timed out; the app and its identified worker tree were terminated.

Reproduced cause: `FileHandle.read(upToCount:)` creates autoreleased NSData backing
buffers. In the long synchronous Photos export task those survived Swift local
scope, retaining approximately all bytes read during verification. A standalone
12 × 64 MiB A/B reproduction grew from 6 MB to 815 MB without chunk pools; with
per-chunk pools it stayed near 11 MB, with identical SHA-256 digests. Evidence and
the reproduction are in `~/mami-lab/memory-incident-20260927/`.

Production hashing now drains an autorelease pool per 4 MiB chunk. PhotoKit fetch
creation, per-asset processing, receipt decoding and download callbacks also have
scoped pools. `--photos-memory-test <file>` exercises the production hash 64 times
inside one deliberately undrained outer pool, verifies all digests and fails if
resident growth exceeds 64 MiB. With the 64 MiB fixture (4 GiB repeated reads),
the corrected app stayed at 17.5–17.7 MB. Native integration and strict signing
checks passed on `prototype-20260927T035230883204Z`, now deployed.

Early supervised real-library importing measured 188, 245, 238 and 312 MiB native
physical footprint over approximately 80 seconds; Photos fetching resumed from
saved history. This is not yet overnight stability validation. A separate one-hour
diagnostic monitor logs native footprint each minute and terminates this exact
build's process tree if it exceeds 8 GiB. Evidence lives in that deployment's
`live-memory-check.json` and `one-hour-memory-watch.jsonl`; the latter is ongoing.
The subsequent `prototype-20260927T035713053696Z` retains that memory fix and
keeps DJI Settings controls enabled during Photos batches. Only an actual camera
import disables the camera controls. A manual camera retry during a Photos batch
is remembered until the importer is free, including with automatic DJI disabled.
Native integration and strict signing passed; this build is now open. The one-hour
monitor was restarted for this exact new build (the prior monitor ends on exit).
Before the browser update, 36 samples over 35.7 minutes reached 654.6 MiB (peak
also 654.6 MiB); this is improved but not month-long stability proof. The browser
build `prototype-20260927T043305729764Z` is now open and has a fresh supervised
one-hour watch in its deployment directory.
That uninterrupted watch completed: 60 samples over 60.08 minutes, all PID 69446;
232.3 MiB start, 339.3 MiB peak, 145.2 MiB end, and 144.3–189.4 MiB during the last
20 samples. Memory was released rather than growing monotonically. Longer uptime
validation is still needed; this does not establish month-long stability. The next
UI build was deployed only after this watch completed.

### Newest-first scheduling

Throughput update `prototype-20260927T063904930325Z`: GPU contention no longer
blocks CPU-only work behind a video waiting for transcription. Speech checkpoints
defer without an error/retry penalty, preserving previews/vectors/audio; the
scheduler continues other visual jobs, revisits deferred speech every 30 seconds,
and waits normally when only speech remains. That build still required three
seconds below 15% utilization; this global gate is superseded by the active-editor
policy below. Frame sampling/transcription quality is unchanged.
Visual inference now uses up to four CPU threads (one inter-op thread). On the
target 8-performance-core M1 Max, eight-frame CPU benchmarks measured median
89–92 ms at one thread, 70 ms at two, 62 ms at four and 56 ms at eight, with minimum
cosine similarity 0.99999964. Four leaves headroom for editing. Evidence is in
`~/mami-lab/benchmarks/index-threads-1790491013903299000/results.json`.
At diagnosis GPU usage was 96% and a 133-frame video was waiting on speech while
9,889 photos remained queued. The ~1.4x gain applies only to embedding inference,
not overall indexing; avoiding head-of-line blocking is the larger scheduling fix.
42 Python tests passed on Linux/macOS, including photo completion during simulated
96% GPU usage and speech resumption without repeating frames or spending retries.
The signed deployment reuses byte-identical tested Swift sources/executable.

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

Work runs out of process at utility QoS with nice(10), four-thread CPU visual
inference and limited FFmpeg threads. Previews and searches do not send scheduling
commands or pause imports/indexing. Manual Pause and active-editor detection remain.
This is resource-conscious scheduling,
not a hard guarantee of zero performance impact. `.background` QoS proved too
restrictive for macOS disk I/O in the first native scan test; utility QoS resumed
the 213 saved file checks and completed the 498-file scan.

`prototype-20260927T064913854347Z` replaces global GPU-load gating entirely:
the native app checks CapCut's exact bundle ID (`com.lemon.lvoverseas`) against
the foreground application plus aggregate keyboard/mouse idle time below 60 seconds.
Only that combination defers speech. CapCut merely running, backgrounded, or left
idle does not block inference. Mami's own GPU usage cannot trigger this policy, and
there is no unconditional three-second quiet delay. The five-second heartbeat
expires after 15 seconds so stale telemetry cannot block indefinitely. In-flight
speech finishes its current chunk; CPU visual work continues when speech defers.
Foreground/recent input is a heuristic, not playback/export detection: long hands-off
playback or background exports may need manual Pause. No keystroke content is read.

`catalog/database/editor-activity.jsonl` records worker policy, activity transitions,
speech deferrals and measured blocking-wait durations. Logs rotate at 1 MiB with
one backup. Forty-four Python tests and native integration passed, including idle
no-sleep/no-global-GPU-read, stale signal expiry, wait resumption and editor detection.
The release is signed and running. The former 12-hour GPU wait history cannot be
reconstructed because those old events were not persisted. Real pinned Whisper
inference and a saved speech checkpoint completed with CapCut open in the retained
`prototype-20260926T092711536655Z/gpu-resume-check` fixture. Finder-launched worker
PATH now includes Homebrew so Whisper can invoke ffmpeg.

The grid refreshes as previews become available. Committed embeddings and speech
segments are loaded by the search worker in the background (polling every five
seconds, plus refresh time), then published as an immutable search snapshot. Results
already on screen are not automatically reranked. Existing experimental indexes
remain the base corpus; no full reindex of those 498 assets is needed.

### Search latency and memory (2026-09-28)

Previously every changed catalog token caused the next query to reopen all
incremental `.npy` files, including during ongoing indexing. The reported query
duration excluded that reload. Search now preloads the catalog before announcing
readiness, keeps unchanged vectors resident, and refreshes snapshots off the query
path. Replacements and removals are detected by asset/stage/ordinal and payload;
unrelated catalog changes do not rebuild the snapshot. Cold startup still reads
the individual vectors once. Failed refreshes retain the previous usable snapshot
and report their error on stderr.

Visual search uses exact vector scoring and per-file maxima before selecting 60
files. No approximate vector database is needed for the current corpus. Speech
uses precomputed word postings, accent folding, and final-word prefix matching.
Text embeddings have a bounded 128-query cache; the encoder warms before readiness.
The UI debounce is 120 ms, and edits invalidate in-flight results immediately.

The indexing process was measured at 22.1 GiB footprint, 21.1 GiB in Metal buffers.
MLX's default free-buffer cache allowance on this Mac was 60.8 GiB. The indexer now
caps that cache at 256 MiB and clears unused buffers after each speech chunk,
including failed chunks; loaded model weights remain resident.
Three real audio chunks through the patched backend held active Metal allocations
at 1,543 MiB, with zero cached buffers after each call and a 2,372 MiB peak.

`benchmark_search.py` measures cold readiness and pipe round-trip latency separately
from the worker's reported query time. Run it with the deployed Python environment,
`--worker`, `--configuration`, `--catalog`, and a new `--output` JSON path. It only
reads the catalog. NumPy-dependent snapshot regression tests run on the Mac and
skip explicitly in minimal Linux environments without NumPy.

Measured on `ludi` with 115,950 frames, while the existing app/indexer remained
open: the old first request took 18.94 s (its UI timer reported only 0.28 s), and
subsequent combined requests were around 140–160 ms. The new worker's 24 combined
requests had 49 ms median / 75 ms p95 / 79 ms maximum round-trip latency; 24 speech
requests had 8 ms median / 12 ms p95. Cold readiness was 20.96 s, including full
catalog preload. These are worker pipe timings, excluding the 120 ms UI debounce
and rendering, and include repeated queries benefiting from the bounded cache.
Results are retained in `prototype-20260928T121526402715Z/search-{before,after}.json`.
The signed release is `prototype-20260928T121844652245Z`; its isolated `ui-check`
passed search/filtering, speech evidence, browsing, selection, playback controls,
and grid-lock checks. The Mac Python checks passed all 52 deployed tests, including
exact visual ranking versus exhaustive scoring, prefix search, cached vector reuse,
replacement/removal, and immutable previous snapshots. Queries entered during
cold startup run when search becomes ready.
The release is running on `ludi`; the old app and both workers exited cleanly.
Activation retained 13,853 completed jobs and 2,872 queued jobs with no errors and
Pause off. macOS reported 23 GiB free after releasing the old process allocations.

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
