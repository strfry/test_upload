from __future__ import annotations

import json
from typing import Any


def call_hf_openai_chat(
    *,
    token: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    base_url: str | None = None,
    timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - dependency edge
        raise RuntimeError("openai package missing. Install with: pip install openai") from exc

    client = OpenAI(
        api_key=token,
        base_url=(base_url or "https://router.huggingface.co/v1").rstrip("/"),
        timeout=timeout_seconds,
    )
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=messages,
        )
    except Exception as exc:  # pragma: no cover - network/provider edge
        raise RuntimeError(str(exc)) from exc
    value = json.loads(response.model_dump_json())
    return value if isinstance(value, dict) else {}


def extract_result_text(response_json: dict[str, Any]) -> str:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""
