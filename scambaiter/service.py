from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime

from scambaiter.core import ChatContext, ScambaiterCore, SuggestionResult
from scambaiter.storage import AnalysisStore


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: datetime
    chat_count: int
    sent_count: int


class BackgroundService:
    def __init__(self, core: ScambaiterCore, interval_seconds: int, store: AnalysisStore | None = None) -> None:
        self.core = core
        self.store = store
        self.interval_seconds = max(15, interval_seconds)
        self.auto_enabled = False
        self._loop_task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self.last_summary: RunSummary | None = None
        self.last_results: list[SuggestionResult] = []
        self._context_fingerprints: dict[int, str] = {}

    async def run_once(self, target_chat_ids: set[int] | None = None) -> RunSummary:
        async with self._run_lock:
            started = datetime.now()
            sent_count = 0
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

            results: list[SuggestionResult] = []

            for context in process_contexts:
                language_hint = None
                if self.store:
                    lang_item = self.store.kv_get(context.chat_id, "sprache")
                    if lang_item:
                        language_hint = lang_item.value

                output = self.core.generate_output(context, language_hint=language_hint)
                results.append(
                    SuggestionResult(
                        context=context,
                        suggestion=output.suggestion,
                        analysis=output.analysis,
                        metadata=output.metadata,
                    )
                )
                if self.store:
                    self.store.save(
                        chat_id=context.chat_id,
                        title=context.title,
                        suggestion=output.suggestion,
                        analysis=output.analysis,
                        metadata=output.metadata,
                    )
                if await self.core.maybe_send_suggestion(context, output.suggestion):
                    sent_count += 1

            summary = RunSummary(
                started_at=started,
                finished_at=datetime.now(),
                chat_count=len(process_contexts),
                sent_count=sent_count,
            )
            self.last_results = results
            self.last_summary = summary
            return summary


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

    async def start_auto(self) -> None:
        if self.auto_enabled:
            return
        self.auto_enabled = True
        self._loop_task = asyncio.create_task(self._loop())

    async def stop_auto(self) -> None:
        self.auto_enabled = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def _loop(self) -> None:
        while self.auto_enabled:
            try:
                await self.run_once()
            except Exception as exc:
                print(f"[ERROR] Auto-Lauf fehlgeschlagen: {exc}")
            await asyncio.sleep(self.interval_seconds)
