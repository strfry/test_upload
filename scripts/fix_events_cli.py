#!/usr/bin/env python3
"""CLI tool to inspect and repair event history in the scambaiter store.

Subcommands:
  list   --chat-id CHAT_ID [--limit N]
  delete --ids ID [ID ...] [--dry-run]
  move   --ids ID [ID ...] --to-chat-id CHAT_ID [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scambaiter.config import load_config
from scambaiter.storage import AnalysisStore


def cmd_list(store: AnalysisStore, args: argparse.Namespace) -> None:
    events = store.list_events(args.chat_id, limit=args.limit)
    if not events:
        print(f"No events for chat_id={args.chat_id}")
        return
    print(f"{'ID':>6}  {'TS':>19}  {'ROLE':>10}  {'TYPE':>8}  TEXT")
    print("-" * 80)
    for e in events:
        ts = (e.ts_utc or "")[:19]
        text_preview = (e.text or "")[:40].replace("\n", "↵")
        print(f"{e.id:>6}  {ts:>19}  {e.role:>10}  {e.event_type:>8}  {text_preview}")
    print(f"\nTotal: {len(events)} event(s)")


def cmd_delete(store: AnalysisStore, args: argparse.Namespace) -> None:
    ids: list[int] = args.ids
    if args.dry_run:
        print(f"[dry-run] Would delete {len(ids)} event(s): {ids}")
        return
    count = store.delete_events_by_ids(ids)
    print(f"Deleted {count} event(s).")


def cmd_move(store: AnalysisStore, args: argparse.Namespace) -> None:
    ids: list[int] = args.ids
    if args.dry_run:
        print(f"[dry-run] Would move {len(ids)} event(s) to chat_id={args.to_chat_id}: {ids}")
        return
    count = store.move_events_to_chat(ids, args.to_chat_id)
    print(f"Moved {count} event(s) to chat_id={args.to_chat_id}.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect and repair event history in the scambaiter store."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List events for a chat")
    p_list.add_argument("--chat-id", type=int, required=True, metavar="CHAT_ID")
    p_list.add_argument("--limit", type=int, default=50, metavar="N")

    p_del = sub.add_parser("delete", help="Delete specific events by ID")
    p_del.add_argument("--ids", type=int, nargs="+", required=True, metavar="ID")
    p_del.add_argument("--dry-run", action="store_true")

    p_move = sub.add_parser("move", help="Reassign events to a different chat_id")
    p_move.add_argument("--ids", type=int, nargs="+", required=True, metavar="ID")
    p_move.add_argument("--to-chat-id", type=int, required=True, metavar="CHAT_ID")
    p_move.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    config = load_config()
    store = AnalysisStore(config.analysis_db_path)

    if args.cmd == "list":
        cmd_list(store, args)
    elif args.cmd == "delete":
        cmd_delete(store, args)
    elif args.cmd == "move":
        cmd_move(store, args)


if __name__ == "__main__":
    main()
