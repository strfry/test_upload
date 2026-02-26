#!/usr/bin/env python3
"""Check which photos in a chat have/don't have vision descriptions.

Usage:
  PYTHONPATH=. python scripts/check_vision_backfill.py --chat-id 7608193197
  PYTHONPATH=. python scripts/check_vision_backfill.py --chat-id 7608193197 --missing-only
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scambaiter.storage import AnalysisStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Check vision backfill status for photos in a chat.")
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument(
        "--db",
        default=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"),
        help="Path to analysis database.",
    )
    parser.add_argument("--missing-only", action="store_true", help="Show only photos without descriptions.")
    args = parser.parse_args()

    store = AnalysisStore(args.db)
    events = store.list_events(chat_id=args.chat_id, limit=9999)

    # Filter to photos only
    photos = [e for e in events if getattr(e, "event_type", None) == "photo"]

    if not photos:
        print(f"No photos found in chat {args.chat_id}.")
        return

    # Categorize by backfill status
    backfilled = [e for e in photos if getattr(e, "description", None)]
    missing = [e for e in photos if not getattr(e, "description", None)]

    print(f"Chat {args.chat_id} photo backfill status:")
    print(f"  ✓ Backfilled: {len(backfilled)}")
    print(f"  ✗ Missing:    {len(missing)}")
    print(f"  Total:        {len(photos)}")
    print()

    if args.missing_only:
        if missing:
            print("Photos needing backfill:")
            for e in missing:
                ts = getattr(e, "ts_utc", "?")
                text = getattr(e, "text", None) or "(no caption)"
                event_id = getattr(e, "id", "?")
                print(f"  [{event_id}] {ts} — {text}")
        else:
            print("All photos are backfilled! ✓")
    else:
        if backfilled:
            print("Backfilled photos:")
            for e in backfilled:
                ts = getattr(e, "ts_utc", "?")
                desc = getattr(e, "description", "")[:80]
                event_id = getattr(e, "id", "?")
                print(f"  [{event_id}] {ts}")
                print(f"       {desc}...")
                print()

        if missing:
            print("Photos needing backfill:")
            for e in missing:
                ts = getattr(e, "ts_utc", "?")
                text = getattr(e, "text", None) or "(no caption)"
                event_id = getattr(e, "id", "?")
                print(f"  [{event_id}] {ts} — {text}")


if __name__ == "__main__":
    main()
