from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scambaiter.bot_api import (
    _build_forward_payload,
    _chat_button_label,
    _delete_control_message,
    _event_ts_utc_for_store,
    _extract_partial_message_preview,
    _extract_forward_profile_info,
    _infer_role_without_target,
    _infer_target_chat_id_from_forward,
    _ingest_forward_payload,
    _profile_lines_from_events,
    _render_prompt_card_text,
    _render_prompt_section_text,
    _render_whoami_text,
    _prompt_keyboard,
    _render_user_card,
    _resolve_target_and_role_without_active,
    _truncate_chat_button_label,
    ingest_forwarded_message,
)
from scambaiter.forward_meta import baiter_name_from_meta, scammer_name_from_meta
from scambaiter.storage import AnalysisStore


class _FakeMessage:
    def __init__(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str | None,
        caption: str | None,
        has_photo: bool,
        with_forward_origin: bool,
        forward_message_id: int | None = 7788,
        forward_date_equals_message_date: bool = False,
    ) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.caption = caption
        self.photo = [object()] if has_photo else None
        self.date = datetime(2026, 2, 21, 14, 5, 0, tzinfo=timezone.utc)
        if with_forward_origin:
            origin_date = self.date if forward_date_equals_message_date else datetime(2026, 2, 21, 13, 59, 0, tzinfo=timezone.utc)
            self.forward_origin = type(
                "FakeForwardOrigin",
                (),
                {
                    "date": origin_date,
                    "message_id": forward_message_id,
                    "sender_user": type(
                        "User",
                        (),
                        {"id": 99, "username": "scammer123", "first_name": "Scam", "last_name": "Mer"},
                    )(),
                    "sender_chat": None,
                },
            )()
        else:
            self.forward_origin = None
        self.from_user = type("ControlUser", (), {"id": 555, "username": "baiter", "first_name": "Baiter", "last_name": "Tester"})()


