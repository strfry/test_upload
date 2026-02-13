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
    hf_base_url: str | None
    folder_name: str
    history_limit: int
    send_enabled: bool
    send_confirm: str
    delete_after_seconds: int
    interactive_enabled: bool
    debug_enabled: bool
    bot_token: str | None
    bot_allowed_chat_id: int | None
    auto_interval_seconds: int
    analysis_db_path: str


TRUE_VALUES = {"1", "true", "yes", "on"}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Umgebungsvariable fehlt: {name}")
    return value


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def load_config() -> AppConfig:
    return AppConfig(
        telegram_api_id=int(require_env("TELEGRAM_API_ID")),
        telegram_api_hash=require_env("TELEGRAM_API_HASH"),
        telegram_session=os.getenv("TELEGRAM_SESSION", "scambaiter"),
        hf_token=require_env("HF_TOKEN"),
        hf_model=require_env("HF_MODEL"),
        hf_base_url=os.getenv("HF_BASE_URL"),
        folder_name=os.getenv("SCAMBAITER_FOLDER_NAME", "Scammers"),
        history_limit=env_int("SCAMBAITER_HISTORY_LIMIT", 20),
        send_enabled=env_flag("SCAMBAITER_SEND"),
        send_confirm=os.getenv("SCAMBAITER_SEND_CONFIRM", ""),
        delete_after_seconds=env_int("SCAMBAITER_DELETE_OWN_AFTER_SECONDS", 0),
        interactive_enabled=env_flag("SCAMBAITER_INTERACTIVE", default=True),
        debug_enabled=env_flag("SCAMBAITER_DEBUG"),
        bot_token=os.getenv("SCAMBAITER_BOT_TOKEN"),
        bot_allowed_chat_id=(
            int(os.getenv("SCAMBAITER_BOT_ALLOWED_CHAT_ID"))
            if os.getenv("SCAMBAITER_BOT_ALLOWED_CHAT_ID")
            else None
        ),
        auto_interval_seconds=env_int("SCAMBAITER_AUTO_INTERVAL_SECONDS", 120),
        analysis_db_path=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"),
    )
