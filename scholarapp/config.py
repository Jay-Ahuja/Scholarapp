"""Runtime configuration: env vars + derived filesystem paths.

`.env` (if present at the working directory) is loaded once at import time. Call
`load_settings()` from CLI commands or modules to get a fresh, immutable Settings object.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(name: str, raw: str | None, default: int) -> int:
    # Parse a stored raw env string lazily (at attribute access). Unset/blank =>
    # the default; a non-numeric value => ConfigError naming the var + the bad
    # value so the CLI renders it cleanly instead of an int() ValueError traceback.
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _parse_float(name: str, raw: str | None, default: float | None) -> float | None:
    # Mirrors _parse_int but allows a None default so an unset var means "no value"
    # (e.g. RUN_MAX_USD unset => no spend ceiling) rather than a magic sentinel.
    # Only a non-numeric parse failure becomes a ConfigError; "inf"/"nan" parse
    # via float and flow through unchanged (the non-finite/negative budget rule
    # lives in the run flow, not here).
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    """Immutable view of resolved env vars + derived paths.

    Construct via `load_settings()`. All filesystem paths are absolute.
    """

    anthropic_api_key: str | None
    tavily_api_key: str | None
    send_enabled: bool
    # Persistent, default-off preference: when True the real-send branch attaches
    # the run's resume PDF (as resume.pdf) to each outgoing email. Lives in
    # config.toml ([send].attach_resume), NOT an env var — it's command-driven via
    # `scholar attach-resume on|off`. The SEND_ENABLED gate is independent of this.
    send_attach_resume: bool
    data_dir: Path
    # Raw, unparsed env strings for the four numeric settings. We capture them at
    # load_settings() time but parse lazily in the @property accessors below, so a
    # malformed value (e.g. SEND_DAILY_CAP=abc) only raises ConfigError when the
    # setting is actually read — a command that never reads it never trips its error,
    # and load_settings() itself never raises on these.
    send_daily_cap_raw: str | None
    anthropic_concurrency_raw: str | None
    anthropic_max_retries_raw: str | None
    run_max_usd_raw: str | None

    @property
    def send_daily_cap(self) -> int:
        return _parse_int("SEND_DAILY_CAP", self.send_daily_cap_raw, 20)

    @property
    def anthropic_concurrency(self) -> int:
        # Concurrency cap for parallel Anthropic calls in each pipeline stage
        # (discovery enrichment / matching / drafting). Tier 1 Anthropic accounts
        # have a 50 RPM cap shared across the org — too-high concurrency triggers
        # 429s the SDK can't always retry through. Default 3 is safe on Tier 1;
        # raise to 10+ once you've upgraded to Tier 2.
        return _parse_int("ANTHROPIC_CONCURRENCY", self.anthropic_concurrency_raw, 3)

    @property
    def anthropic_max_retries(self) -> int:
        # SDK-level retry attempts for transient errors (429, 5xx, network).
        # Anthropic's SDK honors Retry-After headers; we just give it more chances
        # to ride out a long rate-limit window.
        return _parse_int("ANTHROPIC_MAX_RETRIES", self.anthropic_max_retries_raw, 5)

    @property
    def run_max_usd(self) -> float | None:
        # Optional per-run spend ceiling in USD. None => no ceiling (the default):
        # estimation/cost work is purely informational and never blocks a run. The
        # CLI decides what to do when an estimate exceeds this (confirm / abort);
        # config only carries the value.
        return _parse_float("RUN_MAX_USD", self.run_max_usd_raw, None)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scholar.db"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def client_secret_path(self) -> Path:
        return self.data_dir / "client_secret.json"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def drafts_root(self) -> Path:
        """Where editable draft .md files are written.

        Defaults to `<cwd>/drafts/` so files show up in Finder / file explorers
        (Mac hides `~/.scholarapp/` by default). Override with `DRAFTS_DIR` if you
        want them somewhere else.
        """
        raw = os.environ.get("DRAFTS_DIR")
        if raw:
            return Path(raw).expanduser().resolve()
        return (Path.cwd() / "drafts").resolve()


def _read_attach_resume(config_path: Path) -> bool:
    """Read [send].attach_resume from config.toml via stdlib tomllib.

    Enables attachments ONLY when the stored value is a genuine boolean True.
    ANY non-boolean value (e.g. the string "false" OR "true", a number), a
    missing key/table, a missing file, or a malformed config => False. We do
    NOT coerce string values like "true"/"yes"/"1" — non-bool means off,
    unconditionally. This guards against a hand-edited or tool-written
    config.toml storing `attach_resume = "false"` (a STRING), which `bool(...)`
    would read as truthy. Never crashes on an absent or malformed config.toml.
    """
    if not config_path.exists():
        return False
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    send_table = data.get("send")
    if not isinstance(send_table, dict):
        return False
    # Strict: only a real bool True counts. `is True` rejects truthy strings,
    # numbers, etc. (note `1 == True` so an equality check would be wrong here).
    return send_table.get("attach_resume") is True


def load_settings() -> Settings:
    """Read env vars + config.toml and return a fresh Settings."""
    data_dir = Path(os.environ.get("DATA_DIR", "~/.scholarapp")).expanduser().resolve()
    config_path = data_dir / "config.toml"
    # Capture the raw env strings only; the numeric @property accessors parse them
    # lazily so a malformed value never makes load_settings() raise.
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        tavily_api_key=os.environ.get("TAVILY_API_KEY") or None,
        send_enabled=_env_bool("SEND_ENABLED", False),
        send_attach_resume=_read_attach_resume(config_path),
        data_dir=data_dir,
        send_daily_cap_raw=os.environ.get("SEND_DAILY_CAP"),
        anthropic_concurrency_raw=os.environ.get("ANTHROPIC_CONCURRENCY"),
        anthropic_max_retries_raw=os.environ.get("ANTHROPIC_MAX_RETRIES"),
        run_max_usd_raw=os.environ.get("RUN_MAX_USD"),
    )
