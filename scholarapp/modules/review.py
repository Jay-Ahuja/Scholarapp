"""Step 7 — Review: drafts ↔ editable markdown files on disk.

Two entry points:

- `write_drafts_to_disk(run_id)` reads Draft rows for a run, writes one .md file
  per draft to `<DRAFTS_DIR>/<run_id>/<slug>.md` (default `./drafts/<run_id>/`
  in the cwd — visible in Finder), and updates each Draft.file_path to point at
  the new file. Returns the drafts directory path.
- `sync_drafts_from_disk(run_id)` parses every .md in that directory, applies
  the editable fields (subject, body, status) to the DB, and warns on
  read-only field tampering. Returns a `SyncReport` summarizing what changed.

The file is the source of truth at sync time: last-write-wins for the editable
fields. Read-only fields (draft_id, professor, institution, email,
matched_projects) are taken from the DB even if edited in the file.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from scholarapp.config import load_settings
from scholarapp.db import repo
from scholarapp.db.models import (
    DraftStatus,
    Professor,
    Project,
)
from scholarapp.db.session import get_session
from scholarapp.errors import NotFoundError, ReviewError

logger = logging.getLogger(__name__)

# Allowed Draft.status transitions when the file's status differs from the DB.
# Self-transitions (same status) are always allowed (no-op).
# Anything not listed is rejected.
_ALLOWED_TRANSITIONS: dict[DraftStatus, set[DraftStatus]] = {
    DraftStatus.PENDING_REVIEW: {DraftStatus.APPROVED, DraftStatus.REJECTED},
    DraftStatus.APPROVED: {DraftStatus.PENDING_REVIEW},
    # rejected, sent, send_disabled have no outgoing edges (terminal)
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ParsedDraft(BaseModel):
    """Typed view of a draft .md file's contents."""

    draft_id: str
    professor: str
    institution: str
    email: str
    matched_projects: list[dict] = Field(default_factory=list)
    status: str
    updated_at: datetime | None = None  # snapshot taken at write time
    subject: str
    body: str


