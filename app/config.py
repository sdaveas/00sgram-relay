"""The relay's six env values, and their defaults. One place, no Config object."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """The six values this process reads from env, and their defaults."""
    api_url: str = "http://web:8080"
    api_token: str = ""
    telegram_token: str = ""
    relay_chat: str = ""
    db: Path = Path("/data/relay.sqlite3")
    interval: int = 20

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        try:
            interval = int(env.get("RELAY_INTERVAL") or 20)
        except ValueError:
            raise SystemExit("RELAY_INTERVAL must be a whole number of seconds") from None
        return cls(
            api_url=(env.get("API_URL") or "http://web:8080").rstrip("/"),
            api_token=env.get("API_TOKEN", "").strip(),
            telegram_token=env.get("TELEGRAM_TOKEN", "").strip(),
            relay_chat=env.get("TELEGRAM_RELAY_CHAT", "").strip(),
            db=Path(env.get("RELAY_DB") or "/data/relay.sqlite3"),
            interval=max(1, interval),
        )
