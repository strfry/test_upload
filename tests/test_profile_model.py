from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scambaiter.bot_api import _profile_patch_from_forward_profile
from scambaiter.core import ScambaiterCore
from scambaiter.storage import AnalysisStore


class ProfileModelTest(unittest.TestCase):
    def test_upsert_chat_profile_tracks_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))

            first = store.upsert_chat_profile(
                chat_id=9001,
                patch={"identity": {"display_name": "Alice", "username": "alice1"}},
                source="botapi_forward",
                changed_at="2026-02-21T20:20:00Z",
            )
            self.assertTrue(first)

            second = store.upsert_chat_profile(
                chat_id=9001,
                patch={"identity": {"display_name": "Alice B"}},
                source="telethon",
                changed_at="2026-02-21T20:21:00Z",
            )
            self.assertTrue(any(item.field_path == "identity.display_name" for item in second))

            profile = store.get_chat_profile(chat_id=9001)
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual("Alice B", profile.snapshot["identity"]["display_name"])

            system_msgs = store.list_profile_system_messages(chat_id=9001, limit=10)
            self.assertTrue(system_msgs)
            self.assertTrue(any("profile_update:" in str(item.get("text", "")) for item in system_msgs))

    def test_profile_system_messages_deduplicate_by_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.upsert_chat_profile(
                chat_id=9010,
                patch={"identity": {"display_name": "Alice"}},
                source="botapi_forward",
                changed_at="2026-02-21T20:20:00Z",
            )
            store.upsert_chat_profile(
                chat_id=9010,
                patch={"identity": {"display_name": "Alice B"}},
                source="telethon",
                changed_at="2026-02-21T20:21:00Z",
            )
            system_msgs = store.list_profile_system_messages(chat_id=9010, limit=10)
            updates = [m for m in system_msgs if "identity.display_name" in str(m.get("text", ""))]
            self.assertEqual(1, len(updates))
            self.assertIn("Alice B", str(updates[0].get("text", "")))

    def test_profile_patch_from_forward_profile_maps_identity(self) -> None:
        patch = _profile_patch_from_forward_profile(
            {
                "sender_user": {
                    "id": 123,
                    "username": "example",
                    "first_name": "Ex",
                    "last_name": "Ample",
                    "is_bot": False,
                    "language_code": "en",
                }
            }
        )
        self.assertEqual(123, patch["identity"]["telegram_user_id"])
        self.assertEqual("example", patch["identity"]["username"])
        self.assertEqual("Ex Ample", patch["identity"]["display_name"])
        self.assertIn("account", patch)

    def test_prompt_events_include_profile_system_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            store.ingest_event(chat_id=77, event_type="message", role="scammer", text="hi")
            store.upsert_chat_profile(
                chat_id=77,
                patch={"identity": {"display_name": "Scammer X"}},
                source="botapi_forward",
                changed_at="2026-02-21T20:22:00Z",
            )
            core = ScambaiterCore(config=SimpleNamespace(hf_max_tokens=500), store=store)
            prompt_events = core.build_prompt_events(chat_id=77)
            self.assertTrue(any(item.get("role") == "system" and "profile_update:" in str(item.get("text")) for item in prompt_events))

    def test_generation_attempts_are_stored_and_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            created = store.save_generation_attempt(
                chat_id=42,
                provider="huggingface_openai_compat",
                model="deepseek-ai/DeepSeek-R1",
                prompt_json={"messages": [{"role": "system", "content": "x"}]},
                response_json={"choices": []},
                result_text="{}",
                status="ok",
            )
            self.assertGreater(created.id, 0)
            listed = store.list_generation_attempts(chat_id=42, limit=5)
            self.assertEqual(1, len(listed))
            self.assertEqual("ok", listed[0].status)


if __name__ == "__main__":
    unittest.main()
