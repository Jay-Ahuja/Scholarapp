"""Unit tests for scholarapp.modules.ingestion.

The Anthropic client is mocked — these tests never hit the real API. We verify both
the response-parsing path and the request shape (cache_control, document block,
forced tool_choice).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scholarapp.errors import IngestionError
from scholarapp.modules import ingestion
from scholarapp.modules.ingestion import parse_prompt, parse_resume


def _mock_response(tool_name: str, tool_input: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", name=tool_name, input=tool_input),
        ],
        usage=SimpleNamespace(
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            input_tokens=100,
            output_tokens=50,
        ),
    )


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


@pytest.fixture
def fake_client(monkeypatch):
    def _install(response):
        client = _FakeClient(response)
        monkeypatch.setattr(ingestion, "_get_client", lambda: client)
        return client

    return _install


# ---------------------------------------------------------------------------
# parse_resume
# ---------------------------------------------------------------------------


_VALID_RESUME_INPUT = {
    "name": "Jane Doe",
    "email": "jane@example.com",
    "education": [
        {"school": "MIT", "degree": "PhD", "field": "Neuroscience", "years": "2020-2024"}
    ],
    "experiences": [
        {
            "org": "MIT Brain Lab",
            "role": "PhD Student",
            "years": "2020-present",
            "bullets": ["Built optogenetic stimulation rig for zebrafish larvae."],
        }
    ],
    "skills": ["Python", "PyTorch"],
    "interests": ["computational neuroscience", "optogenetics"],
    "publications": [
        {"title": "Circuits paper", "venue": "Nature Neuro", "year": 2024, "url": None}
    ],
}


def test_parse_resume_returns_validated_model(tmp_path, fake_client):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nplaceholder bytes")
    fake_client(_mock_response("extract_resume", _VALID_RESUME_INPUT))

    result = parse_resume(pdf)

    assert result.name == "Jane Doe"
    assert result.email == "jane@example.com"
    assert result.education[0].school == "MIT"
    assert result.experiences[0].bullets == [
        "Built optogenetic stimulation rig for zebrafish larvae."
    ]
    assert "optogenetics" in result.interests
    assert result.publications[0].year == 2024


def test_parse_resume_sends_correct_request_shape(tmp_path, fake_client):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nplaceholder bytes")
    client = fake_client(_mock_response("extract_resume", _VALID_RESUME_INPUT))

    parse_resume(pdf)
    kw = client.messages.last_kwargs
    assert kw is not None

    # Model + caching
    assert kw["model"] == ingestion.MODEL_SONNET
    assert isinstance(kw["system"], list)
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "resume" in kw["system"][0]["text"].lower()

    # User content includes the PDF document block
    user_content = kw["messages"][0]["content"]
    doc = next(c for c in user_content if c["type"] == "document")
    assert doc["source"]["type"] == "base64"
    assert doc["source"]["media_type"] == "application/pdf"
    # base64 of the file bytes
    import base64 as _b64
    assert doc["source"]["data"] == _b64.standard_b64encode(pdf.read_bytes()).decode("ascii")

    # Tool is forced
    assert kw["tool_choice"] == {"type": "tool", "name": "extract_resume"}
    assert kw["tools"][0]["name"] == "extract_resume"


def test_parse_resume_raises_when_no_tool_use(tmp_path, fake_client):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nx")
    fake_client(
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="I refuse")],
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )
    )

    with pytest.raises(IngestionError, match="extract_resume"):
        parse_resume(pdf)


def test_parse_resume_raises_when_tool_input_invalid(tmp_path, fake_client):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nx")
    # Missing required 'email' field
    bad_input = dict(_VALID_RESUME_INPUT)
    bad_input.pop("email")
    fake_client(_mock_response("extract_resume", bad_input))

    with pytest.raises(IngestionError, match="schema"):
        parse_resume(pdf)


def test_parse_resume_raises_on_missing_file(tmp_path):
    missing = tmp_path / "nope.pdf"
    with pytest.raises(IngestionError, match="not found"):
        parse_resume(missing)


# ---------------------------------------------------------------------------
# parse_prompt
# ---------------------------------------------------------------------------


def test_parse_prompt_returns_data_on_success(fake_client):
    fake_client(
        _mock_response(
            "extract_prompt",
            {
                "count": 3,
                "field": "computational neuroscience",
                "goal": "30-min chat",
                "considerations": "west coast schools",
                "missing_fields": [],
            },
        )
    )

    result = parse_prompt(
        "Email 3 comp neuro profs on the west coast for a 30 min chat."
    )

    assert result.count == 3
    assert result.field == "computational neuroscience"
    assert result.goal == "30-min chat"
    assert result.considerations == "west coast schools"


def test_parse_prompt_sends_cache_control(fake_client):
    client = fake_client(
        _mock_response(
            "extract_prompt",
            {
                "count": 1,
                "field": "robotics",
                "goal": "lab position",
                "considerations": "",
                "missing_fields": [],
            },
        )
    )
    parse_prompt("Find me 1 robotics professor for a lab position.")
    kw = client.messages.last_kwargs
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["tool_choice"] == {"type": "tool", "name": "extract_prompt"}


def test_parse_prompt_raises_when_required_field_missing(fake_client):
    fake_client(
        _mock_response(
            "extract_prompt",
            {
                "count": 5,
                "field": "neuroscience",
                "goal": None,
                "considerations": "",
                "missing_fields": ["goal"],
            },
        )
    )

    with pytest.raises(IngestionError, match="goal"):
        parse_prompt("Email 5 neuroscience professors.")


def test_parse_prompt_infers_missing_from_empty_value(fake_client):
    """If Claude returns an empty `field` but doesn't list it in missing_fields, we still catch it."""
    fake_client(
        _mock_response(
            "extract_prompt",
            {
                "count": 5,
                "field": "",
                "goal": "chat",
                "considerations": "",
                "missing_fields": [],
            },
        )
    )

    with pytest.raises(IngestionError, match="field"):
        parse_prompt("Vague prompt.")


