"""Per-call Anthropic token + cost accounting.

Each module that calls `client.messages.create(...)` should call `record(label, model,
response.usage)` afterwards. The CLI installs a tracker via `set_tracker(...)` for the
duration of `scholar run`, then prints a summary at the end.

If no tracker is installed (e.g., during unit tests that don't care), `record` is a no-op.
This keeps the modules ignorant of CLI-level concerns.

Pricing values reflect published per-million-token rates at the time of writing. Update
if Anthropic changes them — actual billing is governed by the Anthropic console, not us.
"""

from __future__ import annotations

import contextvars
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# USD per token. Cache write is +25%, cache read is 10% of base input rate.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.0e-6,
        "output": 15.0e-6,
        "cache_write": 3.75e-6,
        "cache_read": 0.30e-6,
    },
    "claude-haiku-4-5": {
        "input": 1.0e-6,
        "output": 5.0e-6,
        "cache_write": 1.25e-6,
        "cache_read": 0.10e-6,
    },
}


@dataclass(frozen=True)
class CallRecord:
    """One Claude call's accounting."""

    label: str
    model: str
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int

    @property
    def is_priced(self) -> bool:
        """True if this record's model has a known entry in PRICING.

        When False, `cost_usd` is 0.0 only because the model is unrecognized — not
        because the call was free. Callers rendering a summary should flag such
        spend as unpriced/unknown rather than display it as $0.00.
        """
        return self.model in PRICING

    @property
    def cost_usd(self) -> float:
        p = PRICING.get(self.model)
        if p is None:
            return 0.0
        return (
            self.input_tokens * p["input"]
            + self.cache_creation_input_tokens * p["cache_write"]
            + self.cache_read_input_tokens * p["cache_read"]
            + self.output_tokens * p["output"]
        )


@dataclass
class UsageTracker:
    records: list[CallRecord] = field(default_factory=list)

    def record(self, label: str, model: str, usage: Any) -> None:
        if usage is None:
            return
        self.records.append(
            CallRecord(
                label=label,
                model=model,
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                cache_creation_input_tokens=int(
                    getattr(usage, "cache_creation_input_tokens", 0) or 0
                ),
                cache_read_input_tokens=int(
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                ),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            )
        )

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)


# Contextvar lets async tasks inherit the parent's tracker without explicit threading.
_current: contextvars.ContextVar[UsageTracker | None] = contextvars.ContextVar(
    "scholarapp_usage_tracker", default=None
)


def set_tracker(tracker: UsageTracker | None) -> contextvars.Token:
    return _current.set(tracker)


def get_tracker() -> UsageTracker | None:
    return _current.get()


def record(label: str, model: str, usage: Any) -> None:
    """Record a call against the current tracker. No-op if no tracker is installed."""
    tracker = _current.get()
    if tracker is None:
        return
    tracker.record(label, model, usage)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

# Shown in place of a dollar figure when a call's model id isn't in PRICING. An
# unrecognized model has no rate, so its true cost is unknown — surfacing "$0.00"
# would silently hide real spend. We flag it instead.
UNPRICED_MARKER = "unpriced"


def summarize(tracker: UsageTracker) -> str:
    """Pretty multi-line summary grouped by call label."""
    if not tracker.records:
        return "  (no Claude calls recorded)"

    # Preserve first-seen order via OrderedDict, but group same-label calls together.
    grouped: OrderedDict[str, list[CallRecord]] = OrderedDict()
    for r in tracker.records:
        grouped.setdefault(r.label, []).append(r)

    rule = "─" * 64
    lines: list[str] = [rule, "  Claude usage", rule]

    total_cost = 0.0
    by_model_cost: dict[str, float] = {}
    any_unpriced = False

    for label, records in grouped.items():
        n = len(records)
        in_tot = sum(r.input_tokens for r in records)
        cache_read = sum(r.cache_read_input_tokens for r in records)
        out_tot = sum(r.output_tokens for r in records)
        cost = sum(r.cost_usd for r in records)
        # A group is unpriced if any of its calls used a model not in PRICING.
        # Such calls contribute 0.0 to `cost` only because their rate is unknown,
        # so we flag the group rather than print a misleading "$0.0000".
        group_unpriced = any(not r.is_priced for r in records)
        cost_text = UNPRICED_MARKER if group_unpriced else f"${cost:.4f}"
        # Short model tag ("sonnet" / "haiku") parsed from the model id.
        model_id = records[0].model
        model_short = model_id.split("-")[1] if "-" in model_id else model_id
        label_with_count = f"{label} × {n}" if n > 1 else label
        lines.append(
            f"  {label_with_count:<22} {model_short:<7} "
            f"in={in_tot:>6}  cache={cache_read:>5}  out={out_tot:>5}  {cost_text}"
        )
        total_cost += cost
        if group_unpriced:
            any_unpriced = True
        else:
            by_model_cost[model_short] = by_model_cost.get(model_short, 0.0) + cost

    lines.append(rule)
    breakdown = ", ".join(
        f"{m.capitalize()} ${c:.4f}" for m, c in by_model_cost.items()
    )
    total_line = f"  Total: ${total_cost:.4f}   ({breakdown})"
    if any_unpriced:
        # The total covers only priced calls; some spend can't be costed.
        total_line += f"  [+ {UNPRICED_MARKER} call(s)]"
    lines.append(total_line)
    lines.append(rule)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cost estimation
