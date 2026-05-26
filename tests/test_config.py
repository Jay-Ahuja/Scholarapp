"""Tests for the persistent resume-attachment toggle.

Covers the config layer (config.load_settings reading [send].attach_resume from
config.toml via stdlib tomllib) and the CLI surface (`scholar attach-resume
on|off` persisting the preference and load_settings reflecting it across runs).

The toggle home is config.toml ([send].attach_resume) — NOT an env var. Missing
file or missing key must default False and must never crash load_settings.
"""

from __future__ import annotations

from typer.testing import CliRunner

from scholarapp.cli import app
from scholarapp.config import load_settings


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
