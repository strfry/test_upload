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


class IterationBDryRunRepairTest(unittest.TestCase):
    def test_dry_run_uses_repair_phase_after_invalid_initial_output(self) -> None:
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

            responses = [
                _response_with_content(
                    '{"schema":"wrong","analysis":{},"message":{"text":"x"},'
                    '"actions":[{"type":"send_message","message":{"text":"x"}}]}'
                ),
                _response_with_content(
                    '{"schema":"scambait.llm.v1","analysis":{},"message":{"text":"ok"},'
                    '"actions":[{"type":"send_message","message":{"text":"ok"}}]}'
                ),
            ]

            with patch("scambaiter.core.call_hf_openai_chat", side_effect=responses):
                result = core.run_hf_dry_run(chat_id=123)

            self.assertTrue(result.get("valid_output"))
            attempts = result.get("attempts")
            self.assertIsInstance(attempts, list)
            assert isinstance(attempts, list)
            self.assertEqual(2, len(attempts))
            self.assertEqual("initial", attempts[0].get("phase"))
            self.assertEqual("invalid", attempts[0].get("status"))
            self.assertIsInstance(attempts[0].get("contract_issues"), list)
            self.assertEqual("repair", attempts[1].get("phase"))
            self.assertEqual("ok", attempts[1].get("status"))


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
