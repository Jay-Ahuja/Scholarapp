"""Unit + orchestration tests for scholarapp.modules.discovery.

We mock at two levels:

- A `FakeAsyncClient` replaces `httpx.AsyncClient` and returns canned JSON keyed
  by (method, URL prefix). This verifies real HTTP request shape (filters, query
  string composition, etc.) without burning Tavily / OpenAlex quota.
- The two LLM helpers (`_llm_pick_topics`, `_llm_extract_email`) are
  monkeypatched directly so we never touch the Anthropic API.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scholarapp.errors import DiscoveryError
from scholarapp.modules import discovery

# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email,expected",
    [
        ("jane@mit.edu", True),
        ("jane@cs.mit.edu", True),
        ("jane.doe@cam.ac.uk", True),
        ("jane@uni-heidelberg.de", False),  # Continental Europe not in allowlist
        ("jane@gmail.com", False),
        ("not-an-email", False),
        ("", False),
        (None, False),
        ("jane@research.edu.au", True),
    ],
)
def test_is_academic_email(email, expected):
    assert discovery.is_academic_email(email) is expected


def test_decompress_abstract_orders_words_by_position():
    inv = {"world": [1], "hello": [0], "foo": [2, 3]}
    assert discovery._decompress_abstract(inv) == "hello world foo foo"


def test_decompress_abstract_handles_empty():
    assert discovery._decompress_abstract(None) is None
    assert discovery._decompress_abstract({}) is None


def test_professor_candidate_rejects_non_academic_email():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        discovery.ProfessorCandidate(
            openalex_id="A1",
            name="X",
            institution="Y",
            email="x@gmail.com",
            recent_works=[],
        )


# ---------------------------------------------------------------------------
# Fake httpx client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_data: Any, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            # httpx.Response.raise_for_status raises HTTPStatusError, not the bare
            # HTTPError — match production so callers' `except HTTPStatusError`
            # blocks behave correctly.
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "http://test"),
                response=self,  # type: ignore[arg-type]
            )


class FakeAsyncClient:
    """Routes (method, URL prefix) → JSON payload. Records all calls."""

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.calls: list[dict] = []

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *a) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        for (m, prefix), payload in self.routes.items():
            if m == method and url.startswith(prefix):
                return _FakeResponse(200, payload)
        return _FakeResponse(404, {"results": []})


# ---------------------------------------------------------------------------
# Low-level HTTP-facing tests
# ---------------------------------------------------------------------------


def test_fetch_recent_works_sends_correct_query():
    fake = FakeAsyncClient(
        {
            ("GET", "https://api.openalex.org/works"): {
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "title": "A paper",
                        "publication_year": 2024,
                        "doi": "https://doi.org/10.1/abc",
                    }
                ]
            }
        }
    )
    results = asyncio.run(discovery._fetch_recent_works(fake, "https://openalex.org/A99"))
    assert len(results) == 1
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.openalex.org/works"
    params = call["params"]
    assert params["filter"] == "author.id:A99"
    assert params["sort"] == "publication_date:desc"
    assert params["per_page"] == 5


def test_tavily_search_posts_api_key_in_body():
    fake = FakeAsyncClient(
        {("POST", "https://api.tavily.com/search"): {"results": [{"title": "x"}]}}
    )
    asyncio.run(discovery._tavily_search(fake, "tvly-secret", "jane doe MIT"))
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["json"]["api_key"] == "tvly-secret"
    assert call["json"]["query"] == "jane doe MIT"
    assert call["json"]["max_results"] == 5


def test_request_retry_eventually_succeeds():
    """A 429 followed by a 200 should yield the 200, after one backoff sleep."""

    class FlakyClient:
        def __init__(self):
            self.n = 0

        async def request(self, method, url, **kwargs):
            self.n += 1
            if self.n == 1:
                return _FakeResponse(429, {}, headers={"retry-after": "0"})
            return _FakeResponse(200, {"ok": True})

    client = FlakyClient()
    resp = asyncio.run(discovery._request_with_retry(client, "GET", "http://x"))
    assert resp.status_code == 200
    assert client.n == 2


# ---------------------------------------------------------------------------
# End-to-end orchestration: find_professors with mocked HTTP + LLM
# ---------------------------------------------------------------------------


def _author(idx: int, name: str | None = None) -> dict:
    return {
        "id": f"https://openalex.org/A{idx}",
        "display_name": name or f"Prof {idx}",
        "last_known_institutions": [{"display_name": "MIT", "type": "education"}],
        "cited_by_count": 1000 - idx,
        "works_count": 50,
    }


def _work(idx: int) -> dict:
    return {
        "id": f"https://openalex.org/W{idx}",
        "title": f"Work {idx}",
        "publication_year": 2024,
        "doi": f"https://doi.org/10.1/{idx}",
        "abstract_inverted_index": {"important": [0], "result": [1]},
    }


@pytest.fixture
def mock_discovery_env(monkeypatch):
    """Bypass real HTTP + LLM. Returns the FakeAsyncClient so tests can inspect calls."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    # No-op Anthropic client (the LLM helpers below are also patched).
    class _NoopAnthropic:
        pass

    monkeypatch.setattr(discovery, "_get_anthropic_client", lambda: _NoopAnthropic())

    async def _pick(_client, _field, candidates, user_interests=None):
        return [discovery._short_id(candidates[0]["id"])]

    monkeypatch.setattr(discovery, "_llm_pick_topics", _pick)


