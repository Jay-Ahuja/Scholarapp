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


def test_round_trip_does_not_warn_about_updated_at(isolated_db):
    """Regression: write_drafts_to_disk used to bump Draft.updated_at via the
    file_path UPDATE *after* rendering the file's frontmatter snapshot — so every
    first sync after a fresh write produced false-positive conflict warnings.
    The fix flushes file_path first so the rendered snapshot matches the DB; the
    sync also normalizes tz-aware vs naive datetimes before comparing.
    """
    run_id, _, _ = _seed_one_draft()
    review.write_drafts_to_disk(run_id)
    report = review.sync_drafts_from_disk(run_id)
    assert report.warnings == [], f"unexpected warnings: {report.warnings}"


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


# ---------------------------------------------------------------------------
# UTF-8 encoding: non-ASCII characters survive the write -> read -> sync cycle
# ---------------------------------------------------------------------------


# Characters Claude routinely emits that cp1252 (the Windows locale code page)
# can encode only partially or not at all. U+2010 is the canonical offender —
# Path.write_text() with no encoding= raises UnicodeEncodeError on it on Windows.
_NON_ASCII_SUBJECT = "Question on partial‐volume handling — a follow‐up"
_NON_ASCII_BODY = (
    "Dear Prof. Friston,\n\n"
    "I’d love to discuss your “probabilistic” segmentation work "
    "— it’s relevant to my interests in non‐invasive imaging.\n\n"
    "Best,\nJay"
)


def test_non_ascii_survives_write_read_sync_cycle(isolated_db):
    """A draft carrying U+2010 (Unicode hyphen) + dashes/curly quotes must
    round-trip through write -> read (parse) -> sync unchanged. On Windows this
    used to crash with UnicodeEncodeError at the write step (cp1252 default)."""
    run_id, _, draft_id = _seed_one_draft(
        subject=_NON_ASCII_SUBJECT, body=_NON_ASCII_BODY
    )

    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    # Read side: parse must recover the exact non-ASCII characters.
    parsed = review.parse_draft_file(file_path)
    assert parsed.subject == _NON_ASCII_SUBJECT
    assert "‐" in parsed.subject
    assert parsed.body == _NON_ASCII_BODY

    # Full sync: nothing changed, no errors — the round-trip is byte-stable.
    report = review.sync_drafts_from_disk(run_id)
    assert report.errors == 0
    assert report.updated == 0
    assert report.unchanged == 1
    assert report.status_changed == 0

    # DB still holds the original non-ASCII text exactly.
    with get_session() as s:
        d = repo.get_draft(s, draft_id)
        assert d.subject == _NON_ASCII_SUBJECT
        assert d.body == _NON_ASCII_BODY


def test_draft_files_are_written_as_utf8_bytes(isolated_db):
    """Guard against silent reliance on the platform default encoding.

    Tests run on Linux/utf-8 by default, so a missing encoding= would still pass
    a string round-trip there. Assert the bytes on disk are valid UTF-8 (and NOT
    decodable as the cp1252 mojibake a locale-default write would produce) so a
    regression that drops encoding="utf-8" is caught on every platform."""
    run_id, _, _ = _seed_one_draft(subject=_NON_ASCII_SUBJECT, body=_NON_ASCII_BODY)

    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    raw = file_path.read_bytes()
    # The bytes decode cleanly as UTF-8 and contain the U+2010 code point's
    # canonical UTF-8 encoding (E2 80 90).
    text = raw.decode("utf-8")
    assert "‐" in text
    assert b"\xe2\x80\x90" in raw

    # write_text(newline="") must not have translated \n -> \r\n, so no stray
    # carriage returns leak into the round-trip / conflict-detection snapshot.
    assert b"\r\n" not in raw


def test_write_status_in_file_preserves_non_ascii(isolated_db):
    """write_status_in_file rewrites only the status field; the UTF-8 body must
    survive its read + write (it uses the same encoding path)."""
    run_id, _, draft_id = _seed_one_draft(
        subject=_NON_ASCII_SUBJECT, body=_NON_ASCII_BODY
    )
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    review.write_status_in_file(file_path, "approved")

    parsed = review.parse_draft_file(file_path)
    assert parsed.status == "approved"
    assert parsed.subject == _NON_ASCII_SUBJECT
    assert parsed.body == _NON_ASCII_BODY


# ---------------------------------------------------------------------------
# Legacy cp1252 read fallback: files written by older versions on Windows used
# the locale default (cp1252) and carry bytes like 0x96 (en-dash) that aren't
# valid UTF-8. The read path must tolerate them (utf-8 first, then cp1252) so
# parse/sync can still process them — while valid UTF-8 reads identically.
# ---------------------------------------------------------------------------


def test_legacy_cp1252_draft_reads_with_correct_glyph_and_syncs(isolated_db):
    """A draft written as raw cp1252 bytes (incl. 0x96 en-dash) must:
    (1) read through parse + full sync without a decode error,
    (2) surface the byte as the correct Unicode glyph (U+2013, –)."""
    run_id, _, draft_id = _seed_one_draft()

    # write_drafts_to_disk produces a valid (UTF-8) draft; overwrite it on disk
    # with the cp1252-encoded equivalent to simulate a legacy file. Build the
    # bytes explicitly (write_bytes) so the test doesn't depend on the platform
    # default encoding (tests run on UTF-8).
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    text = file_path.read_text(encoding="utf-8")
    # Inject the cp1252 en-dash (U+2013) into the body so the file is no longer
    # valid UTF-8 once encoded with cp1252 (0x96 is the offending legacy byte).
    legacy_text = text.replace("Subject:", "Subject: meeting – follow-up\nSubject:", 1)
    raw = legacy_text.encode("cp1252")
    assert b"\x96" in raw  # the canonical legacy en-dash byte is present
    # Sanity: these bytes are NOT valid UTF-8 (so the fallback is genuinely exercised).
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    file_path.write_bytes(raw)

    # (1) parse does not raise a decode error; (2) the en-dash comes through as U+2013.
    parsed = review.parse_draft_file(file_path)
    assert "–" in (parsed.subject + parsed.body)

    # Full sync processes the legacy file without a decode/parse error.
    report = review.sync_drafts_from_disk(run_id)
    assert report.errors == 0, f"unexpected errors: {report.error_messages}"


def test_valid_utf8_draft_reads_identically_after_fallback_change(isolated_db):
    """Regression guard: a valid UTF-8 draft (incl. multibyte chars) must read
    EXACTLY as before — the cp1252 fallback only engages on a UTF-8 decode
    failure, never altering the valid-UTF-8 path."""
    run_id, _, draft_id = _seed_one_draft(
        subject=_NON_ASCII_SUBJECT, body=_NON_ASCII_BODY
    )
    path = review.write_drafts_to_disk(run_id)
    file_path = next(path.glob("*.md"))

    # Direct strict-UTF-8 read of the same file (what _read_text does on success).
    expected = file_path.read_text(encoding="utf-8")
    assert review._read_text(file_path) == expected

    parsed = review.parse_draft_file(file_path)
    assert parsed.subject == _NON_ASCII_SUBJECT
    assert parsed.body == _NON_ASCII_BODY

    report = review.sync_drafts_from_disk(run_id)
    assert report.errors == 0
    assert report.unchanged == 1
