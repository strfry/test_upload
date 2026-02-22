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
    parser = argparse.ArgumentParser(description="Probe dry-run prompt and HF request.")
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--save-attempt", action="store_true")
    parser.add_argument("--db", default=os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3"))
    args = parser.parse_args()

    config = load_config()
    if args.db:
        config.analysis_db_path = args.db
    store = AnalysisStore(config.analysis_db_path)
    core = ScambaiterCore(config=config, store=store)

    if args.preview_only:
        messages = core.build_model_messages(chat_id=args.chat_id)
        print(json.dumps({"chat_id": args.chat_id, "messages": messages}, ensure_ascii=False, indent=2))
        return

    status = "ok"
    result_text = ""
    response_json: dict[str, object] = {}
    prompt_json: dict[str, object] = {}
    provider = "huggingface_openai_compat"
    model = (config.hf_model or "").strip()
    error_message: str | None = None
    try:
        result = core.run_hf_dry_run(chat_id=args.chat_id)
        result_text = str(result.get("result_text") or "")
        response_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
        prompt_json = result.get("prompt_json") if isinstance(result.get("prompt_json"), dict) else {}
        provider = str(result.get("provider") or provider)
        model = str(result.get("model") or model)
    except Exception as exc:
        status = "error"
        error_message = str(exc)

    if args.save_attempt:
        attempt = store.save_generation_attempt(
            chat_id=args.chat_id,
            provider=provider,
            model=model,
            prompt_json=prompt_json,
            response_json=response_json,
            result_text=result_text,
            status=status,
            error_message=error_message,
        )
        print(f"saved attempt id={attempt.id} status={attempt.status}")

    if status == "ok":
        print(result_text or "<empty-result>")
    else:
        print(f"dry-run failed: {error_message}")


if __name__ == "__main__":
    main()
