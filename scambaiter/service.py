from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

PendingListener = Callable[[int, "PendingMessage | None"], None]

from scambaiter.core import ChatContext, ScambaiterCore, SuggestionResult
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
    ESCALATED = "escalated"
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
    action_queue: list[dict[str, object]] | None = None
    schema: str | None = None
    escalation_reason: str | None = None
    escalation_notified: bool = False




@dataclass
class KnownChatEntry:
    chat_id: int
    title: str
    updated_at: datetime

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
        self._folder_prefetch_task: asyncio.Task | None = None
        self._known_chats_refresh_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._periodic_run_task: asyncio.Task | None = None
        self._known_chats: dict[int, KnownChatEntry] = {}
        self._chat_auto_enabled: set[int] = set()
        self._pending_listeners: list[PendingListener] = []
        self._general_warnings: list[str] = []
        if self.core.config.hf_max_tokens < 1000:
            self.add_general_warning(
                f"HF_MAX_TOKENS={self.core.config.hf_max_tokens} ist niedrig; empfohlen sind >= 1000."
            )

    def add_pending_listener(self, listener: PendingListener) -> None:
        self._pending_listeners.append(listener)

    def _notify_pending_changed(self, chat_id: int) -> None:
        pending = self._pending_messages.get(chat_id)
        for listener in list(self._pending_listeners):
            try:
                listener(chat_id, pending)
            except Exception as exc:
                print(f"[WARN] Pending-Listener fehlgeschlagen für Chat {chat_id}: {exc}")

    def list_known_chats(self, limit: int = 50) -> list[KnownChatEntry]:
        items = sorted(self._known_chats.values(), key=lambda item: item.updated_at, reverse=True)
        return items[:limit]

    def add_general_warning(self, message: str) -> None:
        text = message.strip()
        if not text:
            return
        if text in self._general_warnings:
            return
        self._general_warnings.append(text)
        if len(self._general_warnings) > 30:
            self._general_warnings = self._general_warnings[-30:]

    def get_general_warnings(self, limit: int = 5) -> list[str]:
        return self._general_warnings[-limit:]

    async def refresh_known_chats_from_folder(self) -> int:
        async with self._run_lock:
            folder_chat_ids = await self.core.get_folder_chat_ids()
            now = datetime.now()
            async for dialog in self.core.client.iter_dialogs():
                if dialog.id not in folder_chat_ids:
                    continue
                updated_at = getattr(getattr(dialog, "message", None), "date", None) or now
                self._known_chats[dialog.id] = KnownChatEntry(
                    chat_id=int(dialog.id),
                    title=str(dialog.title),
                    updated_at=updated_at,
                )
            return len(self._known_chats)

    def start_known_chats_refresh(self) -> bool:
        if self._known_chats_refresh_task and not self._known_chats_refresh_task.done():
            return False
        self._known_chats_refresh_task = asyncio.create_task(self.refresh_known_chats_from_folder())
        return True

    def start_startup_bootstrap(self) -> bool:
        if self._startup_task and not self._startup_task.done():
            return False

        async def _bootstrap() -> None:
            try:
                await self.refresh_known_chats_from_folder()
                self.start_folder_prefetch()
            except Exception as exc:
                print(f"[WARN] Startup-Bootstrap fehlgeschlagen: {exc}")

        self._startup_task = asyncio.create_task(_bootstrap())
        return True

    def start_periodic_run(self) -> bool:
        if self._periodic_run_task and not self._periodic_run_task.done():
            return False

        async def _periodic_run_loop() -> None:
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"[WARN] Periodischer Lauf fehlgeschlagen: {exc}")
                await asyncio.sleep(self.interval_seconds)

        self._periodic_run_task = asyncio.create_task(_periodic_run_loop())
        return True

    async def scan_folder(self, force: bool = False) -> int:
        """Scan folder chats and generate suggestions."""
        await self.refresh_known_chats_from_folder()
        async with self._run_lock:
            folder_chat_ids = await self.core.get_folder_chat_ids()
            folder_contexts = await self.core.collect_folder_chats(folder_chat_ids)
            contexts_to_generate = list(folder_contexts)
            if not force:
                contexts_to_generate = [
                    ctx for ctx in contexts_to_generate if ctx.chat_id not in self._pending_messages
                ]
            if not contexts_to_generate:
                return 0

            results = await self._generate_for_contexts(contexts_to_generate, on_warning=None, trigger="manual-scan")
            self.last_results = results + self.last_results
            return len(results)


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
                if not self._chat_auto_enabled:
                    summary = RunSummary(
                        started_at=started,
                        finished_at=datetime.now(),
                        chat_count=0,
                        sent_count=0,
                    )
                    self.last_summary = summary
                    return summary
                folder_chat_ids = await self.core.get_folder_chat_ids()
                contexts = await self.core.collect_folder_chats(folder_chat_ids)
                process_contexts = [
                    ctx
                    for ctx in contexts
                    if ctx.chat_id in self._chat_auto_enabled and self._should_process_context(ctx)
                ]

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

    def start_folder_prefetch(self) -> bool:
        """Start async prefetch without blocking /chats response."""
        if self._folder_prefetch_task and not self._folder_prefetch_task.done():
            return False
        self._folder_prefetch_task = asyncio.create_task(self._prefetch_folder_suggestions())
        return True

    async def _prefetch_folder_suggestions(self) -> None:
        try:
            if not self._chat_auto_enabled:
                return
            folder_chat_ids = await self.core.get_folder_chat_ids()
            contexts = await self.core.collect_folder_chats(folder_chat_ids)
            for ctx in contexts:
                if ctx.chat_id not in self._chat_auto_enabled:
                    continue
                pending = self._pending_messages.get(ctx.chat_id)
                if pending and pending.state in {
                    MessageState.GENERATING,
                    MessageState.WAITING,
                    MessageState.SENDING_TYPING,
                }:
                    continue
                await self.schedule_suggestion_generation(
                    chat_id=ctx.chat_id,
                    title=ctx.title,
                    trigger="chat-overview-prefetch",
                    auto_send=False,
                )
        except Exception as exc:
            print(f"[WARN] Prefetch der Ordner-Chats fehlgeschlagen: {exc}")

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
        if existing and existing.state in {MessageState.WAITING, MessageState.SENDING_TYPING}:
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
        self._notify_pending_changed(chat_id)
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
                    self._notify_pending_changed(chat_id)
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
                self._notify_pending_changed(chat_id)
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
            prompt_context: dict[str, object] = {"messenger": "telegram"}
            existing_pending = self._pending_messages.get(context.chat_id)
            if self.store:
                previous = self.store.latest_for_chat(context.chat_id)
                previous_analysis = previous.analysis if previous else None
                language_hint = self._extract_language_hint(previous_analysis)
                if previous_analysis:
                    prompt_context["previous_analysis"] = previous_analysis
            if existing_pending and existing_pending.action_queue:
                prompt_context["planned_queue"] = existing_pending.action_queue
                prompt_context["planned_queue_trigger"] = existing_pending.trigger

            output = self.core.generate_output(
                context,
                language_hint=language_hint,
                prompt_context=prompt_context,
                on_warning=(
                    (lambda message, chat_id=context.chat_id: self._handle_generation_warning(chat_id, message, on_warning))
                ),
            )
            result = SuggestionResult(
                context=context,
                suggestion=output.suggestion,
                analysis=output.analysis,
                metadata=output.metadata,
                actions=output.actions,
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

            self._register_waiting_message(
                context,
                output.suggestion,
                trigger=trigger,
                action_queue=output.actions,
                schema=output.metadata.get("schema"),
            )
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
            await self.core.client.send_read_acknowledge(chat_id)
            await self.trigger_send(chat_id, trigger="auto-timeout")
        except asyncio.CancelledError:
            raise
        finally:
            task = self._chat_tasks.get(chat_id)
            if task is asyncio.current_task():
                self._chat_tasks.pop(chat_id, None)

    def _register_waiting_message(
        self,
        context: ChatContext,
        suggestion: str,
        trigger: str,
        action_queue: list[dict[str, object]] | None = None,
        schema: str | None = None,
    ) -> None:
        if not suggestion.strip():
            return

        previous = self._pending_messages.get(context.chat_id)
        if previous:
            self._cancel_chat_task(context.chat_id)
            previous_actions = list(previous.action_queue or [])
            new_actions = list(action_queue or [])
            if previous_actions != new_actions:
                self.add_general_warning(
                    f"Chat {context.chat_id}: Bestehende Queue durch neue Modell-Actions ersetzt."
                )

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
            action_queue=list(action_queue or []),
            schema=(schema or "").strip() or None,
            escalation_reason=None,
            escalation_notified=False,
        )
        self._notify_pending_changed(context.chat_id)
        self._start_waiting_task(context.chat_id)

    @staticmethod
    def _extract_language_hint(analysis: dict[str, object] | None) -> str | None:
        if not analysis:
            return None
        for key in ("language", "sprache"):
            value = analysis.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _handle_generation_warning(
        self,
        chat_id: int,
        message: str,
        on_warning: Callable[[int, str], None] | None,
    ) -> None:
        self.add_general_warning(f"Chat {chat_id}: {message}")
        if on_warning:
            on_warning(chat_id, message)

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
            self._notify_pending_changed(chat_id)
            return True

        if pending.state not in {MessageState.WAITING, MessageState.ERROR}:
            return False

        self._cancel_chat_task(chat_id)
        pending.state = MessageState.SENDING_TYPING
        pending.trigger = trigger
        self._notify_pending_changed(chat_id)
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
                self._notify_pending_changed(chat_id)
                return
            if not self.core.config.send_enabled or self.core.config.send_confirm != "SEND":
                pending.state = MessageState.ERROR
                pending.last_error = "Senden deaktiviert (SCAMBAITER_SEND/SCAMBAITER_SEND_CONFIRM)."
                self._notify_pending_changed(chat_id)
                return

            sent_message_id: int | None = None
            sent = False
            outgoing_text = pending.suggestion
            action_queue = list(pending.action_queue or [])
            if not action_queue:
                action_queue = [
                    {"type": "mark_read"},
                    {"type": "simulate_typing", "duration_seconds": 2.0},
                    {"type": "send_message"},
                ]

            mark_read_done = False

            async def ensure_mark_read() -> None:
                nonlocal mark_read_done
                if mark_read_done:
                    return
                await self.core.client.send_read_acknowledge(chat_id)
                mark_read_done = True

            for action in action_queue:
                action_type = str(action.get("type", "")).strip().lower()
                if action_type == "mark_read":
                    await ensure_mark_read()
                    continue

                if action_type == "simulate_typing":
                    await ensure_mark_read()
                    duration = float(action.get("duration_seconds", 0.0))
                    duration = min(60.0, max(0.0, duration))
                    async with self.core.client.action(chat_id, "typing"):
                        await asyncio.sleep(duration)
                    continue

                if action_type == "delay_send":
                    delay = float(action.get("delay_seconds", 0.0))
                    delay = min(86400.0, max(0.0, delay))
                    await asyncio.sleep(delay)
                    continue

                if action_type == "send_message":
                    await ensure_mark_read()
                    reply_to = action.get("reply_to")
                    send_kwargs: dict[str, object] = {}
                    if isinstance(reply_to, int):
                        send_kwargs["reply_to"] = reply_to
                    sent_message = await self.core.client.send_message(chat_id, outgoing_text, **send_kwargs)
                    sent_message_id = int(sent_message.id)
                    sent = True
                    continue

                if action_type == "edit_message":
                    await ensure_mark_read()
                    message_id = action.get("message_id")
                    new_text = str(action.get("new_text", "")).strip()
                    if isinstance(message_id, int) and new_text:
                        try:
                            await self.core.client.edit_message(chat_id, message_id, new_text)
                        except Exception:
                            pass
                    continue

                if action_type == "noop":
                    continue

                if action_type == "escalate_to_human":
                    reason = str(action.get("reason", "")).strip() or "Eskalation durch Action-Plan."
                    pending.state = MessageState.ESCALATED
                    pending.escalation_reason = reason
                    pending.last_error = None
                    self._notify_pending_changed(chat_id)
                    return

            if not sent:
                pending.state = MessageState.ERROR
                pending.last_error = "Action-Queue enthält keine send_message-Aktion."
                self._notify_pending_changed(chat_id)
                return

            pending.sent_message_id = sent_message_id
            pending.state = MessageState.SENT
            self._notify_pending_changed(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pending.state = MessageState.ERROR
            pending.last_error = str(exc)
            self._notify_pending_changed(chat_id)
        finally:
            task = self._chat_tasks.get(chat_id)
            if task is asyncio.current_task():
                self._chat_tasks.pop(chat_id, None)

    async def abort_send(self, chat_id: int, trigger: str = "manual-stop") -> str:
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
            pending.trigger = trigger
            self._notify_pending_changed(chat_id)
            if previous_state == MessageState.GENERATING:
                return "Vorschlagserzeugung wurde abgebrochen."
            if previous_state == MessageState.WAITING:
                return "Wartephase beendet. Nachricht wird nicht gesendet."
            return "Sendevorgang während Tippbenachrichtigung abgebrochen."

        if pending.sent_message_id is not None:
            await self.core.client.delete_messages(chat_id, [pending.sent_message_id])
            pending.state = MessageState.CANCELLED
            pending.trigger = trigger
            self._notify_pending_changed(chat_id)
            return f"Gesendete Nachricht {pending.sent_message_id} wurde gelöscht."

        if pending.state in {MessageState.WAITING, MessageState.GENERATING}:
            pending.state = MessageState.CANCELLED
            pending.trigger = trigger
            self._notify_pending_changed(chat_id)
            return "Warte-/Generierungsphase beendet. Nachricht wird nicht gesendet."

        return "Kein laufender oder sendbarer Vorgang vorhanden."

    def get_pending_message(self, chat_id: int) -> PendingMessage | None:
        return self._pending_messages.get(chat_id)

    def pending_count(self) -> int:
        return len(self._pending_messages)

    def list_pending_messages(self) -> list[PendingMessage]:
        return sorted(self._pending_messages.values(), key=lambda item: item.created_at, reverse=True)

    def mark_escalation_notified(self, chat_id: int) -> bool:
        pending = self._pending_messages.get(chat_id)
        if not pending or pending.state != MessageState.ESCALATED:
            return False
        if pending.escalation_notified:
            return False
        pending.escalation_notified = True
        self._notify_pending_changed(chat_id)
        return True

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
        joined = "\n".join(
            f"{msg.timestamp.isoformat()}|{msg.role}|{msg.sender}|{msg.text}" for msg in context.messages
        )
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

        pending.trigger = "bot-auto-on" if enabled else "bot-auto-off"
        pending.wait_until = datetime.now() + timedelta(seconds=self.interval_seconds) if enabled else None
        self._notify_pending_changed(chat_id)
        self._start_waiting_task(chat_id)

    async def shutdown(self) -> None:
        if self._periodic_run_task and not self._periodic_run_task.done():
            self._periodic_run_task.cancel()
            try:
                await self._periodic_run_task
            except asyncio.CancelledError:
                pass
            self._periodic_run_task = None

        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass
            self._startup_task = None

        if self._known_chats_refresh_task and not self._known_chats_refresh_task.done():
            self._known_chats_refresh_task.cancel()
            try:
                await self._known_chats_refresh_task
            except asyncio.CancelledError:
                pass
            self._known_chats_refresh_task = None

        if self._folder_prefetch_task and not self._folder_prefetch_task.done():
            self._folder_prefetch_task.cancel()
            try:
                await self._folder_prefetch_task
            except asyncio.CancelledError:
                pass
            self._folder_prefetch_task = None

        for task in list(self._chat_tasks.values()):
            if not task.done():
                task.cancel()
        self._chat_tasks.clear()
