from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

PendingListener = Callable[[int, "PendingMessage | None"], None]
WarningListener = Callable[[str], None]

from scambaiter.core import ChatContext, ScambaiterCore, SuggestionResult
from scambaiter.storage import AnalysisStore, StoredDirective, StoredGenerationAttempt


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
    current_action_index: int | None = None
    current_action_total: int | None = None
    current_action_label: str | None = None
    current_action_until: datetime | None = None




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
        self._skip_current_action: set[int] = set()
        self._pending_listeners: list[PendingListener] = []
        self._warning_listeners: list[WarningListener] = []
        self._general_warnings: list[str] = []
        self._recent_generation_attempts: dict[int, list[StoredGenerationAttempt]] = {}
        if self.core.config.hf_max_tokens < 1000:
            self.add_general_warning(
                f"HF_MAX_TOKENS={self.core.config.hf_max_tokens} ist niedrig; empfohlen sind >= 1000."
            )

    def add_pending_listener(self, listener: PendingListener) -> None:
        self._pending_listeners.append(listener)

    def add_warning_listener(self, listener: WarningListener) -> None:
        self._warning_listeners.append(listener)

    def list_chat_directives(self, chat_id: int, active_only: bool = True, limit: int = 50) -> list[StoredDirective]:
        if not self.store:
            return []
        return self.store.list_directives(chat_id=chat_id, active_only=active_only, limit=limit)

    def add_chat_directive(self, chat_id: int, text: str, scope: str = "session") -> StoredDirective | None:
        if not self.store:
            return None
        return self.store.add_directive(chat_id=chat_id, text=text, scope=scope)

    def delete_chat_directive(self, chat_id: int, directive_id: int) -> bool:
        if not self.store:
            return False
        return self.store.delete_directive(chat_id=chat_id, directive_id=directive_id)

    def request_skip_current_action(self, chat_id: int) -> bool:
        pending = self._pending_messages.get(chat_id)
        if not pending or pending.state != MessageState.SENDING_TYPING:
            return False
        # Allow skipping both explicit wait and typing simulation phases.
        if pending.current_action_label not in {"wait", "simulate_typing"}:
            return False
        self._skip_current_action.add(int(chat_id))
        return True

    def _consume_skip_request(self, chat_id: int) -> bool:
        if int(chat_id) in self._skip_current_action:
            self._skip_current_action.discard(int(chat_id))
            return True
        return False

    async def _sleep_with_optional_skip(self, chat_id: int, seconds: float) -> bool:
        remaining = max(0.0, float(seconds))
        if remaining <= 0:
            return False
        step = 0.4
        while remaining > 0:
            if self._consume_skip_request(chat_id):
                return True
            current = min(step, remaining)
            await asyncio.sleep(current)
            remaining -= current
        return self._consume_skip_request(chat_id)

    def _notify_pending_changed(self, chat_id: int) -> None:
        pending = self._pending_messages.get(chat_id)
        for listener in list(self._pending_listeners):
            try:
                listener(chat_id, pending)
            except Exception as exc:
                print(f"[WARN] Pending-Listener fehlgeschlagen für Chat {chat_id}: {exc}")

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _coerce_message_id(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if not isinstance(value, str):
            return None
        text = value.strip()
        # Handle quoted numeric IDs such as '"123"' or "'123'".
        while len(text) >= 2 and ((text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'")):
            text = text[1:-1].strip()
        if not text or not text.isdigit():
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def list_known_chats(self, limit: int = 50) -> list[KnownChatEntry]:
        items = sorted(
            self._known_chats.values(),
            key=lambda item: self._as_utc(item.updated_at),
            reverse=True,
        )
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
        for listener in list(self._warning_listeners):
            try:
                listener(text)
            except Exception as exc:
                print(f"[WARN] Warning-Listener fehlgeschlagen: {exc}")

    def list_generation_attempts(self, chat_id: int, limit: int = 12) -> list[StoredGenerationAttempt]:
        if self.store:
            return self.store.list_generation_attempts_for_chat(chat_id=int(chat_id), limit=int(limit))
        return list(self._recent_generation_attempts.get(int(chat_id), []))[: int(limit)]

    def list_recent_generation_attempts(self, limit: int = 30) -> list[StoredGenerationAttempt]:
        if self.store:
            return self.store.list_generation_attempts_recent(limit=int(limit))
        all_items: list[StoredGenerationAttempt] = []
        for items in self._recent_generation_attempts.values():
            all_items.extend(items)
        return sorted(all_items, key=lambda item: item.created_at, reverse=True)[: int(limit)]

    def get_general_warnings(self, limit: int = 5) -> list[str]:
        return self._general_warnings[-limit:]

    async def refresh_known_chats_from_folder(self) -> int:
        async with self._run_lock:
            folder_chat_ids = await self.core.get_folder_chat_ids()
            now = datetime.now(timezone.utc)
            async for dialog in self.core.client.iter_dialogs():
                if dialog.id not in folder_chat_ids:
                    continue
                raw_updated_at = getattr(getattr(dialog, "message", None), "date", None) or now
                updated_at = self._as_utc(raw_updated_at)
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
                    if (
                        ctx.chat_id in self._chat_auto_enabled
                        and self._is_generation_allowed_for_chat(ctx.chat_id)
                        and self._should_process_context(ctx)
                    )
                ]

            results = await self._generate_for_contexts(
                process_contexts, on_warning=on_warning, trigger="suggestion-generated"
            )
            sent_count = 0

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
            prompt_context, language_hint, previous_analysis = self.build_prompt_context_for_chat(context.chat_id)

            output = self.core.generate_output(
                context,
                language_hint=language_hint,
                prompt_context=prompt_context,
                on_warning=(
                    (lambda message, chat_id=context.chat_id: self._handle_generation_warning(chat_id, message, on_warning))
                ),
                on_attempt=(
                    lambda event, chat_id=context.chat_id, title=context.title: self._record_generation_attempt(
                        chat_id=chat_id,
                        title=title,
                        trigger=trigger,
                        event=event,
                    )
                ),
            )
            merged_analysis = self._merge_analysis(previous_analysis if self.store else None, output.analysis)
            result = SuggestionResult(
                context=context,
                suggestion=output.suggestion,
                analysis=merged_analysis,
                metadata=output.metadata,
                actions=output.actions,
            )
            results.append(result)

            if self.store:
                self.store.save(
                    chat_id=context.chat_id,
                    title=context.title,
                    suggestion=output.suggestion,
                    analysis=merged_analysis,
                    actions=output.actions,
                    metadata=output.metadata,
                )
                self._consume_once_directives(context.chat_id, merged_analysis)

            self._register_waiting_message(
                context,
                output.suggestion,
                trigger=trigger,
                action_queue=output.actions,
                schema=output.metadata.get("schema"),
            )
        return results

    async def run_dry_run_once(
        self,
        chat_id: int,
        trigger: str = "directive-dry-run",
        on_warning: Callable[[int, str], None] | None = None,
    ) -> SuggestionResult | None:
        context = await self.core.build_chat_context(chat_id)
        if not context:
            return None

        prompt_context, language_hint, previous_analysis = self.build_prompt_context_for_chat(chat_id)
        output = self.core.generate_output(
            context,
            language_hint=language_hint,
            prompt_context=prompt_context,
            on_warning=(lambda message, cid=chat_id: self._handle_generation_warning(cid, message, on_warning)),
            on_attempt=(
                lambda event, cid=chat_id, title=context.title: self._record_generation_attempt(
                    chat_id=cid,
                    title=title,
                    trigger=trigger,
                    event=event,
                )
            ),
        )
        merged_analysis = self._merge_analysis(previous_analysis if self.store else None, output.analysis)
        result = SuggestionResult(
            context=context,
            suggestion=output.suggestion,
            analysis=merged_analysis,
            metadata=output.metadata,
            actions=output.actions,
        )

        # Dry-run mutates neither pending queue nor stored chat analysis/suggestion snapshot.
        self.last_results = [result] + self.last_results
        if len(self.last_results) > 50:
            self.last_results = self.last_results[:50]
        return result

    def _consume_once_directives(self, chat_id: int, analysis: dict[str, object] | None) -> None:
        if not self.store or not isinstance(analysis, dict):
            return
        applied_raw = analysis.get("operator_applied")
        if not isinstance(applied_raw, list) or not applied_raw:
            return
        applied_ids: set[int] = set()
        for item in applied_raw:
            if isinstance(item, int):
                applied_ids.add(item)
                continue
            if isinstance(item, str):
                value = item.strip()
                if not value:
                    continue
                try:
                    applied_ids.add(int(value))
                except ValueError:
                    continue
        if not applied_ids:
            return
        directives = self.store.list_directives(chat_id=chat_id, active_only=True, limit=200)
        for directive in directives:
            if directive.scope.strip().lower() != "once":
                continue
            if directive.id in applied_ids:
                self.store.delete_directive(chat_id=chat_id, directive_id=directive.id)

    def _record_generation_attempt(
        self,
        *,
        chat_id: int,
        title: str,
        trigger: str,
        event: dict[str, object],
    ) -> None:
        attempt_no = int(event.get("attempt_no") or 0)
        phase = str(event.get("phase") or "initial")
        parsed_ok = bool(event.get("parsed_ok"))
        accepted = bool(event.get("accepted"))
        reject_reason_value = event.get("reject_reason")
        reject_reason = str(reject_reason_value) if isinstance(reject_reason_value, str) else None
        raw_excerpt_value = event.get("raw_excerpt")
        raw_excerpt = str(raw_excerpt_value) if isinstance(raw_excerpt_value, str) and raw_excerpt_value else None
        suggestion_value = event.get("suggestion")
        suggestion = str(suggestion_value) if isinstance(suggestion_value, str) else None
        schema_value = event.get("schema")
        schema = str(schema_value) if isinstance(schema_value, str) and schema_value else None
        heuristic_score_value = event.get("heuristic_score")
        heuristic_score = float(heuristic_score_value) if isinstance(heuristic_score_value, (int, float)) else None
        flags_value = event.get("heuristic_flags")
        heuristic_flags = [str(item) for item in flags_value] if isinstance(flags_value, list) else []
        prompt_tokens_value = event.get("prompt_tokens")
        completion_tokens_value = event.get("completion_tokens")
        total_tokens_value = event.get("total_tokens")
        reasoning_tokens_value = event.get("reasoning_tokens")
        prompt_tokens = int(prompt_tokens_value) if isinstance(prompt_tokens_value, (int, float)) else None
        completion_tokens = int(completion_tokens_value) if isinstance(completion_tokens_value, (int, float)) else None
        total_tokens = int(total_tokens_value) if isinstance(total_tokens_value, (int, float)) else None
        reasoning_tokens = int(reasoning_tokens_value) if isinstance(reasoning_tokens_value, (int, float)) else None

        if self.store:
            self.store.save_generation_attempt(
                chat_id=chat_id,
                title=title,
                trigger=trigger,
                attempt_no=attempt_no,
                phase=phase,
                parsed_ok=parsed_ok,
                accepted=accepted,
                reject_reason=reject_reason,
                heuristic_score=heuristic_score,
                heuristic_flags=heuristic_flags,
                raw_excerpt=raw_excerpt,
                suggestion=suggestion,
                schema=schema,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                reasoning_tokens=reasoning_tokens,
            )
            self._notify_pending_changed(chat_id)
            return

        # Fallback im Speicher, falls kein Store konfiguriert ist.
        created = datetime.now()
        synthetic = StoredGenerationAttempt(
            id=0,
            created_at=created,
            chat_id=int(chat_id),
            title=title,
            trigger=trigger,
            attempt_no=attempt_no,
            phase=phase,
            parsed_ok=parsed_ok,
            accepted=accepted,
            reject_reason=reject_reason,
            heuristic_score=heuristic_score,
            heuristic_flags=heuristic_flags,
            raw_excerpt=raw_excerpt,
            suggestion=suggestion,
            schema=schema,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            reasoning_tokens=reasoning_tokens,
        )
        bucket = self._recent_generation_attempts.setdefault(int(chat_id), [])
        bucket.insert(0, synthetic)
        del bucket[30:]
        self._notify_pending_changed(chat_id)

    def build_prompt_context_for_chat(
        self,
        chat_id: int,
    ) -> tuple[dict[str, object], str | None, dict[str, object] | None]:
        language_hint: str | None = None
        previous_analysis: dict[str, object] | None = None
        prompt_context: dict[str, object] = {
            "messenger": "telegram",
            "now_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        existing_pending = self._pending_messages.get(chat_id)
        if self.store:
            previous = self.store.latest_for_chat(chat_id)
            previous_analysis = previous.analysis if previous else None
            language_hint = self._extract_language_hint(previous_analysis)
            if previous_analysis:
                prompt_context["previous_analysis"] = previous_analysis
            directives = self.store.list_directives(chat_id=chat_id, active_only=True, limit=25)
            if directives:
                prompt_context["operator"] = {
                    "directives": [
                        {
                            "id": str(item.id),
                            "text": item.text,
                            "scope": item.scope,
                        }
                        for item in directives
                    ]
                }
        if existing_pending and existing_pending.action_queue:
            prompt_context["planned_queue"] = existing_pending.action_queue
            prompt_context["planned_queue_trigger"] = existing_pending.trigger
        typing_hint = self.core.get_recent_typing_hint(chat_id, max_age_seconds=120)
        if typing_hint:
            prompt_context["counterparty_live_activity"] = typing_hint
        return prompt_context, language_hint, previous_analysis

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
        actions = list(action_queue or [])
        if not suggestion.strip() and not actions:
            return

        previous = self._pending_messages.get(context.chat_id)
        if previous:
            self._cancel_chat_task(context.chat_id)
            previous_actions = list(previous.action_queue or [])
            new_actions = actions
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
            action_queue=actions,
            schema=(schema or "").strip() or None,
            escalation_reason=None,
            escalation_notified=False,
            current_action_index=None,
            current_action_total=None,
            current_action_label=None,
            current_action_until=None,
        )
        self._notify_pending_changed(context.chat_id)
        self._start_waiting_task(context.chat_id)

    @staticmethod
    def _merge_analysis(
        previous: dict[str, object] | None,
        current: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if not isinstance(previous, dict) and not isinstance(current, dict):
            return None
        if not isinstance(previous, dict):
            return dict(current or {})
        if not isinstance(current, dict):
            return dict(previous)

        merged: dict[str, object] = dict(previous)
        for key, value in current.items():
            old_value = merged.get(key)
            if isinstance(old_value, dict) and isinstance(value, dict):
                nested = BackgroundService._merge_analysis(old_value, value)
                merged[key] = nested if isinstance(nested, dict) else {}
            else:
                merged[key] = value
        return merged

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

    async def trigger_send(self, chat_id: int, trigger: str = "manual") -> bool:
        pending = self._pending_messages.get(chat_id)
        if not pending:
            restored = self._restore_pending_from_store(chat_id, trigger=trigger)
            if not restored:
                return False
            pending = self._pending_messages.get(chat_id)
            if not pending:
                return False

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

    def _restore_pending_from_store(self, chat_id: int, trigger: str) -> bool:
        if not self.store:
            return False
        latest = self.store.latest_for_chat(chat_id)
        if not latest:
            return False
        actions = list(latest.actions or [])
        if not actions:
            return False
        has_send_message = any(str(action.get("type", "")).strip().lower() == "send_message" for action in actions)
        if has_send_message and not latest.suggestion.strip():
            return False
        self._pending_messages[chat_id] = PendingMessage(
            chat_id=chat_id,
            title=latest.title,
            suggestion=latest.suggestion,
            created_at=datetime.now(),
            state=MessageState.WAITING,
            wait_until=None,
            trigger=trigger,
            send_requested=False,
            action_queue=actions,
            schema=(latest.metadata.get("schema") if latest.metadata else None),
            escalation_reason=None,
            escalation_notified=False,
            current_action_index=None,
            current_action_total=None,
            current_action_label=None,
            current_action_until=None,
        )
        self._notify_pending_changed(chat_id)
        return True

    def restore_pending_from_store(self, chat_id: int, trigger: str = "manual-restore") -> bool:
        return self._restore_pending_from_store(chat_id=int(chat_id), trigger=str(trigger))

    async def _send_flow(self, chat_id: int) -> None:
        pending = self._pending_messages.get(chat_id)
        if not pending:
            return
        try:
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
            has_send_message = any(str(action.get("type", "")).strip().lower() == "send_message" for action in action_queue)
            if has_send_message and not pending.suggestion.strip():
                pending.state = MessageState.ERROR
                pending.last_error = "Leerer Antwortvorschlag für send_message."
                self._notify_pending_changed(chat_id)
                return
            if has_send_message and not self.core.config.send_enabled:
                pending.state = MessageState.ERROR
                pending.last_error = "Senden deaktiviert (SCAMBAITER_SEND)."
                self._notify_pending_changed(chat_id)
                return

            mark_read_done = False

            async def ensure_mark_read() -> None:
                nonlocal mark_read_done
                if mark_read_done:
                    return
                await self.core.client.send_read_acknowledge(chat_id)
                mark_read_done = True

            pending.current_action_total = len(action_queue)
            for idx, action in enumerate(action_queue, start=1):
                action_type = str(action.get("type", "")).strip().lower()
                pending.current_action_index = idx
                pending.current_action_label = action_type
                pending.current_action_until = None
                self._notify_pending_changed(chat_id)

                if action_type == "mark_read":
                    await ensure_mark_read()
                    continue

                if action_type == "simulate_typing":
                    await ensure_mark_read()
                    duration = float(action.get("duration_seconds", 0.0))
                    duration = min(60.0, max(0.0, duration))
                    pending.current_action_until = datetime.now() + timedelta(seconds=duration)
                    self._notify_pending_changed(chat_id)
                    async with self.core.client.action(chat_id, "typing"):
                        skipped = await self._sleep_with_optional_skip(chat_id, duration)
                        if skipped:
                            pending.current_action_until = None
                            self._notify_pending_changed(chat_id)
                    continue

                if action_type == "wait":
                    raw_value = action.get("value", 0.0)
                    raw_unit = str(action.get("unit", "")).strip().lower()
                    wait_value = float(raw_value) if isinstance(raw_value, (int, float)) else 0.0
                    if raw_unit == "minutes":
                        delay = wait_value * 60.0
                    else:
                        delay = wait_value
                    delay = min(604800.0, max(0.0, delay))
                    pending.current_action_until = datetime.now() + timedelta(seconds=delay)
                    self._notify_pending_changed(chat_id)
                    skipped = await self._sleep_with_optional_skip(chat_id, delay)
                    if skipped:
                        pending.current_action_until = None
                        self._notify_pending_changed(chat_id)
                    continue

                if action_type == "send_message":
                    await ensure_mark_read()
                    reply_to = self._coerce_message_id(action.get("reply_to"))
                    send_at_utc = action.get("send_at_utc")
                    if isinstance(send_at_utc, str) and send_at_utc.strip():
                        delay = self._seconds_until_utc(send_at_utc)
                        if delay > 0:
                            pending.current_action_until = datetime.now() + timedelta(seconds=delay)
                            self._notify_pending_changed(chat_id)
                            skipped = await self._sleep_with_optional_skip(chat_id, delay)
                            if skipped:
                                pending.current_action_until = None
                                self._notify_pending_changed(chat_id)
                    send_kwargs: dict[str, object] = {}
                    if reply_to is not None:
                        send_kwargs["reply_to"] = reply_to
                    sent_message = await self.core.client.send_message(chat_id, outgoing_text, **send_kwargs)
                    sent_message_id = int(sent_message.id)
                    sent = True
                    continue

                if action_type == "edit_message":
                    await ensure_mark_read()
                    message_id = self._coerce_message_id(action.get("message_id"))
                    new_text = str(action.get("new_text", "")).strip()
                    if message_id is not None and new_text:
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

            pending.sent_message_id = sent_message_id
            pending.state = MessageState.SENT
            pending.last_error = None
            pending.current_action_index = None
            pending.current_action_total = None
            pending.current_action_label = None
            pending.current_action_until = None
            self._notify_pending_changed(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pending.state = MessageState.ERROR
            pending.last_error = str(exc)
            pending.current_action_index = None
            pending.current_action_total = None
            pending.current_action_label = None
            pending.current_action_until = None
            self._notify_pending_changed(chat_id)
        finally:
            self._skip_current_action.discard(int(chat_id))
            task = self._chat_tasks.get(chat_id)
            if task is asyncio.current_task():
                self._chat_tasks.pop(chat_id, None)

    async def abort_send(self, chat_id: int, trigger: str = "manual-stop") -> str:
        pending = self._pending_messages.get(chat_id)
        if not pending:
            return "Kein Nachrichtenprozess für diesen Chat gefunden."
        self._skip_current_action.discard(int(chat_id))

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
        # Only fingerprint incoming user messages. Own outgoing messages and
        # queue progress should not retrigger model generation by themselves.
        joined = "\n".join(
            f"{msg.timestamp.isoformat()}|{msg.role}|{msg.sender}|{msg.text}"
            for msg in context.messages
            if msg.role == "user"
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def _is_generation_allowed_for_chat(self, chat_id: int) -> bool:
        pending = self._pending_messages.get(chat_id)
        if not pending:
            return True
        # While a queue is active (or explicitly escalated), do not regenerate.
        if pending.state in {
            MessageState.GENERATING,
            MessageState.WAITING,
            MessageState.SENDING_TYPING,
            MessageState.ESCALATED,
        }:
            return False
        return True

    @staticmethod
    def _seconds_until_utc(send_at_utc: str) -> float:
        try:
            dt = datetime.fromisoformat(send_at_utc.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (dt.astimezone(timezone.utc) - now).total_seconds())


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
