# Mami backup and restore

## Important data

- `~/Media/Originals/` — full-resolution media. Include any other media/exports
  you keep outside this folder and your CapCut projects.
- `~/Library/Application Support/Mami/Personal/` — authoritative `user.sqlite`,
  ownership/migration records and consistent `Backups/user-state/` snapshots.
- `~/Library/Preferences/local.mami.prototype.plist` — app preferences such as
  automatic offload, source-removal and Photos-import settings (managed by UserDefaults).
- CapCut projects currently live in `~/Movies/CapCut/User Data/Projects/`.

Backblaze normally includes user-level Application Support and Preferences. Check
the restore browser and restore one original plus one personal snapshot as a test;
Mami's byte verification confirms the local saved original, not its cloud upload.

## Rebuildable / excluded data

The installed application has a real home at `~/Applications/Mami.app` and its
Python/media tools are bundled. After the recorded lab-unavailable acceptance,
the whole `~/mami-lab/` may be excluded from backup.

Additional exclusions:

- `~/Library/Application Support/Mami/Derived/`
- `~/Library/Application Support/Mami/Models/`
- `~/Library/Caches/Mami/`

`~/Library/Application Support/Mami/Recovery/` contains a reinstallable, signed
previous app, not the personal database. It is optional backup material.

Derived and Models remain persistent for offline use; they are marked excluded
from Apple-managed backups. Backblaze's handling depends on its settings, so use
explicit folder exclusions when needed. Do not exclude all of Application Support
or the Mami parent folder. Do not globally exclude SQLite, JSON or media extensions.

Developer signing material is separate: `~/Library/Application Support/Mami Developer/Signing/`
contains the encrypted recovery archive/certificate; the identity is in
`~/Library/Keychains/Mami-signing.keychain-db`, with its unlock credential in login
Keychain. Preserve developer/Keychain recovery through your normal Mac backup.
These are not runtime dependencies of Mami.

## Restoring

1. Quit Mami. Restore originals to their saved/user-chosen location.
2. Restore the Personal folder. Prefer a verified consistent user-state snapshot
   if the live database copy is unavailable or inconsistent; restore it as
   `Personal/user.sqlite`, retaining the matching ownership identity.
3. Reinstall the signed self-contained app. Do not resurrect an old lab-backed app
   against a newer personal store. macOS permissions may need approval on a new Mac.
4. Generated catalog/search data can be rebuilt. If indexing models were excluded,
   use **Settings → Storage and local models → Download / repair indexing models**.
5. Verify selections, roots and annotations reconnect by content identity. Rebuilt
   previews require originals to be accessible; already retained SSD previews work offline.

The migration preserves the old tree for initial recovery. Once new user changes
exist, its personal database is stale: never restore that old copy over current
Personal data simply to undo an application update.

For an application rollback, quit Mami and reinstall the self-contained recovery
bundle recorded in `Personal/installation-final.json`; keep the current Personal
folder in place. Do not switch back to a pre-migration lab-backed executable.

Backblaze Computer Backup does not cover NAS/network shares or follow a symlink
as a substitute for selecting its physical drive. If originals move to an external
disk, include that disk explicitly; NAS backup requires an appropriate separate setup.