def test_find_professors_returns_count_when_enough_emails(monkeypatch, mock_discovery_env):
    # Six candidates; first four have academic emails.
    email_by_name = {
        "Prof 0": ("p0@mit.edu", "https://mit.edu/p0"),
        "Prof 1": ("p1@mit.edu", None),
        "Prof 2": ("p2@mit.edu", None),
        "Prof 3": ("p3@mit.edu", None),
        "Prof 4": (None, None),
        "Prof 5": ("p5@gmail.com", None),
    }

    async def _email(_client, name, _inst, _results):
        e, page = email_by_name.get(name, (None, None))
        return discovery._EmailExtraction(email=e, faculty_page_url=page)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C42", "display_name": "Field", "level": 1}]
        },
        ("GET", "https://api.openalex.org/authors"): {
            "results": [_author(i) for i in range(6)]
        },
        ("GET", "https://api.openalex.org/works"): {
            "results": [_work(1), _work(2)]
        },
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "Faculty Page", "url": "https://mit.edu/x", "content": "..."}]
        },
    }
    fake_http = FakeAsyncClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    result = asyncio.run(discovery.find_professors("neuroscience", count=2, user_interests=[]))
    results = result.professors

    assert len(results) == 2
    assert [r.name for r in results] == ["Prof 0", "Prof 1"]
    assert all(r.email.endswith(".edu") for r in results)
    # Works got persisted as WorkRefs with decompressed abstracts.
    assert results[0].recent_works[0].abstract == "important result"
    assert results[0].recent_works[0].url == "https://doi.org/10.1/1"
    # faculty page surfaces
    assert results[0].faculty_page_url == "https://mit.edu/p0"
    # attempted_ids covers the full enriched pool of 6 authors (A0..A5) — both
    # survivors AND the dropped-for-no-email candidates incurred a paid lookup.
    assert result.attempted_ids == {f"A{i}" for i in range(6)}


def test_find_professors_logs_when_no_email_found(monkeypatch, mock_discovery_env, caplog):
    async def _email(_client, _name, _inst, _results):
        return discovery._EmailExtraction(email=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/authors"): {
            "results": [_author(i) for i in range(3)]
        },
        ("GET", "https://api.openalex.org/works"): {"results": []},
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "x", "url": "https://x", "content": "no email"}]
        },
    }
    fake_http = FakeAsyncClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    with caplog.at_level("WARNING", logger="scholarapp.modules.discovery"):
        result = asyncio.run(
            discovery.find_professors("neuroscience", count=2, user_interests=[])
        )

    assert result.professors == []
    # No survivors, but all 3 authors were enriched (paid) — they're attempted.
    assert result.attempted_ids == {"A0", "A1", "A2"}
    assert any("only 0 survived" in rec.message for rec in caplog.records)


