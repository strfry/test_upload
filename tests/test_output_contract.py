from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scambaiter.core import (
    SYSTEM_PROMPT_CONTRACT,
    ChatContext,
    ModelOutput,
    parse_structured_model_output_detailed,
    ScambaiterCore,
    parse_structured_model_output,
    violates_scambait_style_policy,
)
from scambaiter.storage import AnalysisStore


class OutputContractParserTest(unittest.TestCase):
    def test_system_prompt_contract_has_scambaiter_persona_and_style(self) -> None:
        self.assertIn("You are the ScamBaiter.", SYSTEM_PROMPT_CONTRACT)
        self.assertIn("play-along-lightly style", SYSTEM_PROMPT_CONTRACT)
        self.assertIn("generic consumer safety advisory tone", SYSTEM_PROMPT_CONTRACT)

    def test_valid_payload_is_parsed(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {"loop_guard_active": True},
            "message": {},
            "actions": [{"type": "send_message", "message": {"text": "Alles klar."}}],
        }
        parsed = parse_structured_model_output(json.dumps(payload))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual("scambait.llm.v1", parsed.metadata.get("schema"))
        self.assertEqual("Alles klar.", parsed.suggestion)
        self.assertEqual([{"type": "send_message", "message": {"text": "Alles klar."}}], parsed.actions)

    def test_scambait_style_policy_flags_advisor_language(self) -> None:
        text = (
            "I’m sorry, but I can’t provide this. Please verify the platform's legitimacy and "
            "consult a qualified financial advisor before moving forward."
        )
        self.assertTrue(violates_scambait_style_policy(text))

    def test_scambait_style_policy_allows_engaged_follow_up(self) -> None:
        text = "Das klingt interessant. Welche Plattform genau meinst du und wie läuft der Auszahlungsprozess dort ab?"
        self.assertFalse(violates_scambait_style_policy(text))

    def test_invalid_payload_missing_schema_is_rejected(self) -> None:
        payload = {
            "analysis": {},
            "message": {},
            "actions": [{"type": "send_message", "message": {"text": "Hallo"}}],
        }
        self.assertIsNone(parse_structured_model_output(json.dumps(payload)))

    def test_unknown_top_level_key_is_tolerated(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {},
            "actions": [{"type": "send_message", "message": {"text": "Hallo"}}],
            "extra": "not-allowed",
        }
        parsed = parse_structured_model_output(json.dumps(payload))
        self.assertIsNotNone(parsed)

    def test_send_message_requires_non_empty_action_message_text(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {},
            "actions": [{"type": "send_message", "message": {"text": "   "}}],
        }
        self.assertIsNone(parse_structured_model_output(json.dumps(payload)))

    def test_action_alias_field_is_normalized(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {},
            "actions": [{"action": "send_message", "message": {"text": "Hallo"}}],
        }
        parsed = parse_structured_model_output(json.dumps(payload))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual([{"type": "send_message", "message": {"text": "Hallo"}}], parsed.actions)

    def test_empty_actions_is_rejected(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {"text": "Hallo"},
            "actions": [],
        }
        self.assertIsNone(parse_structured_model_output(json.dumps(payload)))

    def test_action_shorthand_is_normalized(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {},
            "actions": [{"send_message": {"message": {"text": "Hallo"}}}],
        }
        parsed = parse_structured_model_output(json.dumps(payload))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual([{"type": "send_message", "message": {"text": "Hallo"}}], parsed.actions)

    def test_send_message_rejects_flat_text_shape(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {"text": "Hallo"},
            "actions": [{"type": "send_message", "text": "Hallo"}],
        }
        parsed = parse_structured_model_output_detailed(json.dumps(payload))
        self.assertIsNone(parsed.output)
        self.assertTrue(parsed.issues)
        self.assertEqual("actions[0]", parsed.issues[0].path)

    def test_send_message_accepts_dotted_message_text_alias(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {},
            "actions": [{"type": "send_message", "message.text": "Hallo"}],
        }
        parsed = parse_structured_model_output(json.dumps(payload))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual([{"type": "send_message", "message": {"text": "Hallo"}}], parsed.actions)

    def test_conflict_allows_missing_send_message(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {"reason": "Insufficient context to continue safely."},
            "message": {},
            "conflict": {
                "type": "semantic_conflict",
                "code": "insufficient_context",
                "reason": "Need operator decision before continuing.",
                "requires_human": True,
                "suggested_mode": "hold",
            },
            "actions": [{"type": "noop"}],
        }
        parsed = parse_structured_model_output(json.dumps(payload))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual("", parsed.suggestion)
        self.assertIsInstance(parsed.conflict, dict)
        assert isinstance(parsed.conflict, dict)
        self.assertEqual("semantic_conflict", parsed.conflict.get("type"))

    def test_parser_returns_structured_issue_path_and_reason(self) -> None:
        payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {},
            "actions": [{"type": "send_message", "message": {}}],
        }
        parsed = parse_structured_model_output_detailed(json.dumps(payload))
        self.assertIsNone(parsed.output)
        self.assertTrue(parsed.issues)
        first = parsed.issues[0]
        self.assertEqual("actions[0].message.text", first.path)
        self.assertIn("missing text", first.reason)


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
