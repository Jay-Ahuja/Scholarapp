"""Step 3 — Ingestion: resume PDF + user prompt → structured Pydantic records.

Two entry points:

- `parse_resume(pdf_path)` sends the PDF to Claude via the documents API and returns a
  `ResumeData` populated by the `extract_resume` tool.
- `parse_prompt(text)` sends the user's prompt text to Claude and returns a `PromptData`
  populated by the `extract_prompt` tool. Raises `IngestionError` if the prompt is
  vague on any required field.

Both calls put the system prompt behind `cache_control={"type": "ephemeral"}` so the
template is reused across runs (savings kick in once templates grow past the cache
minimum — currently they're small).
"""

from __future__ import annotations

import base64
from importlib.resources import files
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError

from scholarapp import usage as usage_tracker
from scholarapp.config import load_settings
from scholarapp.errors import ConfigError, IngestionError

# Model tiers. Sonnet for tasks where quality materially matters (PDF parsing,
# email drafting); Haiku for narrow extraction/classification calls. See
# docs/03-ingestion.md and docs/04-discovery.md for the per-call rationale.
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"

MAX_TOKENS_RESUME = 4096
MAX_TOKENS_PROMPT = 1024


# ---------------------------------------------------------------------------
# Pydantic schemas — these double as Anthropic tool input_schemas.
# ---------------------------------------------------------------------------


class Education(BaseModel):
    school: str
    degree: str
    field: str
    years: str


class Experience(BaseModel):
    org: str
    role: str
    years: str
    bullets: list[str]


class Publication(BaseModel):
    title: str
    venue: str
    year: int | None = None
    url: str | None = None


class ResumeData(BaseModel):
    name: str
    email: str
    education: list[Education]
    experiences: list[Experience]
    skills: list[str]
    interests: list[str]
    publications: list[Publication]


class PromptData(BaseModel):
    count: int = Field(ge=1, le=50)
    field: str
    goal: str
    considerations: str


class _PromptExtraction(BaseModel):
    """Internal schema for Claude's response. Nullable so missing fields are explicit."""

    count: int | None = None
    field: str | None = None
    goal: str | None = None
    considerations: str = ""
    missing_fields: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get_client() -> anthropic.Anthropic:
    settings = load_settings()
    if not settings.anthropic_api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Add it to .env or your shell environment."
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _load_prompt(name: str) -> str:
    return files("scholarapp.prompts").joinpath(name).read_text()


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "input_schema": schema}


def _extract_tool_input(response: Any, expected_name: str) -> dict[str, Any]:
    """Pull the first tool_use block matching `expected_name`. Raise if absent."""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == expected_name:
            return getattr(block, "input")
    raise IngestionError(
        f"Claude did not call the expected tool `{expected_name}`."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_resume(pdf_path: Path) -> ResumeData:
    """Extract structured fields from a resume PDF via Claude.

    The PDF is sent directly to Claude as a base64-encoded document block. Claude is
    forced to call the `extract_resume` tool, whose input schema matches `ResumeData`.
    """
    if not pdf_path.exists():
        raise IngestionError(f"Resume PDF not found: {pdf_path}")
    pdf_bytes = pdf_path.read_bytes()
    if not pdf_bytes:
        raise IngestionError(f"Resume PDF is empty: {pdf_path}")
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

    client = _get_client()
    system_text = _load_prompt("parse_resume.txt")
    tool = _tool(
        name="extract_resume",
        description="Save the structured fields extracted from the resume PDF.",
        schema=ResumeData.model_json_schema(),
    )

    try:
        response = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=MAX_TOKENS_RESUME,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[tool],
            tool_choice={"type": "tool", "name": "extract_resume"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract the resume fields from the attached PDF.",
                        },
                    ],
                }
            ],
        )
    except anthropic.APIError as e:
        raise IngestionError(f"Anthropic API error while parsing resume: {e}") from e

    usage_tracker.record("parse_resume", MODEL_SONNET, getattr(response, "usage", None))
    tool_input = _extract_tool_input(response, "extract_resume")
    try:
        return ResumeData.model_validate(tool_input)
    except ValidationError as e:
        raise IngestionError(
            f"Resume parser returned data that did not match the schema: {e}"
        ) from e


def parse_prompt(text: str) -> PromptData:
    """Extract count / field / goal / considerations from the user's prompt.

    Raises `IngestionError` if Claude reports any required field as missing or the
    count is out of the 1–50 range.
    """
    if not text.strip():
        raise IngestionError("Prompt text is empty.")

    client = _get_client()
    system_text = _load_prompt("parse_prompt.txt")
    tool = _tool(
        name="extract_prompt",
        description="Save the four extracted fields from the user's prompt.",
        schema=_PromptExtraction.model_json_schema(),
    )

    try:
        response = client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=MAX_TOKENS_PROMPT,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[tool],
            tool_choice={"type": "tool", "name": "extract_prompt"},
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.APIError as e:
        raise IngestionError(f"Anthropic API error while parsing prompt: {e}") from e

    usage_tracker.record("parse_prompt", MODEL_HAIKU, getattr(response, "usage", None))
    tool_input = _extract_tool_input(response, "extract_prompt")
    try:
        extracted = _PromptExtraction.model_validate(tool_input)
    except ValidationError as e:
        raise IngestionError(
            f"Prompt parser returned data that did not match the schema: {e}"
        ) from e

    missing = list(extracted.missing_fields)
    for required in ("count", "field", "goal"):
        value = getattr(extracted, required)
        is_empty = value is None or (isinstance(value, str) and not value.strip())
        if is_empty and required not in missing:
            missing.append(required)

    if missing:
        raise IngestionError(
            "Your prompt is missing or unclear on: "
            + ", ".join(missing)
            + ". Rewrite your prompt so each of these is clearly stated."
        )

    assert extracted.count is not None  # narrowed by the check above
    assert extracted.field is not None
    assert extracted.goal is not None

    if not (1 <= extracted.count <= 50):
        raise IngestionError(
            f"count must be between 1 and 50, got {extracted.count}."
        )

    return PromptData(
        count=extracted.count,
        field=extracted.field,
        goal=extracted.goal,
        considerations=extracted.considerations or "",
    )
