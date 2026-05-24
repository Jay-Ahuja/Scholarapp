"""Tests for scholarapp.modules.delivery — the gated Gmail send path.

Three core paths from the Step 8 spec:
1. SEND_ENABLED=false → SendingDisabled raised, SendLog rows written, no Gmail call.
2. SEND_ENABLED=true (mocked Gmail) → MIME built, send called per draft, statuses flip to sent.
3. Daily cap reached → DeliveryError raised before any Gmail call.

We never touch the real Anthropic or Google APIs — `_load_credentials` and
`_gmail_service` are monkeypatched in the real-send tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from scholarapp.db import repo
from scholarapp.db.models import (
    DraftStatus,
    RunStatus,
    SendLog,
    SendOutcome,
)
from scholarapp.db.session import get_session
from scholarapp.errors import DeliveryError, NotFoundError, SendingDisabled
from scholarapp.modules import delivery


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Tmp DATA_DIR + fresh SQLAlchemy state per test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from scholarapp.db import session as db_session

    db_session.reset_engine()
    yield tmp_path
    db_session.reset_engine()


def _seed_n_approved_drafts(n: int) -> tuple[str, list[str], list[str]]:
    """Insert one run + n professors + n approved drafts. Returns
    (run_id, [prof_ids], [draft_ids]).
    """
    with get_session() as session:
        run = repo.create_run(
            session,
            field="x",
            goal="chat",
            considerations="",
            count=n,
            resume_path="/tmp/r.pdf",
            template_text="t",
        )
        repo.update_run_status(session, run.id, RunStatus.REVIEW)
        prof_ids = []
        draft_ids = []
        for i in range(n):
            prof = repo.add_professor(
                session,
                run_id=run.id,
                name=f"Prof {i}",
                institution="Uni",
                email=f"prof{i}@uni.edu",
                openalex_id=f"A{i}",
            )
            draft = repo.add_draft(
                session,
                run_id=run.id,
                professor_id=prof.id,
                subject=f"Subject {i}",
                body=f"Body for prof {i}.\n\nMultiline.",
                status=DraftStatus.APPROVED,
            )
            prof_ids.append(prof.id)
            draft_ids.append(draft.id)
        return run.id, prof_ids, draft_ids


# ---------------------------------------------------------------------------
# Gated path: SEND_ENABLED=false (the default + the explicit case)
# ---------------------------------------------------------------------------


def test_gated_path_raises_sending_disabled_and_logs(isolated_db, monkeypatch):
    """With 3 approved drafts and SEND_ENABLED=false:
    - SendingDisabled is raised
    - exactly 3 SendLog rows with outcome='send_disabled'
    - all 3 draft statuses remain APPROVED (unchanged)
    """
    monkeypatch.setenv("SEND_ENABLED", "false")
    run_id, _, draft_ids = _seed_n_approved_drafts(3)

    with pytest.raises(SendingDisabled, match="3 draft"):
        delivery.send_approved(run_id)

    with get_session() as s:
        send_logs = list(
            s.scalars(
                select(SendLog).where(
                    SendLog.outcome == SendOutcome.SEND_DISABLED
                )
            )
        )
        assert len(send_logs) == 3
        # Draft statuses unchanged
        for did in draft_ids:
            assert repo.get_draft(s, did).status == DraftStatus.APPROVED


def test_gated_path_with_zero_approved_returns_empty_report_no_raise(
    isolated_db, monkeypatch
):
    monkeypatch.setenv("SEND_ENABLED", "false")
    # Create a run but no approved drafts (just one pending_review)
    with get_session() as session:
        run = repo.create_run(
            session, field="x", goal="g", considerations="", count=1,
            resume_path="/tmp/r.pdf", template_text="t",
        )
        prof = repo.add_professor(
            session, run_id=run.id, name="X", institution="Y",
            email="x@y.edu", openalex_id="A",
        )
        repo.add_draft(
            session, run_id=run.id, professor_id=prof.id,
            subject="s", body="b", status=DraftStatus.PENDING_REVIEW,
        )
        run_id = run.id

    report = delivery.send_approved(run_id)
    assert report.attempted == 0
    assert report.sent == 0
    assert report.send_disabled == 0


def test_send_approved_unknown_run_raises(isolated_db, monkeypatch):
    monkeypatch.setenv("SEND_ENABLED", "false")
    with pytest.raises(NotFoundError):
        delivery.send_approved("bogus-id")


# ---------------------------------------------------------------------------
# Real send path: SEND_ENABLED=true (mocked Gmail)
# ---------------------------------------------------------------------------


def _make_fake_gmail_service(send_id_prefix: str = "gmail-msg-"):
    """A MagicMock-backed Gmail service.

    - getProfile().execute() → {"emailAddress": "me@example.com"}
    - messages().send().execute() → {"id": "<prefix><call#>"}
    """
    service = MagicMock()
    service.users().getProfile().execute.return_value = {
        "emailAddress": "me@example.com",
    }

    # Each call to send().execute() returns an incrementing id.
    counter = {"n": 0}

    def _send_execute(*_a, **_kw):
        counter["n"] += 1
        return {"id": f"{send_id_prefix}{counter['n']}"}

    service.users().messages().send().execute.side_effect = _send_execute
    return service


def test_real_send_path_with_mocked_gmail(isolated_db, monkeypatch):
    """With SEND_ENABLED=true and 3 approved drafts:
    - users.messages().send is called 3 times
    - all draft statuses become SENT
    - each SendLog row carries gmail_message_id
    """
    monkeypatch.setenv("SEND_ENABLED", "true")
    monkeypatch.setattr(delivery, "_load_credentials", lambda: MagicMock())

    fake_service = _make_fake_gmail_service()
    monkeypatch.setattr(delivery, "_gmail_service", lambda creds: fake_service)

    run_id, _, draft_ids = _seed_n_approved_drafts(3)
    report = delivery.send_approved(run_id)

    assert report.attempted == 3
    assert report.sent == 3
    assert report.errors == 0

    # Three users.messages().send().execute() calls happened
    assert fake_service.users().messages().send().execute.call_count == 3

    with get_session() as s:
        for did in draft_ids:
            d = repo.get_draft(s, did)
            assert d.status == DraftStatus.SENT
        sent_logs = list(
            s.scalars(select(SendLog).where(SendLog.outcome == SendOutcome.SENT))
        )
        assert len(sent_logs) == 3
        assert all(log.gmail_message_id is not None for log in sent_logs)


def test_real_send_path_continues_after_individual_failure(isolated_db, monkeypatch):
    """One per-draft Gmail error doesn't stop the others."""
    from googleapiclient.errors import HttpError

    monkeypatch.setenv("SEND_ENABLED", "true")
    monkeypatch.setattr(delivery, "_load_credentials", lambda: MagicMock())

    fake_service = MagicMock()
    fake_service.users().getProfile().execute.return_value = {
        "emailAddress": "me@example.com",
    }

    counter = {"n": 0}
    def _send_execute(*_a, **_kw):
        counter["n"] += 1
        if counter["n"] == 2:
            # Second call fails. Build a minimal HttpError.
            resp = SimpleNamespace(status=403, reason="rate limit")
            raise HttpError(resp=resp, content=b"nope")
        return {"id": f"msg-{counter['n']}"}

    fake_service.users().messages().send().execute.side_effect = _send_execute
    monkeypatch.setattr(delivery, "_gmail_service", lambda creds: fake_service)

    run_id, _, _ = _seed_n_approved_drafts(3)
    report = delivery.send_approved(run_id)

    assert report.attempted == 3
    assert report.sent == 2
    assert report.errors == 1
    assert any("draft" in e for e in report.error_messages)