def test_find_professors_raises_when_no_topics(monkeypatch, mock_discovery_env):
    routes = {
        ("GET", "https://api.openalex.org/topics"): {"results": []},
    }
    fake_http = FakeAsyncClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    with pytest.raises(DiscoveryError, match="no topics"):
        asyncio.run(discovery.find_professors("zzzzz-bogus", count=1, user_interests=[]))


def test_find_professors_raises_when_no_authors(monkeypatch, mock_discovery_env):
    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/authors"): {"results": []},
    }
    fake_http = FakeAsyncClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    with pytest.raises(DiscoveryError, match="no authors"):
        asyncio.run(discovery.find_professors("narrow", count=1, user_interests=[]))


def test_find_professors_returns_empty_for_zero_count(mock_discovery_env):
    # Should short-circuit without any HTTP call.
    result = asyncio.run(discovery.find_professors("x", 0, []))
    assert result.professors == []
    assert result.attempted_ids == set()


def test_tavily_429_aborts_discovery_immediately(monkeypatch, mock_discovery_env):
    """A single Tavily 429 should raise DiscoveryError and circuit-break
    the rest of the run (no Tavily-quota churn through 60 candidates)."""

    # FakeAsyncClient that returns 429 from Tavily, success from OpenAlex.
    class _RateLimitedClient(FakeAsyncClient):
        async def request(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            if url.startswith("https://api.tavily.com"):
                return _FakeResponse(429, {"error": "rate limit"})
            for (m, prefix), payload in self.routes.items():
                if m == method and url.startswith(prefix):
                    return _FakeResponse(200, payload)
            return _FakeResponse(404, {"results": []})

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/T1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/authors"): {
            "results": [_author(i) for i in range(6)]
        },
        ("GET", "https://api.openalex.org/works"): {"results": []},
    }
    fake_http = _RateLimitedClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    # No need to mock _llm_extract_email — _tavily_search raises before it's called.

    with pytest.raises(DiscoveryError, match="Tavily.*429|rate limit"):
        asyncio.run(discovery.find_professors("neuro", count=2, user_interests=[]))

    # Sanity: we did not blow through dozens of Tavily requests. With 6 candidates
    # and immediate abort, we expect at most a handful before the gather propagates.
    tavily_calls = [
        c for c in fake_http.calls if c["url"].startswith("https://api.tavily.com")
    ]
    assert len(tavily_calls) <= 6, f"unexpected Tavily call count: {len(tavily_calls)}"


def test_tavily_rate_limit_flag_resets_between_runs(monkeypatch, mock_discovery_env):
    """Set the flag manually, then verify find_professors clears it at start."""
    discovery._tavily_rate_limited.set()
    assert discovery._tavily_rate_limited.is_set()

    # Empty topics → terminates early without reaching Tavily.
    routes = {("GET", "https://api.openalex.org/topics"): {"results": []}}
    fake_http = FakeAsyncClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    with pytest.raises(DiscoveryError):
        asyncio.run(discovery.find_professors("xxx", count=1, user_interests=[]))

    # The flag should have been cleared at the top of find_professors.
    assert not discovery._tavily_rate_limited.is_set()


# ---------------------------------------------------------------------------
# Within-run deduplication
# ---------------------------------------------------------------------------


def _author_with(
    idx: int, name: str, institution: str, department: str | None = None
) -> dict:
    """Author dict with controllable name / institution / department."""
    inst: dict = {"display_name": institution, "type": "education"}
    if department is not None:
        inst["department"] = department
    return {
        "id": f"https://openalex.org/A{idx}",
        "display_name": name,
        "last_known_institutions": [inst],
        "cited_by_count": 1000 - idx,
        "works_count": 50,
    }


