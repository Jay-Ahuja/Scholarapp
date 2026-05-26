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
    lines: list[str] = [rule, f"  Claude usage", rule]

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
