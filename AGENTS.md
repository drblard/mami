# Mami engineering standards

Read `app/SCALING.md` and `app/CODE_REVIEW.md` before continuing substantial work.
Keep their checklists current, including failed checks and the live deployment.

- Prefer simple, cohesive components. Separate policy from persistence, process
  control and UI; introduce abstractions for actual responsibilities, not speculation.
- Give business rules and resource limits descriptive names. Keep defaults in one
  production location. Ordinary loop counters and explicit test fixtures need not
  be turned into global constants.
- Make clocks and relevant dependencies controllable in tests. Assert exact
  outcomes and failure/recovery behavior; avoid loose tolerances that hide defects.
- `user.sqlite` is authoritative personal data. Generated catalog, search and
  preview state must not cause personal backups. Migrations must verify data and
  publish atomically. Never silently reconstruct missing personal data from stale caches.
- Tests and experiments must use isolated catalogs, backups and output directories.
  An overridden library directory must not write backups into the live library.
- Keep production configuration independent of feasibility/benchmark programs.
  Treat worker cancellation, EOF, timeouts, errors and shutdown as explicit behavior.
- Use parameterized SQL and preserve transaction boundaries. Propagate actionable
  failures; intentional best-effort cleanup should preserve the original error.
- Preserve original media and unfamiliar user work. Source deletion is limited to
  the existing explicitly selected, verified import-cleanup flow.
- Use the established style and meaningful checks appropriate to each change.
  Prefer focused changes with tests over broad cosmetic rewrites or premature frameworks.
- Build/test Swift on `ludi`. Deploy new immutable signed bundles; never patch a
  live signed bundle. Record migrations and rollback/restore requirements.
- Check foreground application/idle activity before Mac builds, tests or benchmarks.
  Pause that work while the user is actively editing in CapCut.
- Import visibility/playback and preview generation take priority over AI indexing.
  Follow the readiness/queue contract in `app/PIPELINE.md`.
- Distinguish measured performance from targets, warm caches from cold startup,
  and synthetic capacity fixtures from real search-quality evidence.
- Commit and push tested milestones as work progresses, as requested by the user.
  Keep incomplete rollout/acceptance items explicit in the checkpoint documents.
