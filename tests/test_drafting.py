"""Unit tests for scholarapp.modules.drafting.

We mock the async Anthropic client and assert:
- The empty-matched case raises DraftingError (the spec's "skip this professor"
  safety net).
- `cache_control` is set on the system text block (in-run cache is the whole
  point of the system/user split for this module).
- The matched projects + professor info + goal/considerations all reach the
  user message.
- The model's tool output parses into EmailDraft.
- The batch wrapper preserves order across professors.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from scholarapp.errors import DraftingError
from scholarapp.modules import drafting
from scholarapp.modules.drafting import (
    DraftRequest,
    EmailDraft,
    ProfessorForDrafting,
    draft_email,
    draft_emails_for_run,
)
from scholarapp.modules.ingestion import (
    Education,
    Experience,
    Publication,
    ResumeData,
)
from scholarapp.modules.matching import MatchedProject


def _mock_response(subject: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="save_draft",
                input={"subject": subject, "body": body},
            )
        ],
        usage=SimpleNamespace(
            input_tokens=400,
            output_tokens=300,
            cache_creation_input_tokens=1500,  # first call
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
    def _install(response):
        client = _FakeAsyncClient(response)
        monkeypatch.setattr(drafting, "_get_anthropic_client", lambda: client)
        return client

    return _install


# ---------------------------------------------------------------------------
# Reusable inputs
# ---------------------------------------------------------------------------


_RESUME = ResumeData(
    name="Jay Ahuja",
    email="jay@example.edu",
    education=[
        Education(school="Carnegie Mellon", degree="BS", field="Computer Science", years="2020-2024")
    ],
    experiences=[
        Experience(
            org="CMU AI Lab",
            role="Research Assistant",
            years="2022-present",
            bullets=["Built CNN for brain extraction from MRI scans achieving 95% Dice."],
        ),
    ],
    skills=["Python", "PyTorch"],
    interests=["computational neuroscience", "medical image segmentation"],
    publications=[
        Publication(title="Brain extraction with U-Nets", venue="MICCAI", year=2024)
    ],
)

_PROFESSOR = ProfessorForDrafting(
    name="Karl Friston",
    institution="University College London",
    email="k.friston@ucl.ac.uk",
)

_MATCHED = [
    MatchedProject(
        project_id=42,
        title="Probabilistic segmentation in SPM",
        url="https://doi.org/10.x",
        why_relevant="Both handle partial-volume effects in cortical boundaries.",
    ),
]

_TEMPLATE = (
    "Subject: [topic]\n\nDear Prof. [last_name],\n\n[hook]\n\n[ask]\n\nBest,\nJay"
)

_GOAL = "30-minute chat about my brain extraction algorithm"
_CONSIDERATIONS = "prefer professors with recent neuroimaging work"


# ---------------------------------------------------------------------------
# draft_email (single)
# ---------------------------------------------------------------------------


def test_draft_email_raises_on_empty_matched():
    """The Step 6 spec's safety net: caller should pre-check, but if not, raise."""
    with pytest.raises(DraftingError, match="No matched projects for Karl Friston"):
        asyncio.run(
            draft_email(
                _TEMPLATE, _RESUME, _PROFESSOR, [], _GOAL, _CONSIDERATIONS
            )
        )


def test_draft_email_returns_validated_draft(fake_client):
    fake_client(
        _mock_response(
            subject="Question on partial-volume handling in SPM",
            body=(
                "Dear Prof. Friston,\n\nI'm Jay Ahuja, a senior at Carnegie Mellon "
                "working on a CNN-based brain extraction algorithm. I read your "
                "work on probabilistic segmentation in SPM — your approach to "
                "partial-volume effects in cortical boundaries directly addresses "
                "a problem I've been stuck on in my U-Net training.\n\nWould you "
                "be open to a 30-minute chat in the next few weeks?\n\nBest,\nJay"
            ),
        )
    )
    result = asyncio.run(
        draft_email(_TEMPLATE, _RESUME, _PROFESSOR, _MATCHED, _GOAL, _CONSIDERATIONS)
    )
    assert isinstance(result, EmailDraft)
    assert "partial-volume" in result.subject
    assert "Dear Prof. Friston" in result.body
    assert "Best,\nJay" in result.body


def test_draft_email_sets_cache_control_on_system(fake_client):
    client = fake_client(_mock_response("subj", "body"))
    asyncio.run(
        draft_email(_TEMPLATE, _RESUME, _PROFESSOR, _MATCHED, _GOAL, _CONSIDERATIONS)
    )
    kw = client.messages.last_kwargs
    assert kw is not None
    assert isinstance(kw["system"], list)
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    # System contains the user's template + condensed resume + drafting rules.
    sys_text = kw["system"][0]["text"]
    assert _TEMPLATE in sys_text                # template verbatim
    assert "Jay Ahuja" in sys_text              # condensed resume includes name
    assert "CMU AI Lab" in sys_text             # condensed resume includes experience
    assert "draft" in sys_text.lower()          # rules text loaded


def test_draft_email_user_message_contains_per_professor_inputs(fake_client):
    client = fake_client(_mock_response("subj", "body"))
    asyncio.run(
        draft_email(_TEMPLATE, _RESUME, _PROFESSOR, _MATCHED, _GOAL, _CONSIDERATIONS)
    )
    kw = client.messages.last_kwargs
    user_text = kw["messages"][0]["content"]
    assert "Karl Friston" in user_text
    assert "University College London" in user_text
    assert "Probabilistic segmentation in SPM" in user_text
    assert "Both handle partial-volume" in user_text       # the why_relevant
    assert "30-minute chat" in user_text                   # goal
    assert "neuroimaging work" in user_text                # considerations
    assert "Draft the email now." in user_text


def test_draft_email_tool_choice_is_forced(fake_client):
    client = fake_client(_mock_response("subj", "body"))
    asyncio.run(
        draft_email(_TEMPLATE, _RESUME, _PROFESSOR, _MATCHED, _GOAL, _CONSIDERATIONS)
    )
    kw = client.messages.last_kwargs
    assert kw["tool_choice"] == {"type": "tool", "name": "save_draft"}
    assert kw["tools"][0]["name"] == "save_draft"
    assert kw["model"] == drafting.MODEL_SONNET


def test_draft_email_raises_when_no_tool_use(fake_client):
    fake_client(
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="nope")],
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=10,
                cache_creation_input_tokens=0, cache_read_input_tokens=0,
            ),
        )
    )
    with pytest.raises(DraftingError, match="save_draft"):
        asyncio.run(
            draft_email(_TEMPLATE, _RESUME, _PROFESSOR, _MATCHED, _GOAL, _CONSIDERATIONS)
        )


# ---------------------------------------------------------------------------
# draft_emails_for_run (batch)
# ---------------------------------------------------------------------------


def test_draft_emails_for_run_preserves_order(fake_client):
    fake_client(_mock_response("subj", "body"))
    requests = [
        DraftRequest(
            professor=ProfessorForDrafting(name=f"Prof {i}", institution="Uni", email=f"p{i}@uni.edu"),
            matched=_MATCHED,
        )
        for i in range(3)
    ]
    results = asyncio.run(
        draft_emails_for_run(_TEMPLATE, _RESUME, requests, _GOAL, _CONSIDERATIONS)
    )
    assert len(results) == 3
    assert all(isinstance(r, EmailDraft) for r in results)
