from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scambaiter.core import ChatContext, ModelOutput
from scambaiter.service import BackgroundService
from scambaiter.storage import AnalysisStore


class _FakeCore:
    def __init__(self, output: ModelOutput) -> None:
        self.config = SimpleNamespace(hf_max_tokens=1500)
        self._output = output

    def generate_output(self, *args, **kwargs) -> ModelOutput:  # pragma: no cover - trivial pass-through
        return self._output

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


if __name__ == "__main__":
    unittest.main()
