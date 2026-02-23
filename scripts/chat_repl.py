#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scambaiter.config import Config, load_config
from scambaiter.core import ScambaiterCore, parse_structured_model_output_detailed
from scambaiter.model_client import call_hf_openai_chat, extract_result_text
from scambaiter.storage import AnalysisStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_model_config(config: Config) -> tuple[str, str]:
    token = (config.hf_token or "").strip()
    model = (config.hf_model or "").strip()
    if not token or not model:
        raise RuntimeError("HF_TOKEN and HF_MODEL must be configured (env or config file).")
    return token, model


def _build_prompt_payload(
    core: ScambaiterCore,
    chat_id: int,
    max_tokens: int | None,
    include_memory: bool,
) -> tuple[list[dict[str, str]], int]:
    messages = core.build_model_messages(
        chat_id=chat_id,
        token_limit=max_tokens if max_tokens is not None else None,
        include_memory=include_memory,
    )
    prompt_budget = max_tokens if max_tokens is not None else core.config.hf_max_tokens
    return messages, prompt_budget


def _dump_prompt(path: str, messages: list[dict[str, str]], max_tokens: int) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"max_tokens": max_tokens, "messages": messages}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_issues(issues: list[dict[str, Any]]) -> str:
    entries = []
    for issue in issues:
        path = issue.get("path")
        reason = issue.get("reason")
        expected = issue.get("expected")
        actual = issue.get("actual")
        parts = [f"{path}: {reason}"]
        if expected:
            parts.append(f"expected={expected}")
        if actual:
            parts.append(f"actual={actual}")
        entries.append(" ".join(parts))
    return "; ".join(entries)


def _run_loop(
    *,
    core: ScambaiterCore,
    store: AnalysisStore,
    chat_id: int,
    hf_token: str,
    hf_model: str,
    prompt_path: str | None,
    max_tokens: int | None,
    include_memory: bool,
) -> int:
    while True:
        try:
            raw_line = input("Scammer> ")
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print("\nExiting.")
            break
        text = raw_line.strip()
        if not text:
            continue
        timestamp = _now_iso()
        store.ingest_event(
            chat_id=chat_id,
            event_type="message",
            role="scammer",
            text=text,
            ts_utc=timestamp,
        )
        try:
            messages, prompt_budget = _build_prompt_payload(core, chat_id, max_tokens, include_memory)
            if prompt_path:
                _dump_prompt(prompt_path, messages, prompt_budget)
            response = call_hf_openai_chat(
                token=hf_token,
                model=hf_model,
                messages=messages,
                max_tokens=prompt_budget,
                base_url=core.config.hf_base_url,
            )
        except Exception as exc:
            print(f"Model request failed: {exc}", file=sys.stderr)
            continue
        result_text = extract_result_text(response)
        parsed_result = parse_structured_model_output_detailed(result_text)
        model_output = parsed_result.output
        reply_text = ""
        if model_output:
            reply_text = model_output.suggestion
        else:
            reply_text = result_text.strip() if isinstance(result_text, str) else ""
            if parsed_result.issues:
                print(f"Contract issues: {_format_issues([issue.as_dict() for issue in parsed_result.issues])}", file=sys.stderr)
        reply_text_display = reply_text or "(no reply)"
        print(f"ScamBaiter> {reply_text_display}")
        if reply_text:
            store.ingest_event(
                chat_id=chat_id,
                event_type="message",
                role="scambaiter",
                text=reply_text,
                ts_utc=_now_iso(),
            )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive ScamBaiter conversation loop.")
    parser.add_argument(
        "--db",
        type=str,
        default=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", ":memory:"),
        help="Path to the analysis SQLite DB (default: ephemeral :memory:).",
    )
    parser.add_argument("--chat-id", type=int, help="Chat ID to seed (default: synthetic negative value).")
    parser.add_argument("--max-tokens", type=int, help="Token budget for each generation round.")
    parser.add_argument("--include-memory", action="store_true", help="Include cached memory summaries in the prompt.")
    parser.add_argument("--prompt-path", type=str, help="When set, dump the generated prompt JSON to this file each round.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config = load_config()
    try:
        hf_token, hf_model = _require_model_config(config)
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    db_path = args.db
    store = AnalysisStore(db_path)
    core = ScambaiterCore(config=config, store=store)
    chat_id = args.chat_id if args.chat_id is not None else -1
    return _run_loop(
        core=core,
        store=store,
        chat_id=chat_id,
        hf_token=hf_token,
        hf_model=hf_model,
        prompt_path=args.prompt_path,
        max_tokens=args.max_tokens,
        include_memory=args.include_memory,
    )


if __name__ == "__main__":
    raise SystemExit(main())