class BotApiForwardIngestTest(unittest.TestCase):
    def test_forwarded_text_is_stored_as_manual_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            message = _FakeMessage(
                chat_id=5000,
                message_id=12,
                text="Scammer says hello",
                caption=None,
                has_photo=False,
                with_forward_origin=True,
            )

            record = ingest_forwarded_message(store=store, target_chat_id=7001, message=message)

            self.assertEqual("message", record.event_type)
            self.assertEqual("manual", record.role)
            self.assertEqual("Scammer says hello", record.text)
            events = store.list_events(chat_id=7001, limit=10)
            self.assertEqual(1, len(events))

    def test_forwarded_photo_is_stored_as_photo_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            message = _FakeMessage(
                chat_id=5000,
                message_id=13,
                text=None,
                caption="photo caption",
                has_photo=True,
                with_forward_origin=True,
            )

            record = ingest_forwarded_message(store=store, target_chat_id=7002, message=message)

            self.assertEqual("photo", record.event_type)
            self.assertEqual("manual", record.role)
            self.assertEqual("photo caption", record.text)

    def test_forward_origin_sender_user_maps_to_target_chat(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=14,
            text="forwarded",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
        )
        target = _infer_target_chat_id_from_forward(message)
        self.assertEqual(99, target)

    def test_forwarded_message_from_target_is_marked_scammer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            message = _FakeMessage(
                chat_id=5000,
                message_id=15,
                text="from scammer side",
                caption=None,
                has_photo=False,
                with_forward_origin=True,
            )

            record = ingest_forwarded_message(store=store, target_chat_id=99, message=message)
            self.assertEqual("scammer", record.role)

    def test_delete_control_message_calls_delete(self) -> None:
        class _DeleteMessage:
            def __init__(self) -> None:
                self.deleted = False

            async def delete(self) -> None:
                self.deleted = True

        message = _DeleteMessage()
        import asyncio

        asyncio.run(_delete_control_message(message))  # type: ignore[arg-type]
        self.assertTrue(message.deleted)

    def test_forward_origin_date_is_used_for_store_timestamp(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=16,
            text="time test",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
        )
        ts = _event_ts_utc_for_store(message)
        self.assertEqual("2026-02-21T13:59:00Z", ts)

    def test_forward_timestamp_is_optional_when_origin_is_not_reliable(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=17,
            text="coarse time",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
            forward_message_id=None,
        )
        self.assertIsNone(_event_ts_utc_for_store(message))

        message_same_time = _FakeMessage(
            chat_id=5000,
            message_id=18,
            text="same time",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
            forward_date_equals_message_date=True,
        )
        self.assertIsNone(_event_ts_utc_for_store(message_same_time))

    def test_unbound_forward_role_is_scammer_when_sender_differs_from_control_user(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=19,
            text="from scammer",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
        )
        role = _infer_role_without_target(message, control_user_id=123456)
        self.assertEqual("scammer", role)

    def test_unbound_forward_role_is_manual_when_sender_matches_control_user(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=20,
            text="my own forwarded message",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
        )
        role = _infer_role_without_target(message, control_user_id=99)
        self.assertEqual("manual", role)

    def test_buffer_payload_can_be_ingested_after_chat_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            message = _FakeMessage(
                chat_id=5000,
                message_id=21,
                text="buffer me",
                caption=None,
                has_photo=False,
                with_forward_origin=True,
            )
            payload = _build_forward_payload(message, role="scammer")
            record = _ingest_forward_payload(store=store, target_chat_id=1234, payload=payload)
            self.assertEqual("scammer", record.role)
            events = store.list_events(chat_id=1234, limit=10)
            self.assertEqual(1, len(events))

    def test_forward_payload_stores_scanner_and_baiter_metadata(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=30,
            text="meta test",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
        )
        payload = _build_forward_payload(message, role="manual")
        meta = payload.get("meta") or {}
        self.assertIn("forward_profile", meta)
        self.assertIn("control_sender", meta)
        self.assertEqual("Scam Mer", scammer_name_from_meta(meta))
        self.assertEqual("Baiter Tester", baiter_name_from_meta(meta))

    def test_resolve_target_and_role_without_active_for_scammer_sender(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=22,
            text="auto target from scammer",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
        )
        target, role = _resolve_target_and_role_without_active(
            message=message,
            control_user_id=123456,
            auto_target_chat_id=None,
        )
        self.assertEqual(99, target)
        self.assertEqual("scammer", role)

    def test_resolve_target_and_role_without_active_for_manual_uses_auto_target(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=23,
            text="manual after scammer",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
        )
        target, role = _resolve_target_and_role_without_active(
            message=message,
            control_user_id=99,
            auto_target_chat_id=5555,
        )
        self.assertEqual(5555, target)
        self.assertEqual("manual", role)

    def test_render_user_card_contains_chat_and_event_count(self) -> None:
        text = _render_user_card(
            target_chat_id=12345,
            event_count=7,
            last_preview="hello",
            profile_lines=["display_name: Test"],
        )
        self.assertIn("Chat Card", text)
        self.assertIn("chat_id: /12345", text)
        self.assertIn("events: 7", text)

    def test_chat_button_label_prefers_display_name_and_username(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.upsert_chat_profile(
                chat_id=9001,
                patch={"identity": {"display_name": "Julia Rose", "username": "jrose"}},
                source="test",
            )
            label = _chat_button_label(store, 9001)
            self.assertEqual("Julia Rose (@jrose) · /9001", label)

    def test_chat_button_label_uses_username_when_display_name_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.upsert_chat_profile(
                chat_id=9002,
                patch={"identity": {"username": "onlyuser"}},
                source="test",
            )
            label = _chat_button_label(store, 9002)
            self.assertEqual("@onlyuser · /9002", label)

    def test_chat_button_label_falls_back_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            label = _chat_button_label(store, 9999)
            self.assertEqual("Unknown · /9999", label)

    def test_truncate_chat_button_label_keeps_chat_id_suffix(self) -> None:
        base = "A Very Long Name That Should Be Trimmed Aggressively For Telegram Button Labels"
        label = _truncate_chat_button_label(base, 123456789, max_len=30)
        self.assertIn("/123456789", label)
        self.assertLessEqual(len(label), 30)

    def test_extract_forward_profile_info_contains_sender_user_fields(self) -> None:
        message = _FakeMessage(
            chat_id=5000,
            message_id=24,
            text="profile info",
            caption=None,
            has_photo=False,
            with_forward_origin=True,
        )
        info = _extract_forward_profile_info(message)
        self.assertIn("origin_kind", info)
        self.assertIn("sender_user", info)
        sender_user = info["sender_user"]
        self.assertEqual(99, sender_user.get("id"))

    def test_profile_lines_prefer_scammer_identity_over_manual(self) -> None:
        manual_event = type(
            "Evt",
            (),
            {
                "role": "manual",
                "meta": {
                    "forward_profile": {
                        "sender_user": {"id": 1, "username": "strfry", "first_name": "Jonathan"}
                    }
                },
            },
        )()
        scammer_event = type(
            "Evt",
            (),
            {
                "role": "scammer",
                "meta": {
                    "forward_profile": {
                        "sender_user": {"id": 2, "username": "scammer123", "first_name": "Scam"}
                    }
                },
            },
        )()
        lines = _profile_lines_from_events([manual_event, scammer_event])
        joined = "\n".join(lines)
        self.assertIn("@scammer123", joined)

    def test_render_prompt_card_text_contains_recent_events(self) -> None:
        prompt_events = [
            {"time": "12:00", "role": "manual", "text": "hello"},
            {"time": "12:01", "role": "scammer", "text": "hi back"},
        ]
        text = _render_prompt_card_text(chat_id=123, prompt_events=prompt_events)
        self.assertIn("Prompt Card", text)
        self.assertIn("chat_id: /123", text)
        self.assertIn("12:01 scammer: hi back", text)

    def test_render_prompt_section_text_includes_prompt_json(self) -> None:
        prompt_events = [
            {"time": "01:00", "role": "manual", "text": "user"},
        ]
        model_messages = [
            {"role": "system", "content": "system msg"},
            {"role": "user", "content": "user msg"},
        ]
        text = _render_prompt_section_text(
            chat_id=321,
            prompt_events=prompt_events,
            model_messages=model_messages,
            latest_payload=None,
            latest_raw="",
            latest_attempt_id=None,
            latest_status=None,
            section="messages",
            memory={"current_intent": {"latest_topic": "topic"}, "key_facts": {"fact": "value"}},
        )
        self.assertIn("Model Input Section: messages", text)
        self.assertIn('"recent_messages"', text)
        self.assertNotIn('"memory_summary"', text)
        self.assertNotIn("system msg", text)

    def test_prompt_keyboard_includes_prompt_button(self) -> None:
        keyboard = _prompt_keyboard(chat_id=999, active_section="messages")
        self.assertTrue(keyboard.inline_keyboard)
        prompt_row = keyboard.inline_keyboard[0]
        self.assertEqual("• messages", prompt_row[0].text)
        self.assertEqual("sc:psec:messages:999", prompt_row[0].callback_data)
        self.assertEqual("memory", prompt_row[1].text)
        self.assertEqual("system", prompt_row[2].text)

    def test_render_whoami_text_reports_authorization_state(self) -> None:
        message = type("Msg", (), {"chat_id": 1234})()
        text = _render_whoami_text(message=message, user_id=777, allowed_chat_id=8450305774)
        self.assertIn("chat_id: 1234", text)
        self.assertIn("user_id: 777", text)
        self.assertIn("allowed_chat_id: 8450305774", text)
        self.assertIn("authorized_here: no", text)

    def test_extract_partial_message_preview_reads_message_text(self) -> None:
        raw = '{"schema":"scambait.llm.v1","message":{"text":"  hello   world  "},"actions":[]}'
        preview = _extract_partial_message_preview(raw)
        self.assertEqual("hello world", preview)

    def test_extract_partial_message_preview_reads_action_send_message_text(self) -> None:
        raw = (
            '{"schema":"scambait.llm.v1","message":{},'
            '"actions":[{"type":"send_message","message":{"text":"  action   text  "}}]}'
        )
        preview = _extract_partial_message_preview(raw)
        self.assertEqual("action text", preview)

    def test_render_prompt_section_memory_includes_memory_summary(self) -> None:
        prompt_events = [{"time": "01:00", "role": "manual", "text": "user"}]
        model_messages = [{"role": "system", "content": "system msg"}]
        latest_payload = {
            "schema": "scambait.llm.v1",
            "message": {},
            "actions": [{"type": "send_message", "message": {"text": "from action"}}],
        }
        text = _render_prompt_section_text(
            chat_id=321,
            prompt_events=prompt_events,
            model_messages=model_messages,
            latest_payload=latest_payload,
            latest_raw="",
            latest_attempt_id=23,
            latest_status="ok",
            section="memory",
            memory={"current_intent": {"latest_topic": "topic"}},
        )
        self.assertIn("Model Input Section: memory", text)
        self.assertIn("state: ok", text)
        self.assertIn("current_intent.topic: topic", text)

    def test_render_prompt_section_system_shows_only_system_prompt(self) -> None:
        prompt_events = [{"time": "01:00", "role": "manual", "text": "user"}]
        model_messages = [
            {"role": "system", "content": "system-only"},
            {"role": "user", "content": "user msg"},
        ]
        text = _render_prompt_section_text(
            chat_id=321,
            prompt_events=prompt_events,
            model_messages=model_messages,
            latest_payload=None,
            latest_raw="",
            latest_attempt_id=None,
            latest_status=None,
            section="system",
            memory=None,
        )
        self.assertIn("Model Input Section: system", text)
        self.assertIn("system-only", text)
        self.assertNotIn("user msg", text)

    def test_render_prompt_section_memory_handles_none_gracefully(self) -> None:
        text = _render_prompt_section_text(
            chat_id=321,
            prompt_events=[],
            model_messages=[],
            latest_payload=None,
            latest_raw="",
            latest_attempt_id=None,
            latest_status=None,
            section="memory",
            memory=None,
        )
        self.assertIn("state: missing", text)
        self.assertIn("memory unavailable", text)

    def test_render_prompt_section_memory_handles_memory_context_object(self) -> None:
        memory_obj = SimpleNamespace(
            summary={
                "claimed_identity": {"name": "Julia", "role_claim": "investor", "confidence": "medium"},
                "current_intent": {"scammer_intent": "convert", "baiter_intent": "delay", "latest_topic": "wallet"},
                "narrative": {"phase": "pitch", "short_story": "story"},
                "key_facts": {"platform": "x"},
                "risk_flags": ["guaranteed return"],
                "open_questions": ["which wallet?"],
                "next_focus": ["ask tx hash"],
            },
            cursor_event_id=88,
            model="openai/gpt-oss-120b",
            last_updated_at="2026-02-22T06:00:00Z",
        )
        text = _render_prompt_section_text(
            chat_id=321,
            prompt_events=[],
            model_messages=[],
            latest_payload=None,
            latest_raw="",
            latest_attempt_id=None,
            latest_status=None,
            section="memory",
            memory=memory_obj,
        )
        self.assertIn("state: ok", text)
        self.assertIn("cursor_event_id: 88", text)
        self.assertIn("model: openai/gpt-oss-120b", text)
        self.assertIn("current_intent.topic: wallet", text)

    def test_render_prompt_section_memory_handles_invalid_type_gracefully(self) -> None:
        text = _render_prompt_section_text(
            chat_id=321,
            prompt_events=[],
            model_messages=[],
            latest_payload=None,
            latest_raw="",
            latest_attempt_id=None,
            latest_status=None,
            section="memory",
            memory="broken",
        )
        self.assertIn("state: invalid", text)
        self.assertIn("unsupported memory type", text)

    def test_extract_partial_message_preview_empty_on_non_json(self) -> None:
        preview = _extract_partial_message_preview("not-json")
        self.assertEqual("", preview)


if __name__ == "__main__":
    unittest.main()
