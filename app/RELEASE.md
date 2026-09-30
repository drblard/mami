# Persistent search / packed previews — release and recovery

Status: **deployed and verified**. Active build: `prototype-20260930T113445153565Z`
(same-moment matches, face index schema v4, per-edit summaries, fatal-error capture
in `~/Library/Caches/Mami/app-errors.log`, launch-time face recheck). Previous:
`prototype-20260930T112112445817Z` at
`~/Applications/.Mami-install-0d0a140571484523b1c149eef2272bca.app`.
Face index schema v2 (origin column) is migrated in place; older face builds refuse
v2 and rebuild into a new directory, so roll back by moving `Derived/Faces` aside.
Personal store: additive `people`, `face_labels`, `people_history` tables (older
builds ignore them; do not delete them when rolling back). Generated face index:
`Derived/Faces/` (rebuildable, excluded from backup). Face model:
`Models/faces/antelopev2` (pinned SHA-256).

## Current installation — 2026-09-29 production-layout migration

- `~/Applications/Mami.app` is a real, immutable signed bundle, with Python, native
  text encoding and media tools included. Original signing identity is preserved.
- Personal authority: `~/Library/Application Support/Mami/Personal/user.sqlite`;
  snapshots: `Personal/Backups/user-state/`. Generated storage: `Mami/Derived/`;
  indexing models: `Mami/Models/`; disposable cache: `~/Library/Caches/Mami/`.
- Verified migration: 17,278 media records, 16,914 Photos verification records,
  selection and roots preserved, personal revision unchanged, 167,643 physical
  frame/vector/audio references checked outside the lab. Originals remain in
  `~/Media/Originals/`. The old lab tree is retained, not current authority.
- 16 Swift / 156 Python tests pass; native UI/catalog/transport, missing personal
  store protection, idle/wake/pause/retry and strict signatures pass. Live idle
  app has zero helpers. Lab-unavailable search, preview, FFmpeg and speech pass.
- Self-contained recovery app: `~/Library/Application Support/Mami/Recovery/Mami-20260929T123229.app`.
  Quit Mami before exchanging bundles; preserve the current Personal folder.
  Never restore stale lab personal data merely to roll back an app update.
- Receipts: `Personal/installation-accepted.json`, `installation-final.json` and
  `migration.json`. Detailed test reports: `~/Library/Caches/MamiMigration/`.
- Runtime minimum for this arm64 build: macOS 26.2, derived from embedded libraries.
- [MAC_APP.md](MAC_APP.md) tracks acceptance; [BACKUP.md](BACKUP.md) gives current
  backup exclusions and recovery. The whole lab may now be excluded from backup.

## Historical scaling release and pre-migration recovery

The remaining sections describe `prototype-20260929T090709977496Z` (`0d43d1f`).
Their lab paths and activation/rollback procedure are historical; use the current
installation and recovery instructions above for the migrated personal store.
Historical gates: [REMAINING.md](REMAINING.md).

## Data and compatibility

- `catalog/database/user.sqlite` remains authoritative. This release does not
  migrate its schema or change personal-backup policy.
- Generated search projection: schema **6**. v5→v6 builds facet tables/triggers in
  one transaction; failure preserves v5 and retries safely. Other incompatible
  generated schemas need a new projection, never reconstruction of personal data.
- Packed vectors: format **4**, immutable generations selected by `current.json`.
  Reader leases protect active generations; compaction defers to active editing.
- Packed previews: atlas format **1**. Publication verifies source-frame state and
  updates generated records transactionally. Raw cache retirement requires exact
  manifest/source/projection parity and verified sheet checksums/signatures.
- Atlas cleanup retains current plus one superseded registered generation.
  Projection references pin older generations until sync catches up. Changed or
  unknown files and unregistered incomplete scratch are retained for review, not
  silently treated as disposable content.
- Preview-maintenance queue schema **2** materializes pending packing/retirement
  work and indexed obsolete generations. Its transactional migration runs before
  activation; existing completed packs are not rescanned on every maintenance tick.

## Release procedure

1. Confirm the user's editing session is finished and check foreground/idle state.
2. Finish native, quality, cold-cache, resource, offline and contention acceptance.
   A fresh process with warm caches does not satisfy the cold-cache gate.
