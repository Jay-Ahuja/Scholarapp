"""Cassette recording variant of the e2e test.

This DOES hit real APIs. Running it costs:
- ~$0.05 of Anthropic credits (Haiku + Sonnet across the pipeline)
- ~9 Tavily search requests (free-tier quota)
- ~12 OpenAlex requests (free)
- Zero Gmail calls (SEND_ENABLED stays false)

Marked `@pytest.mark.record` and skipped by default. Opt in:

    # First, export real keys (do NOT use placeholder values):
    export ANTHROPIC_API_KEY=sk-ant-...
    export TAVILY_API_KEY=tvly-...

    # Then refresh the cassette:
    pytest -m record tests/test_e2e_recording.py

    # Commit the refreshed cassette:
    git add tests/cassettes/test_full_pipeline_no_send.yaml
    git commit -m "refresh e2e cassette"
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from scholarapp.cli import app


CASSETTE_NAME = "test_full_pipeline_no_send.yaml"


def _looks_like_real_key(key: str | None) -> bool:
    """Defensive check — refuse to record if the env still has placeholder keys."""
    if not key:
        return False
    if key.startswith("test-") or key.lower() == "redacted" or key == "":
        return False
    return len(key) > 20  # real Anthropic / Tavily keys are much longer


@pytest.mark.record
def test_record_full_pipeline_cassette(e2e_setup, vcr_record, monkeypatch):
    """Hit real APIs and overwrite tests/cassettes/test_full_pipeline_no_send.yaml.

    The `e2e_setup` fixture installs PLACEHOLDER keys via monkeypatch. Here we
    override with the real keys from the user's actual environment so the
    recording goes through.
    """
    real_anthropic = os.environ.get("_REAL_ANTHROPIC_API_KEY") or os.environ.get(
        "ANTHROPIC_API_KEY"
    )
    real_tavily = os.environ.get("_REAL_TAVILY_API_KEY") or os.environ.get(
        "TAVILY_API_KEY"
    )

    if not _looks_like_real_key(real_anthropic):
        pytest.skip(
            "Set ANTHROPIC_API_KEY to a real key in your env (or "
            "_REAL_ANTHROPIC_API_KEY to override e2e_setup's placeholder) "
            "before recording."
        )
    if not _looks_like_real_key(real_tavily):
        pytest.skip(
            "Set TAVILY_API_KEY to a real key (or _REAL_TAVILY_API_KEY) "
            "before recording."
        )

    # The e2e_setup fixture set placeholder keys via monkeypatch. Override
    # with real keys for this one test — monkeypatch unwinds at teardown.
    monkeypatch.setenv("ANTHROPIC_API_KEY", real_anthropic)
    monkeypatch.setenv("TAVILY_API_KEY", real_tavily)

    runner = CliRunner()
    with vcr_record.use_cassette(CASSETTE_NAME):
        result = runner.invoke(app, ["run", "--inputs", str(e2e_setup["inputs"])])

    assert result.exit_code == 0, (
        f"scholar run failed during recording:\n{result.stdout}"
    )
    # After this passes, tests/cassettes/test_full_pipeline_no_send.yaml is
    # populated and the regular `pytest` run picks it up via test_e2e.py.
