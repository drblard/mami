# Mami

A local-first macOS photo/video manager built in SwiftUI, with Python workers
for visual search, Romanian transcription, and background indexing.

## Project map

- [`app/`](app/README.md): native app, persistent SQLite catalog, automatic
  snapshots, search/index workers, deployment and restore tools.
- [`feasibility/`](feasibility/README.md): pinned-model experiments and evaluation
  tools; [`FINDINGS.md`](feasibility/FINDINGS.md) records search-quality results.

Development takes place on Linux. Swift builds and media experiments run on the
Mac via the `mami-mac` SSH alias. See the app README for deployment instructions.

## Development checks

```bash
python -m unittest discover -s app -p 'test_*.py'
python -m unittest discover -s feasibility -p 'test_*.py'
```

Native integration checks run from a deployed Mac bundle. They cover catalog
restore/rollback, UI/search/playback controls, and background scanning controls.
All Mac test outputs use new directories and are preserved.

## Data boundaries

- Originals: `~/Media/Originals`, read-only to current app tooling.
- Development builds, experimental indexes and generated artifacts: `~/mami-lab`.
- Live catalog: `~/mami-lab/catalog/database/catalog.sqlite`.
- Versioned catalog snapshots: `~/mami-lab/backups/catalog`.
- Git contains source, tests, configuration templates, and project documentation.
  Media, databases, model weights, caches and generated run outputs are excluded.

Catalog snapshots supplement the media backup; they are not copies of originals
or cached search artifacts. Existing Mac files and previous results are preserved.

## Current status

Browsing, cached scrubbing, local visual/spoken-word search, playback controls,
manual labels, content-based relocation, and change-aware catalog backups are
implemented. Automatic scanning/indexing with pause/resume and per-unit crash
recovery is undergoing Mac integration checks. Visual search remains approximate.
Verified device imports, collections, named people, and archive management are
still planned.
