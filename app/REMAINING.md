# Open work

Only open items live here; finished work is in git history and the findings in
[CODE_REVIEW.md](CODE_REVIEW.md). Keep failed checks and unexplained reports listed
until they are understood.

## To confirm on the installed build

- [ ] Delete flow by hand: select, trash button / ⌘⌫ / context menu, confirmation,
  preview advancing to the next take; Put Back restoring an item.
- [ ] Photos keep-alive: relaunch after Photos is quit (launch hidden and prompt
  incremental passes are confirmed in the log, 2026-10-02).
- [ ] Click latency feels immediate (user feedback; no remote UI automation).
- [ ] People: user acceptance of naming, group correction and the People filter.

## Unexplained reports (watch for recurrence)

- [ ] The 2026-09-30 click crashes (5×, `MainActor.assumeIsolated` in a button
  action) are explained by R9 and fixed in `5891401`; keep this open until a week
  passes without one. `~/Library/Caches/Mami/app-errors.log` names any new one.
- [ ] Grid thumbnails vanished once after All → Videos → Photos → Videos
  (2026-09-29). Not reproduced, including by `--kind-switch-test` on live copies.
- [ ] After naming four face groups 20–40 s apart, the fourth stayed listed and all
  groups showed 0 faces (2026-09-30); labels on disk were correct. Two races were
  fixed; read `~/Library/Caches/Mami/lane-events.log` after any recurrence.
- [ ] The first-night face worker stayed alive ~8 h after finishing (2026-09-29);
  `--face-lifecycle-test` passes.

## Planned features

- [ ] People: split a mixed face group at a stricter threshold (deselect-and-name
  covers the common case today).
- [ ] People: face boxes in the preview; click a face to name it.
- [ ] Archiving to NFS/HDD: map content identities to multiple locations and stable
  volume IDs; tell offline storage from missing files; keep thumbnails/search local;
  move by copy, flush, fresh hash and durable receipt before removing the local
  original; resumable moves; playback/CapCut export request offline archives. Missing
  files or offline mounts must never clear Photos import history.
- [ ] Reclaim preview sheets and frame artifacts of deleted media (their originals
  are freed; generated leftovers are small).

## Engineering

- [ ] Blocking worker readers still run on Swift's cooperative thread pool; move to
  dedicated threads if pool starvation appears (CODE_REVIEW R10).
