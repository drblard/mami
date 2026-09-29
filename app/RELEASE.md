# Persistent search / packed previews — release and recovery

Status: acceptance in progress. Follow [REMAINING.md](REMAINING.md) for live gates.
The active application is still `prototype-20260928T180349767667Z` until activation
is explicitly recorded below. All paths below are on `ludi`, under `~/mami-lab`.

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
target Mac. Sustained measurements and editor-contention checks must verify those
limits before this decision is accepted. The completed 4,096-query kernel-counter
run measured ~3 GiB steady and a 5.57 GiB visual-process lifetime peak during swap,
with p95 40.6 ms. The controlled CapCut/decode/import/inference check also passes.
UI and background inference are measured
separately; macOS shared file-cache residency is not the same as private footprint.

## Activation record

- [x] Performance, quality, recovery, offline and contention gates have evidence.
- [x] Crop-aware fallback validated.
- [ ] Personal-state audit/backup verified.
- [ ] Active bundle and stable link recorded.
- [ ] Live import/search/preview/signature checks pass.
