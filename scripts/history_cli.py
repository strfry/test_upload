#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect stored history by chat_id.")
    parser.add_argument("--db", default=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"))
    parser.add_argument("--chat-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--list-chats", action="store_true")
    args = parser.parse_args()

    store = AnalysisStore(args.db)
    if args.list_chats or args.chat_id is None:
        chat_ids = store.list_chat_ids(limit=500)
        if not chat_ids:
            print("No chats found.")
            return
        print("Known chat_ids:")
        for chat_id in chat_ids:
            print(chat_id)
        if args.chat_id is None:
            return

    events = store.list_events(chat_id=args.chat_id, limit=args.limit)
    print(f"History chat_id={args.chat_id} events={len(events)}")
    print("    id ts_utc               role       event_type      text")
    for event in events:
        print(_format_line(event))


if __name__ == "__main__":
    main()
