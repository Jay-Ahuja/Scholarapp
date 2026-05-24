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

    results = asyncio.run(discovery.find_professors("neuroscience", count=2, user_interests=[]))

    assert len(results) == 2
    assert [r.name for r in results] == ["Prof 0", "Prof 1"]
    assert all(r.email.endswith(".edu") for r in results)
    # Works got persisted as WorkRefs with decompressed abstracts.
    assert results[0].recent_works[0].abstract == "important result"
    assert results[0].recent_works[0].url == "https://doi.org/10.1/1"
    # faculty page surfaces
    assert results[0].faculty_page_url == "https://mit.edu/p0"


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
        results = asyncio.run(
            discovery.find_professors("neuroscience", count=2, user_interests=[])
        )

    assert results == []
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
    assert asyncio.run(discovery.find_professors("x", 0, [])) == []


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
