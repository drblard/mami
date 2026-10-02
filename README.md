# Mami

A local-first macOS photo and video library for the family, in SwiftUI with
bundled Python workers for visual search, Romanian transcription, face recognition
and verified imports from DJI cameras and iCloud Photos.

## Where to read

- [`app/README.md`](app/README.md): what the app does, how it works, where data
  lives, development and releasing.
- [`app/RELEASE.md`](app/RELEASE.md): installed build, release procedure, rollback
  and store compatibility.
- [`app/REMAINING.md`](app/REMAINING.md): open work and unexplained reports.
- [`app/CODE_REVIEW.md`](app/CODE_REVIEW.md): review findings and their fixes.
- [`app/PIPELINE.md`](app/PIPELINE.md): import → preview → AI → faces readiness contract.
- [`app/FACES.md`](app/FACES.md): people recognition design and measurements.
- [`app/SCALING.md`](app/SCALING.md): 50× search design decisions and acceptance evidence.
- [`app/BACKUP.md`](app/BACKUP.md): what to back up and how to restore.

Development happens on Linux; the Mac (`ssh ludi`) builds, tests and runs the app.

```bash
cd app
python3 verify.py --python-only      # Python + MamiCore Swift tests
mise x ruff@0.16.9 -- ruff check ..   # lint
python3 release.py --install          # build, test, sign and install on the Mac
```

## Data boundaries

- Originals: `~/Media/Originals/`. The app never deletes them except through
  verified card cleanup and the confirmed Move to Trash.
- Personal data: `~/Library/Application Support/Mami/Personal/user.sqlite`, the
  only authoritative store; everything under `Mami/Derived/` is rebuildable.
- Build snapshots: `~/mami-lab/apps/` on the Mac (immutable once deployed).
