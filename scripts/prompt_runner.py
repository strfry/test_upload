#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scambaiter.config import AppConfig, load_config
from scambaiter.core import (
    SYSTEM_PROMPT,
    ChatContext,
    ChatMessage,
    ScambaiterCore,
    parse_structured_model_output,
)
from scambaiter.service import BackgroundService
from scambaiter.storage import AnalysisStore


@dataclass
class RunnerInput:
    context: ChatContext
    prompt_context: dict[str, object] | None
    language_hint: str | None


def _parse_iso_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_optional_message_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    while len(text) >= 2 and ((text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'")):
        text = text[1:-1].strip()
    if not text or not text.isdigit():
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _load_input_file(path: Path) -> RunnerInput:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Input JSON muss ein Objekt sein.")

    chat_id = int(raw.get("chat_id", 0))
    title = str(raw.get("title", "")).strip() or str(chat_id)
    messages_raw = raw.get("messages")
    if not isinstance(messages_raw, list) or not messages_raw:
        raise ValueError("Input JSON braucht ein nicht-leeres Array: messages")

    messages: list[ChatMessage] = []
    for idx, item in enumerate(messages_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"messages[{idx}] muss ein Objekt sein.")
        ts_raw = item.get("ts_utc")
        role_raw = str(item.get("role", "")).strip().lower()
        sender = str(item.get("sender", "")).strip() or ("self" if role_raw == "assistant" else "user")
        text = str(item.get("text", ""))
        message_id = _parse_optional_message_id(item.get("message_id"))
        reply_to = _parse_optional_message_id(item.get("reply_to"))
        if not ts_raw or not isinstance(ts_raw, str):
            raise ValueError(f"messages[{idx}].ts_utc fehlt oder ist ungültig.")
        if role_raw not in {"assistant", "user"}:
            raise ValueError(f"messages[{idx}].role muss assistant|user sein.")
        messages.append(
            ChatMessage(
                timestamp=_parse_iso_utc(ts_raw),
                sender=sender,
                role=role_raw,  # type: ignore[arg-type]
                text=text,
                message_id=message_id,
                reply_to=reply_to,
            )
        )

    prompt_context = raw.get("prompt_context")
    if prompt_context is not None and not isinstance(prompt_context, dict):
        raise ValueError("prompt_context muss ein Objekt sein.")
    language_hint = raw.get("language_hint")
    if language_hint is not None and not isinstance(language_hint, str):
        raise ValueError("language_hint muss ein String sein.")

    return RunnerInput(
        context=ChatContext(chat_id=chat_id, title=title, messages=messages),
        prompt_context=prompt_context,
        language_hint=language_hint,
    )


async def _build_runner_input(
    core: ScambaiterCore,
    service: BackgroundService,
    chat_id: int | None,
    input_json_path: str | None,
    language_hint_override: str | None,
) -> RunnerInput:
    if input_json_path:
        loaded = _load_input_file(Path(input_json_path))
        if language_hint_override:
            loaded.language_hint = language_hint_override
        return loaded

    if chat_id is None:
        raise ValueError("Bitte entweder --chat-id oder --input-json angeben.")

    context = await core.build_chat_context(chat_id)
    if not context:
        raise ValueError(f"Kein Chatkontext gefunden für chat_id={chat_id}")
    prompt_context, language_hint, _previous = service.build_prompt_context_for_chat(chat_id)
    if language_hint_override:
        language_hint = language_hint_override
    return RunnerInput(context=context, prompt_context=prompt_context, language_hint=language_hint)


def _build_model_messages_without_core(
    context: ChatContext,
    prompt_context: dict[str, object] | None,
    language_hint: str | None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    language_system_prompt = ScambaiterCore.build_language_system_prompt(language_hint)
    if language_system_prompt:
        messages.append({"role": "system", "content": language_system_prompt})
    messages.append(
        {
            "role": "system",
            "content": (
                f"Konversation mit {context.title} (Telegram Chat-ID: {context.chat_id}). "
                "Die folgenden Nachrichten sind chronologisch sortiert."
            ),
        }
    )
    if prompt_context:
        context_json = json.dumps(prompt_context, ensure_ascii=False, indent=2)
        messages.append(
            {
                "role": "system",
                "content": "Strukturierter System-Kontext (nur intern, nicht wortwörtlich zitieren):\n" + context_json,
            }
        )
    for item in context.messages:
        ts_utc = item.timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        payload = {
            "ts_utc": ts_utc,
            "role": item.role,
            "sender": ("self" if item.role == "assistant" else item.sender),
            "text": item.text,
        }
        if item.message_id is not None:
            payload["message_id"] = item.message_id
        if item.reply_to is not None:
            payload["reply_to"] = item.reply_to
        messages.append({"role": item.role, "content": json.dumps(payload, ensure_ascii=False)})
    return messages


def _dump_request_json(path: str, model: str, max_tokens: int, messages: list[dict[str, str]]) -> None:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _print_curl_hint(request_path: str, base_url: str | None) -> None:
    api_url = (base_url or "https://router.huggingface.co/v1").rstrip("/") + "/chat/completions"
    print("=== CURL ===")
    print(
        "curl -sS \\"
        "\n  -H \"Authorization: Bearer $HF_TOKEN\" \\"
        f"\n  -H \"Content-Type: application/json\" \\"
        f"\n  \"{api_url}\" \\"
        f"\n  -d @{request_path}"
    )
    print()


async def _run(args: argparse.Namespace) -> int:
    if args.input_json and args.preview_only:
        loaded = _load_input_file(Path(args.input_json))
        if args.language_hint:
            loaded.language_hint = args.language_hint
        model_messages = _build_model_messages_without_core(
            context=loaded.context,
            prompt_context=loaded.prompt_context,
            language_hint=loaded.language_hint,
        )
        if args.show_prompt:
            print("=== PROMPT (model_messages) ===")
            print(json.dumps(model_messages, ensure_ascii=False, indent=2))
            print()
        if args.dump_request_json:
            model_name = (os.getenv("HF_MODEL") or "").strip() or "REPLACE_WITH_MODEL"
            max_tokens_raw = (os.getenv("HF_MAX_TOKENS") or "1000").strip()
            try:
                max_tokens = int(max_tokens_raw)
            except ValueError:
                max_tokens = 1000
            _dump_request_json(
                path=args.dump_request_json,
                model=model_name,
                max_tokens=max_tokens,
                messages=model_messages,
            )
            print(f"Request geschrieben: {args.dump_request_json}")
            if args.print_curl:
                _print_curl_hint(args.dump_request_json, os.getenv("HF_BASE_URL"))
        elif args.print_curl:
            print("Hinweis: --print-curl ist nur mit --dump-request-json sinnvoll.")
        return 0

    try:
        config = load_config()
    except Exception:
        if args.input_json:
            hf_token = (os.getenv("HF_TOKEN") or "").strip()
            hf_model = (os.getenv("HF_MODEL") or "").strip()
            hf_vision_model = (os.getenv("HF_VISION_MODEL") or hf_model).strip()
            hf_base_url = (os.getenv("HF_BASE_URL") or "").strip() or None
            hf_max_tokens_raw = (os.getenv("HF_MAX_TOKENS") or "1000").strip()
            try:
                hf_max_tokens = int(hf_max_tokens_raw)
            except ValueError:
                hf_max_tokens = 1000
            if not args.preview_only and (not hf_token or not hf_model):
                raise ValueError(
                    "Für --input-json ohne --preview-only werden HF_TOKEN und HF_MODEL benötigt."
                )
            config = AppConfig(
                telegram_api_id=0,
                telegram_api_hash="",
                telegram_session="scambaiter",
                hf_token=hf_token,
                hf_model=hf_model,
                hf_vision_model=hf_vision_model,
                hf_base_url=hf_base_url,
                hf_max_tokens=hf_max_tokens,
                folder_name="Scammers",
                history_limit=20,
                send_enabled=False,
                delete_after_seconds=0,
                interactive_enabled=False,
                debug_enabled=False,
                bot_token=None,
                auto_interval_seconds=120,
                analysis_db_path="scambaiter.sqlite3",
            )
        else:
            raise
    store = AnalysisStore(config.analysis_db_path)
    core = ScambaiterCore(config, store=store)
    service: BackgroundService | None = None
    needs_telegram = bool(args.chat_id)
    if needs_telegram:
        service = BackgroundService(core, interval_seconds=config.auto_interval_seconds, store=store)
        await core.start()
    try:
        runner_input = await _build_runner_input(
            core=core,
            service=service or BackgroundService(core, interval_seconds=config.auto_interval_seconds, store=store),
            chat_id=args.chat_id,
            input_json_path=args.input_json,
            language_hint_override=args.language_hint,
        )
        model_messages = core.build_model_messages(
            context=runner_input.context,
            language_hint=runner_input.language_hint,
            prompt_context=runner_input.prompt_context,
        )
        if args.show_prompt:
            print("=== PROMPT (model_messages) ===")
            print(json.dumps(model_messages, ensure_ascii=False, indent=2))
            print()
        if args.dump_request_json:
            _dump_request_json(
                path=args.dump_request_json,
                model=config.hf_model,
                max_tokens=config.hf_max_tokens,
                messages=model_messages,
            )
            print(f"Request geschrieben: {args.dump_request_json}")
            if args.print_curl:
                _print_curl_hint(args.dump_request_json, config.hf_base_url)
        elif args.print_curl:
            print("Hinweis: --print-curl ist nur mit --dump-request-json sinnvoll.")

        if args.preview_only:
            return 0

        output = core.generate_output(
            runner_input.context,
            language_hint=runner_input.language_hint,
            prompt_context=runner_input.prompt_context,
        )
        parsed = parse_structured_model_output(output.raw)

        print("=== RAW ===")
        print(output.raw)
        print()
        print("=== PARSED ===")
        print("valid_json:", "yes" if parsed else "no")
        print("suggestion:", output.suggestion)
        print("actions:", json.dumps(output.actions, ensure_ascii=False))
        print("analysis:", json.dumps(output.analysis or {}, ensure_ascii=False))
        print("metadata:", json.dumps(output.metadata, ensure_ascii=False))
        return 0
    finally:
        if needs_telegram:
            await core.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lokaler Prompt-Runner für Scambaiter.")
    parser.add_argument("--chat-id", type=int, help="Telegram Chat-ID für Live-Kontext.")
    parser.add_argument(
        "--input-json",
        type=str,
        help="Pfad zu JSON-Datei mit context/messages und optional prompt_context/language_hint.",
    )
    parser.add_argument(
        "--language-hint",
        type=str,
        help="Optionaler Override für language_hint (z.B. de/en).",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Gibt den vollständigen model_messages-Input aus.",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Nur Prompt anzeigen, keinen Modell-Request ausführen.",
    )
    parser.add_argument(
        "--dump-request-json",
        type=str,
        help="Schreibt den vollständigen HF-Request-Body als JSON-Datei.",
    )
    parser.add_argument(
        "--print-curl",
        action="store_true",
        help="Gibt ein curl-Beispiel aus, das den exportierten Request ausführt.",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not args.chat_id and not args.input_json:
        parser.error("Bitte --chat-id oder --input-json angeben.")
    if args.chat_id and args.input_json:
        parser.error("Bitte genau eine Quelle wählen: --chat-id ODER --input-json.")
    exit_code = asyncio.run(_run(args))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
