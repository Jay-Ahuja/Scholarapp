"""Tests for the per-call usage tracker."""

from __future__ import annotations

from types import SimpleNamespace

from scholarapp import usage as usage_tracker
from scholarapp.usage import (
    PRICING,
    CallRecord,
    UsageTracker,
    record,
    set_tracker,
    summarize,
)


def _u(*, input_tokens=0, output_tokens=0, cache_creation=0, cache_read=0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def test_call_record_cost_pricing_sonnet():
    r = CallRecord(
        label="parse_resume",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=500,
    )
    # 1000 * $3/M + 500 * $15/M = $0.003 + $0.0075 = $0.0105
    assert abs(r.cost_usd - 0.0105) < 1e-9


def test_call_record_cost_pricing_haiku():
    r = CallRecord(
        label="extract_email",
        model="claude-haiku-4-5",
        input_tokens=2000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=200,
    )
    # 2000 * $1/M + 200 * $5/M = $0.002 + $0.001 = $0.003
    assert abs(r.cost_usd - 0.003) < 1e-9


def test_call_record_includes_cache_pricing():
    r = CallRecord(
        label="x",
        model="claude-sonnet-4-6",
        input_tokens=100,
        cache_creation_input_tokens=1000,
        cache_read_input_tokens=2000,
        output_tokens=50,
    )
    p = PRICING["claude-sonnet-4-6"]
    expected = (
        100 * p["input"]
        + 1000 * p["cache_write"]
        + 2000 * p["cache_read"]
        + 50 * p["output"]
    )
    assert abs(r.cost_usd - expected) < 1e-9


def test_unknown_model_zero_cost():
    r = CallRecord(
        label="x",
        model="claude-bogus-9-9",
        input_tokens=10000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=1000,
    )
    assert r.cost_usd == 0.0


def test_record_appends_to_current_tracker():
    tracker = UsageTracker()
    set_tracker(tracker)
    try:
        record("parse_resume", "claude-sonnet-4-6", _u(input_tokens=100, output_tokens=20))
        record("extract_email", "claude-haiku-4-5", _u(input_tokens=200, output_tokens=10))
    finally:
        set_tracker(None)

    assert len(tracker.records) == 2
    assert tracker.records[0].label == "parse_resume"
    assert tracker.records[1].label == "extract_email"
    assert tracker.total_cost_usd > 0


def test_record_is_noop_with_no_tracker_installed():
    # No exception should be raised; the call is silently dropped.
    record("parse_resume", "claude-sonnet-4-6", _u(input_tokens=999))


def test_record_is_noop_with_none_usage():
    tracker = UsageTracker()
    set_tracker(tracker)
    try:
        record("parse_resume", "claude-sonnet-4-6", None)
    finally:
        set_tracker(None)
    assert tracker.records == []


def test_summarize_groups_repeated_labels():
    tracker = UsageTracker()
    # Three extract_email calls + one parse_resume call.
    for _ in range(3):
        tracker.record(
            "extract_email", "claude-haiku-4-5", _u(input_tokens=1000, output_tokens=100)
        )
    tracker.record(
        "parse_resume", "claude-sonnet-4-6", _u(input_tokens=5000, output_tokens=500)
    )

    out = summarize(tracker)
    # Three extract_email calls aggregated into one row, marked with × 3.
    assert "extract_email × 3" in out
    assert "parse_resume" in out
    # No "× 1" annotation for the single-call entry.
    assert "parse_resume × 1" not in out
    # Aggregated input tokens (3 × 1000 = 3000) appear.
    assert "in=  3000" in out or "in= 3000" in out or "3000" in out
    # Total line present.
    assert "Total: $" in out


def test_summarize_empty_tracker():
    out = summarize(UsageTracker())
    assert "no Claude calls" in out


def test_set_tracker_isolates_contexts():
    tracker_a = UsageTracker()
    tracker_b = UsageTracker()

    set_tracker(tracker_a)
    record("x", "claude-haiku-4-5", _u(input_tokens=10))
    set_tracker(tracker_b)
    record("y", "claude-haiku-4-5", _u(input_tokens=20))
    set_tracker(None)

    assert len(tracker_a.records) == 1
    assert tracker_a.records[0].label == "x"
    assert len(tracker_b.records) == 1
    assert tracker_b.records[0].label == "y"
