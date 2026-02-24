from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scambaiter.core import (
    ParseResult,
    parse_tool_calls_to_model_output,
)
from scambaiter.storage import AnalysisStore


def _make_act_call(actions: list[dict]) -> list[dict]:
    return [{"function": {"name": "act", "arguments": json.dumps({"actions": actions})}}]


class ParseToolCallsTest(unittest.TestCase):
    def test_send_message_produces_correct_action_and_suggestion(self) -> None:
        tool_calls = _make_act_call([{"type": "send_message", "text": "Hallo!"}])
        result, memory_pairs = parse_tool_calls_to_model_output(tool_calls)
        self.assertIsNotNone(result.output)
        assert result.output is not None
        self.assertEqual("Hallo!", result.output.suggestion)
        self.assertEqual([{"type": "send_message", "message": {"text": "Hallo!"}}], result.output.actions)
        self.assertEqual([], memory_pairs)

    def test_wait_short_latency(self) -> None:
        tool_calls = _make_act_call([{"type": "wait", "latency_class": "short"}])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        self.assertIsNotNone(result.output)
        assert result.output is not None
        self.assertEqual([{"type": "wait", "value": 30, "unit": "seconds"}], result.output.actions)

    def test_wait_medium_latency(self) -> None:
        tool_calls = _make_act_call([{"type": "wait", "latency_class": "medium"}])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        self.assertEqual([{"type": "wait", "value": 3, "unit": "minutes"}], result.output.actions)

    def test_wait_long_latency(self) -> None:
        tool_calls = _make_act_call([{"type": "wait", "latency_class": "long"}])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        self.assertEqual([{"type": "wait", "value": 15, "unit": "minutes"}], result.output.actions)

    def test_set_memory_echoed_in_analysis_and_returned_in_pairs(self) -> None:
        tool_calls = _make_act_call([
            {"type": "set_memory", "key": "phase", "value": "trust_building"},
            {"type": "send_message", "text": "Interessant!"},
        ])
        result, memory_pairs = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        self.assertEqual("trust_building", result.output.analysis.get("phase"))
        self.assertIn(("phase", "trust_building"), memory_pairs)

    def test_add_note_appended_to_analysis_notes(self) -> None:
        tool_calls = _make_act_call([
            {"type": "add_note", "text": "Scammer is using urgency tactics"},
            {"type": "send_message", "text": "Ja, das klingt gut."},
        ])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        notes = result.output.analysis.get("notes")
        self.assertIsInstance(notes, list)
        self.assertIn("Scammer is using urgency tactics", notes)

    def test_set_memory_and_add_note_only_produces_noop(self) -> None:
        tool_calls = _make_act_call([
            {"type": "set_memory", "key": "status", "value": "active"},
            {"type": "add_note", "text": "No message needed this turn."},
        ])
        result, memory_pairs = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        self.assertEqual([{"type": "noop"}], result.output.actions)
        self.assertEqual("", result.output.suggestion)
        self.assertIn(("status", "active"), memory_pairs)
        self.assertIn("No message needed this turn.", result.output.analysis.get("notes", []))

    def test_decide_handoff_produces_escalate_action(self) -> None:
        tool_calls = _make_act_call([{"type": "decide_handoff", "reason": "Scammer asked for real money transfer"}])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        self.assertEqual(
            [{"type": "escalate_to_human", "reason": "Scammer asked for real money transfer"}],
            result.output.actions,
        )

    def test_empty_tool_calls_returns_none_output(self) -> None:
        result, memory_pairs = parse_tool_calls_to_model_output([])
        self.assertIsNone(result.output)
        self.assertTrue(result.issues)
        self.assertEqual("tool_calls", result.issues[0].path)
        self.assertEqual([], memory_pairs)

    def test_duplicate_send_message_second_is_skipped(self) -> None:
        tool_calls = _make_act_call([
            {"type": "send_message", "text": "First message"},
            {"type": "send_message", "text": "Second message"},
        ])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        send_actions = [a for a in result.output.actions if a.get("type") == "send_message"]
        self.assertEqual(1, len(send_actions))
        self.assertEqual("First message", send_actions[0]["message"]["text"])
        # Non-fatal: issues list should mention duplicate skip
        self.assertTrue(any("duplicate" in i.reason for i in result.issues))

    def test_send_typing_produces_simulate_typing_action_with_clamped_duration(self) -> None:
        tool_calls = _make_act_call([{"type": "send_typing", "duration_seconds": 120}])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        # 120 should be clamped to 60
        self.assertEqual([{"type": "simulate_typing", "duration_seconds": 60.0}], result.output.actions)

    def test_send_typing_within_range(self) -> None:
        tool_calls = _make_act_call([{"type": "send_typing", "duration_seconds": 5}])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        self.assertEqual(5.0, result.output.actions[0]["duration_seconds"])

    def test_unknown_action_type_is_non_fatal(self) -> None:
        tool_calls = _make_act_call([
            {"type": "some_unknown_action", "foo": "bar"},
            {"type": "send_message", "text": "Hallo"},
        ])
        result, _ = parse_tool_calls_to_model_output(tool_calls)
        assert result.output is not None
        self.assertEqual("Hallo", result.output.suggestion)
        self.assertTrue(any("unknown action type" in i.reason for i in result.issues))