def test_dedup_key_uses_name_institution_department():
    a = _author_with(1, "Jane Doe", "MIT", "Brain & Cognitive Sciences")
    b = _author_with(2, "Jane Doe", "MIT", "Brain & Cognitive Sciences")
    c = _author_with(3, "Jane Doe", "MIT", "Computer Science")
    # Same name + institution but different department → NOT the same professor.
    assert discovery._dedup_key(a) == discovery._dedup_key(b)
    assert discovery._dedup_key(a) != discovery._dedup_key(c)
    # Department participates in the key.
    assert len(discovery._dedup_key(a)) == 3


def test_dedup_key_falls_back_to_name_institution_without_department():
    a = _author_with(1, "Jane Doe", "MIT")  # no department key
    b = _author_with(2, "Jane Doe", "MIT", "")  # empty department
    # Both collapse to the 2-tuple (name, institution) fallback.
    assert discovery._dedup_key(a) == discovery._dedup_key(b)
    assert len(discovery._dedup_key(a)) == 2


def test_dedup_key_is_case_and_whitespace_insensitive():
    a = _author_with(1, "Jane Doe", "MIT")
    b = _author_with(2, "  jane   doe ", "mit")
    assert discovery._dedup_key(a) == discovery._dedup_key(b)


def test_dedup_authors_keeps_first_seen_order_and_drops_repeats():
    authors = [
        _author_with(0, "Jane Doe", "MIT"),
        _author_with(1, "John Roe", "Stanford"),
        _author_with(2, "Jane Doe", "MIT"),  # dup of 0
    ]
    deduped = discovery._dedup_authors(authors)
    assert [a["display_name"] for a in deduped] == ["Jane Doe", "John Roe"]
    # First-seen record is the one retained.
    assert deduped[0]["id"] == "https://openalex.org/A0"


def test_dedup_authors_no_duplicates_is_unchanged():
    authors = [
        _author_with(0, "A", "MIT"),
        _author_with(1, "B", "MIT"),
        _author_with(2, "C", "Stanford"),
    ]
    assert discovery._dedup_authors(authors) == authors


def _dedup_routes(authors: list[dict]) -> dict:
    return {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [
                {"id": "https://openalex.org/C1", "display_name": "F", "level": 1}
            ]
        },
        ("GET", "https://api.openalex.org/authors"): {"results": authors},
        ("GET", "https://api.openalex.org/works"): {"results": []},
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "x", "url": "https://mit.edu/x", "content": "..."}]
        },
    }


