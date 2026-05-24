"""Shared pytest fixtures + vcrpy configuration.

The vcrpy bits power the e2e tests (test_e2e.py + test_e2e_recording.py). Unit
tests under test_*.py don't need them — they monkeypatch their own narrow
boundaries.

Markers:
- `record`: tests that hit real APIs to record cassettes. Skipped by default
  via pyproject.toml's `addopts = -m 'not record'`. Run with:
      pytest -m record tests/test_e2e_recording.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest


FIXTURES_DIR: Path = Path(__file__).parent / "fixtures"
CASSETTES_DIR: Path = Path(__file__).parent / "cassettes"


# ---------------------------------------------------------------------------
# E2E fixtures — isolated DATA_DIR, dummy keys, copied inputs
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_setup(tmp_path, monkeypatch):
    """One-stop env + filesystem isolation for end-to-end tests.

    - DATA_DIR + DRAFTS_DIR scoped to tmp_path
    - Dummy API keys (real keys only used by test_e2e_recording.py)
    - SEND_ENABLED stays false (gate test depends on this)
    - ANTHROPIC_CONCURRENCY=1 to serialize requests for stable cassette replay
    - Fixture files (sample_resume.pdf, sample_prompt.md, sample_template.md)
      copied into tmp_path/inputs/ with the names `scholar run` expects
    - SQLAlchemy engine reset so the fresh DATA_DIR is picked up
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("SEND_ENABLED", "false")
    # Serialize the pipeline so vcrpy's sequential match order is stable.
    monkeypatch.setenv("ANTHROPIC_CONCURRENCY", "1")
    monkeypatch.chdir(tmp_path)

    from scholarapp.db import session as db_session
    db_session.reset_engine()

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    shutil.copy(FIXTURES_DIR / "sample_resume.pdf", inputs / "resume.pdf")
    shutil.copy(FIXTURES_DIR / "sample_prompt.md", inputs / "prompt.md")
    shutil.copy(FIXTURES_DIR / "sample_template.md", inputs / "template.md")

    yield {
        "data_dir": tmp_path / "data",
        "drafts_dir": tmp_path / "drafts",
        "inputs": inputs,
        "tmp_path": tmp_path,
    }

    db_session.reset_engine()


# ---------------------------------------------------------------------------
# vcrpy configuration
# ---------------------------------------------------------------------------


def _redact_request(request):
    """Strip API keys from outgoing requests before they hit the cassette."""
    # Tavily ships api_key in the JSON body.
    if request.body and "api.tavily.com" in request.uri:
        try:
            body = json.loads(request.body)
            if isinstance(body, dict) and "api_key" in body:
                body["api_key"] = "REDACTED"
                request.body = json.dumps(body).encode()
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return request


def _redact_response(response):
    """No-op today; placeholder if responses ever embed sensitive info."""
    return response


def _build_vcr(record_mode: str):
    """Build a configured VCR instance. Used by both replay + record tests."""
    import vcr

    return vcr.VCR(
        cassette_library_dir=str(CASSETTES_DIR),
        record_mode=record_mode,
        # Don't match on body — PDF base64 + JSON ordering would make replay brittle.
        match_on=["method", "scheme", "host", "port", "path", "query"],
        filter_headers=[
            ("authorization", "REDACTED"),
            ("x-api-key", "REDACTED"),
            ("anthropic-version", None),  # noise; not security-sensitive
        ],
        before_record_request=_redact_request,
        before_record_response=_redact_response,
        decode_compressed_response=True,
    )


@pytest.fixture
def vcr_replay():
    """Strict-replay VCR — fails if a cassette is missing or doesn't match a request."""
    return _build_vcr(record_mode="none")


@pytest.fixture
def vcr_record():
    """Recording VCR — hits real APIs and overwrites cassettes.

    Used only by test_e2e_recording.py. Don't use in normal test paths.
    """
    return _build_vcr(record_mode="all")