class MemoryKvStorageTest(unittest.TestCase):
    def test_set_and_get_memory_kv_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.set_memory_kv(chat_id=1, key="phase", value="warmup")
            store.set_memory_kv(chat_id=1, key="trust", value="low")
            kv = store.get_memory_kv(chat_id=1)
            self.assertEqual({"phase": "warmup", "trust": "low"}, kv)

    def test_set_memory_kv_upsert_overwrites_existing_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.set_memory_kv(chat_id=1, key="phase", value="warmup")
            store.set_memory_kv(chat_id=1, key="phase", value="extraction")
            kv = store.get_memory_kv(chat_id=1)
            self.assertEqual("extraction", kv["phase"])
            self.assertEqual(1, len(kv))

    def test_get_memory_kv_returns_empty_dict_for_unknown_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            kv = store.get_memory_kv(chat_id=999)
            self.assertEqual({}, kv)

    def test_memory_kv_isolated_per_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.set_memory_kv(chat_id=1, key="x", value="a")
            store.set_memory_kv(chat_id=2, key="x", value="b")
            self.assertEqual({"x": "a"}, store.get_memory_kv(chat_id=1))
            self.assertEqual({"x": "b"}, store.get_memory_kv(chat_id=2))


class SummaryRenameMigrationTest(unittest.TestCase):
    def test_fresh_db_uses_summaries_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            saved = store.upsert_summary(
                chat_id=1,
                summary={"schema": "scambait.memory.v1"},
                cursor_event_id=5,
                model="test-model",
            )
            self.assertEqual(1, saved.chat_id)
            loaded = store.get_summary(chat_id=1)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(5, loaded.cursor_event_id)

    def test_existing_memory_contexts_table_is_migrated_to_summaries(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"

            # Manually create old-style DB with memory_contexts table
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE memory_contexts (
                    chat_id INTEGER PRIMARY KEY,
                    summary_json TEXT NOT NULL,
                    cursor_event_id INTEGER NOT NULL DEFAULT 0,
                    model TEXT NOT NULL,
                    last_updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO memory_contexts VALUES (?, ?, ?, ?, ?)",
                (42, '{"schema":"scambait.memory.v1"}', 3, "test-model", "2024-01-01T00:00:00Z"),
            )
            conn.commit()
            conn.close()

            # Open with AnalysisStore — migration should rename the table
            store = AnalysisStore(str(db_path))
            loaded = store.get_summary(chat_id=42)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(42, loaded.chat_id)
            self.assertEqual(3, loaded.cursor_event_id)


if __name__ == "__main__":
    unittest.main()