#
# Pure functions only — no DB, no Rich, no I/O. The CLI passes prior-run history
# IN and persists estimates/results OUT; this module just does the arithmetic so
# it stays trivially testable and reusable.
# ---------------------------------------------------------------------------

# The pipeline stages we estimate and report cost for, in display order. This is
# the vocabulary shared with RunUsage.stage_costs and the CLI renderer.
STAGES: tuple[str, ...] = ("discovery", "matching", "drafting")

# CallRecord.label -> stage. Labels not present here (parse_resume / parse_prompt
# — resume + prompt parsing) belong to NO stage and are intentionally excluded
# from per-stage cost: they're one-off ingestion, not part of the per-professor
# discovery/matching/drafting fan-out we estimate. Kept in sync with the
# usage_tracker.record(...) call sites in scholarapp/modules/*.
_LABEL_TO_STAGE: dict[str, str] = {
    "pick_topics": "discovery",
    "extract_email": "discovery",
    "match_projects": "matching",
    "draft_email": "drafting",
}

# Switch from the static guess to historical averaging once we have at least this
# many real runs to average. One sample is too noisy (a single odd run skews the
# mean); three gives a stable-enough baseline without waiting forever.
_MIN_HISTORY_SAMPLES: int = 3

# Static per-PROFESSOR cost assumptions (USD), used only when history is too thin.
# Each value is a representative token mix priced via PRICING at module rates:
#   discovery  ~ extract_email  (haiku): ~3000 in + 300 out
#   matching   ~ match_projects (haiku): ~4000 in + 400 out
#   drafting   ~ draft_email   (sonnet): ~3000 in + 600 out
# These are deliberately round order-of-magnitude figures, not precise billing;
# the historical basis supersedes them as soon as enough real runs exist.
_STATIC_PER_PROF_USD: dict[str, float] = {
    "discovery": 0.0045,
    "matching": 0.0060,
    "drafting": 0.0180,
}

# Static fixed (per-RUN, count-independent) cost. `pick_topics` runs once per run
# regardless of professor count, so it can't scale with `count`; we attribute it
# to discovery as a flat add-on (haiku: ~2000 in + 200 out ~= $0.003).
_STATIC_FIXED_USD: dict[str, float] = {
    "discovery": 0.0030,
}


@dataclass(frozen=True)
class StageEstimate:
    stage: str  # one of STAGES
    cost_usd: float


@dataclass(frozen=True)
class CostEstimate:
    count: int
    total_usd: float  # invariant: == sum(s.cost_usd for s in stages)
    stages: list[StageEstimate]  # one entry per STAGES key, in STAGES order
    basis: str  # "historical" or "static"
    sample_size: int  # historical runs averaged; 0 when "static"


@dataclass(frozen=True)
class RunCostSample:
    count: int  # requested count; informational only — NOT used for rate math
    stage_costs: dict[str, float]  # keyed by STAGES
    # Realized professors that actually passed through each stage, keyed by STAGES.
    # This is what the rate math divides by now (a budget-stopped run drafts fewer
    # than `count`, so dividing spend by the requested count would understate the
    # per-prof rate). EMPTY for legacy rows persisted before this field existed —
    # such samples are excluded from a stage's pool rather than skewing it.
    stage_counts: dict[str, int] = field(default_factory=dict)


# --- Persisted-shape helpers ------------------------------------------------
#
# The run_usage.stage_costs JSON column carries BOTH the per-stage costs and the
# per-stage realized counts. We cannot add a column (create_all can't ALTER an
# existing table — see RunUsage docstring / ADR 0001), so the richer value rides
# inside the existing JSON column. New rows use the tagged {"costs", "counts"}
# shape; legacy rows are the bare {stage: float} cost map and unpack with empty
# counts so old history still feeds the estimator (just excluded per-stage).


def pack_stage_usage(
    stage_costs: dict[str, float], stage_counts: dict[str, int]
) -> dict:
    """Build the tagged JSON value stored in run_usage.stage_costs.

    Shape: {"costs": {stage: float}, "counts": {stage: int}}. The two tag keys
    distinguish a new row from a legacy bare-costs map on read.
    """
    return {"costs": dict(stage_costs), "counts": dict(stage_counts)}


