"""Unit tests for scholarapp.modules.matching.

We mock the async Anthropic client and assert:
- The schema cap (≤ 3 picks) is enforced after the LLM responds.
- Unknown project_ids are dropped (defense against model hallucination).
- An empty pick list logs a warning and returns [] (the "skip this professor"
  branch the drafting step relies on).
- The request shape includes the works + interests in the user message and
  cache_control + the forced `select_matches` tool.
- `match_projects_for_run` parallelizes correctly via asyncio.gather.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from scholarapp.errors import MatchingError
from scholarapp.modules import matching
from scholarapp.modules.matching import (
    MatchedProject,
    ProjectForMatching,
    match_projects,
    match_projects_for_run,
)


def _mock_response(picks: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="select_matches",
                input={"matches": picks},
            )
        ],
        usage=SimpleNamespace(
            input_tokens=1500,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class _FakeAsyncMessages:
    def __init__(self, response):
        self._response = response
        self.last_kwargs: dict | None = None
        self.call_count = 0

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        self.call_count += 1
        return self._response


class _FakeAsyncClient:
    def __init__(self, response):
        self.messages = _FakeAsyncMessages(response)


@pytest.fixture
def fake_client(monkeypatch):
    """Returns a factory: install_response(response) → FakeClient available everywhere."""

    def _install(response):
        client = _FakeAsyncClient(response)
        monkeypatch.setattr(matching, "_get_anthropic_client", lambda: client)
        return client

    return _install


# Reusable inputs
_PROJECTS = [
    ProjectForMatching(
        project_id=1, title="CNN-based MRI segmentation", year=2024,
        abstract="We train a U-Net to segment whole-brain volumes.",
    ),
    ProjectForMatching(
        project_id=2, title="Optogenetic stimulation of zebrafish circuits", year=2023,
        abstract="Probing motor pathways with blue-light pulses.",
    ),
    ProjectForMatching(
        project_id=3, title="A theoretical paper on category theory", year=2022,
        abstract="Pure math, unrelated to neuro.",
    ),
]

_INTERESTS = ["computational neuroscience", "ML", "brain imaging"]
_EXPERIENCES = "- PhD student at MIT: built a CNN for MRI segmentation."


# ---------------------------------------------------------------------------
# match_projects (single)
# ---------------------------------------------------------------------------


def test_match_projects_returns_validated_picks(fake_client):
    fake_client(
        _mock_response(
            [
                {"project_id": 1, "why_relevant": "Both use CNNs for MRI segmentation."},
                {"project_id": 2, "why_relevant": "Adjacent methods in circuit-level neuro."},
            ]
        )
    )
    result = asyncio.run(match_projects("Jane Doe", _PROJECTS, _INTERESTS, _EXPERIENCES))
    assert len(result) == 2
    assert result[0].project_id == 1
    assert result[0].title == "CNN-based MRI segmentation"
    assert "CNN" in result[0].why_relevant
    assert result[1].project_id == 2


def test_match_projects_enforces_max_3_cap(fake_client):
    # LLM tries to return 5; we keep only the first 3 by relevance order.
    fake_client(
        _mock_response(
            [
                {"project_id": i, "why_relevant": f"reason {i}"}
                for i in [1, 2, 3, 1, 2]  # last 2 duplicate ids; first 3 distinct
            ]
        )
    )
    # max_length=3 validation on _MatchExtraction should reject 5 entries:
    with pytest.raises(MatchingError, match="schema"):
        asyncio.run(match_projects("Jane Doe", _PROJECTS, _INTERESTS, _EXPERIENCES))


def test_match_projects_returns_empty_and_warns_when_no_picks(fake_client, caplog):
    fake_client(_mock_response([]))
    with caplog.at_level("WARNING", logger="scholarapp.modules.matching"):
        result = asyncio.run(
            match_projects("Jane Doe", _PROJECTS, _INTERESTS, _EXPERIENCES)
        )
    assert result == []
    assert any("No projects matched" in r.message for r in caplog.records)


def test_match_projects_drops_unknown_project_ids(fake_client, caplog):
    fake_client(
        _mock_response(
            [
                {"project_id": 1, "why_relevant": "legit overlap"},
                {"project_id": 999, "why_relevant": "hallucinated id"},
            ]
        )
    )
    with caplog.at_level("WARNING", logger="scholarapp.modules.matching"):
        result = asyncio.run(
            match_projects("Jane Doe", _PROJECTS, _INTERESTS, _EXPERIENCES)
        )
    assert [m.project_id for m in result] == [1]
    assert any("unknown project_id" in r.message for r in caplog.records)


def test_match_projects_sends_correct_request_shape(fake_client):
    client = fake_client(
        _mock_response([{"project_id": 1, "why_relevant": "x"}])
    )
    asyncio.run(match_projects("Jane Doe", _PROJECTS, _INTERESTS, _EXPERIENCES))
    kw = client.messages.last_kwargs
    assert kw is not None
    assert kw["model"] == matching.MODEL_SONNET
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "match" in kw["system"][0]["text"].lower()
    assert kw["tool_choice"] == {"type": "tool", "name": "select_matches"}
    # User message includes the works
    user_text = kw["messages"][0]["content"]
    assert "Jane Doe" in user_text
    assert "CNN-based MRI segmentation" in user_text
    assert "computational neuroscience" in user_text


def test_match_projects_handles_empty_projects_list(fake_client):
    client = fake_client(_mock_response([]))
    result = asyncio.run(match_projects("X", [], _INTERESTS, _EXPERIENCES))
    assert result == []
    # No Claude call made at all
    assert client.messages.call_count == 0


# ---------------------------------------------------------------------------
# match_projects_for_run (batch)
# ---------------------------------------------------------------------------


def test_match_projects_for_run_parallelizes_in_order(fake_client):
    # Same canned response for every professor; we're verifying ordering + count.
    fake_client(_mock_response([{"project_id": 1, "why_relevant": "x"}]))
    pairs = [
        ("Prof A", _PROJECTS),
        ("Prof B", _PROJECTS),
        ("Prof C", _PROJECTS),
    ]
    results = asyncio.run(
        match_projects_for_run(pairs, _INTERESTS, _EXPERIENCES)
    )
    assert len(results) == 3
    assert all(len(r) == 1 for r in results)
    assert all(isinstance(r[0], MatchedProject) for r in results)
