"""Tests for the persistent resume-attachment toggle.

Covers the config layer (config.load_settings reading [send].attach_resume from
config.toml via stdlib tomllib) and the CLI surface (`scholar attach-resume
on|off` persisting the preference and load_settings reflecting it across runs).

The toggle home is config.toml ([send].attach_resume) — NOT an env var. Missing
file or missing key must default False and must never crash load_settings.
"""

from __future__ import annotations

import math

import pytest
from typer.testing import CliRunner

from scholarapp.cli import app
from scholarapp.config import load_settings
from scholarapp.errors import ConfigError


def _write_config(data_dir, body: str) -> None:
    (data_dir / "config.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# load_settings reading config.toml
# ---------------------------------------------------------------------------


def test_default_off_when_config_absent(tmp_path, monkeypatch):
    """No config.toml at all => send_attach_resume False, no crash."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert not (tmp_path / "config.toml").exists()
    settings = load_settings()
    assert settings.send_attach_resume is False


def test_default_off_when_key_missing(tmp_path, monkeypatch):
    """config.toml present but no [send] table => default False."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, '[app]\nversion = "0.1.0"\n')
    settings = load_settings()
    assert settings.send_attach_resume is False


def test_default_off_when_send_table_lacks_key(tmp_path, monkeypatch):
    """[send] present but attach_resume key missing => default False."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, "[send]\nsomething_else = true\n")
    settings = load_settings()
    assert settings.send_attach_resume is False


def test_reads_true(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, "[send]\nattach_resume = true\n")
    settings = load_settings()
    assert settings.send_attach_resume is True


def test_reads_false(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, "[send]\nattach_resume = false\n")
    settings = load_settings()
    assert settings.send_attach_resume is False


# ---------------------------------------------------------------------------
# Strict-boolean interpretation: ONLY a genuine boolean True enables attachments.
# Any non-bool value (string, number) — even a truthy one — resolves to off.
# Guards a hand-edited / tool-written config where attach_resume is a STRING.
# ---------------------------------------------------------------------------


def test_string_false_is_off(tmp_path, monkeypatch):
    """`attach_resume = "false"` (a STRING) must be off, not truthy-on."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, '[send]\nattach_resume = "false"\n')
    settings = load_settings()
    assert settings.send_attach_resume is False


def test_string_true_is_off(tmp_path, monkeypatch):
    """`attach_resume = "true"` (a STRING) must be off — we do not coerce strings."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, '[send]\nattach_resume = "true"\n')
    settings = load_settings()
    assert settings.send_attach_resume is False


def test_string_truthy_values_are_off(tmp_path, monkeypatch):
    """Common truthy strings ("yes"/"1"/"on") must NOT coerce to on."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for value in ("yes", "1", "on"):
        _write_config(tmp_path, f'[send]\nattach_resume = "{value}"\n')
        assert load_settings().send_attach_resume is False, value


def test_number_one_is_off(tmp_path, monkeypatch):
    """A numeric 1 must be off — `1 == True` but `1 is not True`."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, "[send]\nattach_resume = 1\n")
    settings = load_settings()
    assert settings.send_attach_resume is False


def test_genuine_bool_true_is_on(tmp_path, monkeypatch):
    """Only a real TOML boolean true flips the toggle on."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, "[send]\nattach_resume = true\n")
    settings = load_settings()
    assert settings.send_attach_resume is True


def test_malformed_config_does_not_crash(tmp_path, monkeypatch):
    """A malformed config.toml is treated as 'preference not set', not a crash."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_config(tmp_path, "this is = = not valid toml [[[")
    settings = load_settings()
    assert settings.send_attach_resume is False


# ---------------------------------------------------------------------------
# `scholar attach-resume on|off` persistence
# ---------------------------------------------------------------------------


def test_attach_resume_on_persists_and_load_settings_reflects(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(app, ["attach-resume", "on"])
    assert result.exit_code == 0, result.stdout

    # Persisted to config.toml and a fresh load_settings sees it (sticky).
    assert (tmp_path / "config.toml").exists()
    assert load_settings().send_attach_resume is True


def test_attach_resume_off_persists_and_load_settings_reflects(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runner = CliRunner()

    # Turn on, then off — the off must overwrite the on.
    assert runner.invoke(app, ["attach-resume", "on"]).exit_code == 0
    assert load_settings().send_attach_resume is True

    result = runner.invoke(app, ["attach-resume", "off"])
    assert result.exit_code == 0, result.stdout
    assert load_settings().send_attach_resume is False


def test_attach_resume_preserves_app_block(tmp_path, monkeypatch):
    """The regenerated config.toml keeps the [app] content."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(app, ["attach-resume", "on"])
    assert result.exit_code == 0, result.stdout

    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "[app]" in text
    assert 'version = "0.1.0"' in text
    assert "[send]" in text
    assert "attach_resume = true" in text


def test_attach_resume_rejects_bad_state(tmp_path, monkeypatch):
    """Only on/off are valid; anything else is a usage error (non-zero exit)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["attach-resume", "maybe"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# RUN_MAX_USD — optional per-run spend ceiling (None => no ceiling)
# ---------------------------------------------------------------------------


def test_run_max_usd_unset_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RUN_MAX_USD", raising=False)
    assert load_settings().run_max_usd is None


def test_run_max_usd_blank_is_none(tmp_path, monkeypatch):
    """An empty/whitespace value is treated as unset, not 0.0."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_MAX_USD", "   ")
    assert load_settings().run_max_usd is None


