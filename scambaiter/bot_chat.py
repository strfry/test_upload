"""Chat management and user cards — rendering, profile extraction, history formatting."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _render_user_card(
    target_chat_id: int,
    event_count: int,
    last_preview: str | None,
    profile_lines: list[str],
) -> str:
    preview = last_preview or "-"
    profile_block = "\n".join(profile_lines) if profile_lines else "profile: unavailable"
    return (
        "Chat Card\n"
        f"chat_id: /{target_chat_id}\n"
        f"events: {event_count}\n"
        f"{profile_block}\n"
        f"last: {preview}"
    )


def _chat_card_keyboard(
    target_chat_id: int,
    live_mode: bool = False,
    auto_send_on: bool = False,
    waiting_phase: str | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Prompt", callback_data=f"sc:prompt:{target_chat_id}")],
    ]
    if live_mode:
        rows.append([
            InlineKeyboardButton("Fetch Profile", callback_data=f"sc:fetch_profile:{target_chat_id}"),
            InlineKeyboardButton("Fetch History", callback_data=f"sc:fetch_history:{target_chat_id}"),
        ])
        auto_label = "Auto-Send: ON" if auto_send_on else "Auto-Send: OFF"
        rows.append([
            InlineKeyboardButton(auto_label, callback_data=f"sc:autosend_toggle:{target_chat_id}")
        ])
        if waiting_phase is not None:
            phase_label = "Lesen" if waiting_phase == "reading" else "Tippen"
            rows.append([InlineKeyboardButton(
                f"⏭ Überspringen ({phase_label})",
                callback_data=f"sc:autosend_skip:{target_chat_id}"
            )])
    rows.append([InlineKeyboardButton("Close", callback_data=f"sc:chat_close:{target_chat_id}")])
    return InlineKeyboardMarkup(rows)


def _truncate_chat_button_label(base: str, chat_id: int, max_len: int = 56) -> str:
    suffix = f" · /{chat_id}"
    compact = " ".join((base or "").split()).strip()
    if not compact:
        compact = "Unknown"
    full = f"{compact}{suffix}"
    if len(full) <= max_len:
        return full
    remaining = max_len - len(suffix)
    if remaining <= 4:
        return f"/{chat_id}"
    return f"{compact[: remaining - 3]}...{suffix}"


def _chat_button_label(store: Any, chat_id: int) -> str:
    display_name: str | None = None
    username: str | None = None
    try:
        profile = store.get_chat_profile(chat_id=chat_id)
    except Exception:
        profile = None
    if profile is not None:
        snapshot = getattr(profile, "snapshot", None)
        if isinstance(snapshot, dict):
            identity = snapshot.get("identity")
            if isinstance(identity, dict):
                candidate_display = identity.get("display_name")
                if isinstance(candidate_display, str) and candidate_display.strip():
                    display_name = candidate_display.strip()
                candidate_username = identity.get("username")
                if isinstance(candidate_username, str) and candidate_username.strip():
                    value = candidate_username.strip()
                    username = value if value.startswith("@") else f"@{value}"
    if display_name:
        base = display_name
        if username:
            base = f"{display_name} ({username})"
    elif username:
        base = username
    else:
        base = "Unknown"
    return _truncate_chat_button_label(base, chat_id)


def _known_chats_keyboard(store: Any, chat_ids: list[int], max_buttons: int = 30) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in chat_ids[:max_buttons]:
        rows.append([InlineKeyboardButton(_chat_button_label(store, item), callback_data=f"sc:selchat:{item}")])
    return InlineKeyboardMarkup(rows)


def _known_chats_card_content(store: Any, chat_ids: list[int]) -> tuple[str, InlineKeyboardMarkup]:
    shown = chat_ids[:30]
    extra = len(chat_ids) - len(shown)
    title = f"Known chat ids ({len(chat_ids)} total):\nSelect one:"
    if extra > 0:
        title += f"\n(showing first {len(shown)}, {extra} hidden)"
    return title, _known_chats_keyboard(store, chat_ids)


def _chat_card_clear_confirm_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm Delete", callback_data=f"sc:clear_history_confirm:{target_chat_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"sc:clear_history_cancel:{target_chat_id}"),
            ]
        ]
    )


def _chat_card_clear_safety_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("I Understand, Continue", callback_data=f"sc:clear_history_arm:{target_chat_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"sc:clear_history_cancel:{target_chat_id}"),
            ]
        ]
    )


def _profile_lines_from_events(events: list[Any]) -> list[str]:
    sender_user: dict[str, Any] = {}
    sender_chat: dict[str, Any] = {}
    sender_user_name: str | None = None

    def _consume_profile(event: Any) -> None:
        nonlocal sender_user, sender_chat, sender_user_name
        meta = getattr(event, "meta", None)
        if not isinstance(meta, dict):
            return
        forward_profile = meta.get("forward_profile")
        if not isinstance(forward_profile, dict):
            return
        if not sender_user:
            candidate = forward_profile.get("sender_user")
            if isinstance(candidate, dict):
                sender_user = candidate
        if not sender_chat:
            candidate = forward_profile.get("sender_chat")
            if isinstance(candidate, dict):
                sender_chat = candidate
        if sender_user_name is None:
            candidate_name = forward_profile.get("sender_user_name")
            if isinstance(candidate_name, str) and candidate_name.strip():
                sender_user_name = candidate_name.strip()

    # Prefer identity from scammer-side events to avoid showing operator profile as chat contact.
    scammer_events = [event for event in events if getattr(event, "role", None) == "scammer"]
    for event in scammer_events:
        _consume_profile(event)
        if sender_user or sender_chat or sender_user_name:
            break
    if not (sender_user or sender_chat or sender_user_name):
        for event in events:
            _consume_profile(event)
            if sender_user or sender_chat or sender_user_name:
                break
    lines: list[str] = []
    display_name = None
    if sender_chat:
        title = sender_chat.get("title")
        if isinstance(title, str) and title.strip():
            display_name = title.strip()
    if display_name is None and sender_user_name:
        display_name = sender_user_name
    if display_name is None and sender_user:
        first = sender_user.get("first_name")
        last = sender_user.get("last_name")
        parts: list[str] = []
        if isinstance(first, str) and first.strip():
            parts.append(first.strip())
        if isinstance(last, str) and last.strip():
            parts.append(last.strip())
        if parts:
            display_name = " ".join(parts)
    lines.append(f"display_name: {display_name or 'unknown'}")
    username = None
    if sender_user:
        user_name = sender_user.get("username")
        if isinstance(user_name, str) and user_name.strip():
            username = "@" + user_name.strip()
    if username is None and sender_chat:
        chat_username = sender_chat.get("username")
        if isinstance(chat_username, str) and chat_username.strip():
            username = "@" + chat_username.strip()
    lines.append(f"username: {username or 'unknown'}")
    lines.append(
        "origin_type: "
        + ("sender_user" if sender_user else "sender_chat" if sender_chat else "unknown")
    )
    lines.append("profile_photos: unknown (not exposed by BotAPI forward metadata)")
    lines.append("bio: unknown (not exposed by BotAPI forward metadata)")
    return lines


def _profile_lines_from_stored_profile(snapshot: dict[str, Any]) -> list[str]:
    identity = snapshot.get("identity") or {}
    first = identity.get("first_name", "")
    last = identity.get("last_name", "")
    display_name = " ".join(p for p in [first, last] if p).strip() or None
    username = identity.get("username")
    bio = identity.get("bio")
    profile_media = snapshot.get("profile_media") or {}
    has_photo = profile_media.get("has_profile_photo")
    return [
        f"display_name: {display_name or 'unknown'}",
        f"username: {'@' + username if username else 'unknown'}",
        f"bio: {bio if bio else 'unknown'}",
        f"profile_photo: {'yes' if has_photo else ('no' if has_photo is False else 'unknown')}",
        "source: telethon",
    ]


def _sanitize_legacy_profile_text(text: str) -> str:
    if text.startswith("profile_update:") and text.endswith("(botapi_forward)"):
        return text[: -len("(botapi_forward)")].rstrip()
    return text


def _format_history_line(event: Any) -> str:
    ts = getattr(event, "ts_utc", None)
    hhmm = "--:--"
    if isinstance(ts, str) and ts:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hhmm = parsed.astimezone().strftime("%H:%M")
        except ValueError:
            if len(ts) >= 16:
                hhmm = ts[11:16]
    role = getattr(event, "role", "unknown")
    event_type = getattr(event, "event_type", "unknown")
    text = getattr(event, "text", None)
    if not text:
        return f"{hhmm} {role}/{event_type}"
    normalized_text = _sanitize_legacy_profile_text(str(text))
    flat_text = " ".join(normalized_text.split())
    if len(flat_text) > 120:
        flat_text = flat_text[:117] + "..."
    return f"{hhmm} {role}/{event_type}: {flat_text}"


def _render_whoami_text(message: Any, user_id: int | None, allowed_chat_id: int | None) -> str:
    chat_id = int(message.chat_id)
    authorized = allowed_chat_id is None or chat_id == int(allowed_chat_id)
    expected = str(allowed_chat_id) if allowed_chat_id is not None else "(not set)"
    return (
        "Control identity\n"
        f"chat_id: {chat_id}\n"
        f"user_id: {user_id if user_id is not None else 'unknown'}\n"
        f"allowed_chat_id: {expected}\n"
        f"authorized_here: {'yes' if authorized else 'no'}"
    )
