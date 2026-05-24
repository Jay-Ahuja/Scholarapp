"""Step 6 — Drafting: write one personalized cold email per professor.

Produces the actual artifact the whole pipeline is built around: a `Draft` row
(subject + body) that goes into review (Step 7) and eventually gets sent (Step 8).

Public entry points:

- `draft_email(...)` — one Claude call for one professor.
- `draft_emails_for_run(...)` — semaphore-bounded `asyncio.gather` across
  professors. The CLI uses this.

Caching strategy: the system prompt holds the user's template + condensed resume
+ drafting rules — all of which are IDENTICAL across professors in the same run.
We mark it with `cache_control: ephemeral` so the first per-professor call writes
the cache (slight premium) and every subsequent call reads it (~90% discount on
those tokens). For a 10-professor run this saves roughly 35% on drafting.

Sonnet is intentional. Drafting is where the user actually feels quality —
generic phrasing or a vague ask kills reply rates. Haiku for this would
undercut the entire pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from importlib.resources import files
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError

from scholarapp import usage as usage_tracker
from scholarapp.config import load_settings
from scholarapp.errors import ConfigError, DraftingError
from scholarapp.modules.ingestion import ResumeData
from scholarapp.modules.matching import MatchedProject

logger = logging.getLogger(__name__)

MODEL_SONNET = "claude-sonnet-4-6"
MAX_TOKENS = 1024
CONCURRENCY = 10


# ---------------------------------------------------------------------------
# Public Pydantic types
# ---------------------------------------------------------------------------


class ProfessorForDrafting(BaseModel):
    """Minimal professor info for drafting. CLI builds from the DB Professor row."""

    name: str
    institution: str
    email: str


class EmailDraft(BaseModel):
    """Module output. CLI persists as a Draft row (status=pending_review)."""

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=3000)


# Internal tool-input schema — matches EmailDraft today but kept separate so we
# can evolve the wire format without breaking the public type.
class _DraftExtraction(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=3000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    settings = load_settings()
    if not settings.anthropic_api_key:
        raise ConfigError("ANTHROPIC_API_KEY is not set.")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _load_prompt(name: str) -> str:
    return files("scholarapp.prompts").joinpath(name).read_text()


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "input_schema": schema}


def _extract_tool_input(response: Any, expected_name: str) -> dict[str, Any]:
    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == expected_name
        ):
            return getattr(block, "input")
    raise DraftingError(f"Claude did not call the expected tool `{expected_name}`.")


def _condense_resume(resume: ResumeData) -> str:
    """Compact, model-friendly resume rendering for the system prompt.

    Aggressively capped: bullets/skills/publications dropped past the most
    informative N. This shows up once in the cached system prompt, so size is
    paid once per run, not per professor.
    """
    lines: list[str] = [f"Name: {resume.name}"]
    if resume.email:
        lines.append(f"Email: {resume.email}")

    if resume.education:
        lines.append("\nEducation:")
        for ed in resume.education[:4]:
            years = f" ({ed.years})" if ed.years else ""
            lines.append(f"  - {ed.degree} {ed.field}, {ed.school}{years}")

    if resume.experiences:
        lines.append("\nExperiences:")
        for exp in resume.experiences[:5]:
            years = f" ({exp.years})" if exp.years else ""
            lines.append(f"  - {exp.role}, {exp.org}{years}")
            for bullet in exp.bullets[:3]:
                lines.append(f"    • {bullet}")

    if resume.skills:
        lines.append(f"\nSkills: {', '.join(resume.skills[:15])}")

    if resume.interests:
        lines.append(f"\nInterests: {', '.join(resume.interests)}")

    if resume.publications:
        lines.append("\nPublications:")
        for pub in resume.publications[:5]:
            yr = f" ({pub.year})" if pub.year else ""
            lines.append(f"  - {pub.title}, {pub.venue}{yr}")

    return "\n".join(lines)


def _build_system_text(template: str, resume: ResumeData) -> str:
    """The cached portion: rules + user template + condensed resume.

    Identical for every professor in a run — the only thing that varies is what
    goes into the user message. Keeping this prefix stable is what lets cache
    reads kick in starting on the second professor.
    """
    rules = _load_prompt("draft_email.txt")
    condensed = _condense_resume(resume)
    return (
        f"{rules}\n\n"
        f"---\nUSER COLD-EMAIL TEMPLATE (stylistic reference):\n\n{template}\n\n"
        f"---\nUSER RESUME (the only source for grounding claims):\n\n{condensed}"
    )


def _build_user_text(
    professor: ProfessorForDrafting,
    matched: list[MatchedProject],
    goal: str,
    considerations: str,
) -> str:
    matched_text = "\n".join(
        f"- [{m.project_id}] {m.title}\n"
        f"    url: {m.url or '(none)'}\n"
        f"    why_relevant: {m.why_relevant}"
        for m in matched
    )
    considerations_line = (
        f"considerations: {considerations}" if considerations.strip() else "considerations: (none)"
    )
    return (
        f"professor: {professor.name}\n"
        f"institution: {professor.institution}\n"
        f"email: {professor.email}\n\n"
        f"matched_projects:\n{matched_text}\n\n"
        f"goal: {goal}\n"
        f"{considerations_line}\n\n"
        f"Draft the email now."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def draft_email(
    template: str,
    resume: ResumeData,
    professor: ProfessorForDrafting,
    matched: list[MatchedProject],
    goal: str,
    considerations: str,
    *,
    anthropic_client: anthropic.AsyncAnthropic | None = None,
) -> EmailDraft:
    """Draft one personalized cold email.

    Raises `DraftingError("No matched projects for {prof.name}")` if `matched` is
    empty — the caller (CLI) should pre-check and skip rather than hit this. The
    exception is the safety net.
    """
    if not matched:
        raise DraftingError(f"No matched projects for {professor.name}")

    client = anthropic_client or _get_anthropic_client()
    system_text = _build_system_text(template, resume)
    user_text = _build_user_text(professor, matched, goal, considerations)

    try:
        response = await client.messages.create(
            model=MODEL_SONNET,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[
                _tool(
                    "save_draft",
                    "Save the drafted email (subject + body) ready to send.",
                    _DraftExtraction.model_json_schema(),
                )
            ],
            tool_choice={"type": "tool", "name": "save_draft"},
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.APIError as e:
        raise DraftingError(
            f"Anthropic API error while drafting email for {professor.name}: {e}"
        ) from e

    usage_tracker.record(
        "draft_email", MODEL_SONNET, getattr(response, "usage", None)
    )

    raw = _extract_tool_input(response, "save_draft")
    try:
        extraction = _DraftExtraction.model_validate(raw)
    except ValidationError as e:
        raise DraftingError(
            f"Drafter returned data that did not match the schema for "
            f"{professor.name}: {e}"
        ) from e

    return EmailDraft(subject=extraction.subject.strip(), body=extraction.body.strip())


class DraftRequest(BaseModel):
    """Per-professor input bundle for the batch wrapper."""

    professor: ProfessorForDrafting
    matched: list[MatchedProject]


async def draft_emails_for_run(
    template: str,
    resume: ResumeData,
    requests: list[DraftRequest],
    goal: str,
    considerations: str,
    *,
    concurrency: int = CONCURRENCY,
) -> list[EmailDraft]:
    """Draft emails for every professor in parallel.

    Output is parallel to `requests` — `result[i]` corresponds to `requests[i]`.
    All requests must have non-empty `matched` (caller pre-checks); if any do,
    that draft call raises and the gather fails.
    """
    client = _get_anthropic_client()
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(req: DraftRequest) -> EmailDraft:
        async with sem:
            return await draft_email(
                template,
                resume,
                req.professor,
                req.matched,
                goal,
                considerations,
                anthropic_client=client,
            )

    return await asyncio.gather(*[_bounded(r) for r in requests])
