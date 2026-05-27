# ADR 0001: Persist run cost history in a new table, not new columns on `runs`

- **Status:** Accepted
- **Date:** 2026-05-26

**Context.** Historical refinement of the pre-run cost estimate requires reading prior runs' actual cost. Nothing was persisted before this feature. The project has no migration framework — schema is created via `Base.metadata.create_all` in an idempotent `init_db()` (`scholarapp/db/session.py`), which creates missing tables but does not ALTER existing ones.

**Options considered.**
- Option A: add columns (`total_usd`, `stage_costs`) to the existing `Run` table. Pros: cost lives with the run. Cons: `create_all` will not add columns to an existing `scholar.db`, so every pre-existing install silently lacks the columns and breaks on read/write — the migration gap.
- Option B: add a new `RunUsage` table keyed by `run_id`. Pros: `create_all` does create missing tables on existing DBs, so it works on fresh and existing installs with zero migration code; isolates the new concern; FK back to `runs` preserves traceability. Cons: one extra table.

**Decision.** Option B. The migration-less schema model makes new-table creation safe and column addition unsafe; isolating cost history also keeps the hot `Run` row unchanged.

**Consequences.** `RunUsage` is created automatically by the existing `init_db()` on first session — no migration step. Estimation reads via `repo.list_recent_run_usage`; the estimator (`scholarapp/usage.py`) never imports `db`. Future per-stage analytics extend the `stage_costs` JSON without a schema change.