# ---------------------------------------------------------------------------
# Daily cap
# ---------------------------------------------------------------------------


def test_daily_cap_raises_before_any_gmail_call(isolated_db, monkeypatch):
    """20 existing SENT logs today + a 21st attempt → DeliveryError before Gmail is touched."""
    monkeypatch.setenv("SEND_ENABLED", "true")
    monkeypatch.setenv("SEND_DAILY_CAP", "20")

    fake_creds_factory = MagicMock()
    monkeypatch.setattr(delivery, "_load_credentials", fake_creds_factory)
    fake_service_factory = MagicMock()
    monkeypatch.setattr(delivery, "_gmail_service", fake_service_factory)

    # Seed a run + 1 approved draft + 20 SENT SendLogs dated today
    run_id, _, _ = _seed_n_approved_drafts(1)
    with get_session() as s:
        for i in range(20):
            s.add(
                SendLog(
                    draft_id="dummy",
                    attempted_at=datetime.now(UTC).replace(tzinfo=None),
                    outcome=SendOutcome.SENT,
                    gmail_message_id=f"old-{i}",
                )
            )

    with pytest.raises(DeliveryError, match="cap reached"):
        delivery.send_approved(run_id)

    # Critical: Gmail was NEVER consulted
    fake_creds_factory.assert_not_called()
    fake_service_factory.assert_not_called()


# ---------------------------------------------------------------------------
# MIME construction
# ---------------------------------------------------------------------------


def test_build_mime_round_trip(isolated_db):
    """Verify the MIME message is parseable + has the right headers/body."""
    import base64
    from email import message_from_bytes

    run_id, _, draft_ids = _seed_n_approved_drafts(1)
    with get_session() as s:
        draft = repo.get_draft(s, draft_ids[0])

    raw = delivery._build_mime(
        draft, to_addr="prof@uni.edu", from_addr="me@example.com"
    )

    # The base64url-encoded blob decodes to a valid MIME message.
    import email.policy
    decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
    msg = message_from_bytes(decoded, policy=email.policy.default)
    assert msg["To"] == "prof@uni.edu"
    assert msg["From"] == "me@example.com"
    assert msg["Subject"] == draft.subject
    body = msg.get_content().rstrip()
    assert body.startswith("Body for prof 0.")
    assert "Multiline." in body
