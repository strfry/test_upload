from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from scambaiter.core import ChatContext, ModelOutput
from scambaiter.service import BackgroundService, MessageState, PendingMessage
from scambaiter.storage import AnalysisStore


class _FakeCore:
    def __init__(self, output: ModelOutput) -> None:
        self.config = SimpleNamespace(hf_max_tokens=1500)
        self._output = output
        self._context: ChatContext | None = None

    def generate_output(self, *args, **kwargs) -> ModelOutput:  # pragma: no cover - trivial pass-through
        return self._output

    async def build_chat_context(self, chat_id: int) -> ChatContext | None:  # pragma: no cover - deterministic stub
        _ = chat_id
        return self._context

    def get_recent_typing_hint(self, chat_id: int, max_age_seconds: int = 120):  # pragma: no cover - deterministic stub
        _ = (chat_id, max_age_seconds)
        return None


class OnceDirectiveConsumptionTest(unittest.IsolatedAsyncioTestCase):
    async def test_once_directive_deleted_when_operator_applied_mentions_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            output = ModelOutput(
                raw="{}",
                suggestion="ok",
                analysis={"operator_applied": ["1"]},
                metadata={"schema": "scambait.llm.v1"},
                actions=[{"type": "send_message"}],
            )
            core = _FakeCore(output=output)
            service = BackgroundService(core=core, interval_seconds=30, store=store)

            created = service.add_chat_directive(chat_id=123, text="Once rule", scope="once")
            self.assertIsNotNone(created)
            assert created is not None

            # Ensure operator_applied refers to the real directive id.
            core._output.analysis = {"operator_applied": [str(created.id)]}
            context = ChatContext(chat_id=123, title="Test", messages=[])

            await service._generate_for_contexts([context], on_warning=None, trigger="test-once")

            remaining = store.list_directives(chat_id=123, active_only=True, limit=50)
            self.assertFalse(any(item.id == created.id for item in remaining))

    async def test_once_directive_kept_when_not_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            output = ModelOutput(
                raw="{}",
                suggestion="ok",
                analysis={"operator_applied": []},
                metadata={"schema": "scambait.llm.v1"},
                actions=[{"type": "send_message"}],
            )
            core = _FakeCore(output=output)
            service = BackgroundService(core=core, interval_seconds=30, store=store)

            created = service.add_chat_directive(chat_id=123, text="Once rule", scope="once")
            self.assertIsNotNone(created)
            assert created is not None
            context = ChatContext(chat_id=123, title="Test", messages=[])

            await service._generate_for_contexts([context], on_warning=None, trigger="test-once")

            remaining = store.list_directives(chat_id=123, active_only=True, limit=50)
            self.assertTrue(any(item.id == created.id for item in remaining))

    async def test_dry_run_does_not_replace_pending_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "analysis.sqlite3"
            store = AnalysisStore(str(db_path))
            output = ModelOutput(
                raw="{}",
                suggestion="dry-run suggestion",
                analysis={},
                metadata={"schema": "scambait.llm.v1"},
                actions=[{"type": "send_message"}],
            )
            core = _FakeCore(output=output)
            service = BackgroundService(core=core, interval_seconds=30, store=store)

            # Existing queue state that must stay untouched by dry-run.
            service._pending_messages[123] = PendingMessage(
                chat_id=123,
                title="Existing",
                suggestion="existing suggestion",
                created_at=datetime.now(),
                state=MessageState.WAITING,
                wait_until=None,
                trigger="existing",
                action_queue=[{"type": "send_message"}],
            )
            before = service.get_pending_message(123)
            self.assertIsNotNone(before)
            assert before is not None
            before_suggestion = before.suggestion

            context = ChatContext(chat_id=123, title="Test", messages=[])
            core._context = context

            result = await service.run_dry_run_once(chat_id=123, trigger="test-dry-run")
            self.assertIsNotNone(result)
            self.assertEqual("dry-run suggestion", result.suggestion if result else "")

            after = service.get_pending_message(123)
            self.assertIsNotNone(after)
            assert after is not None
            self.assertEqual(before_suggestion, after.suggestion)


if __name__ == "__main__":
    unittest.main()
