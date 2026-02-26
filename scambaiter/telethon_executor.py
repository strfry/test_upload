from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telethon import TelegramClient

from .model_client import call_hf_vision

_log = logging.getLogger(__name__)

_VISION_PROMPT = (
    "Describe this image/document in full detail:\n"
    "1. Document type (passport, ID card, bank statement, certificate, personal photo, etc.)\n"
    "2. All readable text (complete OCR): names, numbers, addresses, dates, IDs, account numbers\n"
    "3. All facts the document is meant to prove or establish\n"
    "4. Visual assessment: does it look authentic? Any visible signs of editing or fakery?\n"
    "Be as thorough and specific as possible."
)


async def _wait_or_skip(duration: float, skip_event: asyncio.Event | None) -> None:
    """Wait for duration or until skip_event is set, whichever comes first."""
    if skip_event is None:
        await asyncio.sleep(duration)
    else:
        try:
            await asyncio.wait_for(asyncio.shield(skip_event.wait()), timeout=duration)
            skip_event.clear()
        except asyncio.TimeoutError:
            pass


def _segment_text_for_typing(text: str) -> list[tuple[float, float]]:
    """
    Split text into (typing_duration, pause_after) segments for realistic typing simulation.

    Segments are created at sentence boundaries (after . ! ? … or newlines).
    Short segments (<25 chars) are merged with the next one. Max 5 segments allowed.
    Typing duration follows 150 chars/min rate (2.5 chars/sec).
    Pause durations vary by ending character: . → 2.5s, ! → 2.0s, ? → 2.2s, …/\n → 3.0s, else → 2.0s.
    Last segment always has pause_after = 0.0.

    Returns list of (typing_duration, pause_after) tuples.
    """
    if not text:
        return []

    if len(text) < 2:
        return [(max(0.5, len(text) / 2.5), 0.0)]

    # Pattern: match sentence boundaries where we split
    # After .!?… when followed by whitespace, or after newlines
    split_pattern = r'(?<=[.!?…])\s+|(?<=\n)'

    # Find split positions to preserve ending character context
    split_positions = [m.start() for m in re.finditer(split_pattern, text)]

    if not split_positions:
        # No sentence breaks found, treat whole text as one segment
        return [(max(0.5, len(text) / 2.5), 0.0)]

    # Build segments preserving what character ended each one
    segments: list[tuple[str, str]] = []  # (text, ending_char)
    prev = 0
    for pos in split_positions:
        segment = text[prev:pos].strip()
        # The ending character is right before the whitespace we're splitting on
        ending_char = text[pos - 1] if pos > 0 else ''
        if segment:
            segments.append((segment, ending_char))
        prev = pos

    # Add final segment
    final = text[prev:].strip()
    if final:
        final_ending = final.rstrip()[-1] if final.rstrip() else ''
        segments.append((final, final_ending))

    if not segments:
        return [(max(0.5, len(text) / 2.5), 0.0)]

    # Merge short segments (<25 chars) with the next one
    merged: list[tuple[str, str]] = []
    i = 0
    while i < len(segments):
        current_text, current_end = segments[i]
        # Keep merging with next while current is short
        while i < len(segments) - 1 and len(current_text) < 25:
            next_text, next_end = segments[i + 1]
            current_text = current_text + ' ' + next_text
            current_end = next_end
            i += 1
        merged.append((current_text, current_end))
        i += 1

    # Limit to max 5 segments by grouping longer texts
    if len(merged) > 5:
        chunk_size = len(merged) / 5.0
        new_merged: list[tuple[str, str]] = []
        for chunk_idx in range(5):
            start_idx = int(chunk_idx * chunk_size)
            end_idx = int((chunk_idx + 1) * chunk_size) if chunk_idx < 4 else len(merged)
            combined_text = ' '.join(seg[0] for seg in merged[start_idx:end_idx])
            combined_end = merged[end_idx - 1][1] if end_idx > 0 else ''
            if combined_text.strip():
                new_merged.append((combined_text, combined_end))
        merged = new_merged

    # Create final result with (typing_duration, pause_after) tuples
    result: list[tuple[float, float]] = []
    for i, (segment, ending_char) in enumerate(merged):
        # Typing duration: 150 chars/min = 2.5 chars/sec
        typing_duration = max(0.5, len(segment) / 2.5)

        # Last segment never pauses
        if i == len(merged) - 1:
            pause_after = 0.0
        else:
            # Pause duration based on sentence-ending character
            if ending_char == '.':
                pause_after = 2.5
            elif ending_char == '!':
                pause_after = 2.0
            elif ending_char == '?':
                pause_after = 2.2
            elif ending_char in ('…', '\n'):
                pause_after = 3.0
            else:
                pause_after = 2.0

        result.append((typing_duration, pause_after))

    return result


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

    async def mark_read(self, chat_id: int) -> None:
        entity = await self._client.get_entity(chat_id)
        await self._client.send_read_acknowledge(entity)

    async def simulate_typing_for(
        self, chat_id: int, duration: float, skip_event: asyncio.Event | None = None
    ) -> None:
        entity = await self._client.get_entity(chat_id)
        async with self._client.action(entity, "typing"):
            if skip_event is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(skip_event.wait()), timeout=duration)
                    skip_event.clear()
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(duration)

    async def simulate_typing_with_pauses(
        self, chat_id: int, message_text: str, skip_event: asyncio.Event | None = None
    ) -> None:
        """Simulate realistic typing with pauses between sentences."""
        segments = _segment_text_for_typing(message_text)
        entity = await self._client.get_entity(chat_id)

        for typing_dur, pause_after in segments:
            async with self._client.action(entity, "typing"):
                await _wait_or_skip(typing_dur, skip_event)
            if pause_after > 0:
                await _wait_or_skip(pause_after, skip_event)

    async def describe_photo(self, config: Any, media: Any) -> str | None:
        """Download and describe a photo using a vision model.

        Args:
            config: Config object with hf_vision_model, hf_token, hf_base_url
            media: Telethon media object (msg.photo, msg.document, etc.)

        Returns:
            Vision model's description of the image, or None if disabled/failed.
        """
        vision_model = getattr(config, "hf_vision_model", None)
        if not vision_model:
            return None
        try:
            img_bytes: bytes = await self._client.download_media(media, bytes)
            token = (getattr(config, "hf_token", None) or "").strip()
            base_url = getattr(config, "hf_base_url", None)
            loop = asyncio.get_event_loop()
            description = await loop.run_in_executor(
                None,
                lambda: call_hf_vision(
                    token=token,
                    model=vision_model,
                    image_bytes=img_bytes,
                    prompt=_VISION_PROMPT,
                    base_url=base_url,
                    max_tokens=800,
                ),
            )
            return description
        except Exception as exc:
            _log.warning("describe_photo failed: %s", exc)
            return None

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        entity = await self._client.get_entity(chat_id)
        await self._client.delete_messages(entity, message_ids=[message_id])

    async def execute_actions(
        self, chat_id: int, parsed_output: dict[str, Any], skip_event: asyncio.Event | None = None
    ) -> ExecutionReport:
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
                    await _wait_or_skip(seconds, skip_event)
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

    async def _resolve_folder_ids(self, folder_name: str) -> set[int]:
        """Return the set of chat IDs in the named Telegram folder, or empty set on failure."""
        from telethon.tl.functions.messages import GetDialogFiltersRequest
        from telethon.tl.types import DialogFilter

        folder_chat_ids: list[int] = []
        try:
            filters_result = await self._client(GetDialogFiltersRequest())
            filters_list = getattr(filters_result, "filters", filters_result)

            for item in filters_list:
                if not isinstance(item, DialogFilter):
                    continue
                title = getattr(item, "title", None)
                # Handle TextWithEntities (Telegram returns title as TextWithEntities, not plain string)
                if hasattr(title, "text"):
                    name = title.text
                elif isinstance(title, str):
                    name = title
                else:
                    name = ""
                print(f"[DEBUG] Filter found: name={name!r}, looking for {folder_name!r}")
                if name != folder_name:
                    continue
                # Extract chat IDs directly from include_peers in the filter
                include_peers = getattr(item, "include_peers", None) or []
                from telethon import utils as tl_utils
                for peer in include_peers:
                    try:
                        chat_id = int(tl_utils.get_peer_id(peer))
                        print("Adding chat", chat_id, "to list")
                        folder_chat_ids.append(chat_id)
                    except Exception as exc:
                        _log.warning("_resolve_folder_ids: failed to extract peer: %s", exc)
                _log.info("_resolve_folder_ids: found %d chats in folder from include_peers", len(folder_chat_ids))
                break
        except Exception as exc:
            _log.error("_resolve_folder_ids: GetDialogFiltersRequest failed: %s", exc)

        return set(folder_chat_ids)

    async def start_listener(
        self,
        store: Any,
        service: Any,
        config: Any,
        folder_name: str = "Scammers",
    ) -> None:
        """Register Live Mode event handlers for auto-receive and typing monitoring.

        Incoming messages from chats in *folder_name* are stored immediately and
        trigger response generation. If the folder cannot be resolved or is empty,
        raises RuntimeError and stops the bot.
        """
        from telethon import events

        _folder_set = await self._resolve_folder_ids(folder_name) if folder_name else set()
        if folder_name and not _folder_set:
            raise RuntimeError(
                f"Folder '{folder_name}' not found or contains no chats. "
                "Cannot start Live Mode without valid scammer folder."
            )
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
                description: str | None = None
            elif getattr(msg, "photo", None):
                event_type = "photo"
                text = msg.message or None
                description = await self.describe_photo(config, msg.photo)
            else:
                event_type = "message"
                text = msg.message or None
                description = None
            try:
                store.ingest_event(
                    chat_id=chat_id,
                    event_type=event_type,
                    role="scammer",
                    text=text,
                    description=description,
                    ts_utc=ts,
                    source_message_id=str(msg.id),
                )
            except Exception as exc:
                _log.debug("start_listener: ingest_event skipped for %d/%d: %s", chat_id, msg.id, exc)
                return
            asyncio.create_task(service.trigger_for_chat(chat_id, trigger="live_message"))
            if store.get_chat_profile(chat_id) is None:
                asyncio.create_task(self.fetch_profile(chat_id, store))

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

    async def startup_backfill(
        self,
        store: Any,
        config: Any,
        folder_name: str = "Scammers",
        history_limit: int = 200,
    ) -> None:
        """Fetch history and profile for all chats in the folder on startup.

        Runs as a background task (non-blocking). Skips chats where history is
        already present (deduplication handled by store's unique index).

        Raises RuntimeError if folder cannot be resolved or contains no chats.
        """
        folder_ids = await self._resolve_folder_ids(folder_name)
        if not folder_ids:
            raise RuntimeError(
                f"Folder '{folder_name}' not found or contains no chats. "
                "Cannot backfill history without valid scammer folder."
            )
        _log.info("startup_backfill: backfilling %d chats", len(folder_ids))
        for chat_id in folder_ids:
            try:
                count = await self.fetch_history(chat_id, store, config, limit=history_limit)
                _log.info("startup_backfill: chat %d — %d new events", chat_id, count)
            except Exception as exc:
                _log.warning("startup_backfill: fetch_history failed for %d: %s", chat_id, exc)
            try:
                await self.fetch_profile(chat_id, store)
            except Exception as exc:
                _log.warning("startup_backfill: fetch_profile failed for %d: %s", chat_id, exc)

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

    async def fetch_history(self, chat_id: int, store: Any, config: Any, limit: int | None = 200) -> int:
        """Fetch the most recent messages from a Telegram chat and ingest into the store.

        Uses msg.out to determine role: outgoing → "scambaiter", incoming → "scammer".
        Deduplication is handled by the store's unique index on (chat_id, source_message_id).
        For photo events, calls vision model to describe images (backfill or initial).
        Returns the count of newly ingested events.
        """
        entity = await self._client.get_entity(chat_id)
        count = 0
        # Collect all messages first to insert them in chronological order (oldest→newest)
        messages = []
        async for msg in self._client.iter_messages(entity, limit=limit):
            messages.append(msg)
        # Reverse to insert oldest first (so IDs increase chronologically)
        for msg in reversed(messages):
            source_message_id = str(msg.id)
            role = "scambaiter" if msg.out else "scammer"
            if getattr(msg, "sticker", None):
                event_type = "sticker"
                text = None
                description = None
            elif getattr(msg, "photo", None):
                event_type = "photo"
                text = msg.message or None
                # Try to look up existing event first (for backfill)
                existing = store._get_event_by_source(chat_id, source_message_id)
                if existing is not None:
                    # Event already in DB — backfill description if missing
                    if existing.description is None:
                        desc = await self.describe_photo(config, msg.photo)
                        if desc is not None:
                            store.update_event_description(existing.id, desc)
                            store.reset_summary_cursor_if_before(chat_id, existing.id)
                    continue  # no new insert needed
                description = await self.describe_photo(config, msg.photo)
            else:
                event_type = "message"
                text = msg.message or None
                description = None
            ts = (
                msg.date.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                if msg.date else None
            )
            try:
                store.ingest_event(
                    chat_id=chat_id,
                    event_type=event_type,
                    role=role,
                    text=text,
                    description=description,
                    ts_utc=ts,
                    source_message_id=source_message_id,
                )
                count += 1
            except Exception:
                pass  # duplicate or invalid — skip silently
        return count
