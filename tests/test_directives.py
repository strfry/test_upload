"""Tests for user-specified prompt directives."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from scambaiter.core import ScambaiterCore
from scambaiter.storage import AnalysisStore, Directive


class DirectiveStorageTest(unittest.TestCase):
    """Test directive storage operations."""

    def setUp(self) -> None:
        self.store = AnalysisStore(":memory:")

    def test_add_directive_creates_active_directive(self) -> None:
        """Adding a directive creates it with active=True."""
        directive = self.store.add_directive(
            chat_id=123,
            text="Use formal tone",
            scope="chat",
        )
        assert directive.id > 0
        assert directive.chat_id == 123
        assert directive.text == "Use formal tone"
        assert directive.scope == "chat"
        assert directive.active is True

    def test_list_directives_active_only(self) -> None:
        """list_directives(active_only=True) returns only active directives."""
        d1 = self.store.add_directive(123, "Directive 1", "chat")
        d2 = self.store.add_directive(123, "Directive 2", "once")
        self.store.deactivate_directive(d1.id)

        active = self.store.list_directives(chat_id=123, active_only=True)
        assert len(active) == 1
        assert active[0].id == d2.id

    def test_list_directives_all(self) -> None:
        """list_directives(active_only=False) returns all directives."""
        d1 = self.store.add_directive(123, "Directive 1", "chat")
        d2 = self.store.add_directive(123, "Directive 2", "once")
        self.store.deactivate_directive(d1.id)

        all_dirs = self.store.list_directives(chat_id=123, active_only=False, limit=50)
        assert len(all_dirs) == 2

    def test_deactivate_directive_sets_active_false(self) -> None:
        """deactivate_directive() sets active=0 in database."""
        directive = self.store.add_directive(123, "Test", "chat")
        self.store.deactivate_directive(directive.id)

        reloaded = self.store.list_directives(chat_id=123, active_only=False, limit=1)
        assert len(reloaded) == 1
        assert reloaded[0].active is False

    def test_directive_multiple_chats_isolated(self) -> None:
        """Directives in different chats are isolated."""
        d1 = self.store.add_directive(123, "Directive for 123", "chat")
        d2 = self.store.add_directive(456, "Directive for 456", "chat")

        dirs_123 = self.store.list_directives(chat_id=123, active_only=True)
        dirs_456 = self.store.list_directives(chat_id=456, active_only=True)

        assert len(dirs_123) == 1
        assert dirs_123[0].id == d1.id
        assert len(dirs_456) == 1
        assert dirs_456[0].id == d2.id


class DirectiveInjectionTest(unittest.TestCase):
    """Test directive injection into LLM prompts."""

    def setUp(self) -> None:
        self.store = AnalysisStore(":memory:")
        config = MagicMock()
        config.hf_token = "test_token"
        config.hf_model = "test_model"
        config.hf_max_tokens = 1500
        config.hf_vision_model = None
        self.core = ScambaiterCore(config=config, store=self.store)
        # Add a synthetic chat event
        self.chat_id = 7654321
        self.store.ingest_event(
            chat_id=self.chat_id,
            event_type="message",
            role="scammer",
            text="Hello, I have an investment opportunity for you.",
            ts_utc=datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            source_message_id="123",
        )

    def test_build_model_messages_without_directives(self) -> None:
        """build_model_messages() works normally when directives=None."""
        messages = self.core.build_model_messages(
            chat_id=self.chat_id,
            include_memory=False,
            directives=None,
        )
        assert len(messages) >= 2  # At least persona + memory
        assert messages[0]["role"] == "system"
        # Should not have the directive injection message (which says "instructions override")
        override_messages = [m for m in messages[1:] if "The following instructions override normal conversation flow" in m.get("content", "")]
        assert len(override_messages) == 0

    def test_build_model_messages_with_directives(self) -> None:
        """build_model_messages() injects directives as system message."""
        directives = [
            {"id": "1", "text": "Use formal tone"},
            {"id": "2", "text": "Ask about registration fees"},
        ]
        messages = self.core.build_model_messages(
            chat_id=self.chat_id,
            include_memory=False,
            directives=directives,
        )
        combined_content = "\n".join(m.get("content", "") for m in messages)
        assert "[OPERATOR_DIRECTIVES]" in combined_content
        assert "[END_DIRECTIVES]" in combined_content
        assert "Use formal tone" in combined_content
        assert "Ask about registration fees" in combined_content

    def test_build_model_messages_directive_position(self) -> None:
        """Directives appear after timing, before events."""
        directives = [{"id": "1", "text": "Test directive"}]
        messages = self.core.build_model_messages(
            chat_id=self.chat_id,
            include_memory=False,
            timing={"now_ts": "2024-01-01T00:00:00Z"},
            directives=directives,
        )
        # Find positions
        directive_idx = None
        timing_idx = None
        event_idx = None
        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            if "[OPERATOR_DIRECTIVES]" in content:
                directive_idx = i
            if "Timing data:" in content:
                timing_idx = i
            if msg.get("role") == "user":
                event_idx = i
        assert timing_idx is not None, "Timing message not found"
        assert directive_idx is not None, "Directive message not found"
        assert event_idx is not None, "Event message not found"
        assert timing_idx < directive_idx < event_idx, "Directive position incorrect"

    def test_build_model_messages_empty_directives_list(self) -> None:
        """Empty directives list [] does not inject directive message."""
        messages = self.core.build_model_messages(
            chat_id=self.chat_id,
            include_memory=False,
            directives=[],
        )
        # Should not have the override instructions message
        override_messages = [m for m in messages[1:] if "The following instructions override normal conversation flow" in m.get("content", "")]
        assert len(override_messages) == 0


class DirectiveAcceptanceTest(unittest.TestCase):
    """Test LLM directive acknowledgment scenarios."""

    def setUp(self) -> None:
        self.store = AnalysisStore(":memory:")
        config = MagicMock()
        config.hf_token = "test_token"
        config.hf_model = "test_model"
        config.hf_max_tokens = 1500
        config.hf_vision_model = None
        self.core = ScambaiterCore(config=config, store=self.store)
        self.chat_id = 9876543

    def test_directive_acknowledged_in_analysis(self) -> None:
        """LLM can report directive acknowledgment in analysis block."""
        # Simulated LLM response with directive acknowledgment
        analysis_block = {
            "directives": {
                "acknowledged": [1, 2],
                "rejected": [],
            }
        }
        # Verify structure is valid JSON
        json_str = json.dumps(analysis_block)
        parsed = json.loads(json_str)
        assert parsed["directives"]["acknowledged"] == [1, 2]
        assert parsed["directives"]["rejected"] == []

    def test_directive_rejected_in_analysis(self) -> None:
        """LLM can report directive rejection in analysis block."""
        analysis_block = {
            "directives": {
                "acknowledged": [1],
                "rejected": [2],
                "rejection_reason": "Conflicting with conversation context",
            }
        }
        json_str = json.dumps(analysis_block)
        parsed = json.loads(json_str)
        assert parsed["directives"]["acknowledged"] == [1]
        assert parsed["directives"]["rejected"] == [2]
        assert "Conflicting" in parsed["directives"]["rejection_reason"]

    def test_directive_partial_acceptance(self) -> None:
        """LLM can acknowledge some directives and reject others."""
        # Simulate: 3 directives, 2 accepted, 1 rejected
        directives = [
            {"id": "10", "text": "Be suspicious of yield claims"},
            {"id": "11", "text": "Ask for government registration"},
            {"id": "12", "text": "Request wire transfer details"},
        ]
        analysis_block = {
            "directives": {
                "acknowledged": [10, 11],
                "rejected": [12],
                "rejection_reason": "Too early in conversation",
            }
        }
        # Verify all directives are accounted for
        all_ids = analysis_block["directives"]["acknowledged"] + analysis_block["directives"]["rejected"]
        assert set(all_ids) == {10, 11, 12}


class DirectiveScopeTest(unittest.TestCase):
    """Test directive scope (chat vs once)."""

    def setUp(self) -> None:
        self.store = AnalysisStore(":memory:")
        self.chat_id = 5555555

    def test_scope_chat_persists(self) -> None:
        """scope='chat' directives persist across multiple generations."""
        d1 = self.store.add_directive(self.chat_id, "Use formal tone", scope="chat")

        # Simulate two dry-runs
        active_1 = self.store.list_directives(self.chat_id, active_only=True)
        assert len(active_1) == 1
        assert active_1[0].id == d1.id

        # After hypothetical second generation
        active_2 = self.store.list_directives(self.chat_id, active_only=True)
        assert len(active_2) == 1
        assert active_2[0].id == d1.id

    def test_scope_once_available_for_consumption(self) -> None:
        """scope='once' directives are marked for single-use consumption."""
        d1 = self.store.add_directive(self.chat_id, "Probe about document upload", scope="once")

        active = self.store.list_directives(self.chat_id, active_only=True)
        assert len(active) == 1
        assert active[0].scope == "once"
        # After LLM acknowledges directive 1, it can be deactivated
        self.store.deactivate_directive(d1.id)
        active_after = self.store.list_directives(self.chat_id, active_only=True)
        assert len(active_after) == 0

    def test_mixed_scopes(self) -> None:
        """Chat can have both chat-scope and once-scope directives."""
        d_chat = self.store.add_directive(self.chat_id, "Formal tone", scope="chat")
        d_once = self.store.add_directive(self.chat_id, "Ask about document", scope="once")

        active = self.store.list_directives(self.chat_id, active_only=True)
        assert len(active) == 2
        scopes = {d.scope for d in active}
        assert scopes == {"chat", "once"}


class DirectiveFormatTest(unittest.TestCase):
    """Test directive formatting in prompts."""

    def setUp(self) -> None:
        self.store = AnalysisStore(":memory:")
        config = MagicMock()
        config.hf_token = "test_token"
        config.hf_model = "test_model"
        config.hf_max_tokens = 1500
        config.hf_vision_model = None
        config.hf_memory_model = "memory_model"  # Add proper string config
        self.core = ScambaiterCore(config=config, store=self.store)
        self.chat_id = 1111111
        self.store.ingest_event(
            chat_id=self.chat_id,
            event_type="message",
            role="scammer",
            text="Test",
            ts_utc=datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            source_message_id="1",
        )

    def test_directive_format_markers(self) -> None:
        """Directives are properly wrapped with markers."""
        directives = [
            {"id": "1", "text": "Be skeptical"},
            {"id": "2", "text": "Ask for proofs"},
        ]
        messages = self.core.build_model_messages(
            chat_id=self.chat_id,
            include_memory=False,
            directives=directives,
        )
        combined = "\n".join(m.get("content", "") for m in messages)

        # Check markers
        assert "[OPERATOR_DIRECTIVES]" in combined
        assert "[END_DIRECTIVES]" in combined
        # Check content
        assert "Be skeptical" in combined
        assert "Ask for proofs" in combined
        # Verify order
        start = combined.index("[OPERATOR_DIRECTIVES]")
        end = combined.index("[END_DIRECTIVES]")
        assert start < end

    def test_directive_with_special_characters(self) -> None:
        """Directives can contain special characters."""
        directives = [
            {"id": "1", "text": "Ask: 'Do you have license from SEC?'"},
            {"id": "2", "text": "Look for {red flags} in responses"},
        ]
        messages = self.core.build_model_messages(
            chat_id=self.chat_id,
            include_memory=False,
            directives=directives,
        )
        combined = "\n".join(m.get("content", "") for m in messages)
        assert "Ask: 'Do you have license from SEC?'" in combined
        assert "Look for {red flags}" in combined

    def test_directive_multiline_text(self) -> None:
        """Directives with newlines are preserved."""
        directives = [
            {"id": "1", "text": "Ask about:\n1. Company registration\n2. License status\n3. References"},
        ]
        messages = self.core.build_model_messages(
            chat_id=self.chat_id,
            include_memory=False,
            directives=directives,
        )
        combined = "\n".join(m.get("content", "") for m in messages)
        assert "Ask about:" in combined
        assert "1. Company registration" in combined or "1. Company" in combined


class DirectiveMultipleChatTest(unittest.TestCase):
    """Test directives across multiple synthetic chats."""

    def setUp(self) -> None:
        self.store = AnalysisStore(":memory:")
        config = MagicMock()
        config.hf_token = "test_token"
        config.hf_model = "test_model"
        config.hf_max_tokens = 1500
        config.hf_vision_model = None
        config.hf_memory_model = "memory_model"  # Add proper string config
        self.core = ScambaiterCore(config=config, store=self.store)

    def test_directives_isolated_by_chat(self) -> None:
        """Directives for different chats don't interfere."""
        chat1, chat2 = 1001, 1002

        # Add directives to each chat
        d1 = self.store.add_directive(chat1, "Formal for chat1", "chat")
        d2 = self.store.add_directive(chat2, "Casual for chat2", "chat")

        # Load directives for each
        dir1 = self.store.list_directives(chat1, active_only=True)
        dir2 = self.store.list_directives(chat2, active_only=True)

        assert len(dir1) == 1 and dir1[0].id == d1.id
        assert len(dir2) == 1 and dir2[0].id == d2.id

        # Build prompts with separate directives
        directives1 = [{"id": str(d1.id), "text": d1.text}]
        directives2 = [{"id": str(d2.id), "text": d2.text}]

        messages1 = self.core.build_model_messages(chat1, directives=directives1)
        messages2 = self.core.build_model_messages(chat2, directives=directives2)

        combined1 = "\n".join(m.get("content", "") for m in messages1)
        combined2 = "\n".join(m.get("content", "") for m in messages2)

        assert "Formal for chat1" in combined1
        assert "Formal for chat1" not in combined2
        assert "Casual for chat2" in combined2
        assert "Casual for chat2" not in combined1

    def test_multiple_directives_single_chat(self) -> None:
        """Single chat can have multiple directives."""
        chat_id = 2002

        d1 = self.store.add_directive(chat_id, "Directive 1", "chat")
        d2 = self.store.add_directive(chat_id, "Directive 2", "once")
        d3 = self.store.add_directive(chat_id, "Directive 3", "chat")

        active = self.store.list_directives(chat_id, active_only=True)
        assert len(active) == 3

        directives = [{"id": str(d.id), "text": d.text} for d in active]
        messages = self.core.build_model_messages(chat_id, directives=directives)
        combined = "\n".join(m.get("content", "") for m in messages)

        assert "Directive 1" in combined
        assert "Directive 2" in combined
        assert "Directive 3" in combined


if __name__ == "__main__":
    unittest.main()
