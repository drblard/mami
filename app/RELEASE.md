# Releases, rollback and compatibility

## Current installation

| | |
|---|---|
| Installed | `~/Applications/Mami.app` (a real signed bundle, not a link) |
| Build | `prototype-20261002T110607500980Z`, revision `0c4e104`, installed 2026-10-02 with `release.py --install` |
| Previous | `~/Applications/.Mami-install-b9e2bf49aade447fa2d3254a92eef775.app` (`prototype-20261002T101500180033Z`, `7ca583c`) |
| Source snapshots | `~/mami-lab/apps/<build>/` on `ludi` (immutable; `REVISION` file from 2026-10-02 on) |

Check the installed revision: `plutil -extract MamiSourceRevision raw ~/Applications/Mami.app/Contents/Info.plist`
(bundles before 2026-10-02 have no revision key; the table above records theirs).

## Releasing

`app/release.py` runs from the Linux checkout:

1. Refuses uncommitted changes; runs `verify.py --python-only` locally.
2. Checks the Mac is not being used for CapCut editing.
3. Deploys a new snapshot (`deploy.py`) stamped with the git revision.
4. Builds (`swift build -c release`) and runs the full Swift and Python suites on the Mac.
5. Packages and signs through the logged-in GUI session (the signing keychain is
   only unlocked there), reusing the installed bundle's Python/media runtime and
   native text encoder (`--runtime-from` to choose another verified bundle).
6. Runs `check_mac_installation.py` on isolated data: catalog, transport and
   worker-exit races, media deletion through the real Trash, worker lifecycles,
   missing-personal-store protection, strict signature.
7. With `--install`: requires two minutes without input (`--now` overrides) and no
   running import, quits Mami normally (never forcibly), installs atomically
   (`install_production.py`, keeping the displaced bundle), relaunches, verifies the
   installed revision and that personal tables are unchanged (`personal_audit.py`).

After installing, update the table above. Never modify files inside a signed
bundle, including an unreleased snapshot's Mami.app; build a new snapshot instead.

## Rollback

1. Quit Mami. Keep `Personal/` as it is: never restore an old personal backup just
   to undo an app update.
2. Install the previous bundle:
   `~/mami-lab/.venv/bin/python <snapshot>/install_production.py --bundle <previous .Mami-install-….app> --receipt <new path>.json`
   then `open ~/Applications/Mami.app`.
3. Check the compatibility notes below for the generated data the older build expects.

A genuinely missing or damaged personal store is a restore, not a rollback:
[BACKUP.md](BACKUP.md).

## Compatibility

| Store | Current | Older builds |
|---|---|---|
| Personal `user.sqlite` | v1 + additive tables: `people`, `face_labels`, `people_history` (2026-09-29), `deleted_media` (2026-10-02) | Ignore unknown tables; keep them. Builds before 2026-10-02 do not skip deleted content during imports. |
| Generated catalog | `user_version` ≤ 2, index queue schema 2 | A newer catalog is refused, never rewritten. |
| Search projection | schema 6 (v5→v6 migrates in one transaction) | Rebuild into a new directory if refused. |
| Packed vectors | format 4, immutable generations via `current.json` | — |
| Preview sheets | atlas format 1, maintenance queue schema 2 | — |
| Face index | schema v4 (`Derived/Faces/`) | Older face builds refuse it: move `Derived/Faces` aside to rebuild. |

Generated stores can always be rebuilt from originals and the personal store;
personal data is never reconstructed from generated caches.

## Install history

| Date | Build | Revision | Notes |
|---|---|---|---|
| 2026-10-02 | `prototype-20261002T110607500980Z` | `0c4e104` | Search warms on focus and stays 10 min; worker shutdown fix |
| 2026-10-02 | `prototype-20261002T101500180033Z` | `7ca583c` | Immediate card-image clicks; real click latency log |
| 2026-10-02 | `prototype-20261002T094634040401Z` | `9f7b733` | Swift 6 mode, Observation, release script; first `release.py` install (all checks passed, personal tables unchanged) |
| 2026-10-02 | `prototype-20261002T090035678054Z` | `2281c35` | Move to Trash, Photos kept syncing and change-history passes, responsive grid, deep-pass fixes |
| 2026-10-01 | `prototype-20261001T083445972243Z` | `5891401` | Fix for the click crash (worker exit-status race) |
| 2026-09-30 | `prototype-20260930T113445153565Z` | `62f846f` | Same-moment face matches, fatal-error capture |
| 2026-09-29 | `prototype-20260929T134121462682Z` | — | Production layout: Application Support storage, self-contained bundle |

Earlier releases and their acceptance evidence are recorded in git history
(`app/RELEASE.md` up to `2281c35`).
