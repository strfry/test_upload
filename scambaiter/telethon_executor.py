from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telethon import TelegramClient


@dataclass(slots=True)
class ExecutionReport:
    ok: bool
    sent_message_id: int | None = None
    executed_actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failed_action_index: int | None = None


class TelethonExecutor:
    def __init__(self, api_id: int, api_hash: str, session: str = "scambaiter.session") -> None:
        self._client = TelegramClient(session, api_id, api_hash)
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._client.start()
        self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        await self._client.disconnect()
        self._started = False

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        entity = await self._client.get_entity(chat_id)
        await self._client.delete_messages(entity, message_ids=[message_id])

    async def execute_actions(self, chat_id: int, parsed_output: dict[str, Any]) -> ExecutionReport:
        report = ExecutionReport(ok=True)
        message_obj = parsed_output.get("message") if isinstance(parsed_output.get("message"), dict) else {}
        fallback_text = str(message_obj.get("text") or "").strip()
        actions = parsed_output.get("actions") if isinstance(parsed_output.get("actions"), list) else []
        entity = await self._client.get_entity(chat_id)

        for idx, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                report.ok = False
                report.failed_action_index = idx
                report.errors.append(f"action #{idx}: invalid payload")
                break
            action_type = str(action.get("type") or "").strip()
            try:
                if action_type == "simulate_typing":
                    duration = float(action.get("duration_seconds") or 0)
                    duration = max(0.0, min(duration, 60.0))
                    async with self._client.action(entity, "typing"):
                        await asyncio.sleep(duration)
                    report.executed_actions.append(f"{idx}. simulate_typing({duration:.1f}s)")
                    continue

                if action_type == "wait":
                    value = float(action.get("value") or 0)
                    unit = str(action.get("unit") or "seconds").strip().lower()
                    if unit == "minutes":
                        seconds = value * 60.0
                    else:
                        seconds = value
                    seconds = max(0.0, seconds)
                    await asyncio.sleep(seconds)
                    report.executed_actions.append(f"{idx}. wait({seconds:.1f}s)")
                    continue

                if action_type == "send_message":
                    action_message = action.get("message") if isinstance(action.get("message"), dict) else {}
                    text = str(action_message.get("text") or fallback_text).strip()
                    if not text:
                        raise RuntimeError("send_message has empty text")
                    send_at_utc = action.get("send_at_utc")
                    if isinstance(send_at_utc, str) and send_at_utc.strip():
                        target = datetime.fromisoformat(send_at_utc.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        wait_seconds = (target.astimezone(timezone.utc) - now).total_seconds()
                        if wait_seconds > 0:
                            await asyncio.sleep(wait_seconds)
                    kwargs: dict[str, Any] = {}
                    reply_to = action.get("reply_to")
                    if isinstance(reply_to, int):
                        kwargs["reply_to"] = reply_to
                    sent = await self._client.send_message(entity, text, **kwargs)
                    report.sent_message_id = int(getattr(sent, "id", 0) or 0) or report.sent_message_id
                    report.executed_actions.append(f"{idx}. send_message")
                    continue

                if action_type == "edit_message":
                    message_id = action.get("message_id")
                    new_text = str(action.get("new_text") or "")
                    if not isinstance(message_id, (str, int)):
                        raise RuntimeError("edit_message missing message_id")
                    await self._client.edit_message(entity, int(message_id), new_text)
                    report.executed_actions.append(f"{idx}. edit_message({message_id})")
                    continue

                if action_type == "delete_message":
                    message_id = action.get("message_id")
                    if not isinstance(message_id, (str, int)):
                        raise RuntimeError("delete_message missing message_id")
                    await self._client.delete_messages(entity, message_ids=[int(message_id)])
                    report.executed_actions.append(f"{idx}. delete_message({message_id})")
                    continue

                if action_type == "mark_read":
                    await self._client.send_read_acknowledge(entity)
                    report.executed_actions.append(f"{idx}. mark_read")
                    continue

                if action_type == "escalate_to_human":
                    report.executed_actions.append(f"{idx}. escalate_to_human(noop)")
                    continue

                if action_type == "noop":
                    report.executed_actions.append(f"{idx}. noop")
                    continue

                raise RuntimeError(f"unsupported action type: {action_type}")
            except Exception as exc:  # pragma: no cover - integration behavior
                report.ok = False
                report.failed_action_index = idx
                report.errors.append(f"action #{idx} ({action_type or 'unknown'}): {exc}")
                break

        return report
