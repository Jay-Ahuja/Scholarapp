# ADR 0002: Carry per-stage realized counts in the existing JSON column, not a new column

- **Status:** Accepted
- **Date:** 2026-05-26

**Context.** Each cost-history sample must record how many professors each stage actually processed, so the estimator divides spend by realized work instead of the requested target (a short run otherwise biases per-professor rates downward). `run_usage` is migration-less (`create_all` can't ALTER it; see ADR 0001), and reading must not break on rows that predate this change.

**Options considered.**
- Option A: add a `stage_counts` JSON column. Pros: clean schema, clean separation. Cons: `create_all` won't add it to an existing `run_usage` table, so any branch dev DB raises "no such column" on read — violating the legacy-graceful requirement; contradicts the model's own no-ALTER rationale.
- Option B: pack counts into the existing `stage_costs` JSON value (`{"costs": {...}, "counts": {...}}`), with legacy rows detected by key absence. Pros: zero schema change, fully backward-compatible by value, consistent with ADR 0001. Cons: the column name `stage_costs` now under-describes its richer value (documented, and contained behind pack/unpack helpers).

**Decision.** Option B. It is the only option that satisfies both the no-migration reality and the "don't break on legacy rows" requirement; the naming wart is documented and contained behind the `pack_stage_usage` / `unpack_stage_usage` helpers in `scholarapp/usage.py`.

**Consequences.** No structural schema change (`scholarapp/db/models.py` gets a docstring update only). All shape knowledge lives in the two pure helpers. Legacy samples (bare `{stage: float}` value) carry no realized-count signal and are excluded per-stage from historical averaging — never read incorrectly, never crashing.
