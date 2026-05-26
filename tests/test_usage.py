"""Tests for the per-call usage tracker."""

from __future__ import annotations

from types import SimpleNamespace

from scholarapp.usage import (
    PRICING,
    STAGES,
    UNPRICED_MARKER,
    CallRecord,
    CostEstimate,
    RunCostSample,
    StageEstimate,
    UsageTracker,
    estimate_run_cost,
    record,
    set_tracker,
    stage_costs_from_tracker,
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
    # cost_usd is still 0.0 (we have no rate), but the record reports itself as
    # NOT priced so callers can flag the hidden spend instead of showing $0.00.
    assert r.cost_usd == 0.0
    assert r.is_priced is False


def test_known_model_is_priced():
    r = CallRecord(
        label="x",
        model="claude-haiku-4-5",
        input_tokens=1000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=100,
    )
    assert r.is_priced is True


def test_summarize_flags_unpriced_model_instead_of_zero():
    tracker = UsageTracker()
    # One recognized-model call and one unknown-model call.
    tracker.record(
        "parse_resume", "claude-sonnet-4-6", _u(input_tokens=1000, output_tokens=500)
    )
    tracker.record(
        "mystery_call", "claude-bogus-9-9", _u(input_tokens=10000, output_tokens=1000)
    )

    out = summarize(tracker)

    # The unknown model is flagged as unpriced, NOT shown as $0.00.
    assert UNPRICED_MARKER in out
    assert "mystery_call" in out
    # The recognized model's cost is unchanged: 1000*$3/M + 500*$15/M = $0.0105.
    assert "$0.0105" in out
    # No misleading zero-dollar figure printed for the unpriced call.
    assert "$0.0000" not in out


def test_summarize_no_unpriced_marker_for_all_known_models():
    tracker = UsageTracker()
    tracker.record(
        "extract_email", "claude-haiku-4-5", _u(input_tokens=2000, output_tokens=200)
    )
    out = summarize(tracker)
    # All-known summaries must not mention the unpriced marker at all.
    assert UNPRICED_MARKER not in out
    # 2000*$1/M + 200*$5/M = $0.003 → total reflects the priced cost unchanged.
    assert "$0.0030" in out


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


# ---------------------------------------------------------------------------
# stage_costs_from_tracker
# ---------------------------------------------------------------------------


def test_stage_costs_always_has_every_stage_key():
    out = stage_costs_from_tracker(UsageTracker())
    assert set(out.keys()) == set(STAGES)
    assert all(v == 0.0 for v in out.values())


def test_stage_costs_maps_labels_to_stages():
    tracker = UsageTracker()
    tracker.record("pick_topics", "claude-haiku-4-5", _u(input_tokens=1000, output_tokens=100))
    tracker.record("extract_email", "claude-haiku-4-5", _u(input_tokens=2000, output_tokens=200))
    tracker.record("match_projects", "claude-haiku-4-5", _u(input_tokens=4000, output_tokens=400))
    tracker.record("draft_email", "claude-sonnet-4-6", _u(input_tokens=3000, output_tokens=600))

    out = stage_costs_from_tracker(tracker)

    # discovery = pick_topics + extract_email (both haiku).
    pick = 1000 * 1.0e-6 + 100 * 5.0e-6
    extract = 2000 * 1.0e-6 + 200 * 5.0e-6
    assert abs(out["discovery"] - (pick + extract)) < 1e-12
    # matching = match_projects (haiku).
    assert abs(out["matching"] - (4000 * 1.0e-6 + 400 * 5.0e-6)) < 1e-12
    # drafting = draft_email (sonnet).
    assert abs(out["drafting"] - (3000 * 3.0e-6 + 600 * 15.0e-6)) < 1e-12


def test_stage_costs_ignores_unmapped_labels():
    tracker = UsageTracker()
    # parse_resume / parse_prompt belong to no stage — must not appear anywhere.
    tracker.record("parse_resume", "claude-sonnet-4-6", _u(input_tokens=5000, output_tokens=500))
    tracker.record("parse_prompt", "claude-haiku-4-5", _u(input_tokens=1000, output_tokens=100))
    out = stage_costs_from_tracker(tracker)
    assert all(v == 0.0 for v in out.values())


# ---------------------------------------------------------------------------
# estimate_run_cost — static fallback
# ---------------------------------------------------------------------------


def test_estimate_static_when_no_history():
    est = estimate_run_cost(20, [])
    assert est.basis == "static"
    assert est.sample_size == 0
    assert est.count == 20
    # One stage entry per STAGES key, in order.
    assert [s.stage for s in est.stages] == list(STAGES)
    # Total invariant.
    assert abs(est.total_usd - sum(s.cost_usd for s in est.stages)) < 1e-12
    assert est.total_usd > 0


def test_estimate_static_when_history_none():
    est = estimate_run_cost(5, None)
    assert est.basis == "static"
    assert est.sample_size == 0


def test_estimate_static_scales_with_count():
    small = estimate_run_cost(1)
    big = estimate_run_cost(10)
    assert big.total_usd > small.total_usd


def test_estimate_static_below_min_samples_stays_static():
    # Two samples is below the threshold -> still static.
    history = [
        RunCostSample(count=10, stage_costs={s: 1.0 for s in STAGES}) for _ in range(2)
    ]
    est = estimate_run_cost(10, history)
    assert est.basis == "static"
    assert est.sample_size == 0


def test_estimate_zero_count_is_nonnegative_static():
    est = estimate_run_cost(0, [])
    assert est.basis == "static"
    assert all(s.cost_usd >= 0 for s in est.stages)
    assert est.total_usd >= 0


# ---------------------------------------------------------------------------
# estimate_run_cost — historical averaging
# ---------------------------------------------------------------------------


def test_estimate_historical_when_enough_samples():
    # Three runs of 10 profs each, $1 total per stage -> $0.10/prof/stage.
    history = [
        RunCostSample(count=10, stage_costs={s: 1.0 for s in STAGES}) for _ in range(3)
    ]
    est = estimate_run_cost(10, history)
    assert est.basis == "historical"
    assert est.sample_size == 3
    # 10 profs * $0.10/prof/stage = $1.00 per stage.
    for s in est.stages:
        assert abs(s.cost_usd - 1.0) < 1e-12
    assert abs(est.total_usd - sum(s.cost_usd for s in est.stages)) < 1e-12


def test_estimate_historical_pools_totals_weighting_larger_runs():
    # Pooled rate = total cost / total profs, NOT mean of per-run rates.
    # Run A: 1 prof, $1 drafting -> 1.0/prof. Run B+C: 9 profs, $9 -> 1.0/prof.
    history = [
        RunCostSample(count=1, stage_costs={"discovery": 0.0, "matching": 0.0, "drafting": 1.0}),
        RunCostSample(count=9, stage_costs={"discovery": 0.0, "matching": 0.0, "drafting": 9.0}),
        RunCostSample(count=5, stage_costs={"discovery": 0.0, "matching": 0.0, "drafting": 5.0}),
    ]
    est = estimate_run_cost(3, history)
    assert est.basis == "historical"
    # pooled drafting rate = (1+9+5)/(1+9+5) = 1.0/prof -> 3 profs = $3.
    drafting = next(s for s in est.stages if s.stage == "drafting")
    assert abs(drafting.cost_usd - 3.0) < 1e-12


def test_estimate_skips_zero_count_samples():
    # Two real samples + one zero-count sample = only 2 usable -> below threshold.
    history = [
        RunCostSample(count=10, stage_costs={s: 1.0 for s in STAGES}),
        RunCostSample(count=10, stage_costs={s: 1.0 for s in STAGES}),
        RunCostSample(count=0, stage_costs={s: 999.0 for s in STAGES}),
    ]
    est = estimate_run_cost(10, history)
    # The zero-count sample is dropped, leaving 2 usable -> static fallback, no div-by-zero.
    assert est.basis == "static"


def test_estimate_historical_count_zero_is_zero():
    history = [
        RunCostSample(count=10, stage_costs={s: 1.0 for s in STAGES}) for _ in range(3)
    ]
    est = estimate_run_cost(0, history)
    assert est.basis == "historical"
    assert est.total_usd == 0.0


def test_dataclasses_are_frozen():
    se = StageEstimate(stage="discovery", cost_usd=1.0)
    ce = CostEstimate(count=1, total_usd=1.0, stages=[se], basis="static", sample_size=0)
    for obj, attr in ((se, "cost_usd"), (ce, "total_usd")):
        try:
            setattr(obj, attr, 2.0)
        except AttributeError:
            pass
        else:  # pragma: no cover - frozen dataclass should not allow this
            raise AssertionError("expected frozen dataclass")