def test_find_professors_dedups_duplicate_professor_and_pays_once(
    monkeypatch, mock_discovery_env
):
    """A duplicated professor returns once AND the paid lookup runs once for them."""
    extract_calls: list[str] = []

    async def _email(_client, name, _inst, _results):
        extract_calls.append(name)
        return discovery._EmailExtraction(email="jane@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    # Three OpenAlex records: two are the same professor (name+inst+dept).
    authors = [
        _author_with(0, "Jane Doe", "MIT", "Brain & Cognitive Sciences"),
        _author_with(1, "Jane Doe", "MIT", "Brain & Cognitive Sciences"),  # dup
        _author_with(2, "John Roe", "Stanford", "Computer Science"),
    ]
    fake_http = FakeAsyncClient(_dedup_routes(authors))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    results = asyncio.run(
        discovery.find_professors("neuroscience", count=5, user_interests=[])
    ).professors

    # Duplicate professor appears at most once in the returned results.
    names = [r.name for r in results]
    assert names.count("Jane Doe") == 1
    assert sorted(names) == ["Jane Doe", "John Roe"]

    # Paid lookup (Claude email extraction) ran once for the duplicated professor.
    assert extract_calls.count("Jane Doe") == 1

    # And the paid external Tavily search was issued once for that professor.
    tavily_calls = [
        c for c in fake_http.calls if c["url"].startswith("https://api.tavily.com")
    ]
    jane_tavily = [c for c in tavily_calls if "Jane Doe" in c["json"]["query"]]
    assert len(jane_tavily) == 1
    # Two unique professors total → exactly two Tavily calls.
    assert len(tavily_calls) == 2


def test_find_professors_dedup_falls_back_to_name_institution_without_department(
    monkeypatch, mock_discovery_env
):
    """When department is absent, name + institution match collapses duplicates."""
    extract_calls: list[str] = []

    async def _email(_client, name, _inst, _results):
        extract_calls.append(name)
        return discovery._EmailExtraction(email="jane@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    authors = [
        _author_with(0, "Jane Doe", "MIT"),  # no department
        _author_with(1, "Jane Doe", "MIT"),  # no department → dup
    ]
    fake_http = FakeAsyncClient(_dedup_routes(authors))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    results = asyncio.run(
        discovery.find_professors("neuroscience", count=5, user_interests=[])
    ).professors

    assert [r.name for r in results] == ["Jane Doe"]
    assert extract_calls.count("Jane Doe") == 1
    tavily_calls = [
        c for c in fake_http.calls if c["url"].startswith("https://api.tavily.com")
    ]
    assert len(tavily_calls) == 1


def test_find_professors_no_duplicates_unchanged_results_and_lookup_count(
    monkeypatch, mock_discovery_env
):
    """A duplicate-free pool yields the same results and the same paid-lookup count."""
    extract_calls: list[str] = []

    async def _email(_client, name, _inst, _results):
        extract_calls.append(name)
        return discovery._EmailExtraction(email="x@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    authors = [
        _author_with(0, "Alice", "MIT", "Physics"),
        _author_with(1, "Bob", "MIT", "Physics"),
        _author_with(2, "Carol", "Stanford", "Biology"),
    ]
    fake_http = FakeAsyncClient(_dedup_routes(authors))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    results = asyncio.run(
        discovery.find_professors("neuroscience", count=3, user_interests=[])
    ).professors

    assert sorted(r.name for r in results) == ["Alice", "Bob", "Carol"]
    # One paid lookup per distinct professor — nothing dropped, nothing doubled.
    assert sorted(extract_calls) == ["Alice", "Bob", "Carol"]
    tavily_calls = [
        c for c in fake_http.calls if c["url"].startswith("https://api.tavily.com")
    ]
    assert len(tavily_calls) == 3


def test_llm_pick_topics_includes_user_interests_in_prompt():
    """The interests should reach the model as the strongest signal — see pick_topics.txt."""

    captured: dict = {}

    class _FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="select_topics",
                        input={"topic_ids": ["T11601"], "rationale": "imaging-focused"},
                    )
                ],
                usage=SimpleNamespace(
                    input_tokens=100, output_tokens=20,
                    cache_creation_input_tokens=0, cache_read_input_tokens=0,
                ),
            )

    class _FakeClient:
        messages = _FakeMessages()

    candidates = [
        {
            "id": "https://openalex.org/T11601",
            "display_name": "Neuroscience and Neural Engineering",
            "field": {"display_name": "Neuroscience"},
            "subfield": {"display_name": "Neural Engineering"},
            "keywords": ["BCI", "neural decoding"],
            "description": "Neural engineering and BCI work.",
        }
    ]

    result = asyncio.run(
        discovery._llm_pick_topics(
            _FakeClient(),
            "neuroscience",
            candidates,
            user_interests=["CNN", "MRI segmentation", "brain extraction"],
        )
    )
    assert result == ["T11601"]
    user_text = captured["messages"][0]["content"]
    assert "user's specific interests" in user_text
    assert "MRI segmentation" in user_text
    assert "CNN" in user_text


# ---------------------------------------------------------------------------
# exclude_ids — top-up support (filter before enrichment + fetch sizing)
# ---------------------------------------------------------------------------


