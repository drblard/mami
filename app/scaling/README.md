# Isolated scaling experiments

Progress and recovery checklist: [`../SCALING.md`](../SCALING.md).

These tools write only to a specified, dedicated experiment directory. The live
catalog is read through SQLite's backup API into an immutable source snapshot;
original media and deployed bundles are never modified. Each command takes a
run lock, saves atomic progress, and can be rerun after interruption.

On `ludi`, the isolated environment is `~/mami-lab/.venv-scaling` (inherits the
installed inference libraries; new dependencies are installed only in this env).
Use `python -B` to avoid modifying source deployments with bytecode.

Example commands (replace RUN and source paths with the retained run):

```sh
python -B corpus.py --run RUN --catalog ~/mami-lab/catalog/database/catalog.sqlite \
  --index ~/mami-lab/runs/visual-repaired-20260925T180807825809Z \
  --speech ~/mami-lab/runs/speech-20260925T151338631656Z
python -B vector_bench.py --run RUN --scale 1 --build
python -B vector_bench.py --run RUN --scale 50 --build
```

The current experiment has gone beyond the initial IVF-PQ configuration. Read
[`RESULTS.md`](RESULTS.md) before using defaults or interpreting old results.
Additional tools cover Core ML/native startup, graph alternatives, FTS ranking,
paged catalogs, lifecycle checks and preview storage. Exact continuation commands
and the active job are recorded in `../SCALING.md`.

Local recovery/helper tests:

```sh
python -m unittest discover -s app/scaling -p 'test_*.py'
```

Run long commands using `nohup`/`caffeinate` on the Mac with stdout/stderr retained
in RUN. A dropped SSH connection must not terminate an expensive build. A machine
reboot requires rerunning the same command: completed batches/stages are reused.
The progress JSON records PID and stage; check the PID/command before restarting.

Synthetic expansion keeps real vectors for replica zero and applies seeded
perturbations to other replicas, keeping temporal clusters and introducing unique
vectors. It is a capacity stress test, **not** proof of relevance on unseen footage.
Quality is measured separately on real vectors with exact distinct-file baselines.
Process restart timings are reported separately from a true reboot/cold-SSD test.
