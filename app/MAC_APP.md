# Mac application layout and worker lifecycle

Status: **production storage migrated; self-contained app installed and validated**.
Accepted build: `prototype-20260929T134121462682Z`, installed as a real bundle at
`~/Applications/Mami.app`. Backup/restore instructions: [BACKUP.md](BACKUP.md).

**Mac work resumed:** user confirmed availability; foreground was Brave Browser,
idle over six minutes. Candidate `prototype-20260929T120320841579Z` is isolated.
Native validation passes after correcting the scheduler annotation and macOS path
alias handling. The original signing identity is now in the user's Keychains and
developer Application Support; its unlock credential is in login Keychain. GUI
signing is required by the existing Keychain access policy. The old app was quit
normally, production storage was copied/published, and old lab data is retained.
The real application is installed at `~/Applications/Mami.app`, and live idle
observation confirms zero helper processes. Final UI/lifecycle acceptance passes,
including superseded searches and explicit paused/retry wake-up.

## Production homes

- `~/Applications/Mami.app`: real, self-contained signed bundle, including Python
  runtime, native dependencies and media tools. No symlink into development builds.
- `~/Library/Application Support/Mami/Personal/`: authoritative `user.sqlite`,
  consistent `Backups/user-state/` snapshots and migration/recovery receipts.
- `~/Library/Application Support/Mami/Derived/`: catalog, search generations,
  offline previews, inference artifacts and retained legacy index inputs.
- `~/Library/Application Support/Mami/Models/`: pinned offline models.
- `~/Library/Caches/Mami/`: disposable library/runtime caches.
- `~/Media/Originals/`: existing user-visible original media, unchanged.
- Signing identity: developer Keychain/support storage outside the lab, preserving
  the existing certificate and Photos permission identity.

The immutable native text encoder ships inside the app so interactive search does
not depend on a development artifact. Optional indexing models have an explicit
download/repair action in Settings for recovery after a models-directory loss.

Persistent offline assets belong in Application Support because caches may be
discarded. Personal data is backed up; Derived/Models are rebuildable and can be
excluded. Preferences continue to use UserDefaults. Apple guidance:
https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/FileSystemOverview/FileSystemOverview.html

## Checklist

- [x] Inventory runtime dependencies and persisted lab-path references: Python
      3.12 plus site packages, Homebrew FFmpeg dependency closure, pinned models,
      legacy frame/vector inputs and catalog JSON paths. All 17,264 current media
      URLs point into Originals; physical lab references are derived frame/vector/audio.
- [x] Centralize standard-directory resolution; isolation checks pass on Linux/Mac.
- [x] Separate personal and generated database paths without changing personal meaning/revision.
- [x] Bundle relocatable Python/media dependencies; imports/media decode and strict
      post-run signing pass. Python's executable/framework and resource data are
      separated according to code-signing layout requirements; no Homebrew loads.
- [x] Migrate required models, legacy inputs, previews, vectors and databases with
      verified copies, atomic publication and interruption recovery.
      Data is published and verified: 17,278 media records, 16,914 Photos verification
      rows and 167,643 physical resource references outside the lab. Search rebuilt.
- [x] Keep ownership/missing-store detection and original-media paths intact.
- [x] Move developer signing material out of the lab without replacing its identity.
- [x] Stop completed preview/index/maintenance workers; wake on relevant changes.
- [x] Start visual inference on demand and release it after idle timeout.
- [x] Verify idle → wake → work → idle, paused/retry, shutdown and restart behavior.
- [x] Investigate reported first DJI connection: retained system logs confirm two
      attachments, with the first mount rejected/ejected by loginwindow while locked.
      Timeline/evidence is below. No app change or deliberate pause caused this.
- [x] Native camera tests pass: enable, busy deferral, once-per-connection,
      reconnect and explicit retry. Physical DJI replug acceptance remains a user
      observation; these changes cannot override a volume mount denied by macOS.
- [x] Install a real signed app and validate with the lab unavailable.
      Native search/scrubbing, bundled Python modules, FFmpeg and actual speech
      inference passed while the development directory was renamed/unavailable.
- [x] Verify personal rows/revision, selection meaning, roots, byte-verified resource
      copies and unchanged original locations. Native packed previews/search pass.
- [x] Document exact Backblaze inclusions/exclusions and restore in [BACKUP.md](BACKUP.md).
      A self-contained, signed previous app is retained under `Mami/Recovery/`.
- [x] Record final live installation and acceptance receipts; commit/push this tested milestone.

No legacy tree is deleted during migration. Once the lab-independent acceptance
passes, the whole lab can be excluded from backup. The prior scaling release's
completed checklist is historical; this document tracks the newly requested work.

## Historical local implementation checkpoint (superseded by acceptance below)

