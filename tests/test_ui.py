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
