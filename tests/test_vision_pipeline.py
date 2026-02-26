from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scambaiter.core import ScambaiterCore
from scambaiter.model_client import call_hf_vision
from scambaiter.storage import AnalysisStore


class VisionPipelineTests(unittest.TestCase):
    """Test vision model integration for photo/document description."""

    def test_call_hf_vision_makes_multimodal_request(self) -> None:
        """Test that call_hf_vision formats image correctly for multimodal API."""
        # Mock the OpenAI client (patched where it's imported in the function)
        with patch("openai.OpenAI") as mock_openai_class:
            mock_client = MagicMock()
            mock_openai_class.return_value = mock_client
            mock_response = MagicMock()
            mock_response.choices = [MagicMock(message=MagicMock(content="Image description"))]
            mock_client.chat.completions.create.return_value = mock_response

            image_bytes = b"\x89PNG\r\n\x1a\n" + b"fake_image_data"
            result = call_hf_vision(
                token="test_token",
                model="test-vision-model",
                image_bytes=image_bytes,
                prompt="Describe this image",
                base_url="https://test.com/v1",
                max_tokens=800,
            )

            # Verify OpenAI client was created with correct parameters
            mock_openai_class.assert_called_once()
            call_kwargs = mock_openai_class.call_args[1]
            self.assertEqual("test_token", call_kwargs["api_key"])
            self.assertEqual("https://test.com/v1", call_kwargs["base_url"])

            # Verify API call was made
            mock_client.chat.completions.create.assert_called_once()
            api_call_kwargs = mock_client.chat.completions.create.call_args[1]
            self.assertEqual("test-vision-model", api_call_kwargs["model"])
            self.assertEqual(800, api_call_kwargs["max_tokens"])

            # Verify message structure contains image and text
            messages = api_call_kwargs["messages"]
            self.assertEqual(1, len(messages))
            self.assertEqual("user", messages[0]["role"])
            content = messages[0]["content"]
            self.assertEqual(2, len(content))  # image + text
            self.assertEqual("image_url", content[0]["type"])
            self.assertIn("data:image/jpeg;base64,", content[0]["image_url"]["url"])
            self.assertEqual("text", content[1]["type"])
            self.assertEqual("Describe this image", content[1]["text"])

            # Verify result
            self.assertEqual("Image description", result)

    def test_ingest_event_with_description(self) -> None:
        """Test that description field is stored and retrieved correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            event = store.ingest_event(
                chat_id=1001,
                event_type="photo",
                role="scammer",
                text="Check this document",
                description="Passport copy, appears authentic, shows name: John Smith",
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:1001:123",
            )

            self.assertEqual(1001, event.chat_id)
            self.assertEqual("photo", event.event_type)
            self.assertEqual("Passport copy, appears authentic, shows name: John Smith", event.description)

            # Verify it's stored in DB
            events = store.list_events(chat_id=1001)
            self.assertEqual(1, len(events))
            retrieved = events[0]
            self.assertEqual("Passport copy, appears authentic, shows name: John Smith", retrieved.description)

    def test_ingest_event_with_none_description(self) -> None:
        """Test that None description is handled correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            event = store.ingest_event(
                chat_id=1001,
                event_type="message",
                role="scammer",
                text="Regular text message",
                description=None,
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:1001:124",
            )

            self.assertIsNone(event.description)

            events = store.list_events(chat_id=1001)
            self.assertEqual(1, len(events))
            self.assertIsNone(events[0].description)

    def test_migration_adds_description_column(self) -> None:
        """Test that _ensure_events_columns() adds description if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            # Create store, which will create the schema
            store = AnalysisStore(str(db_path))

            # Verify description column exists by attempting to use it
            event = store.ingest_event(
                chat_id=999,
                event_type="photo",
                role="scammer",
                text="Test",
                description="Test description",
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:999:999",
            )

            self.assertIsNotNone(event.description)

    def test_update_event_description(self) -> None:
        """Test updating description on an existing event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            # Create event without description
            event = store.ingest_event(
                chat_id=1001,
                event_type="photo",
                role="scammer",
                text="Document",
                description=None,
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:1001:555",
            )

            # Update it
            store.update_event_description(event.id, "Updated description")

            # Retrieve and verify
            events = store.list_events(chat_id=1001)
            self.assertEqual(1, len(events))
            self.assertEqual("Updated description", events[0].description)

    def test_reset_summary_cursor_if_before(self) -> None:
        """Test cursor reset when backfilling earlier events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            # Create a memory summary with cursor at 100
            store._conn.execute(
                """
                INSERT INTO summaries(chat_id, summary_json, cursor_event_id, model, last_updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (1001, '{}', 100, "test-model", "2026-02-26T10:00:00Z"),
            )
            store._conn.commit()

            # Reset cursor if we backfill event_id 80
            store.reset_summary_cursor_if_before(1001, 80)

            # Cursor should be 79
            summary = store.get_summary(1001)
            self.assertEqual(79, summary.cursor_event_id)

            # Create a new summary with cursor at 50
            store._conn.execute(
                "UPDATE summaries SET cursor_event_id = 50 WHERE chat_id = 1001"
            )
            store._conn.commit()

            # Reset cursor if we backfill event_id 80
            store.reset_summary_cursor_if_before(1001, 80)

            # Cursor should remain 50 (not reset)
            summary = store.get_summary(1001)
            self.assertEqual(50, summary.cursor_event_id)

    def test_get_event_by_source(self) -> None:
        """Test looking up event by source_message_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            # Ingest an event
            event = store.ingest_event(
                chat_id=1001,
                event_type="photo",
                role="scammer",
                text="Test",
                description="Test desc",
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:1001:666",
            )

            # Lookup by source
            found = store._get_event_by_source(1001, "tg:1001:666")
            self.assertIsNotNone(found)
            self.assertEqual(event.id, found.id)
            self.assertEqual("Test desc", found.description)

            # Lookup non-existent
            not_found = store._get_event_by_source(1001, "tg:1001:999")
            self.assertIsNone(not_found)

    def test_build_prompt_events_includes_description(self) -> None:
        """Test that build_prompt_events includes description field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            config = SimpleNamespace(hf_max_tokens=2000)
            core = ScambaiterCore(config=config, store=store)

            # Create a photo event with description
            store.ingest_event(
                chat_id=1001,
                event_type="photo",
                role="scammer",
                text="Look at this",
                description="Fake passport showing: John Doe, DoB 1990-05-15",
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:1001:777",
            )

            # Create a message event without description
            store.ingest_event(
                chat_id=1001,
                event_type="message",
                role="scammer",
                text="Here is more text",
                description=None,
                ts_utc="2026-02-26T10:01:00Z",
                source_message_id="tg:1001:778",
            )

            prompt_events = core.build_prompt_events(chat_id=1001)

            # Photo event should have description
            photo_event = [e for e in prompt_events if e["event_type"] == "photo"][0]
            self.assertIn("description", photo_event)
            self.assertEqual("Fake passport showing: John Doe, DoB 1990-05-15", photo_event["description"])

            # Message event should have None for description
            msg_event = [e for e in prompt_events if e["event_type"] == "message"][0]
            self.assertIn("description", msg_event)
            self.assertIsNone(msg_event["description"])

    def test_build_memory_events_includes_image_description(self) -> None:
        """Test that build_memory_events includes image_description field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            config = SimpleNamespace(hf_max_tokens=2000)
            core = ScambaiterCore(config=config, store=store)

            # Create a photo event with description
            store.ingest_event(
                chat_id=1001,
                event_type="photo",
                role="scammer",
                text="Bank statement",
                description="Statement from Bank XYZ, account ending in 5678, balance: $1000000",
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:1001:888",
            )

            memory_events = core.build_memory_events(chat_id=1001, after_event_id=0)

            self.assertEqual(1, len(memory_events))
            event = memory_events[0]
            self.assertIn("image_description", event)
            self.assertEqual(
                "Statement from Bank XYZ, account ending in 5678, balance: $1000000",
                event["image_description"],
            )

    def test_memory_events_image_description_none_for_non_photo(self) -> None:
        """Test that non-photo events have None for image_description."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            config = SimpleNamespace(hf_max_tokens=2000)
            core = ScambaiterCore(config=config, store=store)

            # Create a message event
            store.ingest_event(
                chat_id=1001,
                event_type="message",
                role="scammer",
                text="Just text",
                description=None,
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:1001:999",
            )

            memory_events = core.build_memory_events(chat_id=1001, after_event_id=0)

            self.assertEqual(1, len(memory_events))
            event = memory_events[0]
            self.assertIn("image_description", event)
            self.assertIsNone(event["image_description"])

    def test_ingest_user_forward_with_description(self) -> None:
        """Test that description is preserved in ingest_user_forward for existing events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            # Ingest a photo event first
            original = store.ingest_event(
                chat_id=1001,
                event_type="photo",
                role="scammer",
                text="Photo",
                description="Original description",
                ts_utc="2026-02-26T10:00:00Z",
                source_message_id="tg:1001:1010",
            )

            # Try to ingest it again via user forward (should return existing)
            forwarded = store.ingest_user_forward(
                chat_id=1001,
                event_type="photo",
                text="Photo (forwarded)",
                source_message_id="tg:1001:1010",
            )

            # Should return the same event with its description intact
            self.assertEqual(original.id, forwarded.id)
            self.assertEqual("Original description", forwarded.description)


if __name__ == "__main__":
    unittest.main()
