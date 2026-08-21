"""
config.py
Centralized environment/credential loading for social-mcp.

All secrets are read from environment variables (recommended: a local
`.env` file loaded via `python-dotenv`, never committed to source control).
Session/state files are stored under a configurable data directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # dotenv is optional; env vars can be set directly in the shell/OpenCode env block.
    pass


def _require_env(name: str, default: str | None = None, required: bool = True) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Set it in your shell or in a local .env file."
        )
    return value or ""


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    session_path: str
    phone: str | None  # only needed for the interactive first-login flow


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    db_path: Path
    telegram: TelegramConfig
    log_level: str


def load_config() -> AppConfig:
    """Load and validate all configuration required to run social-mcp."""

    data_dir = Path(os.environ.get("SOCIAL_MCP_DATA_DIR", str(Path.home() / ".social-mcp")))
    data_dir.mkdir(parents=True, exist_ok=True)

    telegram = TelegramConfig(
        api_id=int(_require_env("TELEGRAM_API_ID")),
        api_hash=_require_env("TELEGRAM_API_HASH"),
        session_path=os.environ.get(
            "TELEGRAM_SESSION_PATH", str(data_dir / "telegram.session")
        ),
        phone=os.environ.get("TELEGRAM_PHONE"),
    )

    return AppConfig(
        data_dir=data_dir,
        db_path=Path(os.environ.get("SOCIAL_MCP_DB_PATH", str(data_dir / "state.sqlite3"))),
        telegram=telegram,
        log_level=os.environ.get("SOCIAL_MCP_LOG_LEVEL", "INFO"),
    )
