from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scambaiter.core import ScambaiterCore
from scambaiter.storage import AnalysisStore


class EventAndPromptFlowTest(unittest.TestCase):
    @staticmethod
    def _memory_response(content: str) -> dict:
        return {"choices": [{"message": {"content": content}}]}

    @staticmethod
    def _valid_memory_summary_json(tag: str) -> str:
        payload = {
            "schema": "scambait.memory.v1",
            "claimed_identity": {"name": tag, "role_claim": "investor", "confidence": "medium"},
            "narrative": {"phase": "pitch", "short_story": "story", "timeline_points": ["t1"]},
            "current_intent": {"scammer_intent": "extract funds", "baiter_intent": "delay", "latest_topic": tag},
            "key_facts": {"k": "v"},
            "risk_flags": ["rf"],
            "open_questions": ["q1"],
            "next_focus": ["n1"],
        }
        import json

        return json.dumps(payload, ensure_ascii=True)

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

    def test_build_model_messages_contains_memory_summary_system_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            config = SimpleNamespace(hf_max_tokens=100, hf_token="")
            core = ScambaiterCore(config=config, store=store)
            store.ingest_event(chat_id=2010, event_type="message", role="scammer", text="hello")

            messages = core.build_model_messages(chat_id=2010)

            self.assertGreaterEqual(len(messages), 3)
            self.assertEqual("system", messages[0]["role"])
            self.assertEqual("system", messages[1]["role"])
            self.assertIn("Memory summary for chat_id=2010", messages[1]["content"])
            memory = store.get_summary(chat_id=2010)
            self.assertIsNotNone(memory)

    def test_memory_context_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            saved = store.upsert_summary(
                chat_id=2020,
                summary={"schema": "scambait.memory.v1", "current_intent": {}},
                cursor_event_id=17,
                model="openai/gpt-oss-120b",
            )
            self.assertEqual(2020, saved.chat_id)
            loaded = store.get_summary(chat_id=2020)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(17, loaded.cursor_event_id)
            self.assertEqual("openai/gpt-oss-120b", loaded.model)
            self.assertEqual("scambait.memory.v1", loaded.summary.get("schema"))

    def test_ensure_memory_context_skips_model_when_cursor_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.ingest_event(chat_id=2050, event_type="message", role="scammer", text="hello")
            store.upsert_summary(
                chat_id=2050,
                summary={"schema": "scambait.memory.v1", "current_intent": {}},
                cursor_event_id=1,
                model="openai/gpt-oss-120b",
            )
            config = SimpleNamespace(hf_token="token", hf_memory_model="openai/gpt-oss-120b", hf_memory_max_tokens=150000)
            core = ScambaiterCore(config=config, store=store)

            with patch("scambaiter.core.call_hf_openai_chat") as mocked_call:
                state = core.ensure_memory_context(chat_id=2050, force_refresh=False)

            mocked_call.assert_not_called()
            self.assertFalse(bool(state.get("updated")))
            self.assertEqual(1, int(state.get("cursor_event_id") or 0))

    def test_ensure_memory_context_uses_existing_cursor_for_incremental_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.ingest_event(chat_id=2060, event_type="message", role="scammer", text="first")
            store.upsert_summary(
                chat_id=2060,
                summary={"schema": "scambait.memory.v1", "current_intent": {"latest_topic": "first"}},
                cursor_event_id=1,
                model="openai/gpt-oss-120b",
            )
            store.ingest_event(chat_id=2060, event_type="message", role="manual", text="second")
            config = SimpleNamespace(hf_token="token", hf_memory_model="openai/gpt-oss-120b", hf_memory_max_tokens=150000)
            core = ScambaiterCore(config=config, store=store)

            captured: dict[str, object] = {}

            def _fake_call(**kwargs: object) -> dict:
                messages = kwargs.get("messages")
                assert isinstance(messages, list)
                user_payload_raw = messages[1]["content"]
                import json

                payload = json.loads(user_payload_raw)
                captured["payload"] = payload
                return self._memory_response(self._valid_memory_summary_json("second"))

            with patch("scambaiter.core.call_hf_openai_chat", side_effect=_fake_call):
                state = core.ensure_memory_context(chat_id=2060, force_refresh=False)

            payload = captured.get("payload")
            self.assertIsInstance(payload, dict)
            assert isinstance(payload, dict)
            self.assertEqual(1, int(payload.get("memory_cursor_event_id") or 0))
            events = payload.get("events")
            self.assertIsInstance(events, list)
            assert isinstance(events, list)
            self.assertEqual(1, len(events))
            self.assertEqual("second", events[0].get("text"))
            self.assertEqual(2, int(state.get("cursor_event_id") or 0))
            self.assertTrue(bool(state.get("updated")))

    def test_ensure_memory_context_falls_back_when_summary_call_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.ingest_event(chat_id=2100, event_type="message", role="scammer", text="first")
            saved = store.upsert_summary(
                chat_id=2100,
                summary={"schema": "scambait.memory.v1", "current_intent": {"latest_topic": "first"}},
                cursor_event_id=1,
                model="openai/gpt-oss-120b",
            )
            store.ingest_event(chat_id=2100, event_type="message", role="manual", text="second")
            config = SimpleNamespace(hf_token="token", hf_memory_model="openai/gpt-oss-120b", hf_memory_max_tokens=150000)
            core = ScambaiterCore(config=config, store=store)

            with patch("scambaiter.core.call_hf_openai_chat", side_effect=RuntimeError("overloaded")):
                state = core.ensure_memory_context(chat_id=2100, force_refresh=False)

            self.assertFalse(bool(state.get("updated")))
            self.assertEqual(saved.cursor_event_id, state.get("cursor_event_id"))
            self.assertEqual(saved.summary, state.get("summary"))

    def test_ensure_memory_context_falls_back_to_empty_summary_when_none_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.ingest_event(chat_id=2110, event_type="message", role="scammer", text="alpha")
            config = SimpleNamespace(hf_token="token", hf_memory_model="openai/gpt-oss-120b", hf_memory_max_tokens=150000)
            core = ScambaiterCore(config=config, store=store)

            with patch("scambaiter.core.call_hf_openai_chat", side_effect=RuntimeError("overloaded")):
                state = core.ensure_memory_context(chat_id=2110, force_refresh=False)

            self.assertTrue(bool(state.get("updated")))
            summary = state.get("summary")
            self.assertIsInstance(summary, dict)
            self.assertEqual("scambait.memory.v1", summary.get("schema"))

    def test_clear_chat_history_deletes_events_for_target_chat_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            store.ingest_event(chat_id=3001, event_type="message", role="manual", text="a")
            store.ingest_event(chat_id=3001, event_type="message", role="scammer", text="b")
            store.ingest_event(chat_id=3002, event_type="message", role="manual", text="other")

            deleted = store.clear_chat_history(chat_id=3001)

            self.assertEqual(2, deleted)
            self.assertEqual(0, len(store.list_events(chat_id=3001, limit=10)))
            self.assertEqual(1, len(store.list_events(chat_id=3002, limit=10)))

    def test_clear_chat_context_deletes_all_context_tables_for_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            # Seed full context for target chat.
            store.ingest_event(chat_id=4001, event_type="message", role="manual", text="e1")
            store.save(
                chat_id=4001,
                title="t",
                suggestion="s",
                analysis={"a": 1},
                actions=[{"type": "send_message"}],
                metadata={"schema": "scambait.llm.v1"},
            )
            store.add_directive(chat_id=4001, text="d1", scope="chat")
            store.save_generation_attempt(
                chat_id=4001,
                provider="hf",
                model="m",
                prompt_json={"messages": []},
                response_json={},
                result_text="{}",
                status="ok",
            )
            store.upsert_chat_profile(
                chat_id=4001,
                patch={"identity": {"display_name": "X"}},
                source="botapi_forward",
                changed_at="2026-02-21T20:20:00Z",
            )
            store.upsert_summary(
                chat_id=4001,
                summary={"schema": "scambait.memory.v1"},
                cursor_event_id=1,
                model="openai/gpt-oss-120b",
            )

            # Seed another chat to ensure isolation.
            store.ingest_event(chat_id=4002, event_type="message", role="manual", text="other")

            deleted = store.clear_chat_context(chat_id=4001)

            self.assertGreaterEqual(deleted.get("events", 0), 1)
            self.assertGreaterEqual(deleted.get("analyses", 0), 1)
            self.assertGreaterEqual(deleted.get("directives", 0), 1)
            self.assertGreaterEqual(deleted.get("generation_attempts", 0), 1)
            self.assertGreaterEqual(deleted.get("profile_changes", 0), 1)
            self.assertGreaterEqual(deleted.get("chat_profile", 0), 1)
            self.assertGreaterEqual(deleted.get("summary", 0), 1)
            self.assertGreaterEqual(deleted.get("total", 0), 7)

            self.assertEqual(0, len(store.list_events(chat_id=4001, limit=10)))
            self.assertIsNone(store.latest_for_chat(chat_id=4001))
            self.assertEqual(0, len(store.list_directives(chat_id=4001, active_only=False, limit=10)))
            self.assertEqual(0, len(store.list_generation_attempts(chat_id=4001, limit=10)))
            self.assertIsNone(store.get_chat_profile(chat_id=4001))
            self.assertIsNone(store.get_summary(chat_id=4001))

            self.assertEqual(1, len(store.list_events(chat_id=4002, limit=10)))


if __name__ == "__main__":
    unittest.main()