def test_parse_prompt_raises_on_count_out_of_range(fake_client):
    fake_client(
        _mock_response(
            "extract_prompt",
            {
                "count": 100,
                "field": "neuroscience",
                "goal": "chat",
                "considerations": "",
                "missing_fields": [],
            },
        )
    )

    with pytest.raises(IngestionError, match="count"):
        parse_prompt("Email 100 neuroscience professors.")


def test_parse_prompt_raises_on_empty_text():
    with pytest.raises(IngestionError, match="empty"):
        parse_prompt("   ")


# ---------------------------------------------------------------------------
# Resume parse cache (lives in cli._parse_resume_cached)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point DATA_DIR at tmp_path and reset SQLAlchemy state.

    Without reset_engine() the test sees an engine bound to whichever DATA_DIR
    was active at first import — typically the user's real ~/.scholarapp/.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from scholarapp.db import session as db_session

    db_session.reset_engine()
    yield tmp_path
    db_session.reset_engine()


def test_resume_cache_miss_then_hit(tmp_path, isolated_db, fake_client):
    """First call: parse_resume runs. Second call with same bytes: cache hit, no Claude call."""
    from scholarapp.cli import _parse_resume_cached

    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nbytes for hash A")
    client = fake_client(_mock_response("extract_resume", _VALID_RESUME_INPUT))

    # First call: cache miss → Claude call → cache write.
    r1 = _parse_resume_cached(pdf)
    assert r1.name == "Jane Doe"
    assert client.messages.last_kwargs is not None

    # Reset the call recorder; if a second call happens, last_kwargs will repopulate.
    client.messages.last_kwargs = None
    r2 = _parse_resume_cached(pdf)
    assert r2.name == "Jane Doe"
    assert client.messages.last_kwargs is None  # cache hit — no Claude call


def test_resume_cache_invalidates_on_byte_change(tmp_path, isolated_db, fake_client):
    """Editing the PDF by even one byte changes the hash, forcing a re-parse."""
    from scholarapp.cli import _parse_resume_cached

    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nversion 1")
    client = fake_client(_mock_response("extract_resume", _VALID_RESUME_INPUT))

    _parse_resume_cached(pdf)
    assert client.messages.last_kwargs is not None

    # Change one byte → different hash → cache miss → Claude called again.
    pdf.write_bytes(b"%PDF-1.4\nversion 2")
    client.messages.last_kwargs = None
    _parse_resume_cached(pdf)
    assert client.messages.last_kwargs is not None
