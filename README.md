# Mami

A local-first macOS photo/video manager built in SwiftUI, with Python workers
for visual search, Romanian transcription, and background indexing.

## Project map

- **[Live scaling checklist](app/SCALING.md)**: 50× library architecture,
  measurements, migration progress, and interruption/reboot recovery instructions.

- [`app/`](app/README.md): native app, persistent SQLite catalog, automatic
  snapshots, search/index workers, deployment and restore tools.
- [`feasibility/`](feasibility/README.md): pinned-model experiments and evaluation
  tools; [`FINDINGS.md`](feasibility/FINDINGS.md) records search-quality results.

Development takes place on Linux. Swift builds and media experiments run on the
Mac via the `ludi` SSH alias. See the app README for deployment instructions.

## Development checks

```bash
python -m unittest discover -s app -p 'test_*.py'
python -m unittest discover -s feasibility -p 'test_*.py'
uv tool run ruff==0.16.9 check app
```

On the Mac, `~/mami-lab/.venv/bin/python -B verify.py` in a deployed source directory
runs bounded Swift Testing and Python checks. Swift Testing is a pinned test-only
dependency for the Command Line Tools environment; versions are locked in
`app/Package.resolved`. Check CapCut activity before Mac-side work.

Native integration checks run from a deployed Mac bundle. They cover catalog
restore/rollback, UI/search/playback controls, and background scanning controls.
All Mac test outputs use new directories and are preserved.

## Data boundaries

- Originals: `~/Media/Originals`, read-only to current app tooling.
- Development builds, experimental indexes and generated artifacts: `~/mami-lab`.
- Live catalog: `~/mami-lab/catalog/database/catalog.sqlite`.
- Personal edits/configuration/import ledger: `~/mami-lab/catalog/database/user.sqlite`
  (migration candidate is being validated; see the live checklist).
- Automatic personal-data backups: `~/mami-lab/backups/catalog/user-state`.
- Retained historical full-catalog snapshots: `~/mami-lab/backups/catalog`.
- Git contains source, tests, configuration templates, and project documentation.
  Media, databases, model weights, caches and generated run outputs are excluded.

Personal-data backups supplement the media backup. Generated catalog/search/preview
data is rebuildable and does not belong in automatic backups. The approved cleanup
of redundant historical catalog snapshots is audited in the scaling checklist.

## Current status

Browsing, cached scrubbing, local visual/spoken-word search, playback controls,
manual labels, content-based relocation, and change-aware catalog backups are
implemented. Automatic scanning with pause/resume passed Mac integration checks;
incremental CPU indexing and live search refresh passed real-media checks. GPU
transcription yields while CapCut is running. Visual search remains approximate.
Verified, resumable imports from mounted camera cards and media folders are
implemented. Direct iPhone transfer, collections, named people, and archive
management are still planned.
