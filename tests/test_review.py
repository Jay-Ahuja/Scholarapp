"""Tests for scholarapp.modules.review — write/sync round-trip + edit semantics."""

from __future__ import annotations

import pytest

from scholarapp.db import repo
from scholarapp.db.models import DraftStatus, RunStatus
from scholarapp.db.session import get_session
from scholarapp.errors import NotFoundError
from scholarapp.modules import review


# ---------------------------------------------------------------------------
# Isolated DB + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point DATA_DIR at tmp_path, chdir into tmp_path (so DRAFTS_DIR defaults
    under tmp_path/drafts), and reset SQLAlchemy state for each test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from scholarapp.db import session as db_session

    db_session.reset_engine()
    yield tmp_path
    db_session.reset_engine()


def _seed_one_draft(
    *,
    subject: str = "Original subject",
    body: str = "Hello professor.\n\nThis is the body.",
    status: DraftStatus = DraftStatus.PENDING_REVIEW,
    prof_name: str = "Karl Friston",
) -> tuple[str, str, str]:
    """Insert a complete pipeline state for one draft. Returns (run_id, prof_id, draft_id)."""
    with get_session() as session:
        run = repo.create_run(
            session,
            field="computational neuroscience",
            goal="30-min chat",
            considerations="",
            count=1,
            resume_path="/tmp/fake.pdf",
            template_text="Dear Prof.",
        )
        repo.update_run_status(session, run.id, RunStatus.REVIEW)
        prof = repo.add_professor(
            session,
            run_id=run.id,
            name=prof_name,
            institution="UCL",
            email="k.friston@ucl.ac.uk",
            openalex_id="A1",
        )
        project = repo.add_project(
            session,
            professor_id=prof.id,
            title="Probabilistic segmentation in SPM",
            url="https://doi.org/x",
            year=2024,
        )
        repo.add_matched_project(
            session,
            professor_id=prof.id,
            project_id=project.id,
            why_relevant="Both handle partial-volume effects.",
        )
        draft = repo.add_draft(
            session,
            run_id=run.id,
            professor_id=prof.id,
            subject=subject,
            body=body,
            status=status,
        )
        return run.id, prof.id, draft.id


# ---------------------------------------------------------------------------
# Slug + parse helpers
# ---------------------------------------------------------------------------


def test_make_slug_simple():
    assert review._make_slug("Karl Friston", "abc12345-def-...").endswith("-abc12345")
    assert review._make_slug("Karl Friston", "abc12345").startswith("friston-")


def test_make_slug_with_initials_and_dots():
    assert review._make_slug("Karl J. Friston", "abc12345").startswith("friston-")


def test_make_slug_hyphenated_lastname():
    assert review._make_slug("Mary Hyman-Smith", "12345678").startswith("hyman-smith-")


def test_make_slug_empty_name():
    assert review._make_slug("", "12345678") == "unknown-12345678"


def test_parse_draft_file_round_trip(tmp_path):
    """render → parse should preserve subject + body exactly."""
    from datetime import datetime, UTC

    content = review.render_draft_file(
        draft_id="draft-1",
        professor_name="Karl Friston",
        institution="UCL",
        email="k.friston@ucl.ac.uk",
        matched=[{"title": "X", "why": "Y"}],
        status="pending_review",
        updated_at=datetime(2026, 5, 23, 23, 30, tzinfo=UTC),
        subject="Question on partial-volume handling",
        body="Dear Prof. Friston,\n\nMultiple paragraphs.\n\nBest,\nJay",
    )
    fp = tmp_path / "test.md"
    fp.write_text(content)
    parsed = review.parse_draft_file(fp)
    assert parsed.draft_id == "draft-1"
    assert parsed.subject == "Question on partial-volume handling"
    assert parsed.body == "Dear Prof. Friston,\n\nMultiple paragraphs.\n\nBest,\nJay"
    assert parsed.status == "pending_review"


# ---------------------------------------------------------------------------
# Round-trip: write_drafts_to_disk → sync_drafts_from_disk → DB unchanged
# ---------------------------------------------------------------------------


def test_round_trip_leaves_db_unchanged(isolated_db):
    run_id, _, draft_id = _seed_one_draft()

    review.write_drafts_to_disk(run_id)
    report = review.sync_drafts_from_disk(run_id)

    assert report.updated == 0
    assert report.unchanged == 1
    assert report.status_changed == 0
    assert report.errors == 0

    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert d.subject == "Original subject"
        assert d.body == "Hello professor.\n\nThis is the body."
        assert d.status == DraftStatus.PENDING_REVIEW


