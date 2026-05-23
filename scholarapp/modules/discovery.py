"""Step 4 — Discovery: field → professors with recent works + verified email.

Public entry point: `find_professors(field, count, user_interests)`. The pipeline:

1. Resolve the user's free-text `field` to OpenAlex topic IDs (LLM pick over
   top OpenAlex /topics results).
2. Query OpenAlex /authors filtered by those topics, scoped to academic
   institutions, sorted by citation count. Over-fetch ~3x because email
   resolution drops candidates.
3. For each author, in parallel (semaphore-bounded), fetch the 5 most recent
   /works and resolve an email via Tavily search + LLM extraction.
4. Drop candidates whose email is missing or fails the academic-TLD allowlist.
5. Return the first `count` survivors.

Network calls go through `_request_with_retry`, which retries 429/5xx with
exponential backoff (max 3 attempts) and honors `Retry-After` headers.

NOTE on the topics taxonomy: OpenAlex deprecated the `concepts` filter for
authors in late 2024 — `concepts.id:...` and `x_concepts.id:...` both now
return zero results. We use `topics.id:...` against the /topics endpoint. The
topic hierarchy is domain > field > subfield > topic; we show the LLM the
subfield + field names so it can pick well.
"""

from __future__ import annotations

import asyncio
import logging
from importlib.resources import files
from typing import Any

import anthropic
import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from scholarapp.config import load_settings
from scholarapp.errors import ConfigError, DiscoveryError

logger = logging.getLogger(__name__)

OPENALEX_BASE = "https://api.openalex.org"
TAVILY_URL = "https://api.tavily.com/search"
# Model tiers. Both LLM calls in discovery are narrow extraction tasks (pick 1-2
# topic IDs; pull one email from web snippets) — Haiku handles them at parity with
# Sonnet at 1/3 the cost. See docs/04-discovery.md.
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"
CONCURRENCY = 10
OVERFETCH_MULTIPLIER = 3
HTTP_TIMEOUT = 30.0

# Suffixes we accept as "academic email." Extend in docs/04-discovery.md.
ACADEMIC_EMAIL_SUFFIXES: tuple[str, ...] = (
    ".edu",
    ".edu.au",
    ".edu.cn",
    ".edu.hk",
    ".edu.sg",
    ".edu.tw",
    ".ac.at",
    ".ac.be",
    ".ac.cn",
    ".ac.il",
    ".ac.in",
    ".ac.jp",
    ".ac.kr",
    ".ac.nz",
    ".ac.uk",
    ".ac.za",
)


# ---------------------------------------------------------------------------
# Public Pydantic models
# ---------------------------------------------------------------------------


class WorkRef(BaseModel):
    openalex_id: str
    title: str
    url: str | None = None
    year: int | None = None
    abstract: str | None = None


class ProfessorCandidate(BaseModel):
    openalex_id: str
    name: str
    institution: str
    email: str
    faculty_page_url: str | None = None
    recent_works: list[WorkRef]

    @field_validator("email")
    @classmethod
    def _email_must_be_academic(cls, v: str) -> str:
        if not is_academic_email(v):
            raise ValueError(
                f"Email {v!r} is not on the academic-TLD allowlist; "
                "see scholarapp.modules.discovery.ACADEMIC_EMAIL_SUFFIXES."
            )
        return v


# Internal extraction schemas used as Anthropic tool input_schemas.


class _TopicPick(BaseModel):
    topic_ids: list[str] = Field(min_length=1, max_length=2)
    rationale: str = ""


class _EmailExtraction(BaseModel):
    email: str | None = None
    faculty_page_url: str | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Helpers — pure / utility
# ---------------------------------------------------------------------------


def is_academic_email(email: str | None) -> bool:
    """True if `email` ends with one of the academic suffixes (case-insensitive)."""
    if not email:
        return False
    cleaned = email.strip().lower()
    if "@" not in cleaned:
        return False
    domain = cleaned.rsplit("@", 1)[1]
    return any(domain.endswith(suffix) for suffix in ACADEMIC_EMAIL_SUFFIXES)


