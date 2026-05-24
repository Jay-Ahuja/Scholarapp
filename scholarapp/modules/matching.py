"""Step 5 — Matching: pick the 2-3 most relevant works per professor.

Produces the "hook" that drafting (Step 6) uses to write a personalized email.
The judgment is intentionally on Sonnet rather than Haiku because the prompt
asks the model to distinguish surface keyword overlap ("both are neuroscience")
from genuine technical overlap ("both use CNNs to segment MRI volumes") — that
distinction is the whole point and smaller models flatten it.

Public entry points:

- `match_projects(...)` — one Claude call for one professor.
- `match_projects_for_run(...)` — semaphore-bounded `asyncio.gather` across
  professors. The CLI uses this.

Outputs are Pydantic; the caller (CLI) does the DB write via repo helpers.
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
from scholarapp.errors import ConfigError, MatchingError

logger = logging.getLogger(__name__)

# Sonnet for the relevance judgment; see module docstring.
MODEL_SONNET = "claude-sonnet-4-6"
MAX_TOKENS = 1024
CONCURRENCY = 10
MAX_PICKS = 3


# ---------------------------------------------------------------------------
# Public Pydantic types — the module's I/O contract
# ---------------------------------------------------------------------------


class ProjectForMatching(BaseModel):
    """Subset of fields the matcher needs about one work. Input to match_projects."""

    project_id: int
    title: str
    url: str | None = None
    abstract: str | None = None
    year: int | None = None


class MatchedProject(BaseModel):
    """One picked work + the one-sentence rationale drafting will reuse."""

    project_id: int
    title: str
    url: str | None = None
    why_relevant: str


# Internal tool-input schema. `why_relevant` is bounded so model can't ramble.
class _MatchedItem(BaseModel):
    project_id: int
    why_relevant: str = Field(min_length=1, max_length=300)


class _MatchExtraction(BaseModel):
    matches: list[_MatchedItem] = Field(default_factory=list, max_length=MAX_PICKS)


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
    raise MatchingError(f"Claude did not call the expected tool `{expected_name}`.")


def _render_works(projects: list[ProjectForMatching]) -> str:
    """Compact, model-friendly listing of a professor's works."""
    lines: list[str] = []
    for p in projects:
        header = f"[{p.project_id}] {p.title}"
        if p.year:
            header += f" ({p.year})"
        lines.append(header)
        if p.abstract:
            # Cap abstract length — long ones eat input tokens fast.
            abstract = p.abstract.strip().replace("\n", " ")
            if len(abstract) > 800:
                abstract = abstract[:800] + "…"
            lines.append(f"  abstract: {abstract}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def match_projects(
    professor_name: str,
    projects: list[ProjectForMatching],
    user_interests: list[str],
    user_experiences: str,
    *,
    anthropic_client: anthropic.AsyncAnthropic | None = None,
) -> list[MatchedProject]:
    """Pick up to MAX_PICKS works for this professor; may return [].

    A single Claude call. The model is forced to call `select_matches` with up
    to 3 picks; we filter to only the project_ids actually in `projects` (so
    the model can't return a stray ID).
    """
    if not projects:
        return []

    client = anthropic_client or _get_anthropic_client()
    system_text = _load_prompt("match_projects.txt")
    user_text = (
        f"professor: {professor_name}\n\n"
        f"interests: {', '.join(user_interests) if user_interests else '(none)'}\n\n"
        f"experiences:\n{user_experiences or '(none)'}\n\n"
        f"works:\n{_render_works(projects)}"
    )

    try:
        response = await client.messages.create(
            model=MODEL_SONNET,
            max_tokens=MAX_TOKENS,
            system=[
                {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
            ],
            tools=[
                _tool(
                    "select_matches",
                    "Save up to 3 ranked picks with one-sentence rationales.",
                    _MatchExtraction.model_json_schema(),
                )
            ],
            tool_choice={"type": "tool", "name": "select_matches"},
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.APIError as e:
        raise MatchingError(
            f"Anthropic API error while matching projects for {professor_name}: {e}"
        ) from e

    usage_tracker.record(
        "match_projects", MODEL_SONNET, getattr(response, "usage", None)
    )

    raw = _extract_tool_input(response, "select_matches")
    try:
        extracted = _MatchExtraction.model_validate(raw)
    except ValidationError as e:
        raise MatchingError(
            f"Matcher returned data that did not match the schema for "
            f"{professor_name}: {e}"
        ) from e

    by_id = {p.project_id: p for p in projects}
    matches: list[MatchedProject] = []
    for item in extracted.matches[:MAX_PICKS]:
        proj = by_id.get(item.project_id)
        if proj is None:
            logger.warning(
                "Matcher returned unknown project_id=%s for %s — dropping.",
                item.project_id,
                professor_name,
            )
            continue
        matches.append(
            MatchedProject(
                project_id=proj.project_id,
                title=proj.title,
                url=proj.url,
                why_relevant=item.why_relevant.strip(),
            )
        )

    if not matches:
        logger.warning(
            "No projects matched for %s — drafting will skip this professor.",
            professor_name,
        )
    return matches


async def match_projects_for_run(
    pairs: list[tuple[str, list[ProjectForMatching]]],
    user_interests: list[str],
    user_experiences: str,
    *,
    concurrency: int = CONCURRENCY,
) -> list[list[MatchedProject]]:
    """Run `match_projects` over every (professor_name, projects) pair in parallel.

    Output is parallel to `pairs` — `result[i]` corresponds to `pairs[i]`. Use
    `zip(pairs, result)` to associate with the caller's professor records.
    """
    client = _get_anthropic_client()
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(name: str, projects: list[ProjectForMatching]) -> list[MatchedProject]:
        async with sem:
            return await match_projects(
                name,
                projects,
                user_interests,
                user_experiences,
                anthropic_client=client,
            )

    return await asyncio.gather(*[_bounded(name, projects) for name, projects in pairs])