def test_find_professors_excludes_ids_before_enrichment(monkeypatch, mock_discovery_env):
    """Excluded authors never trigger a paid Tavily search / Claude extraction."""
    extract_calls: list[str] = []

    async def _email(_client, name, _inst, _results):
        extract_calls.append(name)
        return discovery._EmailExtraction(email="p@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/authors"): {
            "results": [_author(i) for i in range(4)]  # A0..A3
        },
        ("GET", "https://api.openalex.org/works"): {"results": []},
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "x", "url": "https://mit.edu/x", "content": "..."}]
        },
    }
    fake_http = FakeAsyncClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    result = asyncio.run(
        discovery.find_professors(
            "neuro", count=2, user_interests=[], exclude_ids={"A0", "A1"}
        )
    )
    results = result.professors

    # A0/A1 are excluded BEFORE enrichment → never looked up, never returned.
    names = [r.name for r in results]
    assert "Prof 0" not in names
    assert "Prof 1" not in names
    assert set(names) == {"Prof 2", "Prof 3"}
    assert "Prof 0" not in extract_calls
    assert "Prof 1" not in extract_calls
    tavily_calls = [
        c for c in fake_http.calls if c["url"].startswith("https://api.tavily.com")
    ]
    assert len(tavily_calls) == 2  # only the two non-excluded professors
    # attempted_ids covers only the enriched (non-excluded) authors A2/A3 — the
    # excluded ones never reached enrichment, so they're never re-recorded.
    assert result.attempted_ids == {"A2", "A3"}


def test_find_professors_pages_authors_with_cursor(monkeypatch, mock_discovery_env):
    """OpenAlex /authors is fetched via cursor paging at the polite-pool page size."""
    async def _email(_client, _name, _inst, _results):
        return discovery._EmailExtraction(email="p@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/authors"): {"results": [_author(0)]},
        ("GET", "https://api.openalex.org/works"): {"results": []},
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "x", "url": "https://mit.edu/x", "content": "..."}]
        },
    }
    fake_http = FakeAsyncClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    asyncio.run(
        discovery.find_professors(
            "neuro", count=2, user_interests=[], exclude_ids={"A9", "A8", "A7"}
        )
    )

    authors_call = next(
        c for c in fake_http.calls
        if c["url"].startswith("https://api.openalex.org/authors")
    )
    # Paging no longer sizes per_page up by exclude count — it pages (free) until
    # the pool fills, skipping excluded IDs at the source. The first page uses the
    # cursor entrypoint and the polite-pool page size.
    assert authors_call["params"]["per_page"] == discovery._AUTHORS_PER_PAGE
    assert authors_call["params"]["cursor"] == "*"
    assert "topics.id:C1" in authors_call["params"]["filter"]