def _short_id(openalex_url_or_id: str) -> str:
    """Strip the `https://openalex.org/` prefix; tolerate bare IDs."""
    return openalex_url_or_id.rsplit("/", 1)[-1]


def _decompress_abstract(inv_index: dict[str, list[int]] | None) -> str | None:
    """OpenAlex stores abstracts as inverted indexes; reconstruct the prose."""
    if not inv_index:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inv_index.items():
        for idx in idxs:
            positions.append((idx, word))
    if not positions:
        return None
    positions.sort()
    return " ".join(word for _, word in positions)


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
    raise DiscoveryError(
        f"Claude did not call the expected tool `{expected_name}`."
    )


# ---------------------------------------------------------------------------
# HTTP retry wrapper
# ---------------------------------------------------------------------------


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int = 3,
    **kwargs: Any,
) -> httpx.Response:
    """Issue an HTTP request, retrying transient failures with exponential backoff.

    Retries on 429 + 5xx, up to `max_attempts` total. Honors `Retry-After`.
    Other 4xx raise immediately via `raise_for_status()`.
    """
    last_response: httpx.Response | None = None
    for attempt in range(max_attempts):
        response = await client.request(method, url, **kwargs)
        last_response = response
        if response.status_code < 400:
            return response
        if response.status_code not in (429, 500, 502, 503, 504):
            response.raise_for_status()
        if attempt == max_attempts - 1:
            response.raise_for_status()
        wait = 2 ** attempt  # 1s, 2s, 4s
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                wait = max(wait, float(retry_after))
            except ValueError:
                pass
        logger.warning(
            "Retrying %s %s in %ss after status %s",
            method,
            url,
            wait,
            response.status_code,
        )
        await asyncio.sleep(wait)
    assert last_response is not None
    return last_response


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    settings = load_settings()
    if not settings.anthropic_api_key:
        raise ConfigError("ANTHROPIC_API_KEY is not set.")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _llm_pick_topics(
    anthropic_client: anthropic.AsyncAnthropic, field: str, candidates: list[dict]
) -> list[str]:
    """Have Claude select 1–2 OpenAlex topic IDs that match the user's field."""
    system_text = _load_prompt("pick_topics.txt")
    summarized = [
        {
            "id": _short_id(c["id"]),
            "display_name": c.get("display_name", ""),
            "subfield": (c.get("subfield") or {}).get("display_name", ""),
            "field": (c.get("field") or {}).get("display_name", ""),
            "keywords": ", ".join((c.get("keywords") or [])[:6]),
            "description": (c.get("description") or "")[:200],
        }
        for c in candidates
    ]
    user_text = f"field: {field}\n\ncandidates:\n" + "\n".join(
        f"- {s['id']}  [{s['field']} > {s['subfield']}]  {s['display_name']}\n"
        f"    keywords: {s['keywords']}\n"
        f"    description: {s['description']}"
        for s in summarized
    )
    response = await anthropic_client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=512,
        system=[
            {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        ],
        tools=[
            _tool(
                "select_topics",
                "Save the 1-2 best matching OpenAlex topic IDs.",
                _TopicPick.model_json_schema(),
            )
        ],
        tool_choice={"type": "tool", "name": "select_topics"},
        messages=[{"role": "user", "content": user_text}],
    )
    raw = _extract_tool_input(response, "select_topics")
    try:
        pick = _TopicPick.model_validate(raw)
    except ValidationError as e:
        raise DiscoveryError(f"Topic pick was malformed: {e}") from e
    allowed = {s["id"] for s in summarized}
    return [tid for tid in pick.topic_ids if tid in allowed]


