from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class Config:
    analysis_db_path: str = "scambaiter.sqlite3"
    hf_max_tokens: int = 1500
    auto_interval_seconds: int = 120
    bot_token: str | None = None
    hf_token: str | None = None
    hf_model: str | None = None
    hf_base_url: str | None = None
    hf_memory_model: str | None = None
    hf_memory_max_tokens: int = 150000


def load_config() -> Config:
    return Config(
        analysis_db_path=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"),
        hf_max_tokens=int(os.getenv("HF_MAX_TOKENS", "1500")),
        auto_interval_seconds=int(os.getenv("SCAMBAITER_AUTO_INTERVAL_SECONDS", "120")),
        bot_token=os.getenv("SCAMBAITER_BOT_TOKEN"),
        hf_token=os.getenv("HF_TOKEN"),
        hf_model=os.getenv("HF_MODEL"),
        hf_base_url=os.getenv("HF_BASE_URL", "https://router.huggingface.co/v1"),
        hf_memory_model=os.getenv("HF_MEMORY_MODEL", "openai/gpt-oss-120b"),
        hf_memory_max_tokens=int(os.getenv("HF_MEMORY_MAX_TOKENS", "150000")),
    )
