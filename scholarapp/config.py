"""Runtime configuration: env vars + derived filesystem paths.

`.env` (if present at the working directory) is loaded once at import time. Call
`load_settings()` from CLI commands or modules to get a fresh, immutable Settings object.
"""

from __future__ import annotations

import os
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
    data_dir: Path

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


def load_settings() -> Settings:
    """Read env vars and return a fresh Settings."""
    data_dir = Path(os.environ.get("DATA_DIR", "~/.scholarapp")).expanduser().resolve()
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        tavily_api_key=os.environ.get("TAVILY_API_KEY") or None,
        send_enabled=_env_bool("SEND_ENABLED", False),
        send_daily_cap=_env_int("SEND_DAILY_CAP", 20),
        data_dir=data_dir,
    )