async def _llm_extract_email(
    anthropic_client: anthropic.AsyncAnthropic,
    name: str,
    institution: str,
    results: list[dict],
) -> _EmailExtraction:
    """Have Claude pull an academic email (and faculty page) from Tavily results."""
    system_text = _load_prompt("extract_email.txt")
    rendered_results = "\n\n".join(
        f"[{i + 1}] {r.get('title', '')}\n  url: {r.get('url', '')}\n  content: "
        f"{(r.get('content') or '')[:600]}"
        for i, r in enumerate(results[:5])
    )
    user_text = (
        f"professor: {name}\ninstitution: {institution}\n\nresults:\n{rendered_results}"
    )
    response = await anthropic_client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=512,
        system=[
            {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
        ],
        tools=[
            _tool(
                "extract_email",
                "Save the extracted email and faculty page URL.",
                _EmailExtraction.model_json_schema(),
            )
        ],
        tool_choice={"type": "tool", "name": "extract_email"},
        messages=[{"role": "user", "content": user_text}],
    )
    raw = _extract_tool_input(response, "extract_email")
    try:
        return _EmailExtraction.model_validate(raw)
    except ValidationError as e:
        logger.warning("Email extraction returned malformed data: %s", e)
        return _EmailExtraction()


# ---------------------------------------------------------------------------
# OpenAlex + Tavily fetchers
# ---------------------------------------------------------------------------


async def _resolve_topics(
    http: httpx.AsyncClient,
    anthropic_client: anthropic.AsyncAnthropic,
    field: str,
) -> list[str]:
    response = await _request_with_retry(
        http,
        "GET",
        f"{OPENALEX_BASE}/topics",
        params={"search": field, "per_page": 10},
    )
    candidates = response.json().get("results", [])
    if not candidates:
        raise DiscoveryError(
            f"OpenAlex returned no topics for field {field!r}. "
            "Try a more standard term (e.g., 'computational neuroscience' instead of 'comp neuro')."
        )
    chosen = await _llm_pick_topics(anthropic_client, field, candidates)
    if not chosen:
        raise DiscoveryError(
            f"Could not match field {field!r} to an OpenAlex topic."
        )
    return chosen


async def _list_authors(
    http: httpx.AsyncClient, topic_ids: list[str], target_count: int
) -> list[dict]:
    topic_filter = "|".join(topic_ids)  # OR over topics
    response = await _request_with_retry(
        http,
        "GET",
        f"{OPENALEX_BASE}/authors",
        params={
            "filter": (
                f"topics.id:{topic_filter},"
                "last_known_institutions.type:education,"
                "works_count:>10"
            ),
            "sort": "cited_by_count:desc",
            "per_page": min(200, max(25, target_count)),
        },
    )
    return response.json().get("results", [])[:target_count]


async def _fetch_recent_works(http: httpx.AsyncClient, author_id: str) -> list[dict]:
    short = _short_id(author_id)
    response = await _request_with_retry(
        http,
        "GET",
        f"{OPENALEX_BASE}/works",
        params={
            "filter": f"author.id:{short}",
            "sort": "publication_date:desc",
            "per_page": 5,
        },
    )
    return response.json().get("results", [])


async def _tavily_search(
    http: httpx.AsyncClient, api_key: str, query: str
) -> list[dict]:
    response = await _request_with_retry(
        http,
        "POST",
        TAVILY_URL,
        json={"api_key": api_key, "query": query, "max_results": 5},
    )
    return response.json().get("results", [])


async def _resolve_email(
    http: httpx.AsyncClient,
    anthropic_client: anthropic.AsyncAnthropic,
    tavily_key: str,
    name: str,
    institution: str,
) -> tuple[str | None, str | None]:
    if not name or not institution:
        return None, None
    query = f'"{name}" "{institution}" faculty email'
    try:
        results = await _tavily_search(http, tavily_key, query)
    except httpx.HTTPError as e:
        logger.warning("Tavily search failed for %s: %s", name, e)
        return None, None
    if not results:
        return None, None
    extraction = await _llm_extract_email(anthropic_client, name, institution, results)
    return extraction.email, extraction.faculty_page_url


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def _safe_works(http: httpx.AsyncClient, author_id: str, name: str) -> list[dict]:
    try:
        return await _fetch_recent_works(http, author_id)
    except httpx.HTTPError as e:
        logger.warning("works fetch failed for %s: %s", name, e)
        return []


