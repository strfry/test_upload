from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telethon import TelegramClient

_log = logging.getLogger(__name__)


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

    async def start_listener(
        self,
        store: Any,
        service: Any,
        folder_name: str = "Scammers",
    ) -> None:
        """Register Live Mode event handlers for auto-receive and typing monitoring.

        Incoming messages from chats in *folder_name* are stored immediately and
        trigger response generation.  If the folder cannot be resolved, all
        incoming messages are accepted.
        """
        from telethon import events
        from telethon.tl.functions.messages import GetDialogFiltersRequest
        from telethon.tl.types import DialogFilter

        folder_chat_ids: list[int] = []
        if folder_name:
            try:
                filters_result = await self._client(GetDialogFiltersRequest())
                for item in getattr(filters_result, "filters", filters_result):
                    if not isinstance(item, DialogFilter):
                        continue
                    title = getattr(item, "title", None)
                    if isinstance(title, str):
                        name = title
                    else:
                        # TextWithEntities or similar — fall back to str()
                        name = str(title) if title is not None else ""
                    if name != folder_name:
                        continue
                    folder_id = int(item.id)
                    try:
                        async for dialog in self._client.iter_dialogs(limit=None, folder=folder_id):
                            folder_chat_ids.append(int(dialog.id))
                    except Exception:
                        # Fall back to include_peers list
                        include_peers = getattr(item, "include_peers", None) or []
                        from telethon import utils as tl_utils
                        for peer in include_peers:
                            try:
                                folder_chat_ids.append(int(tl_utils.get_peer_id(peer)))
                            except Exception:
                                pass
                    break
            except Exception as exc:
                _log.warning("start_listener: could not resolve folder %r: %s", folder_name, exc)

        _folder_set: set[int] = set(folder_chat_ids)
        _log.info(
            "start_listener: Live Mode active, folder=%r, %d chats tracked",
            folder_name,
            len(_folder_set),
        )

        @self._client.on(events.NewMessage(incoming=True))
        async def _on_new_message(event: Any) -> None:
            chat_id = int(event.chat_id)
            if _folder_set and chat_id not in _folder_set:
                return
            msg = event.message
            ts = (
                msg.date.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                if msg.date
                else None
            )
            if getattr(msg, "sticker", None):
                event_type = "sticker"
                text: str | None = None
            elif getattr(msg, "photo", None):
                event_type = "photo"
                text = msg.message or None
            else:
                event_type = "message"
                text = msg.message or None
            try:
                store.ingest_event(
                    chat_id=chat_id,
                    event_type=event_type,
                    role="scammer",
                    text=text,
                    ts_utc=ts,
                    source_message_id=str(msg.id),
                )
            except Exception as exc:
                _log.debug("start_listener: ingest_event skipped for %d/%d: %s", chat_id, msg.id, exc)
                return
            asyncio.create_task(service.trigger_for_chat(chat_id, trigger="live_message"))

        @self._client.on(events.UserUpdate())
        async def _on_user_update(event: Any) -> None:
            if not getattr(event, "typing", False):
                return
            try:
                chat_id = int(event.chat_id)
            except Exception:
                return
            if _folder_set and chat_id not in _folder_set:
                return
            ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            try:
                store.ingest_event(
                    chat_id=chat_id,
                    event_type="typing_interval",
                    role="scammer",
                    ts_utc=ts,
                )
            except Exception as exc:
                _log.debug("start_listener: typing ingest skipped for %d: %s", chat_id, exc)

    async def fetch_profile(self, chat_id: int, store: Any) -> None:
        """Fetch profile metadata from Telegram and persist it to the store.

        Stores username, first/last name, bio and profile photo presence in
        ``chat_profiles`` via ``store.upsert_chat_profile()``.
        """
        try:
            entity = await self._client.get_entity(chat_id)
        except Exception as exc:
            _log.warning("fetch_profile: get_entity failed for %d: %s", chat_id, exc)
            return

        identity: dict[str, Any] = {}
        for attr in ("first_name", "last_name", "username"):
            val = getattr(entity, attr, None)
            if val:
                identity[attr] = str(val)
        bio = getattr(entity, "about", None)
        if bio:
            identity["bio"] = str(bio)

        patch: dict[str, Any] = {}
        if identity:
            patch["identity"] = identity

        try:
            photos = await self._client.get_profile_photos(entity, limit=1)
            patch["profile_media"] = {"has_profile_photo": bool(photos)}
            if photos:
                patch["profile_media"]["current_photo_telegram_id"] = int(photos[0].id)
        except Exception as exc:
            _log.debug("fetch_profile: photo fetch failed for %d: %s", chat_id, exc)

        if patch:
            store.upsert_chat_profile(chat_id=chat_id, patch=patch, source="telethon")
