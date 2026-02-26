from __future__ import annotations

import base64
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
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
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

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice

    try:
        response = client.chat.completions.create(**kwargs)
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


def extract_tool_calls(response_json: dict[str, Any]) -> list[dict[str, Any]]:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    first = choices[0]
    if not isinstance(first, dict):
        return []
    message = first.get("message")
    if not isinstance(message, dict):
        return []
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    result: list[dict[str, Any]] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            result.append(tc)
    return result


def extract_reasoning_details(response_json: dict[str, Any]) -> tuple[int, str]:
    def _text_from_reasoning(reasoning: object) -> str | None:
        if isinstance(reasoning, str):
            normalized = reasoning.strip()
            return normalized or None
        if isinstance(reasoning, dict):
            for key in ("content", "text", "reasoning"):
                value = reasoning.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            # There might be nested structures; try flattening to JSON.
            try:
                return json.dumps(reasoning, ensure_ascii=True)
            except Exception:
                return None
        if isinstance(reasoning, list):
            parts: list[str] = []
            for item in reasoning:
                text_part = _text_from_reasoning(item)
                if text_part:
                    parts.append(text_part)
            return "\n".join(parts) if parts else None
        return None

    cycles = 0
    snippet: str = ""
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return 0, ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        reasoning = message.get("reasoning")
        if reasoning is None:
            continue
        text = _text_from_reasoning(reasoning)
        if text:
            cycles += 1
            if not snippet:
                snippet = text
        else:
            cycles += 1
    if snippet and len(snippet) > 1000:
        snippet = snippet[: 997] + "..."
    return cycles, snippet


def call_hf_vision(
    *,
    token: str,
    model: str,
    image_bytes: bytes,
    prompt: str,
    base_url: str | None = None,
    max_tokens: int = 800,
    timeout_seconds: float = 60.0,
) -> str:
    """Call a vision/multimodal model to describe an image.

    Args:
        token: HuggingFace API token
        model: Model identifier (e.g., "Qwen/Qwen2-VL-7B-Instruct")
        image_bytes: Raw image data
        prompt: Text prompt describing what to extract from the image
        base_url: Optional custom base URL (defaults to HuggingFace router)
        max_tokens: Max tokens in response
        timeout_seconds: Request timeout

    Returns:
        The vision model's description of the image as a string.

    Raises:
        RuntimeError: If openai package is missing or API call fails.
    """
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - dependency edge
        raise RuntimeError("openai package missing. Install with: pip install openai") from exc

    client = OpenAI(
        api_key=token,
        base_url=(base_url or "https://router.huggingface.co/v1").rstrip("/"),
        timeout=timeout_seconds,
    )

    # Encode image to base64 for inline embedding
    b64 = base64.b64encode(image_bytes).decode("ascii")

    # Build multimodal message with image + text
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }
    ]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # pragma: no cover - network/provider edge
        raise RuntimeError(str(exc)) from exc

    # Extract and return the response text directly
    if response.choices and len(response.choices) > 0:
        choice = response.choices[0]
        if hasattr(choice, "message") and hasattr(choice.message, "content"):
            return str(choice.message.content).strip()
    return ""
