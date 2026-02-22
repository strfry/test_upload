#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scambaiter.storage import AnalysisStore


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List stored chat IDs (same source as /chats).")
    parser.add_argument("--db", default=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"))
    parser.add_argument("--limit", type=int, default=100, help="How many chat ids to list (default 100).")
    return parser.parse_args(argv)


def _format_label(store: AnalysisStore, chat_id: int) -> str:
    profile = store.get_chat_profile(chat_id=chat_id)
    if profile is None:
        return f"/{chat_id}"
    identity = profile.snapshot.get("identity", {})
    display_name = identity.get("display_name")
    username = identity.get("username")
    label_components: list[str] = []
    if isinstance(display_name, str) and display_name.strip():
        label_components.append(display_name.strip())
    if isinstance(username, str) and username.strip():
        label_components.append(f"@{username.strip().lstrip('@')}")
    if label_components:
        return f"/{chat_id} ({' · '.join(label_components)})"
    return f"/{chat_id}"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    store = AnalysisStore(args.db)
    limit = args.limit if args.limit > 0 else 100
    chat_ids = store.list_chat_ids(limit=limit)
    if not chat_ids:
        print("No stored chats.")
        return 0
    print(f"Known chat ids (showing up to {limit}):")
    for chat_id in chat_ids:
        print(f"- {_format_label(store, chat_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