def test_run_max_usd_parses_float(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_MAX_USD", "2.50")
    assert load_settings().run_max_usd == 2.50


def test_run_max_usd_parses_integer_string(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_MAX_USD", "5")
    assert load_settings().run_max_usd == 5.0


# ---------------------------------------------------------------------------
# Lazy parsing of numeric env vars: malformed values fail gracefully.
#
# load_settings() captures raw env strings and must NEVER raise on these four;
# the numeric @property accessors parse lazily and raise ConfigError (a
# ScholarError the CLI renders cleanly) only when a malformed setting is read.
# A command that never reads a given setting never trips its error.
# ---------------------------------------------------------------------------


def test_load_settings_never_raises_on_malformed_numerics(tmp_path, monkeypatch):
    """All four numeric vars malformed at once => load_settings() still succeeds."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEND_DAILY_CAP", "abc")
    monkeypatch.setenv("ANTHROPIC_CONCURRENCY", "lots")
    monkeypatch.setenv("ANTHROPIC_MAX_RETRIES", "x")
    monkeypatch.setenv("RUN_MAX_USD", "cheap")
    # No exception — parsing is deferred to attribute access.
    load_settings()


# --- send_daily_cap ---


def test_send_daily_cap_unset_is_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SEND_DAILY_CAP", raising=False)
    assert load_settings().send_daily_cap == 20


def test_send_daily_cap_blank_is_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEND_DAILY_CAP", "   ")
    assert load_settings().send_daily_cap == 20


def test_send_daily_cap_valid_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEND_DAILY_CAP", "42")
    assert load_settings().send_daily_cap == 42


def test_send_daily_cap_malformed_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEND_DAILY_CAP", "abc")
    settings = load_settings()  # no raise here
    with pytest.raises(ConfigError) as exc:
        _ = settings.send_daily_cap
    assert "SEND_DAILY_CAP" in str(exc.value)
    assert "abc" in str(exc.value)


# --- anthropic_concurrency ---


def test_anthropic_concurrency_unset_is_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_CONCURRENCY", raising=False)
    assert load_settings().anthropic_concurrency == 3


def test_anthropic_concurrency_valid_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_CONCURRENCY", "10")
    assert load_settings().anthropic_concurrency == 10


def test_anthropic_concurrency_malformed_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_CONCURRENCY", "lots")
    settings = load_settings()
    with pytest.raises(ConfigError) as exc:
        _ = settings.anthropic_concurrency
    assert "ANTHROPIC_CONCURRENCY" in str(exc.value)
    assert "lots" in str(exc.value)


# --- anthropic_max_retries ---


def test_anthropic_max_retries_unset_is_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_MAX_RETRIES", raising=False)
    assert load_settings().anthropic_max_retries == 5


def test_anthropic_max_retries_valid_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_MAX_RETRIES", "8")
    assert load_settings().anthropic_max_retries == 8


def test_anthropic_max_retries_malformed_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_MAX_RETRIES", "x")
    settings = load_settings()
    with pytest.raises(ConfigError) as exc:
        _ = settings.anthropic_max_retries
    assert "ANTHROPIC_MAX_RETRIES" in str(exc.value)
    assert "x" in str(exc.value)


# --- run_max_usd ---


def test_run_max_usd_malformed_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_MAX_USD", "cheap")
    settings = load_settings()
    with pytest.raises(ConfigError) as exc:
        _ = settings.run_max_usd
    assert "RUN_MAX_USD" in str(exc.value)
    assert "cheap" in str(exc.value)


def test_run_max_usd_inf_parses_through(tmp_path, monkeypatch):
    """`inf` must still parse via float and flow through — the non-finite rule
    lives in the run flow, not config."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_MAX_USD", "inf")
    assert load_settings().run_max_usd == math.inf


def test_run_max_usd_nan_parses_through(tmp_path, monkeypatch):
    """`nan` parses via float (no ConfigError); config never inspects finiteness."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_MAX_USD", "nan")
    assert math.isnan(load_settings().run_max_usd)


# ---------------------------------------------------------------------------
# A command that never reads a malformed setting never trips its error: the
# CLI surface (`scholar list` / `scholar init`) succeeds even with RUN_MAX_USD
# garbage, while `scholar run` would render the ConfigError cleanly (exit 1).
# ---------------------------------------------------------------------------


def test_unread_malformed_setting_does_not_block_other_commands(tmp_path, monkeypatch):
    """RUN_MAX_USD=abc must not break commands that never read run_max_usd."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_MAX_USD", "abc")
    runner = CliRunner()
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0, result.stdout
