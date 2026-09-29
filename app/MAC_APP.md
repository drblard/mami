# Mac application layout and worker lifecycle

Status: **migration in progress**. Existing live release remains in `mami-lab`
until the verified migration and installation steps below complete.

**Mac work paused:** user reports his wife is using it. A one-time process check
found no agent build/test/migration jobs to stop. No production data has moved;
the installed app is unchanged. Continue Linux-only implementation/checks until
the user confirms availability and foreground/idle activity is checked again.

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
- [ ] Centralize standard-directory resolution; isolate test overrides.
- [ ] Separate personal and generated database paths without changing personal data.
- [ ] Bundle relocatable Python/media dependencies; preserve licenses and sign nested code.
- [ ] Migrate required models, legacy inputs, previews, vectors and databases with
      verified copies, atomic publication and interruption recovery.
- [ ] Keep ownership/missing-store detection and original-media paths intact.
- [ ] Move developer signing material out of the lab without replacing its identity.
- [ ] Stop completed preview/index/maintenance workers; wake on relevant changes.
- [ ] Start visual inference on demand and release it after idle timeout.
- [ ] Verify idle → wake → work → idle, paused/retry, shutdown and restart behavior.
- [x] Investigate reported first DJI connection: retained system logs confirm two
      attachments, with the first mount rejected/ejected by loginwindow while locked.
      Timeline/evidence is below. No app change or deliberate pause caused this.
- [ ] Validate the prospective mount-notification/connection-status improvements;
      these cannot override a volume mount denied by macOS.
- [ ] Install a real signed app and validate with the lab unavailable.
- [ ] Verify all personal rows, selections, roots, original bytes and offline previews.
- [ ] Document exact Backblaze inclusions/exclusions, restore and rollback.
- [ ] Commit/push tested milestones and record the final live installation.

No legacy tree is deleted during migration. Once the lab-independent acceptance
passes, the whole lab can be excluded from backup. The prior scaling release's
completed checklist is historical; this document tracks the newly requested work.

## Local implementation checkpoint (not deployed)

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