def test_list_authors_follows_next_cursor_until_pool_filled(monkeypatch):
    """_list_authors pages deeper when the first page is exhausted by excludes.

    The first OpenAlex page is entirely excluded; only the SECOND page (reached
    via next_cursor) holds fresh authors. The paged pool must surface them and
    report exhausted=False (OpenAlex still had a further page to give).
    """
    page1 = {
        "results": [_author(0), _author(1)],  # A0, A1 — both excluded
        "meta": {"next_cursor": "CURSOR_2"},
    }
    page2 = {
        "results": [_author(2), _author(3)],  # A2, A3 — fresh
        "meta": {"next_cursor": "CURSOR_3"},
    }

    class _PagingClient(FakeAsyncClient):
        async def request(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            if url.startswith("https://api.openalex.org/authors"):
                cursor = kwargs["params"]["cursor"]
                return _FakeResponse(200, page1 if cursor == "*" else page2)
            return _FakeResponse(404, {"results": []})

    fake = _PagingClient({})
    pool = asyncio.run(
        discovery._list_authors(
            fake, ["T1"], target_count=2, exclude_ids={"A0", "A1"}
        )
    )

    assert [a["id"] for a in pool.authors] == [
        "https://openalex.org/A2",
        "https://openalex.org/A3",
    ]
    # The pool filled (target_count=2) before OpenAlex ran out → not exhausted.
    assert pool.exhausted is False
    # Two /authors requests: the first (all excluded) and the second (fresh).
    author_calls = [
        c for c in fake.calls if c["url"].startswith("https://api.openalex.org/authors")
    ]
    assert len(author_calls) == 2
    assert author_calls[0]["params"]["cursor"] == "*"
    assert author_calls[1]["params"]["cursor"] == "CURSOR_2"


def test_list_authors_reports_exhausted_when_openalex_runs_out(monkeypatch):
    """When OpenAlex returns no next_cursor before the pool fills, exhausted=True."""
    page1 = {
        "results": [_author(0)],  # only one author, then no further page
        "meta": {"next_cursor": None},
    }

    class _SinglePageClient(FakeAsyncClient):
        async def request(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            if url.startswith("https://api.openalex.org/authors"):
                return _FakeResponse(200, page1)
            return _FakeResponse(404, {"results": []})

    fake = _SinglePageClient({})
    # Ask for a pool of 10 but OpenAlex only has 1 author and no next page.
    pool = asyncio.run(
        discovery._list_authors(fake, ["T1"], target_count=10, exclude_ids=set())
    )

    assert [a["id"] for a in pool.authors] == ["https://openalex.org/A0"]
    assert pool.exhausted is True


def test_find_professors_exclude_none_pages_from_cursor_entrypoint(
    monkeypatch, mock_discovery_env
):
    """exclude_ids=None still pages from the cursor entrypoint at the page size."""
    async def _email(_client, _name, _inst, _results):
        return discovery._EmailExtraction(email="p@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/authors"): {"results": [_author(0)]},
        ("GET", "https://api.openalex.org/works"): {"results": []},
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "x", "url": "https://mit.edu/x", "content": "..."}]
        },
    }
    fake_http = FakeAsyncClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    asyncio.run(discovery.find_professors("neuro", count=10, user_interests=[]))

    authors_call = next(
        c for c in fake_http.calls
        if c["url"].startswith("https://api.openalex.org/authors")
    )
    assert authors_call["params"]["per_page"] == discovery._AUTHORS_PER_PAGE
    assert authors_call["params"]["cursor"] == "*"


# ---------------------------------------------------------------------------
# Paging exhaustion semantics surfaced through find_professors
# ---------------------------------------------------------------------------


class _CursorPagingClient(FakeAsyncClient):
    """OpenAlex /authors paginated by cursor; other endpoints from `routes`.

    `pages` is an ordered list of `results` lists; the client walks them via
    next_cursor. After the last page, next_cursor is None (OpenAlex exhausted).
    """

    def __init__(self, routes: dict, pages: list[list[dict]]):
        super().__init__(routes)
        self.pages = pages

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if url.startswith("https://api.openalex.org/authors"):
            cursor = kwargs["params"]["cursor"]
            idx = 0 if cursor == "*" else int(cursor)
            results = self.pages[idx] if idx < len(self.pages) else []
            next_cursor = str(idx + 1) if idx + 1 < len(self.pages) else None
            return _FakeResponse(200, {"results": results, "meta": {"next_cursor": next_cursor}})
        for (m, prefix), payload in self.routes.items():
            if m == method and url.startswith(prefix):
                return _FakeResponse(200, payload)
        return _FakeResponse(404, {"results": []})


