from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from scambaiter.core import ScambaiterCore, SuggestionResult


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: datetime
    chat_count: int
    sent_count: int


class BackgroundService:
    def __init__(self, core: ScambaiterCore, interval_seconds: int) -> None:
        self.core = core
        self.interval_seconds = max(15, interval_seconds)
        self.auto_enabled = False
        self._loop_task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self.last_summary: RunSummary | None = None
        self.last_results: list[SuggestionResult] = []

    async def run_once(self) -> RunSummary:
        async with self._run_lock:
            started = datetime.now()
            sent_count = 0
            folder_chat_ids = await self.core.get_folder_chat_ids()
            contexts = await self.core.collect_unanswered_chats(folder_chat_ids)
            results: list[SuggestionResult] = []

            for context in contexts:
                suggestion = self.core.generate_suggestion(context)
                results.append(SuggestionResult(context=context, suggestion=suggestion))
                if await self.core.maybe_send_suggestion(context, suggestion):
                    sent_count += 1

            summary = RunSummary(
                started_at=started,
                finished_at=datetime.now(),
                chat_count=len(contexts),
                sent_count=sent_count,
            )
            self.last_results = results
            self.last_summary = summary
            return summary

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
