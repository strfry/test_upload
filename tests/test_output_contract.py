from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scambaiter.core import ChatContext, ModelOutput, ScambaiterCore, parse_structured_model_output
from scambaiter.storage import AnalysisStore


class OutputContractParserTest(unittest.TestCase):
    def test_valid_payload_is_parsed(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {"loop_guard_active": True},
            "message": {"text": "Alles klar."},
            "actions": [{"type": "send_message"}],
        }
        parsed = parse_structured_model_output(json.dumps(payload))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual("scambait.llm.v1", parsed.metadata.get("schema"))
        self.assertEqual("Alles klar.", parsed.suggestion)
        self.assertEqual([{"type": "send_message"}], parsed.actions)

    def test_invalid_payload_missing_schema_is_rejected(self) -> None:
        payload = {
            "analysis": {},
            "message": {"text": "Hallo"},
            "actions": [{"type": "send_message"}],
        }
        self.assertIsNone(parse_structured_model_output(json.dumps(payload)))

    def test_invalid_payload_unknown_top_level_key_is_rejected(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {"text": "Hallo"},
            "actions": [{"type": "send_message"}],
            "extra": "not-allowed",
        }
        self.assertIsNone(parse_structured_model_output(json.dumps(payload)))

    def test_send_message_requires_non_empty_message_text(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {"text": "   "},
            "actions": [{"type": "send_message"}],
        }
        self.assertIsNone(parse_structured_model_output(json.dumps(payload)))

    def test_action_shorthand_is_normalized(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {"text": "Hallo"},
            "actions": [{"send_message": {}}],
        }
        parsed = parse_structured_model_output(json.dumps(payload))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual([{"type": "send_message"}], parsed.actions)


class OutputContractCoreFlowTest(unittest.TestCase):
    def test_generate_output_emits_contract_valid_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            config = SimpleNamespace(hf_max_tokens=200)
            core = ScambaiterCore(config=config, store=store)
            context = ChatContext(
                chat_id=42,
                title="chat-42",
                messages=[{"event_type": "message", "role": "scammer", "text": "need screenshot"}],
            )

            output = core.generate_output(context)
            self.assertIsInstance(output, ModelOutput)
            self.assertEqual("scambait.llm.v1", output.metadata.get("schema"))
            self.assertTrue(output.suggestion)
            self.assertTrue(any(item.get("type") == "send_message" for item in output.actions))


if __name__ == "__main__":
    unittest.main()