def test_find_professors_first_page_excluded_surfaces_deeper_authors(
    monkeypatch, mock_discovery_env
):
    """(a) First page fully excluded, but deeper authors exist → discovery
    surfaces them and reports exhausted=False (the loop should keep going)."""
    async def _email(_client, _name, _inst, _results):
        return discovery._EmailExtraction(email="p@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/works"): {"results": []},
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "x", "url": "https://mit.edu/x", "content": "..."}]
        },
    }
    # Page 1 is A0..A2 (all excluded); deeper pages hold plenty of fresh authors,
    # MORE than the over-fetch pool needs (count=2 → target_pool=6). The pool fills
    # from the deeper pages well before OpenAlex runs out → exhausted=False.
    pages = [
        [_author(0), _author(1), _author(2)],  # all excluded
        [_author(i) for i in range(3, 9)],  # A3..A8 — fresh, fills the pool of 6
        [_author(i) for i in range(9, 15)],  # A9..A14 — still more available
    ]
    fake_http = _CursorPagingClient(routes, pages)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    result = asyncio.run(
        discovery.find_professors(
            "neuro", count=2, user_interests=[], exclude_ids={"A0", "A1", "A2"}
        )
    )

    # Survivors come from the DEEPER pages — the first page was entirely excluded
    # yet discovery still made progress (the core bug fix).
    assert len(result.professors) == 2
    assert all(r.name not in {"Prof 0", "Prof 1", "Prof 2"} for r in result.professors)
    # Excluded first-page authors never reach enrichment / attempted_ids.
    assert result.attempted_ids.isdisjoint({"A0", "A1", "A2"})
    # The pool filled before OpenAlex ran out → NOT exhaustion. This is what stops
    # the CLI loop from prematurely declaring the field exhausted.
    assert result.exhausted is False
    # We paged at least twice to get past the all-excluded first page.
    author_calls = [
        c for c in fake_http.calls
        if c["url"].startswith("https://api.openalex.org/authors")
    ]
    assert len(author_calls) >= 2


def test_find_professors_reports_exhausted_when_openalex_out_of_authors(
    monkeypatch, mock_discovery_env
):
    """(b) OpenAlex genuinely out of new authors on a top-up → exhausted=True,
    empty result (which the CLI loop reads as a stop)."""
    async def _email(_client, _name, _inst, _results):
        return discovery._EmailExtraction(email="p@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/works"): {"results": []},
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "x", "url": "https://mit.edu/x", "content": "..."}]
        },
    }
    # The only two authors OpenAlex has (A0, A1) are both excluded; no further
    # page exists → genuine exhaustion for this top-up pass.
    pages = [[_author(0), _author(1)]]
    fake_http = _CursorPagingClient(routes, pages)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    result = asyncio.run(
        discovery.find_professors(
            "neuro", count=3, user_interests=[], exclude_ids={"A0", "A1"}
        )
    )

    assert result.professors == []
    assert result.attempted_ids == set()
    assert result.exhausted is True


def test_find_professors_enrichment_count_stays_bounded_by_overfetch(
    monkeypatch, mock_discovery_env
):
    """(c) Cost invariant: deeper paging does NOT increase enrichment. The number
    of paid Tavily searches stays bounded by count * OVERFETCH_MULTIPLIER even
    when many pages are paged to skip excluded authors."""

    async def _email(_client, _name, _inst, _results):
        return discovery._EmailExtraction(email="p@mit.edu", faculty_page_url=None)

    monkeypatch.setattr(discovery, "_llm_extract_email", _email)

    routes = {
        ("GET", "https://api.openalex.org/topics"): {
            "results": [{"id": "https://openalex.org/C1", "display_name": "F", "level": 1}]
        },
        ("GET", "https://api.openalex.org/works"): {"results": []},
        ("POST", "https://api.tavily.com/search"): {
            "results": [{"title": "x", "url": "https://mit.edu/x", "content": "..."}]
        },
    }
    # Three pages of 5 fresh authors each (A0..A14) — far more than the over-fetch
    # pool. count=2 → target_pool = 2*3 = 6, so only 6 authors should ever be
    # enriched, not all 15, even though paging is free to walk all pages.
    pages = [
        [_author(i) for i in range(0, 5)],
        [_author(i) for i in range(5, 10)],
        [_author(i) for i in range(10, 15)],
    ]
    fake_http = _CursorPagingClient(routes, pages)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    result = asyncio.run(
        discovery.find_professors("neuro", count=2, user_interests=[])
    )

    tavily_calls = [
        c for c in fake_http.calls if c["url"].startswith("https://api.tavily.com")
    ]
    bound = 2 * discovery.OVERFETCH_MULTIPLIER  # = 6
    assert len(tavily_calls) == bound
    # attempted_ids (the paid pool) is exactly the bounded over-fetch size.
    assert len(result.attempted_ids) == bound