async def find_professors(
    field: str, count: int, user_interests: list[str]
) -> list[ProfessorCandidate]:
    """Discover up to `count` professors in `field` with verified academic emails.

    `user_interests` is accepted for future use (e.g., narrowing the author pool)
    but not currently consulted — matching against interests happens in Step 5.
    """
    if count <= 0:
        return []

    settings = load_settings()
    if not settings.anthropic_api_key:
        raise ConfigError("ANTHROPIC_API_KEY is not set.")
    if not settings.tavily_api_key:
        raise ConfigError("TAVILY_API_KEY is not set.")

    # OpenAlex asks API users to be in the "polite pool" via a mailto identifier.
    headers = {"User-Agent": "Scholarapp/0.1 (mailto:scholarapp@example.com)"}
    anthropic_client = _get_anthropic_client()

    async with httpx.AsyncClient(headers=headers, timeout=HTTP_TIMEOUT) as http:
        topic_ids = await _resolve_topics(http, anthropic_client, field)
        logger.info("Resolved field %r to topic ids %s", field, topic_ids)

        target_pool = count * OVERFETCH_MULTIPLIER
        authors = await _list_authors(http, topic_ids, target_pool)
        if not authors:
            raise DiscoveryError(
                f"OpenAlex returned no authors for field {field!r}. "
                "The topic may be too narrow."
            )
        logger.info("OpenAlex returned %d candidate authors", len(authors))

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _enrich(author: dict) -> dict | None:
            async with sem:
                author_id = author.get("id") or ""
                name = author.get("display_name") or ""
                inst_list = author.get("last_known_institutions") or []
                institution = inst_list[0].get("display_name", "") if inst_list else ""
                try:
                    works, email_page = await asyncio.gather(
                        _safe_works(http, author_id, name),
                        _resolve_email(
                            http, anthropic_client, settings.tavily_api_key, name, institution
                        ),
                    )
                except Exception as e:  # belt and suspenders
                    logger.warning("enrichment failed for %s: %s", name, e)
                    return None
                email, page = email_page
                return {
                    "author": author,
                    "name": name,
                    "institution": institution,
                    "works": works,
                    "email": email,
                    "faculty_page_url": page,
                }

        enriched = await asyncio.gather(
            *[_enrich(a) for a in authors], return_exceptions=False
        )

    final: list[ProfessorCandidate] = []
    for item in enriched:
        if item is None:
            continue
        email = item["email"]
        if not is_academic_email(email):
            logger.info(
                "Dropping %s @ %s — no academic email confirmed",
                item["name"],
                item["institution"],
            )
            continue
        works = [
            WorkRef(
                openalex_id=_short_id(w.get("id", "")),
                title=(w.get("title") or w.get("display_name") or "").strip(),
                url=_pick_work_url(w),
                year=w.get("publication_year"),
                abstract=_decompress_abstract(w.get("abstract_inverted_index")),
            )
            for w in item["works"]
            if w.get("id")
        ]
        try:
            candidate = ProfessorCandidate(
                openalex_id=_short_id(item["author"].get("id", "")),
                name=item["name"],
                institution=item["institution"],
                email=email,
                faculty_page_url=item["faculty_page_url"],
                recent_works=works,
            )
        except ValidationError as e:
            logger.warning("Skipping malformed candidate for %s: %s", item["name"], e)
            continue
        final.append(candidate)
        if len(final) >= count:
            break

    if len(final) < count:
        logger.warning(
            "Wanted %d professors but only %d survived email validation. "
            "Consider a broader field or running again later.",
            count,
            len(final),
        )
    return final[:count]


def _pick_work_url(work: dict) -> str | None:
    doi = work.get("doi")
    if doi:
        return doi
    primary = work.get("primary_location") or {}
    landing = primary.get("landing_page_url")
    if landing:
        return landing
    return None