class SyncReport(BaseModel):
    """Result of sync_drafts_from_disk."""

    updated: int = 0          # drafts whose subject or body changed
    unchanged: int = 0        # drafts where nothing changed
    status_changed: int = 0   # drafts whose status changed (may overlap with `updated`)
    errors: int = 0           # files that failed to sync (parse error, invalid transition, ...)
    warnings: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"updated: {self.updated}",
            f"unchanged: {self.unchanged}",
            f"status_changed: {self.status_changed}",
            f"errors: {self.errors}",
        ]
        if self.warnings:
            lines.append("warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        if self.error_messages:
            lines.append("errors:")
            for e in self.error_messages:
                lines.append(f"  - {e}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SLUG_NONALNUM = re.compile(r"[^a-z0-9-]+")

# Draft files routinely contain non-ASCII characters Claude emits (the Unicode
# hyphen U+2010, en/em dashes, curly quotes). Pin UTF-8 on every text read/write
# so the round-trip is stable regardless of the platform's locale code page
# (cp1252 on Windows can't encode these and raises UnicodeEncodeError).
_FILE_ENCODING = "utf-8"

# Legacy fallback for the READ path only. Older versions wrote draft files using
# the Windows locale default (cp1252), so on-disk files can carry bytes like 0x96
# (cp1252 en-dash) that aren't valid UTF-8. cp1252 is the accurate inverse of what
# wrote those files (0x96 -> U+2013 en-dash); latin-1 would mis-map 0x80-0x9F and
# errors="replace"/"ignore" would corrupt the glyph. Writes stay UTF-8, so any
# file read via this fallback is normalized to UTF-8 the next time it's rewritten
# by the normal flow — self-healing, no migration step needed.
_LEGACY_FILE_ENCODING = "cp1252"


def _read_text(path: Path) -> str:
    """Read a draft text file with universal-newline translation.

    UTF-8 first; on a UTF-8 decode failure, fall back to cp1252 (the legacy
    locale-default encoding older versions wrote on Windows) and warn. A valid
    UTF-8 file reads exactly as before — the fallback only engages when the
    strict UTF-8 decode raises.

    Universal newlines (the default) collapse any \\r\\n on disk back to \\n so
    parsing and write->read equality stay platform-independent.
    """
    try:
        return path.read_text(encoding=_FILE_ENCODING)
    except UnicodeDecodeError:
        logger.warning(
            "%s is not valid UTF-8; falling back to %s (legacy encoding). "
            "Re-saving this draft through the normal flow will normalize it to UTF-8.",
            path.name,
            _LEGACY_FILE_ENCODING,
        )
        return path.read_text(encoding=_LEGACY_FILE_ENCODING)


def _write_text(path: Path, content: str) -> None:
    """Write a draft text file as UTF-8 without newline translation.

    `newline=""` disables the platform line-ending translation so the bytes on
    disk keep the \\n that callers render; this keeps the write->read round-trip
    and sync's conflict-detection snapshot byte-stable on Windows.
    """
    path.write_text(content, encoding=_FILE_ENCODING, newline="")


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize a datetime to naive UTC for comparison.

    SQLite stores updated_at without tz info; the YAML round-trip restores it
    as tz-aware (+00:00). Both represent the same moment, but compare unequal
    in Python — so we strip tz before comparing.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _make_slug(name: str, draft_id: str) -> str:
    """Filename slug: lowercase-last-name + first 8 chars of draft_id.

    "Karl J. Friston" + "7f3a..." → "friston-7f3aabcd".
    Hyphenated names ("Hyman-Smith") preserve the hyphen.
    Falls back to "unknown" if the name is empty or unparsable.
    """
    if name:
        # Take the last whitespace-separated token, strip dots/quotes, lowercase.
        last = name.strip().split()[-1].lower().replace(".", "").replace(",", "")
        last = _SLUG_NONALNUM.sub("", last)
    else:
        last = ""
    if not last:
        last = "unknown"
    return f"{last}-{draft_id[:8]}"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (yaml_dict, body_text). Raises ReviewError on missing frontmatter."""
    if not text.startswith("---"):
        raise ReviewError("File is missing leading '---' frontmatter delimiter")
    # text = "---\n<yaml>\n---\n<rest>"
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ReviewError("File frontmatter is unterminated (missing closing '---')")
    yaml_text = parts[1]
    rest = parts[2].lstrip("\n")
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        raise ReviewError(f"Invalid YAML frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise ReviewError("Frontmatter YAML must be a mapping")
    return data, rest


def _split_subject_and_body(rest: str) -> tuple[str, str]:
    """The first line is `Subject: ...`; everything after a blank line is the body."""
    if "\n" not in rest:
        line, body = rest, ""
    else:
        line, body = rest.split("\n", 1)
    line = line.strip()
    if not line.lower().startswith("subject:"):
        raise ReviewError(
            "Expected first non-frontmatter line to start with 'Subject:'"
        )
    subject = line[len("subject:"):].strip()
    # Trim a single leading blank line that visually separates Subject from body.
    body = body.lstrip("\n").rstrip("\n")
    return subject, body


def parse_draft_file(path: Path) -> ParsedDraft:
    """Read a draft markdown file and return its typed contents."""
    text = _read_text(path)
    fm, rest = _split_frontmatter(text)
    subject, body = _split_subject_and_body(rest)

    required = ("draft_id", "professor", "institution", "email", "status")
    missing = [k for k in required if k not in fm]
    if missing:
        raise ReviewError(f"Frontmatter is missing required field(s): {missing}")

    updated_at = fm.get("updated_at")
    if isinstance(updated_at, str):
        try:
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            updated_at = None

    return ParsedDraft(
        draft_id=str(fm["draft_id"]),
        professor=str(fm["professor"]),
        institution=str(fm["institution"]),
        email=str(fm["email"]),
        matched_projects=list(fm.get("matched_projects") or []),
        status=str(fm["status"]),
        updated_at=updated_at,
        subject=subject,
        body=body,
    )


def render_draft_file(
    *,
    draft_id: str,
    professor_name: str,
    institution: str,
    email: str,
    matched: list[dict[str, str]],
    status: str,
    updated_at: datetime,
    subject: str,
    body: str,
) -> str:
    """Build the .md content for one draft. Inverse of parse_draft_file."""
    frontmatter = {
        "draft_id": draft_id,
        "professor": professor_name,
        "institution": institution,
        "email": email,
        "matched_projects": matched,
        "status": status,
        "updated_at": updated_at.isoformat(),
    }
    yaml_text = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return f"---\n{yaml_text}---\nSubject: {subject}\n\n{body}\n"


def write_status_in_file(path: Path, new_status: str) -> None:
    """Rewrite just the `status:` field in an existing draft file.

    Used by `scholar approve` so the user doesn't have to open each file by hand.
    Preserves every other byte of the file.
    """
    parsed = parse_draft_file(path)
    text = _read_text(path)
    fm, rest = _split_frontmatter(text)
    fm["status"] = new_status
    new_yaml = yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    _write_text(path, f"---\n{new_yaml}---\n{rest}")


# ---------------------------------------------------------------------------
# write_drafts_to_disk
# ---------------------------------------------------------------------------


def write_drafts_to_disk(run_id: str) -> Path:
    """Materialize each Draft for `run_id` as a markdown file under drafts/<run_id>/.

    Default location is `<cwd>/drafts/<run_id>/` so the files are visible in Finder.
    Override with the DRAFTS_DIR env var. Idempotent — re-running overwrites the
    files and updates Draft.file_path.
    """
    settings = load_settings()
    drafts_dir = settings.drafts_root / run_id
    drafts_dir.mkdir(parents=True, exist_ok=True)

    with get_session() as session:
        if repo.get_run(session, run_id) is None:
            raise NotFoundError(f"No run with id {run_id}")

        drafts = repo.list_drafts_for_run(session, run_id)
        professors = {
            p.id: p for p in repo.list_professors_for_run(session, run_id)
        }

        for draft in drafts:
            prof = professors.get(draft.professor_id)
            if prof is None:
                logger.warning(
                    "Draft %s references missing professor %s — skipping write.",
                    draft.id,
                    draft.professor_id,
                )
                continue

            matched_info: list[dict[str, str]] = []
            for mp in repo.list_matched_projects_for_professor(session, prof.id):
                project = session.get(Project, mp.project_id)
                if project is None:
                    continue
                matched_info.append(
                    {"title": project.title, "why": mp.why_relevant}
                )

            slug = _make_slug(prof.name, draft.id)
            file_path = drafts_dir / f"{slug}.md"

            # Set file_path + flush FIRST so SQLAlchemy's `onupdate=_utcnow` fires
            # on the row and `draft.updated_at` advances in memory. Then render the
            # file with the post-flush timestamp — this way the file's frontmatter
            # snapshot matches the DB exactly, so sync's conflict-detection
            # warning (DB updated_at > file snapshot) doesn't fire spuriously on
            # every first sync after a write.
            draft.file_path = str(file_path.resolve())
            session.flush()

            content = render_draft_file(
                draft_id=draft.id,
                professor_name=prof.name,
                institution=prof.institution,
                email=prof.email,
                matched=matched_info,
                status=draft.status.value,
                updated_at=draft.updated_at,
                subject=draft.subject,
                body=draft.body,
            )
            _write_text(file_path, content)

    return drafts_dir


# ---------------------------------------------------------------------------
# sync_drafts_from_disk
# ---------------------------------------------------------------------------


def sync_drafts_from_disk(run_id: str) -> SyncReport:
    """Read every .md under drafts/<run_id>/ and apply editable changes to the DB."""
    settings = load_settings()
    drafts_dir = settings.drafts_root / run_id
    if not drafts_dir.exists():
        raise NotFoundError(
            f"No drafts directory at {drafts_dir} for run {run_id}. "
            f"Run `scholar review {run_id}` first."
        )

    report = SyncReport()
    files = sorted(drafts_dir.glob("*.md"))

    with get_session() as session:
        for file_path in files:
            try:
                _sync_one_file(session, run_id, file_path, report)
            except ReviewError as e:
                report.errors += 1
                report.error_messages.append(f"{file_path.name}: {e}")
            except Exception as e:  # noqa: BLE001 — defensive; we want all files attempted
                report.errors += 1
                report.error_messages.append(f"{file_path.name}: unexpected: {e}")

    return report


def _sync_one_file(session, run_id: str, file_path: Path, report: SyncReport) -> None:
    parsed = parse_draft_file(file_path)

    draft = repo.get_draft(session, parsed.draft_id)
    if draft is None:
        raise ReviewError(f"draft_id {parsed.draft_id} is not in the DB")
    if draft.run_id != run_id:
        raise ReviewError(
            f"draft belongs to a different run ({draft.run_id}, not {run_id})"
        )

    # Conflict detection: warn if the DB was modified after the file was written.
    # Normalize to naive UTC for comparison — SQLite stores updated_at as naive
    # while the round-tripped YAML value comes back tz-aware (+00:00).
    if parsed.updated_at is not None:
        file_ts = _to_naive_utc(parsed.updated_at)
        db_ts = _to_naive_utc(draft.updated_at)
        if db_ts != file_ts:
            report.warnings.append(
                f"{file_path.name}: DB updated_at ({db_ts.isoformat()}) "
                f"differs from the file's snapshot ({file_ts.isoformat()}); "
                "another process may have modified this draft after the file was written"
            )

    # Read-only field tampering: ignore + warn.
    prof = session.get(Professor, draft.professor_id)
    if prof is not None:
        if parsed.professor != prof.name:
            report.warnings.append(
                f"{file_path.name}: 'professor' was edited to {parsed.professor!r}; "
                f"ignoring (read-only). Original: {prof.name!r}"
            )
        if parsed.institution != prof.institution:
            report.warnings.append(
                f"{file_path.name}: 'institution' was edited; ignoring (read-only)."
            )
        if parsed.email != prof.email:
            report.warnings.append(
                f"{file_path.name}: 'email' was edited to {parsed.email!r}; "
                f"ignoring (read-only). Original: {prof.email!r}"
            )

    # Status transition validation.
    new_status_str = parsed.status.strip().lower()
    try:
        new_status = DraftStatus(new_status_str)
    except ValueError as e:
        raise ReviewError(
            f"invalid status {parsed.status!r}; valid values: "
            f"{[s.value for s in DraftStatus]}"
        ) from e

    old_status = draft.status
    status_changed = old_status != new_status
    if status_changed and new_status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
        raise ReviewError(
            f"status transition {old_status.value!r} → {new_status.value!r} "
            f"is not allowed. Allowed from {old_status.value!r}: "
            f"{sorted(s.value for s in _ALLOWED_TRANSITIONS.get(old_status, set()))}"
        )

    # Apply the editable fields.
    changed_subject = parsed.subject != draft.subject
    changed_body = parsed.body != draft.body
    if changed_subject:
        draft.subject = parsed.subject
    if changed_body:
        draft.body = parsed.body
    if status_changed:
        draft.status = new_status
        report.status_changed += 1

    if changed_subject or changed_body:
        report.updated += 1
    elif not status_changed:
        report.unchanged += 1
