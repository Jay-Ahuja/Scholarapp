"""Step 5 — Matching: pick the 2-3 most relevant works per professor.

Produces the "hook" that drafting (Step 6) uses to write a personalized email.

Model: Haiku. The task — read 2-5 abstracts, pick 2-3 with concrete technical
overlap, write a one-sentence rationale per pick — is bounded extract-and-justify
work. The prompt's hard constraints ("technical specifics over field-level
overlap", "don't invent overlap", "≤25 words naming the concrete overlap") force
specificity regardless of which model runs them. Haiku handles this at parity
with Sonnet for ~⅓ the cost; we keep Sonnet for the actual email writing
(drafting), where voice and cohesion matter.

Bonus: keeping matching off Sonnet frees the entire Sonnet ITPM budget for
drafting — which is the main 429 bottleneck on Tier 1 Anthropic accounts.

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

# Both model constants kept for clarity / easy swap. See module docstring for
# the choice rationale.
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"
# Active model for matching. Edit this one line to swap.
MATCH_MODEL = MODEL_HAIKU
MAX_TOKENS = 1024
# Default concurrency; overridden by settings.anthropic_concurrency at runtime.
CONCURRENCY = 3
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
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.anthropic_max_retries,
    )


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
            model=MATCH_MODEL,
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
        "match_projects", MATCH_MODEL, getattr(response, "usage", None)
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
    concurrency: int | None = None,
) -> list[list[MatchedProject]]:
    """Run `match_projects` over every (professor_name, projects) pair in parallel.

    Output is parallel to `pairs` — `result[i]` corresponds to `pairs[i]`. Use
    `zip(pairs, result)` to associate with the caller's professor records.

    Concurrency defaults to `settings.anthropic_concurrency` (env-driven), with
    the module constant `CONCURRENCY` as the fallback when caller passes None
    explicitly. Lower this on Tier 1 Anthropic accounts to avoid 429s.
    """
    settings = load_settings()
    effective_concurrency = concurrency if concurrency is not None else settings.anthropic_concurrency
    client = _get_anthropic_client()
    sem = asyncio.Semaphore(effective_concurrency)

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