- Standard-directory resolution and separate personal-store routing are implemented.
  Ownership checks protect a missing personal store independently of catalog caches.
- The migration copies/verifies files, preserves originals and personal meaning,
  rewrites physical frame/vector/audio references, preserves completed atlas state
  and publishes without clobbering an existing destination. Interrupted staging is
  retained for diagnosis; restarting before publication uses a fresh staging tree.
- Runtime bundling resolves native dependencies into the app, removes external load
  paths and preserves license material. Nested signing and relocation need Mac checks.
- Search starts lazily and has a 60-second idle exit. Preview/index workers have
  a 15-second idle grace; native readiness checks wake them for work. Maintenance
  is one-shot and scheduled from native state, rather than three permanent workers.
- DJI mount notifications, normalized connection identity and visible busy deferral
  are added as robustness improvements. The reported incident was subsequently
  traced to macOS mount refusal, not those possible application-level causes.
- All of this is pending native compilation, lab-unavailable acceptance, real idle/
  wake measurements and production migration after the Mac becomes available.
- Local validation: **155 Python tests, 150 passed / 5 Mac-only skips**; Ruff and
  diff whitespace checks pass. Native tests are prepared, not yet run.

## Resumed native validation

Current build/runtime notes: Python and native modules are embedded and relocated;
the native text encoder is bundled. The dependency-derived deployment minimum for
this local arm64 build is macOS 26.2 (the target Mac is newer). No claim is made
that this bundle supports older macOS versions. Application updates exchange whole
signed bundles atomically; runtime data stays in Application Support.

- 16 Swift tests / 156 Python tests pass on the Mac. Fixed a main-actor helper
  annotation and canonical directory aliases exposed by macOS `/var` paths.
- Packaging initially failed because executable/data files from a full Python
  distribution were placed in nested-code slots. Reworked to a small signed
  launcher, a versioned Python framework, resource data and individually signed
  extension libraries. Removed unused developer entry points. Runtime imports,
  media-tool decode and post-execution signature verification now pass.
- The old app was shut down normally before copying. Originals and original lab
  files remain untouched. Personal meaning, revision and ownership were verified;
  copied physical references resolve in Application Support. The new native path
  loads 100 items and passes browsing/search/crop checks against migrated data.
- Lifecycle acceptance passed against isolated support/media roots, including
  lazy search startup, idle exit and new-media wake-up. No old-lab runtime is being
  used by the installed application's workers.
- Installed-app audit reached **zero helper processes while idle**, with the
  native application remaining open. Fresh media wakes previews/inference/search
  and all helpers return to idle. Missing-personal-store protection and deep
  signature checks pass. Native UI acceptance exposed an old test race between
  manual and debounced searches after lazy startup; the test now awaits the latest
  request rather than asserting against a superseded task. Final UI rerun passed.
- Pausing persists and releases the worker; resuming and retrying an errored job
  wake a sleeping worker and complete successfully. Catalog/transport and missing
  personal-store checks pass. No personal data is silently recreated.
- Backblaze cloud restore and a physical DJI reconnect were not performed by these
  automated checks. Local data integrity and lab-independent operation are verified.
- Final installed build passed a second lab-unavailable run and reached zero helpers
  again; Mami is left running. `Personal/migration.json` records phase `accepted`.
  Selected acceptance logs/reports are archived on Linux under
  `/home/steevel/mami-lab/benchmarks/mac-app-20260929/` without databases/originals.

## DJI incident — retained log diagnosis

2026-09-29, Mac local time (+03:00), inspected read-only while work stayed paused:

- **13:50:17.017:** disk4/disk4s1 appeared; exFAT probe succeeded.
- **13:50:17.177–.203:** loginwindow `CopySLMountApprovalCallback` logged
  `Allow = NO`, `calling DADiskEject`, then `kDAReturnNotPermitted`.
  Disk Arbitration had logged the session as locked at 13:50:09.786.
- **13:50:17.209–.210:** disk4 was removed/ejected successfully without mounting.
- **13:50:27.202:** Disk Arbitration logged the session unlocked.
- **13:53:06.202–.400:** second disk4/disk4s1 appearance and successful mount;
  FSKit identified its path as `/Volumes/Pocket4P`.
- **13:53:13.842:** import journal modification time; new DJI files have saved
  destinations in Originals. Journal schema records files, not connection times.
- **13:53:25.573–.725:** successful unmount/eject following the import.

The system-level refusal explains why Mami could not offload on the first attempt.
The exact reason loginwindow considered the session locked is not established.
Unlock before connecting; retain security settings rather than bypassing mount denial.
Sources: unified logs for diskarbitrationd/loginwindow/fskitd, 13:45–14:05, and the
read-only `~/Media/Originals/.mami-imports/journal.sqlite` inspection.
