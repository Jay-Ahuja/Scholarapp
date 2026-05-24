"""Step 8 — Delivery: Gmail send path, gated behind SEND_ENABLED=false.

The whole path — OAuth, MIME construction, the daily cap, the send loop — is
implemented and tested. But by default `config.send_enabled` is False, and
`send_approved` short-circuits into a "log only, never call Gmail" branch.
Flipping `SEND_ENABLED=true` in .env activates the real send.

The deliberate split:

- Setup once: `scholar init` triggers `_oauth_flow()` if `client_secret.json` is
  present, writes a refresh token to `~/.scholarapp/credentials.json`. The
  setup is harmless to run even before you intend to send — it just authorizes.
- Run per send: `scholar send <run_id>` calls `send_approved`, which is the
  only function that ever calls the Gmail API. The SEND_ENABLED check sits at
  the top of that function — there is no other code path that reaches Gmail.

Scope is intentionally narrow: `gmail.send` only. We can't read inbox, can't
modify other mail, can't delete anything. The token is a write capability for
outgoing mail and nothing else. See docs/08-delivery.md for the why.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from email.message import EmailMessage

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field
from sqlalchemy import select

from scholarapp.config import load_settings
from scholarapp.db import repo
from scholarapp.db.models import (
    Draft,
    DraftStatus,
    Professor,
    SendLog,
    SendOutcome,
)
from scholarapp.db.session import get_session
from scholarapp.errors import DeliveryError, NotFoundError, SendingDisabled

logger = logging.getLogger(__name__)

# Narrow scope: write-only outgoing mail. Cannot read inbox or modify other mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class DeliveryReport(BaseModel):
    """Result of send_approved."""

    attempted: int = 0
    sent: int = 0
    send_disabled: int = 0
    errors: int = 0
    error_messages: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# OAuth + credentials
# ---------------------------------------------------------------------------


def _oauth_flow() -> Credentials:
    """Run the Google OAuth installed-app flow and persist a refresh token.

    Opens a browser, captures the auth code via a local redirect, exchanges for
    tokens, writes them to `~/.scholarapp/credentials.json` with chmod 600.
    """
    settings = load_settings()
    client_secret = settings.client_secret_path
    if not client_secret.exists():
        raise DeliveryError(
            f"client_secret.json not found at {client_secret}. "
            "Create a Google Cloud OAuth Desktop client and download the JSON "
            "(see docs/08-delivery.md for the full setup)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0)  # 0 = OS picks a free port
    settings.credentials_path.write_text(creds.to_json())
    try:
        settings.credentials_path.chmod(0o600)
    except OSError:
        # chmod may not be supported on all filesystems; not fatal
        logger.warning(
            "Could not chmod 600 on %s; refresh token may be world-readable.",
            settings.credentials_path,
        )
    return creds


def _load_credentials() -> Credentials:
    """Load saved credentials, refreshing if expired. Raises if missing.

    First-time setup: run `scholar init` after placing client_secret.json.
    """
    settings = load_settings()
    cred_path = settings.credentials_path
    if not cred_path.exists():
        raise DeliveryError(
            f"No Gmail credentials at {cred_path}. "
            "Run `scholar init` after placing client_secret.json under "
            "~/.scholarapp/. See docs/08-delivery.md."
        )
    creds = Credentials.from_authorized_user_file(str(cred_path), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            cred_path.write_text(creds.to_json())
        except Exception as e:  # noqa: BLE001 — surface as DeliveryError
            raise DeliveryError(
                f"Failed to refresh Gmail credentials: {e}. "
                f"Delete {cred_path} and re-run `scholar init` to re-authorize."
            ) from e
    return creds


def _gmail_service(creds: Credentials):
    """Build the Gmail API service. Factored out as a single mock surface for tests."""
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# MIME construction
# ---------------------------------------------------------------------------


def _build_mime(draft: Draft, to_addr: str, from_addr: str) -> str:
    """Build an RFC 822 MIME message and return its base64url-encoded raw string.

    Plain text only — no HTML, no attachments. The draft.body is multi-paragraph
    plain text; Gmail will render line breaks as the user wrote them.

    Note: the spec says `_build_mime(draft, from_addr)`. We need `to_addr` too
    (Draft has professor_id but not the email itself), so the signature has an
    extra arg. The caller (`send_approved`) looks the To address up from the
    Professor row.
    """
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["From"] = from_addr
    msg["Subject"] = draft.subject
    msg.set_content(draft.body)
    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# Daily cap
# ---------------------------------------------------------------------------


def _count_sends_today(session) -> int:
    """Number of SendLog rows with outcome=sent and attempted_at in the current UTC day."""
    today_start = datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).replace(tzinfo=None)
    stmt = select(SendLog).where(
        SendLog.outcome == SendOutcome.SENT,
        SendLog.attempted_at >= today_start,
    )
    return len(list(session.scalars(stmt)))


# ---------------------------------------------------------------------------
# send_approved (the only entry point that talks to Gmail)
# ---------------------------------------------------------------------------


def send_approved(run_id: str) -> DeliveryReport:
    """Send (or, when gated, simulate-send) every approved draft for a run.

    Raises:
      - SendingDisabled when SEND_ENABLED=False — SendLog rows are written
        but no Gmail call happens.
      - DeliveryError on configuration / cap / credential failures.

    Per-draft Gmail errors are caught: one failure does not stop the rest. Each
    failure is logged to SendLog with outcome='error' and surfaces in the
    returned DeliveryReport.error_messages.
    """
    settings = load_settings()

    with get_session() as session:
        if repo.get_run(session, run_id) is None:
            raise NotFoundError(f"No run with id {run_id}")
        drafts = repo.list_drafts_for_run_by_status(
            session, run_id, DraftStatus.APPROVED
        )

    report = DeliveryReport(attempted=len(drafts))

    if not drafts:
        return report

    # GATED PATH ----------------------------------------------------------
    if not settings.send_enabled:
        with get_session() as session:
            for d in drafts:
                repo.add_send_log(
                    session, draft_id=d.id, outcome=SendOutcome.SEND_DISABLED
                )
                report.send_disabled += 1
        raise SendingDisabled(
            f"{report.send_disabled} draft(s) would have been sent. "
            "SEND_ENABLED is false; no email was actually sent. "
            "See docs/08-delivery.md for how to enable."
        )

    # REAL SEND PATH ------------------------------------------------------
    # Daily cap check happens BEFORE we call Gmail or load credentials.
    with get_session() as session:
        already_sent_today = _count_sends_today(session)
    if already_sent_today >= settings.send_daily_cap:
        raise DeliveryError(
            f"Daily send cap reached: {already_sent_today}/"
            f"{settings.send_daily_cap} sent today. "
            "Wait until tomorrow or raise SEND_DAILY_CAP."
        )

    creds = _load_credentials()
    service = _gmail_service(creds)

    try:
        profile = service.users().getProfile(userId="me").execute()
    except HttpError as e:
        raise DeliveryError(f"Failed to fetch Gmail profile: {e}") from e
    from_addr = profile.get("emailAddress", "")
    if not from_addr:
        raise DeliveryError("Gmail profile returned no emailAddress")

    # Look up professor emails for the To: header (Draft has only professor_id).
    with get_session() as session:
        professors = {
            p.id: p for p in repo.list_professors_for_run(session, run_id)
        }

    remaining_budget = settings.send_daily_cap - already_sent_today

    for draft in drafts:
        if report.sent >= remaining_budget:
            logger.info(
                "Stopping at daily cap: %d more drafts remain approved.",
                len(drafts) - (report.sent + report.errors),
            )
            break

        prof = professors.get(draft.professor_id)
        if prof is None:
            with get_session() as session:
                repo.add_send_log(
                    session,
                    draft_id=draft.id,
                    outcome=SendOutcome.ERROR,
                    error="professor row missing",
                )
            report.errors += 1
            report.error_messages.append(
                f"draft {draft.id}: professor row missing"
            )
            continue

        try:
            raw = _build_mime(draft, to_addr=prof.email, from_addr=from_addr)
            result = service.users().messages().send(
                userId="me", body={"raw": raw}
            ).execute()
            gmail_msg_id = result.get("id") if isinstance(result, dict) else None
            with get_session() as session:
                repo.update_draft(session, draft.id, status=DraftStatus.SENT)
                repo.add_send_log(
                    session,
                    draft_id=draft.id,
                    outcome=SendOutcome.SENT,
                    gmail_message_id=gmail_msg_id,
                )
            report.sent += 1
        except HttpError as e:
            with get_session() as session:
                repo.add_send_log(
                    session,
                    draft_id=draft.id,
                    outcome=SendOutcome.ERROR,
                    error=str(e),
                )
            report.errors += 1
            report.error_messages.append(f"draft {draft.id}: {e}")
        except Exception as e:  # noqa: BLE001 — defensive; one bad draft can't kill the rest
            with get_session() as session:
                repo.add_send_log(
                    session,
                    draft_id=draft.id,
                    outcome=SendOutcome.ERROR,
                    error=f"unexpected: {e}",
                )
            report.errors += 1
            report.error_messages.append(f"draft {draft.id}: unexpected: {e}")

    return report
