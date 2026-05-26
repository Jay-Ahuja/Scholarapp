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

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """Immutable view of resolved env vars + derived paths.

    Construct via `load_settings()`. All filesystem paths are absolute.
    """

    anthropic_api_key: str | None
    tavily_api_key: str | None
    send_enabled: bool
    send_daily_cap: int
    # Persistent, default-off preference: when True the real-send branch attaches
    # the run's resume PDF (as resume.pdf) to each outgoing email. Lives in
    # config.toml ([send].attach_resume), NOT an env var — it's command-driven via
    # `scholar attach-resume on|off`. The SEND_ENABLED gate is independent of this.
    send_attach_resume: bool
    data_dir: Path
    # Concurrency cap for parallel Anthropic calls in each pipeline stage
    # (discovery enrichment / matching / drafting). Tier 1 Anthropic accounts
    # have a 50 RPM cap shared across the org — too-high concurrency triggers
    # 429s the SDK can't always retry through. Default 3 is safe on Tier 1;
    # raise to 10+ once you've upgraded to Tier 2.
    anthropic_concurrency: int
    # SDK-level retry attempts for transient errors (429, 5xx, network).
    # Anthropic's SDK honors Retry-After headers; we just give it more chances
    # to ride out a long rate-limit window.
    anthropic_max_retries: int

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

    Missing file OR missing key/table => False. Never crashes on an absent or
    malformed config.toml — a bad file is treated as "preference not set".
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
    return bool(send_table.get("attach_resume", False))


def load_settings() -> Settings:
    """Read env vars + config.toml and return a fresh Settings."""
    data_dir = Path(os.environ.get("DATA_DIR", "~/.scholarapp")).expanduser().resolve()
    config_path = data_dir / "config.toml"
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        tavily_api_key=os.environ.get("TAVILY_API_KEY") or None,
        send_enabled=_env_bool("SEND_ENABLED", False),
        send_daily_cap=_env_int("SEND_DAILY_CAP", 20),
        send_attach_resume=_read_attach_resume(config_path),
        data_dir=data_dir,
        anthropic_concurrency=_env_int("ANTHROPIC_CONCURRENCY", 3),
        anthropic_max_retries=_env_int("ANTHROPIC_MAX_RETRIES", 5),
    )
