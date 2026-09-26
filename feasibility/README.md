# Mami search feasibility

Python experiments run on the M1 Max over SSH. This is an evaluation harness,
not the production Swift app.

## Storage boundaries

- Originals now live under `~/Media/Originals` (read-only inputs). Historical
  inventories reference `~/Desktop/Video`; use the app's verified relocation
  report when reconnecting those existing experiment indexes.
- Experiment: `~/mami-lab`.
- Python: `~/mami-lab/.venv/bin/python`.
- Models/caches: `~/mami-lab/cache`.
- Every command creates a uniquely named output directory under `runs`.
- Code deployments use new filenames and exclusive creation, not overwrite.
- No deletion, cleanup, source relocation, or Photos/iCloud operations.

Homebrew provides `ffmpeg` and `uv`. Brew commands use
`HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1`.
Model repositories and revisions are pinned in `lab.py`.

## Commands

Run on the Mac with `/opt/homebrew/bin` on `PATH`:

```bash
caffeinate -i ~/mami-lab/.venv/bin/python <deployed-lab.py> transcribe \
  --inventory <inventory.json> --limit 3
caffeinate -i ~/mami-lab/.venv/bin/python <deployed-lab.py> visual \
  --inventory <inventory.json> --limit 5
caffeinate -i ~/mami-lab/.venv/bin/python <deployed-lab.py> visual \
  --inventory <inventory.json>
caffeinate -i ~/mami-lab/.venv/bin/python <deployed-lab.py> search \
  --index <visual-run-directory> --query 'milking goats'
caffeinate -i ~/mami-lab/.venv/bin/python <deployed-lab.py> transcribe \
  --inventory <inventory.json> --task translate --limit 3
caffeinate -i ~/mami-lab/.venv-vlm/bin/python <deployed-lab.py> caption \
  --inventory <inventory.json> --limit 3
```

The initial inventory JSON contains `root` and a `files` list with relative
`path`, `kind`, `bytes`, and FFprobe `probe` results. Existing metadata errors
are skipped. Search emits JSON and a self-contained HTML contact sheet.

Each run records its Python PID. For new jobs, use the detached launcher from
Linux instead of holding a foreground SSH connection:

```bash
python feasibility/launch.py --label example -- \
  /Users/ludi/mami-lab/.venv/bin/python /absolute/deployed-lab.py <arguments>
```

The launcher creates an exclusive directory under `~/mami-lab/jobs` containing
the command, supervisor PID, child PID, output log, and completion record. A
detached session survives SSH loss. `caffeinate -is` guards the command lifetime
(system-sleep prevention requires AC power). Keep the lid open. No persistent
power configuration is changed. Optional `--after-pid PID` waits for an existing
Mac process to exit before starting inference, to avoid loading two large models
at once. This is an ordering dependency, not a check that the prior job succeeded.
Automatic checkpoint resumption is not yet implemented.

## Evaluation caveats

- Original visual baseline: SigLIP 2, sampled frames every ten seconds. New
  runs default to one-second intervals, including partial final intervals.
  Both may miss sub-second actions and cannot establish motion from one frame.
  The same model is used to isolate the effect of denser sampling.
- Visual runs now save per-file sample manifests and embedding checkpoints
  under `checkpoints/`, with progress logged after every file. Completed files
  remain recoverable if a job exits early; automatic resumption is not yet wired.

### Caption retrieval diagnostic

`lab.py caption-search --captions CAPTION_RUN/summary.json --source-results
SEARCH_RUN/results.json --visual-index VISUAL_RUN` compares visual similarity
with multilingual E5 retrieval over the existing query-blind descriptions.
Use the original search results that selected the captions and their original
visual index. It reconstructs the same round-robin candidate selection.
Both columns rank the same moments; captions have multi-frame context while
the visual baseline uses a single frame. This is a small development-set
diagnostic, not full-library evaluation. Romanian queries are embedded directly.

`python feasibility/evaluate_caption_search.py --results RESULTS_JSON --source
SOURCE_RESULTS_JSON` reports ranks of previously labeled positive moments.
It matches path plus timestamp, leaves unjudged moments unjudged, and does not
transfer feeding labels to bringing-food queries. Both methods currently return
nearest neighbors for absent activities too.
- Similarity scores are not calibrated confidence or presence probabilities.
  The baseline always returns nearest neighbors, even for absent concepts.
- Romanian and English queries need comparison against human-labeled matches.
- Speech baseline: Whisper large-v3-turbo via MLX, forced Romanian. Code-switching,
  language detection, background music hallucinations, and proper names need
  evaluation. All timestamped segments and word metadata are retained.
- English speech translation uses full Whisper large-v3, not Turbo (which was
  not trained for translation). It is a separate audio-based pass, not a literal
  translation of the stored Romanian transcript. English segment boundaries can
  differ from Romanian; translated word-level timestamps are not requested.
- Caption trial: Qwen2.5-VL-7B-Instruct 4-bit via `mlx-vlm==0.7.3` in a separate
  environment. Four chronological frames cover 7.5 seconds near each clip's
  midpoint. This tests multi-image inference, not a native temporal video encoder.
- Photo EXIF, HDR/tone mapping, Live Photo pairing, and slow-motion semantics
  need separate validation before production import support.
- Model initialization/download time and warm inference must be distinguished
  in performance reporting.

## Bounded query-blind diagnostic

The 16-moment test selects candidates round-robin from the contextual search
results and deduplicates identical file/timestamp pairs. The 32B captioner sees
only frames and a generic description prompt, never the retrieval query or user
labels. Descriptions cover the retrieved moment (not an unrelated clip midpoint).
The source results and rank labels are recorded in `contextual-labels.json`.

Judge whether descriptions explicitly capture the relevant activity/object,
miss it, or invent it. Distinguish event relevance (a clip from the plum-picking
outing) from visible evidence in these sampled frames. This development-set
diagnostic cannot establish full-library recall or replace an independent test.
Do not hard-code goat/plum rules from this set as a general search solution.
