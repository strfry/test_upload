#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scambaiter.config import load_config
from scambaiter.core import ScambaiterCore
from scambaiter.forward_meta import baiter_name_from_meta, scammer_name_from_meta
from scambaiter.storage import AnalysisStore


def _format_line(event: object) -> str:
    event_id = getattr(event, "id", "?")
    ts_utc = getattr(event, "ts_utc", None) or "--:--"
    role = getattr(event, "role", "unknown")
    event_type = getattr(event, "event_type", "unknown")
    text = getattr(event, "text", None) or ""
    compact = " ".join(str(text).split())
    if len(compact) > 120:
        compact = compact[:117] + "..."
    return f"{event_id:>6} {ts_utc:<20} {role:<10} {event_type:<14} {compact}"


def _history_summary(store: AnalysisStore, chat_id: int, limit: int) -> None:
    events = store.list_events(chat_id=chat_id, limit=limit)
    print(f"History chat_id={chat_id} events={len(events)}")
    scammer = None
    baiter = None
    for event in events:
        meta = getattr(event, "meta", None)
        if scammer is None:
            scammer = scammer_name_from_meta(meta)
        if baiter is None:
            baiter = baiter_name_from_meta(meta)
        if scammer and baiter:
            break
    print(f"Scammer: {scammer or 'unknown'}")
    print(f"Baiter: {baiter or 'unknown'}")
    print("    id ts_utc               role       event_type      text")
    for event in events:
        print(_format_line(event))


def _build_prompt_payload(core: ScambaiterCore, chat_id: int, max_tokens: int | None) -> dict[str, object]:
    messages = core.build_model_messages(chat_id=chat_id, token_limit=max_tokens)
    memory_state = core.ensure_memory_context(chat_id=chat_id, force_refresh=False)
    payload: dict[str, object] = {
        "messages": messages,
        "summary": memory_state.get("summary") or {},
        "cursor_event_id": int(memory_state.get("cursor_event_id") or 0),
    }
    if isinstance(max_tokens, int):
        payload["max_tokens"] = max_tokens
    return payload


def _build_prompt_payload_with_refresh(
    core: ScambaiterCore, chat_id: int, max_tokens: int | None, refresh_memory: bool
) -> dict[str, object]:
    if refresh_memory:
        memory_state = core.ensure_memory_context(chat_id=chat_id, force_refresh=True)
    else:
        memory_state = core.ensure_memory_context(chat_id=chat_id, force_refresh=False)
    messages = core.build_model_messages(chat_id=chat_id, token_limit=max_tokens)
    payload: dict[str, object] = {
        "messages": messages,
        "summary": memory_state.get("summary") or {},
        "cursor_event_id": int(memory_state.get("cursor_event_id") or 0),
        "memory_updated": bool(memory_state.get("updated")),
    }
    if isinstance(max_tokens, int):
        payload["max_tokens"] = max_tokens
    return payload


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect prompt/context for a chat.")
    parser.add_argument("--db", default=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"))
    parser.add_argument("--chat-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=50, help="Number of history events to show.")
    parser.add_argument("--history", action="store_true", help="Only render the history table.")
    parser.add_argument("--model-view", action="store_true", help="Dump the prompt JSON the model receives.")
    parser.add_argument("--refresh-memory", action="store_true", help="Force rebuild of summary memory before output.")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Simulate HF token budget when trimming prompt events.",
    )
    parser.add_argument("--list-chats", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    store = AnalysisStore(args.db)
    if args.list_chats or args.chat_id is None:
        chat_ids = store.list_chat_ids(limit=500)
        if not chat_ids:
            print("No chats found.")
            return 1
        print("Known chat_ids:")
        for chat_id in chat_ids:
            print(chat_id)
        if args.chat_id is None or not args.history:
            return 0

    chat_id = args.chat_id
    if chat_id is None:
        return 1

    if args.history:
        _history_summary(store, chat_id, args.limit)
        return 0

    config = load_config()
    config.analysis_db_path = args.db
    core = ScambaiterCore(config=config, store=store)
    payload = _build_prompt_payload_with_refresh(
        core,
        chat_id,
        args.max_tokens if args.max_tokens is not None else None,
        refresh_memory=bool(args.refresh_memory),
    )
    if args.model_view:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
