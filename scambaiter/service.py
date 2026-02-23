from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from .core import ChatContext, ModelOutput
from .storage import AnalysisStore, Directive


class MessageState(str, Enum):
    GENERATING = "generating"
    WAITING = "waiting"
    SENT = "sent"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(slots=True)
class PendingMessage:
    chat_id: int
    title: str
    suggestion: str
    created_at: datetime
    state: MessageState
    wait_until: datetime | None
    trigger: str
    action_queue: list[dict[str, Any]]


class BackgroundService:
    """Coordinates generation and queue preparation.

    The service does not send chat messages itself. It stores pending actions
    that a control channel can inspect and trigger.
    """

    def __init__(self, core: Any, interval_seconds: int, store: AnalysisStore) -> None:
        self.core = core
        self.interval_seconds = interval_seconds
        self.store = store
        self._pending_messages: dict[int, PendingMessage] = {}

    def start_startup_bootstrap(self) -> None:  # pragma: no cover - integration hook
        return

    def start_periodic_run(self) -> None:  # pragma: no cover - integration hook
        return

    async def shutdown(self) -> None:  # pragma: no cover - integration hook
        return

    def add_chat_directive(self, chat_id: int, text: str, scope: str = "chat") -> Directive:
        return self.store.add_directive(chat_id=chat_id, text=text, scope=scope)

    def get_pending_message(self, chat_id: int) -> PendingMessage | None:
        return self._pending_messages.get(chat_id)

    async def trigger_for_chat(self, chat_id: int, trigger: str = "live_message") -> None:
        """Generate a response for a single chat immediately (Live Mode auto-receive)."""
        context = await self.core.build_chat_context(chat_id)
        if context is None:
            return
        await self._generate_for_contexts([context], on_warning=None, trigger=trigger)

    async def run_dry_run_once(self, chat_id: int, trigger: str) -> ModelOutput | None:
        context = await self.core.build_chat_context(chat_id)
        if context is None:
            return None
        return self.core.generate_output(context, prompt_context={"trigger": trigger, "mode": "dry_run"})

    async def _generate_for_contexts(
        self,
        contexts: list[ChatContext],
        on_warning: Callable[[str], None] | None,
        trigger: str,
    ) -> None:
        for context in contexts:
            directives = self.store.list_directives(chat_id=context.chat_id, active_only=True, limit=50)
            prompt_context = {
                "trigger": trigger,
                "operator": {
                    "directives": [
                        {"id": str(item.id), "text": item.text, "scope": item.scope}
                        for item in directives
                    ]
                },
            }
            try:
                output = self.core.generate_output(context, prompt_context=prompt_context)
            except Exception as exc:  # pragma: no cover - defensive fallback
                if on_warning:
                    on_warning(f"generation failed for {context.chat_id}: {exc}")
                continue
            self._persist_generation(context, output)
            self._consume_once_directives(context.chat_id, output.analysis)

    def _persist_generation(self, context: ChatContext, output: ModelOutput) -> None:
        self.store.save(
            chat_id=context.chat_id,
            title=context.title,
            suggestion=output.suggestion,
            analysis=output.analysis,
            actions=output.actions,
            metadata=output.metadata,
        )
        self._pending_messages[context.chat_id] = PendingMessage(
            chat_id=context.chat_id,
            title=context.title,
            suggestion=output.suggestion,
            created_at=datetime.now(),
            state=MessageState.WAITING,
            wait_until=None,
            trigger="generated",
            action_queue=list(output.actions),
        )

    def _consume_once_directives(self, chat_id: int, analysis: dict[str, Any] | None) -> None:
        if not isinstance(analysis, dict):
            return
        applied = analysis.get("operator_applied")
        if not isinstance(applied, list):
            return
        applied_ids: set[int] = set()
        for item in applied:
            try:
                applied_ids.add(int(str(item)))
            except ValueError:
                continue
        if not applied_ids:
            return
        directives = self.store.list_directives(chat_id=chat_id, active_only=True, limit=100)
        for directive in directives:
            if directive.scope == "once" and directive.id in applied_ids:
                self.store.deactivate_directive(directive.id)
