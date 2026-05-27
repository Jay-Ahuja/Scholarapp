"""SQLAlchemy 2.x typed models for Scholarapp.

Schema overview:

    Run 1──N Professor 1──N Project
                  │           ▲
                  │           │
                  └─N MatchedProject
                  │
    Run 1─────────┴────N Draft 1──N SendLog

Status enums live alongside the models. Stored as their `.value` (lowercase string)
so the DB is easy to inspect with `sqlite3`.

This module defines schema only — no query logic, no business rules. CRUD lives in
`scholarapp.db.repo`; session/engine wiring in `scholarapp.db.session`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import JSON, ForeignKey, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    """Pipeline progress for a Run. Linear except for FAILED, which is a sink."""

    PENDING = "pending"
    PARSING = "parsing"
    DISCOVERING = "discovering"
    MATCHING = "matching"
    DRAFTING = "drafting"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


class DraftStatus(str, Enum):
    """Per-draft lifecycle. See docs/02-persistence.md for the full state machine."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    SEND_DISABLED = "send_disabled"


class SendOutcome(str, Enum):
    """Result of a single send attempt logged in SendLog."""

    SENT = "sent"
    SEND_DISABLED = "send_disabled"
    ERROR = "error"


def _enum_col(enum_cls: type[Enum]) -> SAEnum:
    """Store enums as their lowercase `.value` strings, not Python names."""
    return SAEnum(
        enum_cls,
        values_callable=lambda e: [m.value for m in e],
        native_enum=False,
        length=32,
    )


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Run(Base):
    """One end-to-end pipeline invocation. Created by `scholar run`."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_uuid)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    status: Mapped[RunStatus] = mapped_column(_enum_col(RunStatus), default=RunStatus.PENDING)

    field: Mapped[str]
    goal: Mapped[str] = mapped_column(Text)
    considerations: Mapped[str] = mapped_column(Text)
    count: Mapped[int]

    resume_path: Mapped[str]
    template_text: Mapped[str] = mapped_column(Text)

    error: Mapped[str | None] = mapped_column(Text)


class Professor(Base):
    """A professor discovered for a Run (Step 4)."""

    __tablename__ = "professors"

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)

    name: Mapped[str]
    institution: Mapped[str]
    email: Mapped[str]
    openalex_id: Mapped[str]
    faculty_page_url: Mapped[str | None]
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class Project(Base):
    """A recent work by a Professor (Step 4). Pool from which MatchedProject draws."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    professor_id: Mapped[str] = mapped_column(ForeignKey("professors.id"), index=True)

    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None]
    year: Mapped[int | None]
    abstract: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class MatchedProject(Base):
    """A Project picked as relevant for the user (Step 5), with an LLM-written rationale."""

    __tablename__ = "matched_projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    professor_id: Mapped[str] = mapped_column(ForeignKey("professors.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    why_relevant: Mapped[str] = mapped_column(Text)


class Draft(Base):
    """A drafted email for one Professor in one Run (Step 6)."""

    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    professor_id: Mapped[str] = mapped_column(ForeignKey("professors.id"), index=True)

    subject: Mapped[str]
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[DraftStatus] = mapped_column(
        _enum_col(DraftStatus), default=DraftStatus.PENDING_REVIEW
    )
    file_path: Mapped[str | None]
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class SendLog(Base):
    """One row per send *attempt* (Step 8). Includes gated send_disabled attempts."""

    __tablename__ = "send_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"), index=True)
    attempted_at: Mapped[datetime] = mapped_column(default=_utcnow)
    outcome: Mapped[SendOutcome] = mapped_column(_enum_col(SendOutcome))
    error: Mapped[str | None] = mapped_column(Text)
    gmail_message_id: Mapped[str | None]


class RunUsage(Base):
    """Realized per-run cost, written once a run finishes (Step: cost accounting).

    A SEPARATE table — not extra columns on `runs` — because the project has no
    migrations: db/session.py's idempotent create_all() adds missing tables but
    cannot ALTER an existing one. Decoupling also keeps a run's input metadata
    (runs) distinct from its measured spend, and lets the cost engine average a
    fresh history without touching the core pipeline schema.

    `stage_costs` is a JSON object carrying BOTH the per-stage USD costs AND the
    per-stage realized professor counts, so the cost engine can divide each
    stage's spend by the size that stage actually processed (a budget-stopped run
    drafts fewer than `count`). Because the project has no migrations (create_all
    cannot ALTER to add a column), the realized counts ride inside this existing
    JSON column rather than a new one. New rows use the tagged shape
    {"costs": {stage: float}, "counts": {stage: int}}; pre-existing rows are the
    legacy bare {stage: float} cost map (no counts). usage.pack_stage_usage /
    unpack_stage_usage own the encoding + legacy detection — the column itself is
    schema-agnostic JSON. `total_usd` is stored denormalized for cheap
    newest-first listing without re-summing the JSON in SQL.
    """

    __tablename__ = "run_usage"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    count: Mapped[int]
    total_usd: Mapped[float]
    stage_costs: Mapped[dict] = mapped_column(JSON)


class ResumeCache(Base):
    """Content-hash cache for parsed resumes.

    Keyed by SHA-256 of the PDF bytes. Hit when the same PDF (byte-for-byte
    identical) was parsed in a previous run — skips a Sonnet call worth ~$0.015.
    No invalidation logic needed; the hash IS the validity check.
    """

    __tablename__ = "resume_cache"

    sha256: Mapped[str] = mapped_column(primary_key=True)
    parsed_json: Mapped[dict] = mapped_column(JSON)
    cached_at: Mapped[datetime] = mapped_column(default=_utcnow)
