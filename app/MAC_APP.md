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
- [ ] Investigate reported first DJI connection doing nothing (reconnect offloaded
      successfully). No changes were deployed/disabled at the time. Check mount
      notifications, per-connection identity, busy deferral/status and launch failures;
  preserve the current working offload and source-removal verification rules.
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
  are added; these are prospective fixes, not a confirmed diagnosis of the report.
- All of this is pending native compilation, lab-unavailable acceptance, real idle/
  wake measurements and production migration after the Mac becomes available.
- Local validation: **155 Python tests, 150 passed / 5 Mac-only skips**; Ruff and
  diff whitespace checks pass. Native tests are prepared, not yet run.
