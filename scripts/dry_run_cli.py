#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scambaiter.config import load_config
from scambaiter.core import ScambaiterCore
from scambaiter.storage import AnalysisStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a read-only HF dry-run for one chat.")
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--db", default=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"))
    args = parser.parse_args()

    config = load_config()
    if args.db:
        config.analysis_db_path = args.db
    store = AnalysisStore(config.analysis_db_path)
    core = ScambaiterCore(config=config, store=store)

    status = "ok"
    payload: dict[str, object] = {"chat_id": int(args.chat_id)}
    try:
        result = core.run_hf_dry_run(chat_id=args.chat_id)
        payload.update(result)
        valid_output = bool(result.get("valid_output"))
        error_message = str(result.get("error_message") or "").strip()
        status = "ok" if valid_output and not error_message else "error"
    except Exception as exc:
        status = "error"
        payload = {
            "chat_id": int(args.chat_id),
            "status": "error",
            "error_message": str(exc),
        }

    if status == "ok":
        payload["status"] = "ok"
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
