#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from typing import Any

from scambaiter.storage import AnalysisStore


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            _flatten(next_prefix, nested, out)
        return
    out[prefix] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Show profile info available from forwarded messages.")
    parser.add_argument("--db", default=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"))
    parser.add_argument("--chat-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    store = AnalysisStore(args.db)
    chat_ids = [args.chat_id] if args.chat_id is not None else store.list_chat_ids(limit=500)
    if not chat_ids:
        print("No chats available.")
        return

    found_any = False
    for chat_id in chat_ids:
        events = store.list_events(chat_id=chat_id, limit=args.limit)
        key_values: dict[str, Any] = {}
        key_counts: dict[str, int] = defaultdict(int)
        for event in events:
            meta = getattr(event, "meta", {})
            if not isinstance(meta, dict):
                continue
            forward_profile = meta.get("forward_profile")
            if not isinstance(forward_profile, dict) or not forward_profile:
                continue
            found_any = True
            flat: dict[str, Any] = {}
            _flatten("", forward_profile, flat)
            for key, value in flat.items():
                key_counts[key] += 1
                key_values.setdefault(key, value)
        if not key_counts:
            continue
        print(f"chat_id={chat_id}")
        for key in sorted(key_counts):
            sample = key_values.get(key)
            print(f"  {key}: seen={key_counts[key]} sample={sample}")
        print()

    if not found_any:
        print("No forward_profile data found yet. Forward some messages first.")


if __name__ == "__main__":
    main()
