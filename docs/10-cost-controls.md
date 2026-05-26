# 10 — Cost controls: pre-flight estimate + `--budget` ceiling

This doc covers the cost-controls feature added to `scholar run`: a pre-flight
cost estimate shown before any paid work, an optional confirmation gate, and an
optional hard USD ceiling (`--budget` / `RUN_MAX_USD`) that stops a run cleanly
before it overspends. Read this before touching the estimate gate, the budget
checks, or the `RunUsage` history.

## 1. Overview

Before discovery/matching/drafting begins, `scholar run` estimates what the run
will cost and (on an interactive terminal) asks the user to confirm. An optional
budget ceiling refuses to start a run whose estimate already exceeds it, and
stops a running pipeline at the next stage boundary once recorded spend reaches
the ceiling — delivering whatever drafted so far. Each completed run records its
realized per-stage cost so future estimates can be sharpened from real history.

## 2. Why it exists

Before this feature, cost was visible only *after* a run finished, via the
end-of-run Claude usage table (`scholarapp/usage.py`, see also docs/01 "Cost
observability"). There was no way to see projected spend before paying for it
and no spend ceiling — a large professor count, or a runaway top-up loop, could
quietly cost more than intended. Cost controls turn that retrospective-only
visibility into a pre-flight estimate plus an enforceable ceiling.

## 3. Architecture

Everything is wired in `scholarapp/cli.py`'s `run` command (`_run_pipeline`).
The flow:

1. **Estimate + confirm gate sits AFTER Parse, BEFORE Discover.** The estimate
   scales with the professor `count`, but `count` is LLM-extracted from
   `prompt.md` by `ingestion.parse_prompt` — it is not known until parsing runs.
   So the two cheap ingestion calls (resume parse, prompt parse) are always
   incurred first; the gate then runs in the "Cost estimate" section before any
   discovery/matching/drafting spend. `usage.estimate_run_cost(count, history)`
   produces the `CostEstimate`; `ui.cost_estimate_panel(estimate)` renders it.
2. **Refuse-to-start.** If the effective budget is below the estimated total,
   the CLI calls `ui.error(...)` and raises `typer.Exit(code=1)` — no new
   exception type, just the orchestrator's existing error model. Nothing past
   the two ingestion calls is spent.
3. **Confirm (opt-out).** On an interactive TTY and absent `--yes`, the CLI
   prompts via `typer.confirm(...)`. Declining returns cleanly (no error).
   Non-interactive sessions never prompt.
4. **Budget enforcement at stage / top-up-pass / draft boundaries.** The helper
   `_over_budget(effective_budget, tracker)` compares `tracker.total_cost_usd`
   (already-recorded spend) against the ceiling with `>=`. It is checked
   *between* stages: pre-discovery, pre-matching, before each top-up pass, and
   pre-drafting. This is **best-effort**: a single stage can overshoot the
   ceiling internally because a Claude call is never aborted mid-flight.
5. **Partial delivery on stop.** Hitting the ceiling sets `stop_reason =
   "budget"` and falls through to the existing partial-delivery path — it drafts
   whatever professors already have a match (possibly zero) and ends in
   `RunStatus.REVIEW` like any normal short run. The end-of-run summary then
   shows `ui.budget_stop_notice(budget_usd, spent_usd)`.
6. **Persistence of actual cost AND realized size.** At each terminal drafting
   path, `_persist_run_usage(run_id, count, drafted, tracker)` writes one
   `RunUsage` row via `repo.add_run_usage`. Besides each stage's realized USD
   cost, it records how many professors each stage *actually* processed —
   discovery = professors persisted for the run, matching = professors with
   `>=1` matched project, drafting = professors actually drafted — so future
   estimates divide spend by the size that produced it rather than the requested
   `count`. `_recent_cost_history()` reads those rows back through
   `repo.list_recent_run_usage`, unpacks each row's JSON via
   `usage.unpack_stage_usage`, and maps them to `RunCostSample`s (carrying both
   `stage_costs` and `stage_counts`) that feed the next run's estimate.

The cost engine (`scholarapp/usage.py`) is intentionally **DB-free and pure** —
the CLI owns all ORM translation and persistence. See
[adr/0001-cost-history-table.md](adr/0001-cost-history-table.md) for why cost
history lives in a new `RunUsage` table rather than new columns on `runs`.

## 4. Key files

| File | Role in this feature |
|---|---|
| `scholarapp/cli.py` | `run` command: `--budget`/`--yes` options, estimate+confirm gate, `_over_budget`, `_recent_cost_history`, `_persist_run_usage`, budget-stop wiring |
| `scholarapp/usage.py` | `STAGES`, `estimate_run_cost`, `stage_costs_from_tracker`, `pack_stage_usage`/`unpack_stage_usage`, `StageEstimate`/`CostEstimate`/`RunCostSample` dataclasses (plus the pre-existing `PRICING`/`UsageTracker`) |
| `scholarapp/config.py` | `Settings.run_max_usd`, `_env_float`, `RUN_MAX_USD` env parsing |
| `scholarapp/db/models.py` | `RunUsage` table |
| `scholarapp/db/repo.py` | `add_run_usage`, `list_recent_run_usage` |
| `scholarapp/db/session.py` | idempotent `init_db()` / `create_all` that auto-creates `run_usage` |
| `scholarapp/ui.py` | `cost_estimate_panel`, `budget_stop_notice` |

## 5. Public interface

### CLI options (on `scholar run`)

| Option | Type | Default | Meaning |
|---|---|---|---|
| `--budget` | float USD | none | Hard ceiling for this run; refuses to start if estimate exceeds it, stops cleanly before exceeding it mid-run. Overrides `RUN_MAX_USD`. |
| `--yes` / `-y` | flag | off | Skip the pre-run confirmation prompt. |

### Environment variable

- `RUN_MAX_USD` — optional default ceiling, parsed by `config._env_float` into
  `Settings.run_max_usd`. Unset (or empty) means `None` (no ceiling). `--budget`
  overrides it for a single run.

### Estimation / persistence functions

- `usage.estimate_run_cost(count: int, history: list[RunCostSample] | None) -> CostEstimate`
  — pure; never raises on empty/short history.
- `usage.stage_costs_from_tracker(tracker: UsageTracker) -> dict[str, float]`
  — aggregates the live tracker into per-`STAGES` USD totals (every `STAGES`
  key present, ingestion labels excluded).
- `repo.add_run_usage(...)` / `repo.list_recent_run_usage(...)` — write/read the
  `RunUsage` history rows.

### Shapes and vocabulary

- `STAGES = ("discovery", "matching", "drafting")` — the shared per-stage cost
  vocabulary; also the keys of `RunUsage.stage_costs`. Ingestion labels
  (`parse_resume`, `parse_prompt`) and `pick_topics` map to no per-professor
  stage (`pick_topics` is folded into a fixed per-run add-on in the static
  estimate) — see `_LABEL_TO_STAGE` in `usage.py`.
- `CostEstimate(count, total_usd, stages: list[StageEstimate], basis, sample_size)`
  — `total_usd == sum(s.cost_usd for s in stages)`; `basis` is `"historical"` or
  `"static"`; `sample_size` is the number of runs averaged (0 when static).
- `StageEstimate(stage, cost_usd)` — one per `STAGES` key, in `STAGES` order.
- `RunCostSample(count, stage_costs: dict[str, float], stage_counts: dict[str, int])`
  — one prior run's realized cost, the estimator's history input. `stage_counts`
  is the realized professor count each stage processed; the rate math divides
  `stage_costs[stage]` by `stage_counts[stage]`, not by `count`. `count` is
  retained for reference only. Legacy samples carry an empty `stage_counts` and
  are excluded per-stage from averaging.

## 6. State and data

The `RunUsage` table (`scholarapp/db/models.py`) stores one row per completed
run:

| Column | Notes |
|---|---|
| `id` | autoincrement PK |
| `run_id` | FK to `runs.id`, indexed |
| `created_at` | timestamp |
| `count` | professors requested for that run (reference only — NOT used for rate math) |
| `total_usd` | denormalized sum of the per-stage costs (cheap newest-first listing) |
| `stage_costs` | JSON; a tagged value `{"costs": {stage: float}, "counts": {stage: int}}` keyed by `usage.STAGES` |

The `stage_costs` column now carries BOTH the per-stage USD costs and the
per-stage *realized* professor counts (how many professors each stage actually
processed) inside one JSON value. Because the project has no migrations
(`create_all` cannot ALTER a table to add a column), the realized counts ride in
the existing column rather than a new one — see
[adr/0002-per-stage-realized-counts.md](adr/0002-per-stage-realized-counts.md).
`usage.pack_stage_usage` writes the tagged shape; `usage.unpack_stage_usage`
reads it back into `(stage_costs, stage_counts)`. **Legacy rows** persisted
before this change are the bare `{stage: float}` cost map (no `"costs"`/`"counts"`
keys); `unpack_stage_usage` detects them by key absence and returns them as
costs-only with an empty counts dict, so old history never crashes the reader.

`total_usd` is summed from the per-stage costs (not `tracker.total_cost_usd`) so
it stays consistent with the stored `stage_costs`, which deliberately exclude
one-off ingestion parsing.

The table is created automatically by the idempotent `Base.metadata.create_all`
in `init_db()` (`scholarapp/db/session.py`) — no migration step, and it appears
on both fresh and pre-existing `scholar.db` files. See
[adr/0001-cost-history-table.md](adr/0001-cost-history-table.md).

**How history feeds estimates.** `estimate_run_cost` computes each stage's rate
*independently* from its *realized* counts: it pools a stage's spend and its
realized professor count over the samples that recorded a positive count for
that stage, computing `rate[stage] = pooled stage cost / pooled stage realized
count`, then scales by the current `count`. Dividing by the realized per-stage
count — not the requested `count` — is the fix this change introduces: a short,
partial, or budget-stopped run processes fewer professors than requested, so
dividing its spend by `count` would understate the per-professor rate and drag
future estimates downward. Using realized counts, a half-completed run reports
the same per-professor rate as a full one. A stage switches to this *historical*
basis once at least `_MIN_HISTORY_SAMPLES` (currently 3) count-bearing samples
exist for it; otherwise that stage falls back to the *static* PRICING-derived
guess (`_STATIC_PER_PROF_USD` per professor plus a fixed per-run
`_STATIC_FIXED_USD` discovery add-on for `pick_topics`). Legacy samples (empty
`stage_counts`) contribute no realized-count signal and are simply absent from
every stage's pool, so a history mixing new and legacy rows estimates without
error. If no stage has enough count-bearing history, the whole estimate is
static.

## 7. Configuration

| Knob | Where | Default | Behavior |
|---|---|---|---|
| `RUN_MAX_USD` | env var (`config.py`) | none (`None`) | Sticky default ceiling. Empty/unset means no ceiling. Must be finite and non-negative; a malformed value hard-stops `scholar run`. |
| `--budget` | `scholar run` option | none | Per-run ceiling; **overrides** `RUN_MAX_USD` when given. Must be finite and non-negative; a malformed value hard-stops the run. |
| `--yes` / `-y` | `scholar run` option | off | Skip the confirmation prompt. |

Effective budget = `--budget` if provided, else `settings.run_max_usd`, else
`None` (unbounded; the estimate is then purely informational).

**Valid budget values.** The effective ceiling must be either unset (`None`,
meaning no ceiling) or a **finite, non-negative** dollar amount. Specifically:

- **Unset / empty** → `None`, unbounded (unchanged).
- **Exactly `0`** → a *valid* ceiling, not an error. Since any non-trivial
  estimate is `> 0`, a zero budget simply trips the existing refuse-to-start
  check below — it refuses to run anything that would cost money.
- **Non-finite (`NaN`, `inf`, `-inf`) or negative** → rejected as malformed. The
  run hard-stops *before any paid work* (no discovery, no drafts, no `run_usage`
  row), raising `ConfigError` (`scholarapp/errors.py`) which `_run_safely`
  renders as `ui.error` + exit code 1.

The check lives at the single convergence point in `cli.py`'s `run` body, right
after `effective_budget = budget if budget is not None else settings.run_max_usd`
and before the refuse-to-start check, so `--budget` and `RUN_MAX_USD` are policed
identically regardless of source.

**Confirmation behavior.** The prompt only appears when `sys.stdin.isatty()` is
true and `--yes` was not passed. Non-interactive sessions (CI, the test runner,
piped input) proceed *without* prompting so they never hang — the budget still
protects them.

## 8. Gotchas and constraints

- **Estimate is best-effort, not a billing guarantee.** It is derived from
  `PRICING` (estimated rates) or pooled history; the Anthropic console is the
  authoritative source. Treat the figure as guidance.
- **Within-stage overshoot is possible.** Budget is checked only at stage / pass
  / draft boundaries against already-recorded spend (`tracker.total_cost_usd`).
  A single stage can blow past the ceiling internally — a Claude call is never
  aborted mid-flight.
- **The gate runs after Parse.** Resume + prompt parsing cost is always incurred
  to learn `count`. A refusal or decline still pays for those two ingestion
  calls (and a resume-cache hit makes the resume parse free).
- **Refuse-to-start.** If the budget is below the estimated total, the run never
  discovers — `ui.error` + `Exit(1)`.
- **A malformed budget is fail-closed.** A non-finite (`NaN`/`±inf`) or negative
  effective ceiling hard-stops the run before any spend, raising `ConfigError`
  (rendered by `_run_safely` as `ui.error` + `Exit(1)`). It is never silently
  treated as "no ceiling" — that would defeat a cost guard the user explicitly
  set. A budget of exactly `0` is *not* malformed; it is a valid ceiling that
  trips refuse-to-start.
- **Validation lives in the run flow, not in `config.py`.** `config._env_float`
  deliberately does *not* raise on a non-finite/negative `RUN_MAX_USD`, so
  unrelated commands (`scholar list`, `scholar init`) that call `load_settings()`
  don't crash on a bad env value. The policy is enforced only on the spending
  path, at the single point where `--budget` and `RUN_MAX_USD` converge in the
  `run` body.
- **The estimator stays DB-free.** `usage.py` never imports `db`; it does
  arithmetic over `RunCostSample`s passed in. The CLI performs all reads
  (`_recent_cost_history`) and writes (`_persist_run_usage`). Keep it that way
  for testability.
- **No new exception type.** The three outcomes use existing mechanisms: refuse
  = `typer.Exit(1)`; decline = clean `return`; mid-run budget stop = fall
  through to partial delivery (a successful short run ending in
  `RunStatus.REVIEW`).
- **One history row per completed run.** `_persist_run_usage` is called at every
  terminal drafting path (including "nothing to draft"), so a run records its
  sample exactly once. Failed / refused / declined runs write nothing, so they
  never pollute the history.
- **Requested `count` is recorded but NOT used for rate math.** The stored
  `count` is the requested target; the per-professor rate divides spend by the
  *realized* per-stage count instead. `count` is kept only for reference/context.
- **Legacy samples are excluded per-stage, not crashed on.** Rows persisted
  before this change carry no realized counts (empty `stage_counts` after
  `unpack_stage_usage`), so they are dropped from each stage's averaging pool
  rather than read incorrectly. Estimation over a mixed new/legacy history is
  graceful and never raises.
- **Full runs estimate the same as before.** When a stage processes all
  requested professors, its realized count equals `count`, so dividing spend by
  the realized count yields the same per-professor rate as dividing by `count`
  did — this change only corrects short/partial/budget-stopped samples.

## 9. Testing

| Test file | Covers |
|---|---|
| `tests/test_usage.py` | `estimate_run_cost` (static vs historical basis, sample threshold, count scaling), `stage_costs_from_tracker`, dataclass invariants |
| `tests/test_config.py` | `RUN_MAX_USD` / `_env_float` parsing into `Settings.run_max_usd` |
| `tests/test_run_usage_repo.py` | `add_run_usage` / `list_recent_run_usage` round-trip and ordering |
| `tests/test_ui.py` | `cost_estimate_panel` and `budget_stop_notice` rendering |
| `tests/test_cli_topup.py` | `scholar run` integration: estimate gate, refuse-to-start, confirm/decline, mid-run budget stop + partial delivery, and budget validation — malformed `--budget`/`RUN_MAX_USD` (`NaN`/`±inf`/negative) hard-stop, and a zero budget being a valid ceiling that refuses to start |

Run:

```bash
pytest
ruff check .
```

## 10. Open questions / future work

- **Per-stage budget ceilings** (e.g. cap drafting separately) — not implemented;
  the single ceiling is checked against total recorded spend only.
- **Mid-call interruption** — explicitly out of scope. We never abort a Claude
  call in flight, hence the accepted within-stage overshoot.
- **Reconciliation against real billing** — estimates and recorded costs use the
  local `PRICING` table, not the Anthropic console. A future reconciliation step
  could correct history against actual invoiced spend.
- **Static-estimate freshness** — `_STATIC_*` constants in `usage.py` are
  hand-tuned order-of-magnitude figures; they drift if Anthropic changes rates
  and are only superseded once enough history accumulates.
