"""End-to-end smoke test (cassette replay).

Exercises the full pipeline — parse → discover → match → draft → (gated) send —
against a vcrpy cassette so it's fast, deterministic, and free.

This test SKIPS gracefully when no cassette has been recorded. To populate the
cassette once with real API responses:

    pytest -m record tests/test_e2e_recording.py

Then commit `tests/cassettes/test_full_pipeline_no_send.yaml` so CI can replay.

What's verified:
- `scholar run` exits 0 and creates a Run row in status='review'
- 3 Draft rows persist with status='pending_review'
- 3 .md draft files exist under `<DRAFTS_DIR>/<run_id>/`
- Each draft body is ≥ 50 words and references the title of at least one
  matched project (case-insensitive substring)
- `scholar send <run_id>` exits 1 with a message containing "Sending disabled"
- The cassette has zero requests to gmail.googleapis.com (the SEND_ENABLED
  gate is the only protection between this test and real outbound email)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from scholarapp.cli import app
from scholarapp.db import repo
from scholarapp.db.models import DraftStatus, Project, RunStatus
from scholarapp.db.session import get_session


CASSETTE_NAME = "test_full_pipeline_no_send.yaml"
CASSETTE_PATH = Path(__file__).parent / "cassettes" / CASSETTE_NAME


pytestmark = pytest.mark.skipif(
    not CASSETTE_PATH.exists(),
    reason=(
        f"E2E cassette not yet recorded at {CASSETTE_PATH}. "
        f"Populate it with: pytest -m record tests/test_e2e_recording.py "
        f"(requires real ANTHROPIC_API_KEY + TAVILY_API_KEY)."
    ),
)


def test_full_pipeline_no_send(e2e_setup, vcr_replay):
    """Replay the full pipeline against the committed cassette."""
    runner = CliRunner()

    with vcr_replay.use_cassette(CASSETTE_NAME):
        run_result = runner.invoke(app, ["run", "--inputs", str(e2e_setup["inputs"])])

    assert run_result.exit_code == 0, (
        f"scholar run failed:\n--- stdout ---\n{run_result.stdout}"
    )

    # --- 1. Run row exists and reached review status ----------------------
    with get_session() as session:
        runs = repo.list_runs(session)
        assert len(runs) == 1, "expected exactly one Run row"
        run = runs[0]
        assert run.status == RunStatus.REVIEW, (
            f"expected status=review, got {run.status.value}"
        )

    # --- 2. Three pending_review Draft rows ------------------------------
    with get_session() as session:
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 3, f"expected 3 drafts, got {len(drafts)}"
    assert all(d.status == DraftStatus.PENDING_REVIEW for d in drafts)

    # --- 3. scholar review writes 3 .md files ----------------------------
    review_result = runner.invoke(app, ["review", run.id])
    assert review_result.exit_code == 0, (
        f"scholar review failed:\n{review_result.stdout}"
    )
    drafts_run_dir = e2e_setup["drafts_dir"] / run.id
    md_files = sorted(drafts_run_dir.glob("*.md"))
    assert len(md_files) == 3, (
        f"expected 3 .md files in {drafts_run_dir}, got {len(md_files)}: {md_files}"
    )

    # --- 4. Each body is substantive + references a matched project -----
    with get_session() as session:
        for d in drafts:
            body_words = d.body.split()
            assert len(body_words) >= 50, (
                f"draft {d.id}: body has {len(body_words)} words (need ≥50)"
            )

            matched = repo.list_matched_projects_for_professor(session, d.professor_id)
            if not matched:
                # Drafting should have skipped this prof; we shouldn't see them
                # in the drafts list at all.
                pytest.fail(
                    f"draft {d.id} exists but its professor has no matched projects"
                )

            titles = []
            for mp in matched:
                project = session.get(Project, mp.project_id)
                if project:
                    titles.append(project.title)
            body_lower = d.body.lower()
            hit = any(t.lower() in body_lower for t in titles if t)
            assert hit, (
                f"draft {d.id}: body does not reference any matched project title.\n"
                f"  Titles: {titles!r}\n"
                f"  Body opener: {d.body[:200]!r}"
            )

    # --- 5. scholar send exits 1 with the gated message ------------------
    # Flip drafts to approved so send_approved actually hits the gate (it short-
    # circuits to empty when nothing is approved).
    with get_session() as session:
        for d in drafts:
            repo.update_draft(session, d.id, status=DraftStatus.APPROVED)

    send_result = runner.invoke(app, ["send", run.id])
    assert send_result.exit_code == 1, (
        f"scholar send should exit 1 when SEND_ENABLED=false, got "
        f"{send_result.exit_code}:\n{send_result.stdout}"
    )
    assert "Sending disabled" in send_result.stdout, (
        f"expected 'Sending disabled' in output, got:\n{send_result.stdout}"
    )

    # --- 6. Zero Gmail API calls in the cassette ------------------------
    cassette_text = CASSETTE_PATH.read_text()
    assert "gmail.googleapis.com" not in cassette_text, (
        "Cassette contains gmail.googleapis.com URLs — the SEND_ENABLED gate "
        "may have leaked. Investigate before re-committing the cassette."
    )
