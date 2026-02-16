from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

from scambaiter.core import PROMPT_KV_KEYS, ChatContext, ScambaiterCore, SuggestionResult
from scambaiter.storage import AnalysisStore


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: datetime
    chat_count: int
    sent_count: int


class MessageState(str, Enum):
    GENERATING = "generating"
    WAITING = "waiting"
    SENDING_TYPING = "sending_typing"
    SENT = "sent"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class PendingMessage:
    chat_id: int
    title: str
    suggestion: str
    created_at: datetime
    state: MessageState
    wait_until: datetime | None
    trigger: str
    sent_message_id: int | None = None
    last_error: str | None = None
    send_requested: bool = False


class BackgroundService:
    def __init__(self, core: ScambaiterCore, interval_seconds: int, store: AnalysisStore | None = None) -> None:
        self.core = core
        self.store = store
        self.interval_seconds = max(15, interval_seconds)
        self._run_lock = asyncio.Lock()
        self.last_summary: RunSummary | None = None
        self.last_results: list[SuggestionResult] = []
        self._context_fingerprints: dict[int, str] = {}
        self._pending_messages: dict[int, PendingMessage] = {}
        # Pro Chat gibt es genau einen aktiven Hintergrundtask (generating ODER sending).
        self._chat_tasks: dict[int, asyncio.Task] = {}
        self._unanswered_prefetch_task: asyncio.Task | None = None
        self._chat_auto_enabled: set[int] = set()

    async def scan_folder(self, force: bool = False) -> int:
        """Scan folder chats, register known chats, and create suggestions for unanswered ones."""
        async with self._run_lock:
            folder_chat_ids = await self.core.get_folder_chat_ids()
            unanswered_contexts = await self.core.collect_unanswered_chats(folder_chat_ids)
            for chat_id in folder_chat_ids:
                title = await self._resolve_chat_title(chat_id)
                if self.store:
                    self.store.upsert_known_chat(chat_id, title)

            contexts_to_generate = list(unanswered_contexts)
            if not force:
                contexts_to_generate = [
                    ctx for ctx in contexts_to_generate if ctx.chat_id not in self._pending_messages
                ]
            if not contexts_to_generate:
                return 0

            results = await self._generate_for_contexts(contexts_to_generate, on_warning=None, trigger="manual-scan")
            self.last_results = results + self.last_results
            return len(results)

    async def _resolve_chat_title(self, chat_id: int) -> str:
        try:
            entity = await self.core.client.get_entity(chat_id)
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
            if title:
                return str(title)
        except Exception:
            pass
        return str(chat_id)


    async def run_once(
        self,
        target_chat_ids: set[int] | None = None,
        on_warning: Callable[[int, str], None] | None = None,
    ) -> RunSummary:
        async with self._run_lock:
            started = datetime.now()

            if target_chat_ids:
                process_contexts: list[ChatContext] = []
                for chat_id in sorted(target_chat_ids):
                    context = await self.core.build_chat_context(chat_id)
                    if context:
                        process_contexts.append(context)
            else:
                folder_chat_ids = await self.core.get_folder_chat_ids()
                contexts = await self.core.collect_unanswered_chats(folder_chat_ids)
                process_contexts = [ctx for ctx in contexts if self._should_process_context(ctx)]

            results = await self._generate_for_contexts(process_contexts, on_warning=on_warning, trigger="suggestion-generated")
            sent_count = await self._process_due_messages()

            summary = RunSummary(
                started_at=started,
                finished_at=datetime.now(),
                chat_count=len(process_contexts),
                sent_count=sent_count,
            )
            self.last_results = results
            self.last_summary = summary
            return summary

    def start_unanswered_prefetch(self) -> bool:
        """Start async prefetch without blocking /chats response."""
        if self._unanswered_prefetch_task and not self._unanswered_prefetch_task.done():
            return False
        self._unanswered_prefetch_task = asyncio.create_task(self._prefetch_unanswered_suggestions())
        return True

    async def _prefetch_unanswered_suggestions(self) -> None:
        try:
            folder_chat_ids = await self.core.get_folder_chat_ids()
            contexts = await self.core.collect_unanswered_chats(folder_chat_ids)
            for ctx in contexts:
                pending = self._pending_messages.get(ctx.chat_id)
                if pending and pending.state in {
                    MessageState.GENERATING,
                    MessageState.WAITING,
                    MessageState.SENDING_TYPING,
                    MessageState.SENT,
                }:
                    continue
                await self.schedule_suggestion_generation(
                    chat_id=ctx.chat_id,
                    title=ctx.title,
                    trigger="chat-overview-prefetch",
                    auto_send=False,
                )
        except Exception as exc:
            print(f"[WARN] Prefetch unbeantworteter Chats fehlgeschlagen: {exc}")

    async def schedule_suggestion_generation(
        self,
        chat_id: int,
        title: str | None,
        trigger: str,
        auto_send: bool = False,
    ) -> bool:
        task = self._chat_tasks.get(chat_id)
        if task and not task.done():
            return False

        existing = self._pending_messages.get(chat_id)
        if existing and existing.state in {MessageState.WAITING, MessageState.SENDING_TYPING, MessageState.SENT}:
            return False

        now = datetime.now()
        self._pending_messages[chat_id] = PendingMessage(
            chat_id=chat_id,
            title=title or str(chat_id),
            suggestion="",
            created_at=now,
            state=MessageState.GENERATING,
            wait_until=None,
            trigger=trigger,
        )
        task = asyncio.create_task(self._generate_single_chat(chat_id, trigger=trigger, auto_send=auto_send))
        self._chat_tasks[chat_id] = task
        return True

    async def _generate_single_chat(self, chat_id: int, trigger: str, auto_send: bool) -> None:
        try:
            context = await self.core.build_chat_context(chat_id)
            if not context:
                pending = self._pending_messages.get(chat_id)
                if pending:
                    pending.state = MessageState.ERROR
                    pending.last_error = "Kein Chatkontext verfügbar."
                return

            result = await self._generate_for_contexts([context], on_warning=None, trigger=trigger)
            if result:
                self.last_results = result + self.last_results
            pending = self._pending_messages.get(chat_id)
            if auto_send or (pending is not None and pending.send_requested):
                await self.trigger_send(chat_id, trigger="auto-send-after-generation")
        except Exception as exc:
            pending = self._pending_messages.get(chat_id)
            if pending:
                pending.state = MessageState.ERROR
                pending.last_error = str(exc)
        finally:
            task = self._chat_tasks.get(chat_id)
            if task is asyncio.current_task():
                self._chat_tasks.pop(chat_id, None)

    async def _generate_for_contexts(
        self,
        contexts: list[ChatContext],
        on_warning: Callable[[int, str], None] | None,
        trigger: str,
    ) -> list[SuggestionResult]:
        results: list[SuggestionResult] = []
        for context in contexts:
            language_hint = None
            prompt_kv_state: dict[str, str] = {"messenger": "telegram"}
            if self.store:
                lang_item = self.store.kv_get(context.chat_id, "sprache")
                if lang_item:
                    language_hint = lang_item.value
                prompt_kv_state.update(self.store.kv_get_many(context.chat_id, list(PROMPT_KV_KEYS)))
            prompt_kv_state["messenger"] = "telegram"

            output = self.core.generate_output(
                context,
                language_hint=language_hint,
                prompt_kv_state=prompt_kv_state,
                on_warning=(
                    (lambda message, chat_id=context.chat_id: on_warning(chat_id, message))
                    if on_warning
                    else None
                ),
            )
            result = SuggestionResult(
                context=context,
                suggestion=output.suggestion,
                analysis=output.analysis,
                metadata=output.metadata,
            )
            results.append(result)

            if self.store:
                self.store.save(
                    chat_id=context.chat_id,
                    title=context.title,
                    suggestion=output.suggestion,
                    analysis=output.analysis,
                    metadata=output.metadata,
                )

            self._register_waiting_message(context, output.suggestion, trigger=trigger)
        return results

    def _cancel_chat_task(self, chat_id: int) -> None:
        task = self._chat_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()

    def _start_waiting_task(self, chat_id: int) -> None:
        self._cancel_chat_task(chat_id)
        task = asyncio.create_task(self._waiting_flow(chat_id))
        self._chat_tasks[chat_id] = task

    async def _waiting_flow(self, chat_id: int) -> None:
        try:
            pending = self._pending_messages.get(chat_id)
            if not pending or pending.state != MessageState.WAITING:
                return

            if pending.wait_until is None:
                await asyncio.Event().wait()
                return

            delay = max(0.0, (pending.wait_until - datetime.now()).total_seconds())
            await asyncio.sleep(delay)
            await self.trigger_send(chat_id, trigger="auto-timeout")
        except asyncio.CancelledError:
            raise
        finally:
            task = self._chat_tasks.get(chat_id)
            if task is asyncio.current_task():
                self._chat_tasks.pop(chat_id, None)

    def _register_waiting_message(self, context: ChatContext, suggestion: str, trigger: str) -> None:
        if not suggestion.strip():
            return

        previous = self._pending_messages.get(context.chat_id)
        if previous:
            self._cancel_chat_task(context.chat_id)

        wait_until = (
            datetime.now() + timedelta(seconds=self.interval_seconds)
            if context.chat_id in self._chat_auto_enabled
            else None
        )
        self._pending_messages[context.chat_id] = PendingMessage(
            chat_id=context.chat_id,
            title=context.title,
            suggestion=suggestion,
            created_at=datetime.now(),
            state=MessageState.WAITING,
            wait_until=wait_until,
            trigger=trigger,
            send_requested=bool(previous and previous.send_requested),
        )
        self._start_waiting_task(context.chat_id)

    async def _process_due_messages(self) -> int:
        # Waiting->Send wird durch _waiting_flow erledigt; dieser Hook bleibt für Kompatibilität.
        return 0

    async def trigger_send(self, chat_id: int, trigger: str = "manual") -> bool:
        pending = self._pending_messages.get(chat_id)
        if not pending:
            started = await self.schedule_suggestion_generation(
                chat_id=chat_id,
                title=str(chat_id),
                trigger="send-without-state",
                auto_send=True,
            )
            return started

        if pending.state == MessageState.GENERATING:
            pending.trigger = trigger
            pending.send_requested = True
            return True

        if pending.state not in {MessageState.WAITING, MessageState.ERROR}:
            return False

        self._cancel_chat_task(chat_id)
        pending.state = MessageState.SENDING_TYPING
        pending.trigger = trigger
        task = asyncio.create_task(self._send_flow(chat_id))
        self._chat_tasks[chat_id] = task
        return True

    async def _send_flow(self, chat_id: int) -> None:
        pending = self._pending_messages.get(chat_id)
        if not pending:
            return
        try:
            if not pending.suggestion.strip():
                pending.state = MessageState.ERROR
                pending.last_error = "Leerer Antwortvorschlag."
                return
            if not self.core.config.send_enabled or self.core.config.send_confirm != "SEND":
                pending.state = MessageState.ERROR
                pending.last_error = "Senden deaktiviert (SCAMBAITER_SEND/SCAMBAITER_SEND_CONFIRM)."
                return

            async with self.core.client.action(chat_id, "typing"):
                await asyncio.sleep(2.0)

            sent = await self.core.client.send_message(chat_id, pending.suggestion)
            pending.sent_message_id = sent.id
            pending.state = MessageState.SENT
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pending.state = MessageState.ERROR
            pending.last_error = str(exc)
        finally:
            task = self._chat_tasks.get(chat_id)
            if task is asyncio.current_task():
                self._chat_tasks.pop(chat_id, None)

    async def abort_send(self, chat_id: int) -> str:
        pending = self._pending_messages.get(chat_id)
        if not pending:
            return "Kein Nachrichtenprozess für diesen Chat gefunden."

        task = self._chat_tasks.get(chat_id)
        if task and not task.done():
            previous_state = pending.state
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            pending.state = MessageState.CANCELLED
            if previous_state == MessageState.GENERATING:
                return "Vorschlagserzeugung wurde abgebrochen."
            if previous_state == MessageState.WAITING:
                return "Wartephase beendet. Nachricht wird nicht gesendet."
            return "Sendevorgang während Tippbenachrichtigung abgebrochen."

        if pending.sent_message_id is not None:
            await self.core.client.delete_messages(chat_id, [pending.sent_message_id])
            pending.state = MessageState.CANCELLED
            return f"Gesendete Nachricht {pending.sent_message_id} wurde gelöscht."

        if pending.state in {MessageState.WAITING, MessageState.GENERATING}:
            pending.state = MessageState.CANCELLED
            return "Warte-/Generierungsphase beendet. Nachricht wird nicht gesendet."

        return "Kein laufender oder sendbarer Vorgang vorhanden."

    def get_pending_message(self, chat_id: int) -> PendingMessage | None:
        return self._pending_messages.get(chat_id)

    def pending_count(self) -> int:
        return len(self._pending_messages)

    def _should_process_context(self, context: ChatContext) -> bool:
        fingerprint = self._fingerprint_context(context)
        previous = self._context_fingerprints.get(context.chat_id)
        self._context_fingerprints[context.chat_id] = fingerprint
        if previous == fingerprint:
            self.core._debug(
                f"Überspringe Chat {context.title} ({context.chat_id}): unverändert seit letztem Lauf."
            )
            return False
        return True

    @staticmethod
    def _fingerprint_context(context: ChatContext) -> str:
        joined = "\n".join(context.lines)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


    def is_chat_auto_enabled(self, chat_id: int) -> bool:
        return chat_id in self._chat_auto_enabled

    def set_chat_auto(self, chat_id: int, enabled: bool) -> None:
        if enabled:
            self._chat_auto_enabled.add(chat_id)
        else:
            self._chat_auto_enabled.discard(chat_id)

        pending = self._pending_messages.get(chat_id)
        if not pending or pending.state != MessageState.WAITING:
            return

        pending.wait_until = datetime.now() + timedelta(seconds=self.interval_seconds) if enabled else None
        self._start_waiting_task(chat_id)

    async def shutdown(self) -> None:
        if self._unanswered_prefetch_task and not self._unanswered_prefetch_task.done():
            self._unanswered_prefetch_task.cancel()
            try:
                await self._unanswered_prefetch_task
            except asyncio.CancelledError:
                pass
            self._unanswered_prefetch_task = None

        for task in list(self._chat_tasks.values()):
            if not task.done():
                task.cancel()
        self._chat_tasks.clear()
