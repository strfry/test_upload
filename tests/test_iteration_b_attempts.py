from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scambaiter.core import ScambaiterCore
from scambaiter.storage import AnalysisStore


def _response_with_content(content: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                }
            }
        ]
    }


def _response_with_tool_calls(tool_calls: list) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": tool_calls,
                }
            }
        ]
    }


def _act_tool_call(actions: list[dict]) -> dict:
    import json

    return {"function": {"name": "act", "arguments": json.dumps({"actions": actions})}}


class IterationBDryRunRepairTest(unittest.TestCase):
    def test_dry_run_returns_initial_only_without_auto_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            # Seed one event so build_model_messages has context.
            store.ingest_event(
                chat_id=123,
                event_type="message",
                role="scammer",
                text="send screenshot",
            )
            config = SimpleNamespace(hf_token="token", hf_model="model", hf_max_tokens=200)
            core = ScambaiterCore(config=config, store=store)

            # With tool calling: valid send_message tool call → ok
            responses = [
                _response_with_tool_calls([
                    _act_tool_call([{"type": "send_message", "text": "x"}]),
                ]),
            ]

            with patch("scambaiter.core.call_hf_openai_chat", side_effect=responses):
                result = core.run_hf_dry_run(chat_id=123)

            self.assertTrue(result.get("valid_output"))
            self.assertEqual("ok", result.get("outcome_class"))
            self.assertFalse(result.get("repair_available"))
            attempts = result.get("attempts")
            self.assertIsInstance(attempts, list)
            assert isinstance(attempts, list)
            self.assertEqual(1, len(attempts))
            self.assertEqual("initial", attempts[0].get("phase"))
            self.assertEqual("ok", attempts[0].get("status"))

    def test_manual_repair_call_returns_repair_phase_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            config = SimpleNamespace(hf_token="token", hf_model="model", hf_max_tokens=200)
            core = ScambaiterCore(config=config, store=store)

            responses = [
                _response_with_tool_calls([
                    _act_tool_call([{"type": "send_message", "text": "ok"}]),
                ]),
            ]
            with patch("scambaiter.core.call_hf_openai_chat", side_effect=responses):
                result = core.run_hf_dry_run_repair(
                    chat_id=123,
                    failed_generation='{"schema":"wrong"}',
                    reject_reason="contract_validation_failed",
                )
            self.assertTrue(result.get("valid_output"))
            attempts = result.get("attempts")
            self.assertIsInstance(attempts, list)
            assert isinstance(attempts, list)
            self.assertEqual(1, len(attempts))
            self.assertEqual("repair", attempts[0].get("phase"))
            self.assertEqual("ok", attempts[0].get("status"))

    def test_dry_run_marks_semantic_conflict_and_generates_pivot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.ingest_event(
                chat_id=124,
                event_type="message",
                role="scammer",
                text="Can you share ownership and returns details?",
            )
            config = SimpleNamespace(
                hf_token="token",
                hf_model="model",
                hf_memory_model="memory-model",
                hf_max_tokens=200,
                hf_memory_max_tokens=300,
            )
            core = ScambaiterCore(config=config, store=store)

            # First call: decide_handoff signals conflict; second call: meta-turn pivot (JSON mode)
            responses = [
                _response_with_tool_calls([
                    _act_tool_call([{"type": "decide_handoff", "reason": "Cannot provide exact investment terms safely."}]),
                ]),
                _response_with_content(
                    '{"schema":"scambait.meta.turn.v1","turn_options":[{"text":"Before terms, can you send the company registration number?","strategy":"verification pivot","risk":"low"}],'
                    '"recommended_text":"Before terms, can you send the company registration number?"}'
                ),
            ]

            with patch("scambaiter.core.call_hf_openai_chat", side_effect=responses):
                result = core.run_hf_dry_run(chat_id=124)

            self.assertTrue(result.get("valid_output"))
            self.assertEqual("semantic_conflict", result.get("outcome_class"))
            self.assertTrue(result.get("semantic_conflict"))
            conflict = result.get("conflict")
            self.assertIsInstance(conflict, dict)
            assert isinstance(conflict, dict)
            self.assertEqual("semantic_conflict", conflict.get("type"))
            pivot = result.get("pivot")
            self.assertIsInstance(pivot, dict)
            assert isinstance(pivot, dict)
            self.assertIn("recommended_text", pivot)
            attempts = result.get("attempts")
            self.assertIsInstance(attempts, list)
            assert isinstance(attempts, list)
            self.assertEqual("semantic_conflict", attempts[-1].get("reject_reason"))
            self.assertFalse(bool(attempts[-1].get("accepted")))


class GenerationAttemptStoreFieldsTest(unittest.TestCase):
    def test_generation_attempt_persists_phase_and_attempt_no(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            first = store.save_generation_attempt(
                chat_id=77,
                provider="hf",
                model="m",
                prompt_json={"messages": []},
                response_json={},
                result_text="x",
                status="invalid",
                attempt_no=3,
                phase="repair",
                accepted=False,
                reject_reason="contract_validation_failed",
            )
            self.assertEqual(3, first.attempt_no)
            self.assertEqual("repair", first.phase)
            self.assertFalse(first.accepted)
            self.assertEqual("contract_validation_failed", first.reject_reason)

            listed = store.list_generation_attempts(chat_id=77, limit=5)
            self.assertEqual(1, len(listed))
            self.assertEqual(3, listed[0].attempt_no)
            self.assertEqual("repair", listed[0].phase)
            self.assertFalse(listed[0].accepted)
            self.assertEqual("contract_validation_failed", listed[0].reject_reason)


if __name__ == "__main__":
    unittest.main()
