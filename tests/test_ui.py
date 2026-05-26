"""Tests for scholarapp.ui — formatters render via Rich.

We never touch the production `console` singleton — each test monkeypatches
`ui.console` (or `ui.error_console`) with a fresh `Console(record=True)` and
inspects the captured output via `export_text()` (or `export_html()`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from rich.console import Console

from scholarapp import ui
from scholarapp import usage as usage_tracker
from scholarapp.db.models import DraftStatus, RunStatus
from scholarapp.modules.review import SyncReport


@pytest.fixture
def capture(monkeypatch):
    """Replace ui.console with a recording Console and return it.

    Width=120 is wide enough that our tables don't wrap unexpectedly in CI.
    """
    rec = Console(record=True, width=120, force_terminal=False)
    monkeypatch.setattr(ui, "console", rec)
    return rec


@pytest.fixture
def capture_stderr(monkeypatch):
    rec = Console(record=True, width=120, force_terminal=False)
    monkeypatch.setattr(ui, "error_console", rec)
    return rec


# ---------------------------------------------------------------------------
# Simple primitives
# ---------------------------------------------------------------------------


def test_section_emits_rule_with_title(capture):
    ui.section("Discover")
    out = capture.export_text()
    assert "Discover" in out
    # Rich's rule uses box-drawing characters; just check at least one is present.
    assert any(ch in out for ch in ("─", "━", "═"))


def test_info_renders_plain(capture):
    ui.info("hello world")
    assert "hello world" in capture.export_text()


def test_warn_prefixes_warning(capture):
    ui.warn("skipping X")
    out = capture.export_text()
    assert "warning" in out.lower()
    assert "skipping X" in out


def test_error_goes_to_error_console(capture_stderr, capture):
    ui.error("something went wrong")
    # stdout console should NOT have the error
    assert "something went wrong" not in capture.export_text()
    # stderr console should — read once, export_text clears by default.
    stderr_out = capture_stderr.export_text()
    assert "something went wrong" in stderr_out
    assert "Error" in stderr_out


# ---------------------------------------------------------------------------
# runs_table
# ---------------------------------------------------------------------------


def _fake_run(id_: str, status: RunStatus, count: int, field: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        created_at=datetime(2026, 5, 23, 12, 0, tzinfo=UTC),
        status=status,
        count=count,
        field=field,
    )


def test_runs_table_empty(capture):
    ui.runs_table([])
    out = capture.export_text()
    assert "No runs yet" in out


def test_runs_table_renders_each_run(capture):
    runs = [
        _fake_run("aaa-111", RunStatus.REVIEW, 3, "computational neuroscience"),
        _fake_run("bbb-222", RunStatus.FAILED, 5, "robotics"),
    ]
    ui.runs_table(runs)
    out = capture.export_text()
    assert "aaa-111" in out
    assert "bbb-222" in out
    assert "review" in out
    assert "failed" in out
    assert "computational neuroscience" in out
    assert "robotics" in out


# ---------------------------------------------------------------------------
# render_usage_table
# ---------------------------------------------------------------------------


def _u(**kw) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=kw.get("input_tokens", 0),
        output_tokens=kw.get("output_tokens", 0),
        cache_creation_input_tokens=kw.get("cache_creation", 0),
        cache_read_input_tokens=kw.get("cache_read", 0),
    )


def test_usage_table_empty(capture):
    tracker = usage_tracker.UsageTracker()
    ui.render_usage_table(tracker)
    assert "no Claude calls recorded" in capture.export_text()


def test_usage_table_renders_grouped_records(capture):
    tracker = usage_tracker.UsageTracker()
    # Two extract_email calls + one parse_resume
    for _ in range(2):
        tracker.record(
            "extract_email", "claude-haiku-4-5",
            _u(input_tokens=1000, output_tokens=100),
        )
    tracker.record(
        "parse_resume", "claude-sonnet-4-6",
        _u(input_tokens=4000, output_tokens=500),
    )
    ui.render_usage_table(tracker)
    out = capture.export_text()
    assert "extract_email × 2" in out
    assert "parse_resume" in out
    assert "sonnet" in out
    assert "haiku" in out
    assert "Total" in out
    # Token counts formatted with commas
    assert "4,000" in out


def test_usage_table_flags_unpriced_model(capture):
    from scholarapp.usage import UNPRICED_MARKER

    tracker = usage_tracker.UsageTracker()
    # One known model + one unrecognized model id.
    tracker.record(
        "parse_resume", "claude-sonnet-4-6",
        _u(input_tokens=1000, output_tokens=500),
    )
    tracker.record(
        "mystery_call", "claude-bogus-9-9",
        _u(input_tokens=10000, output_tokens=1000),
    )
    ui.render_usage_table(tracker)
    out = capture.export_text()
    # Unknown model row is flagged unpriced rather than rendered as $0.00.
    assert UNPRICED_MARKER in out
    assert "mystery_call" in out
    assert "$0.0000" not in out
    # The recognized model's cost is unchanged: 1000*$3/M + 500*$15/M = $0.0105.
    assert "$0.0105" in out


def test_usage_table_no_unpriced_marker_for_known_models(capture):
    from scholarapp.usage import UNPRICED_MARKER

    tracker = usage_tracker.UsageTracker()
    tracker.record(
        "extract_email", "claude-haiku-4-5",
        _u(input_tokens=2000, output_tokens=200),
    )
    ui.render_usage_table(tracker)
    out = capture.export_text()
    assert UNPRICED_MARKER not in out


# ---------------------------------------------------------------------------
# cost_estimate_panel
# ---------------------------------------------------------------------------


def _estimate(**kw) -> SimpleNamespace:
    stages = kw.get(
        "stages",
        [
            SimpleNamespace(stage="discovery", cost_usd=0.0100),
            SimpleNamespace(stage="matching", cost_usd=0.0250),
            SimpleNamespace(stage="drafting", cost_usd=0.1500),
        ],
    )
    return SimpleNamespace(
        count=kw.get("count", 3),
        total_usd=kw.get("total_usd", 0.1850),
        stages=stages,
        basis=kw.get("basis", "historical"),
        sample_size=kw.get("sample_size", 5),
    )


def test_cost_estimate_panel_renders_stages_total_and_count(capture):
    ui.cost_estimate_panel(_estimate())
    out = capture.export_text()
    # Each stage appears in pipeline order with its cost.
    assert "discovery" in out
    assert "matching" in out
    assert "drafting" in out
    assert "$0.0100" in out
    assert "$0.0250" in out
    assert "$0.1500" in out
    # Total is surfaced.
    assert "Total" in out
    assert "$0.1850" in out
    # Count of professors is surfaced.
    assert "3" in out


def test_cost_estimate_panel_historical_mentions_sample_size(capture):
    ui.cost_estimate_panel(_estimate(basis="historical", sample_size=5))
    out = capture.export_text()
    assert "5" in out
    assert "runs" in out
    # Plural form for sample_size > 1.
    assert "run" in out


def test_cost_estimate_panel_historical_singular_run(capture):
    ui.cost_estimate_panel(_estimate(basis="historical", sample_size=1))
    out = capture.export_text()
    assert "last 1 run" in out


def test_cost_estimate_panel_static_reads_as_rough(capture):
    ui.cost_estimate_panel(_estimate(basis="static", sample_size=0))
    out = capture.export_text()
    assert "static" in out.lower()
    # A static estimate should NOT claim to be based on past runs.
    assert "based on the last" not in out


def test_cost_estimate_panel_preserves_stage_order(capture):
    ui.cost_estimate_panel(_estimate())
    out = capture.export_text()
    assert out.index("discovery") < out.index("matching") < out.index("drafting")


# ---------------------------------------------------------------------------
# budget_stop_notice
# ---------------------------------------------------------------------------


def test_budget_stop_notice_names_budget_and_spent(capture):
    ui.budget_stop_notice(budget_usd=2.00, spent_usd=1.9876)
    out = capture.export_text()
    assert "warning" in out.lower()
    assert "budget" in out.lower()
    # Both the spent amount and the budget ceiling are named.
    assert "$1.9876" in out
    assert "$2.0000" in out


def test_budget_stop_notice_priced_default_renders_plain_ceiling_line(capture):
    # The default (unpriced=False) path must render exactly the plain ceiling line.
    ui.budget_stop_notice(budget_usd=2.00, spent_usd=1.9876)
    out = capture.export_text()
    assert "budget reached" in out
    # No fail-closed/unpriced phrasing leaks into the priced-case message.
    assert "could not be measured" not in out
    assert "EXCLUDES" not in out


def test_budget_stop_notice_unpriced_names_fail_closed(capture):
    ui.budget_stop_notice(budget_usd=2.00, spent_usd=0.0030, unpriced=True)
    out = capture.export_text()
    assert "warning" in out.lower()
    # Conveys that a model could not be priced/measured against the budget.
    assert "could not be measured" in out
    # The budget ceiling is still named.
    assert "$2.0000" in out
    # The shown priced spend is named AND flagged as excluding the unaccountable calls.
    assert "$0.0030" in out
    assert "EXCLUDES" in out
    # The fail-closed message takes precedence over the plain "budget reached" line.
    assert "of $2.0000 budget" not in out


# ---------------------------------------------------------------------------
# professors_table
# ---------------------------------------------------------------------------


def test_professors_table_renders_rows(capture):
    cands = [
        SimpleNamespace(
            name="Karl Friston",
            institution="UCL",
            email="k@ucl.ac.uk",
            recent_works=[1, 2, 3],
        )
    ]
    ui.professors_table(cands)
    out = capture.export_text()
    assert "Karl Friston" in out
    assert "UCL" in out
    assert "k@ucl.ac.uk" in out
    assert "3" in out  # number of works


# ---------------------------------------------------------------------------
# run_header + drafts_table
# ---------------------------------------------------------------------------


def test_run_header_includes_key_fields(capture):
    run = _fake_run("xyz", RunStatus.REVIEW, 5, "neuro")
    ui.run_header(run)
    out = capture.export_text()
    assert "xyz" in out
    assert "review" in out
    assert "neuro" in out
    assert "5" in out


def test_drafts_table_empty(capture):
    ui.drafts_table([], {})
    assert "(no drafts yet)" in capture.export_text()


def test_drafts_table_renders_with_status_colors(capture):
    draft = SimpleNamespace(
        professor_id="p1",
        status=DraftStatus.APPROVED,
        file_path="/tmp/test.md",
    )
    profs = {
        "p1": SimpleNamespace(name="Karl Friston", email="k@ucl.ac.uk"),
    }
    ui.drafts_table([draft], profs)
    out = capture.export_text()
    assert "Karl Friston" in out
    assert "k@ucl.ac.uk" in out
    assert "approved" in out
    assert "/tmp/test.md" in out


# ---------------------------------------------------------------------------
# sync_report_panel
# ---------------------------------------------------------------------------


def test_sync_report_panel_counts_only(capture):
    report = SyncReport(updated=2, unchanged=1, status_changed=3, errors=0)
    ui.sync_report_panel(report)
    out = capture.export_text()
    assert "updated=2" in out
    assert "unchanged=1" in out
    assert "status_changed=3" in out
    assert "errors=0" in out


def test_sync_report_panel_with_warnings_and_errors(capture):
    report = SyncReport(
        updated=0,
        unchanged=0,
        status_changed=0,
        errors=1,
        warnings=["draft.md: email was edited; ignoring"],
        error_messages=["draft.md: invalid transition"],
    )
    ui.sync_report_panel(report)
    out = capture.export_text()
    assert "Warnings" in out
    assert "ignoring" in out
    assert "Errors" in out
    assert "invalid transition" in out
