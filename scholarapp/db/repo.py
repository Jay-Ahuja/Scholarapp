"""Thin CRUD helpers over the SQLAlchemy models.

Rules for this module:

- Pure read/write. No business logic, no LLM calls, no HTTP. Validation and orchestration
  belong upstream.
- Every function takes a `Session` as its first argument. The caller manages the
  session lifecycle (typically via `scholarapp.db.session.get_session`).
- Functions that look something up by id return `None` when missing; callers decide
  whether that should raise.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from scholarapp.db.models import (
    Draft,
    DraftStatus,
    MatchedProject,
    Professor,
    Project,
    ResumeCache,
    Run,
    RunStatus,
    RunUsage,
    SendLog,
    SendOutcome,
)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def create_run(
    session: Session,
    *,
    field: str,
    goal: str,
    considerations: str,
    count: int,
    resume_path: str,
    template_text: str,
) -> Run:
    run = Run(
        field=field,
        goal=goal,
        considerations=considerations,
        count=count,
        resume_path=resume_path,
        template_text=template_text,
    )
    session.add(run)
    session.flush()
    return run


def get_run(session: Session, run_id: str) -> Run | None:
    return session.get(Run, run_id)


def list_runs(session: Session) -> list[Run]:
    """Newest first."""
    stmt = select(Run).order_by(Run.created_at.desc())
    return list(session.scalars(stmt))


def update_run_status(
    session: Session, run_id: str, status: RunStatus, *, error: str | None = None
) -> None:
    run = session.get(Run, run_id)
    if run is None:
        raise KeyError(run_id)
    run.status = status
    if error is not None:
        run.error = error


# ---------------------------------------------------------------------------
# Professor + Project
# ---------------------------------------------------------------------------


def add_professor(
    session: Session,
    *,
    run_id: str,
    name: str,
    institution: str,
    email: str,
    openalex_id: str,
    faculty_page_url: str | None = None,
    raw_json: dict | None = None,
) -> Professor:
    prof = Professor(
        run_id=run_id,
        name=name,
        institution=institution,
        email=email,
        openalex_id=openalex_id,
        faculty_page_url=faculty_page_url,
        raw_json=raw_json,
    )
    session.add(prof)
    session.flush()
    return prof


def get_professor(session: Session, professor_id: str) -> Professor | None:
    return session.get(Professor, professor_id)


def list_professors_for_run(session: Session, run_id: str) -> list[Professor]:
    stmt = select(Professor).where(Professor.run_id == run_id)
    return list(session.scalars(stmt))


def add_project(
    session: Session,
    *,
    professor_id: str,
    title: str,
    url: str | None = None,
    year: int | None = None,
    abstract: str | None = None,
    raw_json: dict | None = None,
) -> Project:
    project = Project(
        professor_id=professor_id,
        title=title,
        url=url,
        year=year,
        abstract=abstract,
        raw_json=raw_json,
    )
    session.add(project)
    session.flush()
    return project


def list_projects_for_professor(session: Session, professor_id: str) -> list[Project]:
    stmt = select(Project).where(Project.professor_id == professor_id)
    return list(session.scalars(stmt))


def add_matched_project(
    session: Session, *, professor_id: str, project_id: int, why_relevant: str
) -> MatchedProject:
    matched = MatchedProject(
        professor_id=professor_id, project_id=project_id, why_relevant=why_relevant
    )
    session.add(matched)
    session.flush()
    return matched


def list_matched_projects_for_professor(
    session: Session, professor_id: str
) -> list[MatchedProject]:
    stmt = select(MatchedProject).where(MatchedProject.professor_id == professor_id)
    return list(session.scalars(stmt))


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


def add_draft(
    session: Session,
    *,
    run_id: str,
    professor_id: str,
    subject: str,
    body: str,
    status: DraftStatus = DraftStatus.PENDING_REVIEW,
) -> Draft:
    draft = Draft(
        run_id=run_id,
        professor_id=professor_id,
        subject=subject,
        body=body,
        status=status,
    )
    session.add(draft)
    session.flush()
    return draft


def get_draft(session: Session, draft_id: str) -> Draft | None:
    return session.get(Draft, draft_id)


def list_drafts_for_run(session: Session, run_id: str) -> list[Draft]:
    stmt = select(Draft).where(Draft.run_id == run_id)
    return list(session.scalars(stmt))


def list_drafts_for_run_by_status(
    session: Session, run_id: str, status: DraftStatus
) -> list[Draft]:
    stmt = select(Draft).where(Draft.run_id == run_id, Draft.status == status)
    return list(session.scalars(stmt))


def update_draft(
    session: Session,
    draft_id: str,
    *,
    subject: str | None = None,
    body: str | None = None,
    status: DraftStatus | None = None,
    file_path: str | None = None,
) -> None:
    draft = session.get(Draft, draft_id)
    if draft is None:
        raise KeyError(draft_id)
    if subject is not None:
        draft.subject = subject
    if body is not None:
        draft.body = body
    if status is not None:
        draft.status = status
    if file_path is not None:
        draft.file_path = file_path


# ---------------------------------------------------------------------------
# SendLog
# ---------------------------------------------------------------------------


def add_send_log(
    session: Session,
    *,
    draft_id: str,
    outcome: SendOutcome,
    error: str | None = None,
    gmail_message_id: str | None = None,
) -> SendLog:
    log = SendLog(
        draft_id=draft_id,
        outcome=outcome,
        error=error,
        gmail_message_id=gmail_message_id,
    )
    session.add(log)
    session.flush()
    return log


def list_send_logs_for_draft(session: Session, draft_id: str) -> list[SendLog]:
    stmt = select(SendLog).where(SendLog.draft_id == draft_id)
    return list(session.scalars(stmt))


# ---------------------------------------------------------------------------
# ResumeCache — content-hash lookup for parsed resumes
# ---------------------------------------------------------------------------


def get_cached_resume(session: Session, sha256: str) -> dict | None:
    """Return the cached parsed-resume JSON for this content hash, or None."""
    row = session.get(ResumeCache, sha256)
    return row.parsed_json if row else None


def cache_resume(session: Session, sha256: str, parsed_json: dict) -> None:
    """Insert-or-replace the parsed resume keyed by content hash."""
    session.merge(ResumeCache(sha256=sha256, parsed_json=parsed_json))


# ---------------------------------------------------------------------------
# RunUsage — realized per-run cost, used as cost-estimation history
# ---------------------------------------------------------------------------


def add_run_usage(
    session: Session,
    *,
    run_id: str,
    count: int,
    total_usd: float,
    stage_costs: dict[str, float],
) -> RunUsage:
    usage = RunUsage(
        run_id=run_id,
        count=count,
        total_usd=total_usd,
        stage_costs=stage_costs,
    )
    session.add(usage)
    session.flush()
    return usage


def list_recent_run_usage(session: Session, *, limit: int = 20) -> list[RunUsage]:
    """Newest first. Caller feeds these into the cost estimator as history."""
    stmt = select(RunUsage).order_by(RunUsage.created_at.desc()).limit(limit)
    return list(session.scalars(stmt))