def test_write_drafts_creates_file_per_draft(isolated_db):
    run_id, _, draft_id = _seed_one_draft()
    path = review.write_drafts_to_disk(run_id)
    files = sorted(path.glob("*.md"))
    assert len(files) == 1
    assert files[0].name.startswith("friston-")
    # Draft.file_path was populated
    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert d.file_path == str(files[0].resolve())


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def test_edit_body_in_file_syncs_to_db(isolated_db):
    run_id, _, draft_id = _seed_one_draft()
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    # Edit the body
    text = file_path.read_text()
    new_text = text.replace(
        "Hello professor.\n\nThis is the body.",
        "Hello professor.\n\nNEW BODY content.",
    )
    file_path.write_text(new_text)

    report = review.sync_drafts_from_disk(run_id)
    assert report.updated == 1
    assert report.unchanged == 0
    assert report.errors == 0

    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert "NEW BODY content" in d.body


def test_edit_subject_syncs(isolated_db):
    run_id, _, draft_id = _seed_one_draft(subject="Old subject")
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    text = file_path.read_text().replace("Subject: Old subject", "Subject: New subject")
    file_path.write_text(text)

    report = review.sync_drafts_from_disk(run_id)
    assert report.updated == 1

    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert d.subject == "New subject"


def test_change_status_to_approved_syncs(isolated_db):
    run_id, _, draft_id = _seed_one_draft()
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    # Flip status in the file
    review.write_status_in_file(file_path, "approved")

    report = review.sync_drafts_from_disk(run_id)
    assert report.status_changed == 1
    assert report.updated == 0  # nothing else changed
    assert report.errors == 0

    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert d.status == DraftStatus.APPROVED
        # subject and body unchanged
        assert d.subject == "Original subject"


# ---------------------------------------------------------------------------
# Read-only field tampering
# ---------------------------------------------------------------------------


def test_tamper_with_email_warns_and_ignores(isolated_db):
    run_id, prof_id, draft_id = _seed_one_draft()
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    text = file_path.read_text().replace(
        "email: k.friston@ucl.ac.uk", "email: imposter@example.com"
    )
    file_path.write_text(text)

    report = review.sync_drafts_from_disk(run_id)
    assert any("email" in w.lower() for w in report.warnings)

    # Professor.email in DB unchanged
    with get_session() as s:
        from scholarapp.db.models import Professor

        prof = s.get(Professor, prof_id)
        assert prof.email == "k.friston@ucl.ac.uk"


def test_tamper_with_professor_name_warns(isolated_db):
    run_id, prof_id, _ = _seed_one_draft()
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    text = file_path.read_text().replace("professor: Karl Friston", "professor: Imposter")
    file_path.write_text(text)

    report = review.sync_drafts_from_disk(run_id)
    assert any("professor" in w.lower() for w in report.warnings)

    with get_session() as s:
        from scholarapp.db.models import Professor

        prof = s.get(Professor, prof_id)
        assert prof.name == "Karl Friston"


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


def test_invalid_status_transition_errors_no_db_change(isolated_db):
    """sent → approved is rejected; DB stays at sent."""
    # Seed with status=sent (illegal terminal state for an outgoing edit).
    run_id, _, draft_id = _seed_one_draft(status=DraftStatus.SENT)
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    review.write_status_in_file(file_path, "approved")

    report = review.sync_drafts_from_disk(run_id)
    assert report.errors == 1
    assert any("transition" in e.lower() for e in report.error_messages)

    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert d.status == DraftStatus.SENT  # unchanged


def test_invalid_status_value_errors(isolated_db):
    """A status value that isn't in the enum at all is rejected."""
    run_id, _, draft_id = _seed_one_draft()
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    review.write_status_in_file(file_path, "bogus_status")

    report = review.sync_drafts_from_disk(run_id)
    assert report.errors == 1

    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert d.status == DraftStatus.PENDING_REVIEW  # unchanged


def test_approved_back_to_pending_review_allowed(isolated_db):
    """User changed their mind: approved → pending_review is allowed."""
    run_id, _, draft_id = _seed_one_draft(status=DraftStatus.APPROVED)
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    review.write_status_in_file(file_path, "pending_review")

    report = review.sync_drafts_from_disk(run_id)
    assert report.errors == 0
    assert report.status_changed == 1

    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert d.status == DraftStatus.PENDING_REVIEW


def test_pending_review_to_rejected_allowed(isolated_db):
    run_id, _, draft_id = _seed_one_draft()
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    review.write_status_in_file(file_path, "rejected")

    report = review.sync_drafts_from_disk(run_id)
    assert report.status_changed == 1
    with get_session() as s:
        assert repo.get_draft(s, draft_id).status == DraftStatus.REJECTED


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_write_drafts_unknown_run_raises(isolated_db):
    with pytest.raises(NotFoundError):
        review.write_drafts_to_disk("bogus-run-id")


def test_sync_drafts_missing_directory_raises(isolated_db):
    with pytest.raises(NotFoundError):
        review.sync_drafts_from_disk("bogus-run-id")
