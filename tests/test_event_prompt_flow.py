from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scambaiter.core import ScambaiterCore
from scambaiter.storage import AnalysisStore


class EventAndPromptFlowTest(unittest.TestCase):
    def test_user_forward_keeps_event_type_and_sets_manual_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            first = store.ingest_user_forward(
                chat_id=1001,
                event_type="message",
                text="Forwarded scammer text",
                source_message_id="tg:1001:777",
            )
            second = store.ingest_user_forward(
                chat_id=1001,
                event_type="message",
                text="Forwarded scammer text duplicate",
                source_message_id="tg:1001:777",
            )

            self.assertEqual("message", first.event_type)
            self.assertEqual("manual", first.role)
            self.assertEqual(first.id, second.id)

    def test_prompt_builder_uses_hhmm_and_trims_oldest_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            config = SimpleNamespace(hf_max_tokens=20)
            core = ScambaiterCore(config=config, store=store)

            store.ingest_event(
                chat_id=2001,
                event_type="message",
                role="scammer",
                text="old message that should be dropped first",
                ts_utc="2026-02-21T14:01:00Z",
            )
            store.ingest_event(
                chat_id=2001,
                event_type="message",
                role="manual",
                text="newest short",
                ts_utc="2026-02-21T14:02:00Z",
            )

            prompt_events = core.build_prompt_events(chat_id=2001)

            self.assertTrue(prompt_events)
            self.assertEqual("14:02", prompt_events[-1]["time"])
            self.assertEqual("newest short", prompt_events[-1]["text"])
            self.assertFalse(any(item.get("text") == "old message that should be dropped first" for item in prompt_events))


if __name__ == "__main__":
    unittest.main()
