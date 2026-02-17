from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    hf_token: str
    hf_model: str
    hf_vision_model: str
    hf_base_url: str | None
    hf_max_tokens: int
    folder_name: str
    history_limit: int
    send_enabled: bool
    send_confirm: str
    delete_after_seconds: int
    interactive_enabled: bool
    debug_enabled: bool
    bot_token: str | None
    auto_interval_seconds: int
    analysis_db_path: str


TRUE_VALUES = {"1", "true", "yes", "on"}


def _sanitize_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    # Remove ASCII control characters (e.g. CR/LF from copied .env entries).
    cleaned = "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()
    return cleaned


def env_str(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return _sanitize_env_value(default)
    return _sanitize_env_value(raw)


def require_env(name: str) -> str:
    value = env_str(name)
    if not value:
        raise ValueError(f"Umgebungsvariable fehlt: {name}")
    return value


def env_flag(name: str, default: bool = False) -> bool:
    value = env_str(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    value = env_str(name)
    if not value:
        return default
    return int(value)


def load_config() -> AppConfig:
    return AppConfig(
        telegram_api_id=int(require_env("TELEGRAM_API_ID")),
        telegram_api_hash=require_env("TELEGRAM_API_HASH"),
        telegram_session=env_str("TELEGRAM_SESSION", "scambaiter") or "scambaiter",
        hf_token=require_env("HF_TOKEN"),
        hf_model=require_env("HF_MODEL"),
        hf_vision_model=env_str("HF_VISION_MODEL") or require_env("HF_MODEL"),
        hf_base_url=env_str("HF_BASE_URL"),
        hf_max_tokens=env_int("HF_MAX_TOKENS", 350),
        folder_name=env_str("SCAMBAITER_FOLDER_NAME", "Scammers") or "Scammers",
        history_limit=env_int("SCAMBAITER_HISTORY_LIMIT", 20),
        send_enabled=env_flag("SCAMBAITER_SEND"),
        send_confirm=env_str("SCAMBAITER_SEND_CONFIRM", "") or "",
        delete_after_seconds=env_int("SCAMBAITER_DELETE_OWN_AFTER_SECONDS", 0),
        interactive_enabled=env_flag("SCAMBAITER_INTERACTIVE", default=True),
        debug_enabled=env_flag("SCAMBAITER_DEBUG"),
        bot_token=env_str("SCAMBAITER_BOT_TOKEN"),
        auto_interval_seconds=env_int("SCAMBAITER_AUTO_INTERVAL_SECONDS", 120),
        analysis_db_path=env_str("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3") or "scambaiter.sqlite3",
    )