3. Use a newly built/signed immutable bundle. Never change files inside an existing
   signed bundle, including Python bytecode or configuration JSON.
4. Quit the old app normally; confirm imports have finished and owned workers exit.
5. Make a verified SQLite backup of `user.sqlite` into the release audit directory;
   record table counts/digests and retain the live store. Record generated catalog
   checkpoints separately for diagnostic recovery.
6. Catch up `catalog/search-runtime-20260929/search.sqlite` using the new source
   worker, then publish a matching managed vector generation. Verify source identity,
   epoch and revision; compare media counts and representative query results.
7. Activate the signed candidate through a stable `~/Applications/Mami.app` symlink.
   Preserve the old immutable bundles. Verify native readiness, independent workers,
   import/preview progress, strict signature and preservation of personal records.
8. Record exact active/fallback bundle paths and the audit files below; commit/push.

## Rollback without losing personal changes

The original fast-preview build does not understand atlas crop coordinates. After
packing starts, use a **crop-aware fallback build**, not that older executable.
Fallback `prototype-20260929T081741386944Z-fallback` is signed with legacy search
defaults and crop-aware rendering. It passes legacy search, offline packed
scrubbing and stale-reference recovery on the retired-raw-frame fixture.

1. Quit the new app and confirm maintenance workers exit; keep all data in place.
2. Point the stable application symlink at the verified crop-aware fallback and open
   it. Its legacy defaults do not start persistent-search maintenance or atlas backfill.
3. Keep the latest authoritative `user.sqlite` and catalog. Do not restore an old
   personal backup merely to reverse a generated-search change.
4. Reconnect stale preview references by content ID through the current catalog.
   If generated search data needs repair, rebuild into a separate directory from
   current catalog/embeddings, then publish only after validation.

For an actual missing/corrupt personal store, use the verified personal-backup
restore procedure in [README.md](README.md); do not synthesize it from catalog caches.

## Resource decision

The initial ~2 GiB search target was provisional. At 5,830,750 × 768 dimensions,
4-bit weights alone require ~2.09 GiB; scales/biases add ~0.52 GiB before caches.
This format cannot meet a 2 GiB total budget while preserving the accepted quality.
The revised acceptance budget is **3.5 GiB steady search-service footprint**, with
**7 GiB transient allowance during an idle-time generation swap**, on the 64 GiB
target Mac. This decision is accepted on the completed sustained and controlled
contention checks. The 4,096-query kernel-counter
run measured ~3 GiB steady and a 5.57 GiB visual-process lifetime peak during swap,
with p95 40.6 ms. The controlled CapCut/decode/import/inference check also passes.
UI and background inference are measured
separately; macOS shared file-cache residency is not the same as private footprint.

## Activation record

- [x] Performance, quality, recovery, offline and contention gates have evidence.
- [x] Crop-aware fallback validated.
- [x] Personal-state audit/backup verified.
- [x] Active bundle and stable link recorded.
- [x] Live search/preview/signature and isolated real-import checks pass.

Active link: `/Users/ludi/Applications/Mami.app` →
`/Users/ludi/mami-lab/apps/prototype-20260929T090709977496Z/Mami.app`.
Audit and verified personal safety backup: `catalog/release-audit-20260929-final/`.
All six personal-table digests were unchanged; catalog/projection both contain
17,257 distinct assets. Live search returned 60 results in 21–24 ms; 4,467 packs
completed with zero pending packing, retirement or error records.

A checksum-recorded archive of 130 measurement/audit reports is retained on Linux
at `/home/steevel/mami-lab/benchmarks/release-20260929/` (15.4 MB). It excludes
personal databases, originals and generated vector/catalog data; personal safety
backups remain in the audited Mac release directories.

Measurements distinguish disk-cache purge from reboot, synthetic 50× capacity
from the real library, and controlled CapCut/decode/import/inference load from
editing the user's projects. Cold launch-to-visual readiness was 3.19 s; first
cold queries have higher latency than steady-state p95. Originals remain authoritative
for full-resolution playback; disconnected-original checks cover browsing/search/scrub.