def unpack_stage_usage(stored: dict) -> tuple[dict[str, float], dict[str, int]]:
    """Split a stored run_usage.stage_costs value into (stage_costs, stage_counts).

    Tolerates the legacy bare-costs shape ({stage: float}, no "costs"/"counts"
    keys) by returning the dict as the costs and an EMPTY counts dict — legacy
    rows carry no realized-count signal, so the estimator excludes them per stage.
    """
    if "costs" in stored or "counts" in stored:
        return dict(stored.get("costs", {})), dict(stored.get("counts", {}))
    # Legacy bare-costs map: the whole dict IS the per-stage cost; no counts.
    return dict(stored), {}


def stage_costs_from_tracker(tracker: UsageTracker) -> dict[str, float]:
    """Aggregate the current run's CallRecords into per-STAGE USD totals.

    Returns a dict with EVERY STAGES key present (0.0 when no call hit that
    stage), so downstream persistence/averaging never has to guard for missing
    keys. Records whose label maps to no stage (ingestion parsing) are ignored.
    """
    totals: dict[str, float] = {stage: 0.0 for stage in STAGES}
    for r in tracker.records:
        stage = _LABEL_TO_STAGE.get(r.label)
        if stage is None:
            continue  # ingestion / unmapped label: not a per-stage pipeline cost
        totals[stage] += r.cost_usd
    return totals


def _static_estimate(count: int) -> CostEstimate:
    """Order-of-magnitude estimate from PRICING-derived token assumptions.

    Per-professor cost scales with `count`; the fixed per-run component does not.
    Used when we have too little history to average. `count` is clamped at 0 so a
    negative/zero count yields just the fixed component (no negative costs).
    """
    n = max(count, 0)
    stages: list[StageEstimate] = []
    for stage in STAGES:
        cost = _STATIC_PER_PROF_USD.get(stage, 0.0) * n + _STATIC_FIXED_USD.get(stage, 0.0)
        stages.append(StageEstimate(stage=stage, cost_usd=cost))
    total = sum(s.cost_usd for s in stages)
    return CostEstimate(
        count=count,
        total_usd=total,
        stages=stages,
        basis="static",
        sample_size=0,
    )


def estimate_run_cost(count: int, history: list[RunCostSample] | None = None) -> CostEstimate:
    """Estimate the USD cost of a run of `count` professors.

    Historical basis, computed INDEPENDENTLY per stage: a stage's per-professor
    rate is pooled over only the samples that recorded a realized count for THAT
    stage —

        rate[stage] = sum(sample.stage_costs[stage]) / sum(sample.stage_counts[stage])

    over samples where stage_counts[stage] > 0; the stage's estimate is
    rate[stage] * count. Dividing by the realized per-stage count (not the
    requested `count`) is the fix: a budget-stopped run drafts fewer professors
    than requested, so its drafting spend reflects the smaller realized size —
    dividing that spend by the requested count would understate the rate and drag
    future estimates below equivalent full runs.

    Per-stage fallback: a stage with fewer than _MIN_HISTORY_SAMPLES count-bearing
    samples (or zero pooled count) uses the static per-stage estimate instead of a
    noisy/undefined rate. `basis` is "historical" only when at least one stage's
    realized rate drove its estimate; `sample_size` is the number of count-bearing
    samples pooled (max across stages — the depth of usable realized history).

    Legacy samples carry empty stage_counts and are simply absent from every
    stage's pool, so a history mixing new + legacy rows estimates without error.
    Never raises on empty/short/legacy history — it falls back to static.
    """
    samples = list(history or [])
    static = _static_estimate(count)
    n = max(count, 0)

    stages: list[StageEstimate] = []
    any_historical = False
    # Depth of the realized history actually used: the largest per-stage pool of
    # count-bearing samples. 0 when no stage had enough realized counts.
    max_pool_samples = 0
    for stage in STAGES:
        # Pool only samples that realized a positive count for THIS stage. A
        # missing/zero count (legacy or a stage a run never reached) contributes
        # neither cost nor count, so it can't skew the rate or divide by zero.
        pooled = [s for s in samples if s.stage_counts.get(stage, 0) > 0]
        pooled_count = sum(s.stage_counts[stage] for s in pooled)
        if len(pooled) >= _MIN_HISTORY_SAMPLES and pooled_count > 0:
            pooled_cost = sum(s.stage_costs.get(stage, 0.0) for s in pooled)
            per_prof = pooled_cost / pooled_count
            stages.append(StageEstimate(stage=stage, cost_usd=per_prof * n))
            any_historical = True
            max_pool_samples = max(max_pool_samples, len(pooled))
        else:
            # Too little realized history for this stage: keep the static guess.
            static_stage = next(s for s in static.stages if s.stage == stage)
            stages.append(static_stage)

    total = sum(s.cost_usd for s in stages)
    return CostEstimate(
        count=count,
        total_usd=total,
        stages=stages,
        basis="historical" if any_historical else "static",
        sample_size=max_pool_samples if any_historical else 0,
    )
