"""Forward ingestion pipeline — message parsing, identity extraction, merge planning."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message


def _is_forward_message(message: Message) -> bool:
    return bool(getattr(message, "forward_origin", None))


def _infer_event_type(message: Message) -> str:
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "text", None) or getattr(message, "caption", None):
        return "message"
    return "forward"


def _extract_text(message: Message) -> str | None:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    caption = getattr(message, "caption", None)
    if isinstance(caption, str) and caption.strip():
        return caption.strip()
    return None


def _extract_origin_message_id(message: Message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    value = getattr(origin, "message_id", None)
    return value if isinstance(value, int) else None


def _build_source_message_id(forward_identity_key: str, strategy: str, event_type: str, text: str | None) -> str:
    key_digest = hashlib.sha1(forward_identity_key.encode("utf-8")).hexdigest()[:16]
    raw = text or ""
    text_digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"fwd:v2:{strategy}:{key_digest}:{event_type}:{text_digest}"


def _extract_forward_profile_info(message: Message) -> dict[str, Any]:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return {}
    info: dict[str, Any] = {"origin_kind": type(origin).__name__}
    origin_date = getattr(origin, "date", None)
    if isinstance(origin_date, datetime):
        info["origin_date_utc"] = origin_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    origin_message_id = getattr(origin, "message_id", None)
    if isinstance(origin_message_id, int):
        info["origin_message_id"] = origin_message_id
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        user_info: dict[str, Any] = {}
        for field in ("id", "username", "first_name", "last_name", "language_code", "is_bot"):
            value = getattr(sender_user, field, None)
            if value not in (None, ""):
                user_info[field] = value
        if user_info:
            info["sender_user"] = user_info
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat is None:
        # MessageOriginChannel exposes "chat" instead of "sender_chat".
        sender_chat = getattr(origin, "chat", None)
    if sender_chat is not None:
        chat_info: dict[str, Any] = {}
        for field in ("id", "type", "title", "username"):
            value = getattr(sender_chat, field, None)
            if value not in (None, ""):
                chat_info[field] = value
        if chat_info:
            info["sender_chat"] = chat_info
    for field in ("sender_user_name",):
        value = getattr(origin, field, None)
        if value not in (None, ""):
            info[field] = value
    return info


def _extract_forward_identity(
    *,
    origin: Any,
    forward_profile: dict[str, Any],
    event_type: str,
    text: str | None,
    message: Message,
) -> dict[str, Any]:
    origin_kind = str(forward_profile.get("origin_kind") or type(origin).__name__)
    origin_message_id = getattr(origin, "message_id", None)
    if isinstance(origin_message_id, int):
        sender_chat = getattr(origin, "chat", None)
        if sender_chat is None:
            sender_chat = getattr(origin, "sender_chat", None)
        sender_chat_id = getattr(sender_chat, "id", None) if sender_chat is not None else None
        if isinstance(sender_chat_id, int):
            key = f"channel:{sender_chat_id}:{origin_message_id}"
            return {"strategy": "channel_message_id", "key": key, "origin_kind": origin_kind}
    origin_date_utc = str(forward_profile.get("origin_date_utc") or "")
    sender_user = forward_profile.get("sender_user")
    sender_chat = forward_profile.get("sender_chat")
    sender_user_name = str(forward_profile.get("sender_user_name") or "")
    sender_user_id = sender_user.get("id") if isinstance(sender_user, dict) else None
    sender_chat_id = sender_chat.get("id") if isinstance(sender_chat, dict) else None
    media = getattr(message, "photo", None)
    media_marker = ""
    if isinstance(media, list) and media:
        last = media[-1]
        marker = getattr(last, "file_unique_id", None)
        if isinstance(marker, str):
            media_marker = marker
    key_payload = {
        "origin_kind": origin_kind,
        "origin_date_utc": origin_date_utc,
        "sender_user_id": sender_user_id if isinstance(sender_user_id, int) else None,
        "sender_chat_id": sender_chat_id if isinstance(sender_chat_id, int) else None,
        "sender_user_name": sender_user_name or None,
        "event_type": event_type,
        "text": text if isinstance(text, str) else None,
        "media_marker": media_marker or None,
    }
    key_json = json.dumps(key_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    key = "sig:" + hashlib.sha1(key_json.encode("utf-8")).hexdigest()
    return {"strategy": "origin_signature", "key": key, "origin_kind": origin_kind}


def _event_ts_utc_for_store(message: Message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    origin_date = getattr(origin, "date", None) if origin is not None else None
    if not isinstance(origin_date, datetime):
        return None
    # Some forwards expose forward-time only; if equal, treat it as unknown.
    if origin_date == message.date:
        return None
    return origin_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _infer_target_chat_id_from_forward(message: Message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat is None:
        sender_chat = getattr(origin, "chat", None)
    if sender_chat is not None:
        sender_chat_id = getattr(sender_chat, "id", None)
        if isinstance(sender_chat_id, int):
            return sender_chat_id
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        sender_user_id = getattr(sender_user, "id", None)
        if isinstance(sender_user_id, int):
            return sender_user_id
    return None


def _should_reuse_forward_target(
    target_chat_id: int | None,
    forward_target_hint: int | None,
    control_user_id: int | None,
) -> bool:
    if not isinstance(target_chat_id, int) or target_chat_id <= 0:
        return False
    if not isinstance(forward_target_hint, int):
        return True
    if isinstance(control_user_id, int) and forward_target_hint == control_user_id:
        return True
    return forward_target_hint == target_chat_id


def _infer_role_from_forward(message: Message, target_chat_id: int) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return "manual"
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat is None:
        sender_chat = getattr(origin, "chat", None)
    if sender_chat is not None:
        sender_chat_id = getattr(sender_chat, "id", None)
        if isinstance(sender_chat_id, int) and sender_chat_id == target_chat_id:
            return "scammer"
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        sender_user_id = getattr(sender_user, "id", None)
        if isinstance(sender_user_id, int) and sender_user_id == target_chat_id:
            return "scammer"
    return "manual"


def _infer_role_without_target(message: Message, control_user_id: int | None) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return "manual"
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        sender_user_id = getattr(sender_user, "id", None)
        if isinstance(sender_user_id, int) and control_user_id is not None and sender_user_id == control_user_id:
            return "manual"
        if isinstance(sender_user_id, int):
            return "scammer"
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat is None:
        sender_chat = getattr(origin, "chat", None)
    if sender_chat is not None:
        sender_chat_id = getattr(sender_chat, "id", None)
        if isinstance(sender_chat_id, int):
            return "scammer"
    return "manual"


def _resolve_target_and_role_without_active(
    message: Message,
    control_user_id: int | None,
    auto_target_chat_id: int | None,
) -> tuple[int | None, str]:
    sender_id = _infer_target_chat_id_from_forward(message)
    if sender_id is None:
        return None, "manual"   # hidden sender — always require manual override
    if control_user_id is not None and sender_id == control_user_id:
        if auto_target_chat_id is None:
            return None, "manual"
        return auto_target_chat_id, "manual"
    return sender_id, "scammer"


def _control_sender_info(message: Message) -> dict[str, Any] | None:
    sender = getattr(message, "from_user", None)
    if sender is None:
        return None
    info: dict[str, Any] = {}
    for field in ("id", "username", "first_name", "last_name"):
        value = getattr(sender, field, None)
        if value not in (None, ""):
            info[field] = value
    if info:
        return info
    return None


def _build_forward_payload(message: Message, role: str) -> dict[str, Any]:
    event_type = _infer_event_type(message)
    text = _extract_text(message)
    origin = getattr(message, "forward_origin", None)
    origin_message_id = _extract_origin_message_id(message)
    forward_profile = _extract_forward_profile_info(message)
    if origin is not None:
        forward_identity = _extract_forward_identity(
            origin=origin,
            forward_profile=forward_profile,
            event_type=event_type,
            text=text,
            message=message,
        )
    else:
        forward_identity = {
            "strategy": "origin_signature",
            "key": f"sig:missing:{message.chat_id}:{message.message_id}",
            "origin_kind": "Unknown",
        }
    source_message_id = _build_source_message_id(
        str(forward_identity.get("key") or ""),
        str(forward_identity.get("strategy") or "origin_signature"),
        event_type,
        text,
    )
    meta: dict[str, Any] = {
        "control_chat_id": int(message.chat_id),
        "control_message_id": int(message.message_id),
        "forward_profile": forward_profile,
        "forward_identity": forward_identity,
        "origin_message_id": origin_message_id,
    }
    control_sender = _control_sender_info(message)
    if control_sender:
        meta["control_sender"] = control_sender
    return {
        "event_type": event_type,
        "source_message_id": source_message_id,
        "origin_message_id": origin_message_id,
        "role": role,
        "text": text,
        "ts_utc": _event_ts_utc_for_store(message),
        "meta": meta,
    }


def _profile_patch_from_forward_profile(forward_profile: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {"identity": {}, "account": {}, "provenance": {}}
    sender_user = forward_profile.get("sender_user")
    sender_chat = forward_profile.get("sender_chat")
    sender_user_name = forward_profile.get("sender_user_name")
    if isinstance(sender_user, dict):
        value = sender_user.get("id")
        if isinstance(value, int):
            patch["identity"]["telegram_user_id"] = value
        value = sender_user.get("username")
        if isinstance(value, str) and value.strip():
            patch["identity"]["username"] = value.strip()
        first = sender_user.get("first_name")
        if isinstance(first, str) and first.strip():
            patch["identity"]["first_name"] = first.strip()
        last = sender_user.get("last_name")
        if isinstance(last, str) and last.strip():
            patch["identity"]["last_name"] = last.strip()
        is_bot = sender_user.get("is_bot")
        if isinstance(is_bot, bool):
            patch["account"]["is_bot"] = is_bot
        lang_code = sender_user.get("language_code")
        if isinstance(lang_code, str) and lang_code.strip():
            patch["account"]["lang_code"] = lang_code.strip()
    if isinstance(sender_chat, dict):
        value = sender_chat.get("id")
        if isinstance(value, int):
            patch["identity"]["telegram_chat_id"] = value
        title = sender_chat.get("title")
        if isinstance(title, str) and title.strip():
            patch["identity"]["display_name"] = title.strip()
        username = sender_chat.get("username")
        if isinstance(username, str) and username.strip():
            patch["identity"]["username"] = username.strip()
    if isinstance(sender_user_name, str) and sender_user_name.strip():
        patch["identity"]["display_name"] = sender_user_name.strip()
    # Derive display_name when only first/last exist.
    first_name = patch["identity"].get("first_name")
    last_name = patch["identity"].get("last_name")
    if "display_name" not in patch["identity"] and isinstance(first_name, str):
        if isinstance(last_name, str):
            patch["identity"]["display_name"] = f"{first_name} {last_name}".strip()
        else:
            patch["identity"]["display_name"] = first_name
    patch["provenance"]["last_source"] = "botapi_forward"
    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        if isinstance(value, dict) and value:
            cleaned[key] = value
    return cleaned


def _ingest_forward_payload(store: Any, target_chat_id: int, payload: dict[str, Any]) -> Any:
    source_message_id = str(payload.get("source_message_id") or "")
    if not source_message_id:
        raise ValueError("missing source_message_id for forward ingestion")
    meta_obj = payload.get("meta")
    forward_identity = meta_obj.get("forward_identity") if isinstance(meta_obj, dict) else None
    if not isinstance(forward_identity, dict) or not isinstance(forward_identity.get("key"), str):
        raise ValueError("missing forward_identity for forward ingestion")
    record = store.ingest_user_forward(
        chat_id=target_chat_id,
        event_type=str(payload["event_type"]),
        text=payload.get("text"),
        source_message_id=source_message_id,
        role=str(payload["role"]),
        ts_utc=payload.get("ts_utc"),
        meta=payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
    )
    meta = payload.get("meta")
    if isinstance(meta, dict):
        forward_profile = meta.get("forward_profile")
        if isinstance(forward_profile, dict) and forward_profile:
            patch = _profile_patch_from_forward_profile(forward_profile)
            if patch:
                changed_at = payload.get("ts_utc")
                store.upsert_chat_profile(
                    chat_id=target_chat_id,
                    patch=patch,
                    source="botapi_forward",
                    changed_at=changed_at if isinstance(changed_at, str) else None,
                )
    return record


def _forward_item_signature(payload: dict[str, Any]) -> tuple[str, str]:
    event_type = str(payload.get("event_type") or "")
    text = payload.get("text")
    return event_type, str(text) if isinstance(text, str) else ""


def _extract_forward_identity_key_from_event(event: Any) -> str | None:
    meta = getattr(event, "meta", None)
    if isinstance(meta, dict):
        forward_identity = meta.get("forward_identity")
        if isinstance(forward_identity, dict):
            key = forward_identity.get("key")
            if isinstance(key, str) and key:
                return key
        origin_id = meta.get("origin_message_id")
        if isinstance(origin_id, int):
            return f"legacy_origin:{origin_id}"
    source_message_id = getattr(event, "source_message_id", None)
    if isinstance(source_message_id, str) and source_message_id:
        return f"legacy_source:{source_message_id}"
    return None


def _extract_forward_identity_key_from_payload(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    if isinstance(meta, dict):
        forward_identity = meta.get("forward_identity")
        if isinstance(forward_identity, dict):
            key = forward_identity.get("key")
            if isinstance(key, str) and key:
                return key
    origin_id = payload.get("origin_message_id")
    if isinstance(origin_id, int):
        return f"legacy_origin:{origin_id}"
    source_message_id = payload.get("source_message_id")
    if isinstance(source_message_id, str) and source_message_id:
        return f"legacy_source:{source_message_id}"
    return None


def _build_existing_identity_index(events: list[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for event in events:
        key = _extract_forward_identity_key_from_event(event)
        if key is None:
            continue
        out.setdefault(key, []).append(event)
    return out


def _plan_forward_merge(
    store: Any,
    target_chat_id: int,
    payloads: list[dict[str, Any]],
    *,
    allow_placeholder: bool = False,
) -> dict[str, Any]:
    if target_chat_id <= 0 and not allow_placeholder:
        return {"mode": "unresolved", "insert_payloads": [], "reason": "target chat unresolved"}
    if not payloads:
        return {"mode": "blocked", "insert_payloads": [], "reason": "batch empty"}
    missing_identity = [p for p in payloads if not isinstance(_extract_forward_identity_key_from_payload(p), str)]
    if missing_identity:
        return {
            "mode": "blocked",
            "insert_payloads": [],
            "reason": f"{len(missing_identity)} item(s) missing forward_identity",
        }

    events = store.list_events(chat_id=target_chat_id, limit=5000)
    existing_by_identity = _build_existing_identity_index(events)
    existing_scammer_keys: list[str] = []
    seen_scammer_keys: set[str] = set()
    for event in events:
        if str(getattr(event, "role", "")) != "scammer":
            continue
        key = _extract_forward_identity_key_from_event(event)
        if not isinstance(key, str):
            continue
        if key in seen_scammer_keys:
            continue
        seen_scammer_keys.add(key)
        existing_scammer_keys.append(key)

    insert_payloads: list[dict[str, Any]] = []
    batch_scammer_keys: list[str] = []
    batch_new_scammer_keys: list[str] = []
    for payload in payloads:
        identity_key = _extract_forward_identity_key_from_payload(payload)
        if not isinstance(identity_key, str):
            continue
        existing_rows = existing_by_identity.get(identity_key, [])
        role = str(payload.get("role") or "")
        if role == "scammer":
            batch_scammer_keys.append(identity_key)
            if not existing_rows:
                batch_new_scammer_keys.append(identity_key)
        sig = _forward_item_signature(payload)
        payload_event_type = str(payload.get("event_type") or "").strip().lower()
        has_same = False
        has_changed = False
        for row in existing_rows:
            row_event_type = str(getattr(row, "event_type", "") or "")
            row_text = str(getattr(row, "text", "") or "")
            row_sig = (row_event_type, row_text)
            row_event_type_lower = row_event_type.strip().lower()
            if row_event_type_lower == "forward" and payload_event_type != "forward":
                has_changed = True
                continue
            if row_sig == sig:
                has_same = True
                break
            has_changed = True
        if has_same:
            continue
        candidate = dict(payload)
        meta = candidate.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        if has_changed:
            meta["revision_of_forward_identity_key"] = identity_key
            meta["revision_reason"] = "content_changed"
        candidate["meta"] = meta
        insert_payloads.append(candidate)

    if not insert_payloads:
        return {"mode": "blocked", "insert_payloads": [], "reason": "batch already present"}

    if batch_new_scammer_keys:
        if not existing_scammer_keys:
            return {"mode": "append", "insert_payloads": insert_payloads, "reason": f"append {len(insert_payloads)} item(s)"}
        existing_pos = {key: idx for idx, key in enumerate(existing_scammer_keys)}
        first_new_idx = next((idx for idx, key in enumerate(batch_scammer_keys) if key not in existing_pos), len(batch_scammer_keys))
        has_known_after_new = any(key in existing_pos for key in batch_scammer_keys[first_new_idx:])
        prefix_known = batch_scammer_keys[:first_new_idx]
        is_suffix_match = bool(prefix_known) and prefix_known == existing_scammer_keys[-len(prefix_known) :]
        if (not has_known_after_new) and is_suffix_match:
            return {"mode": "append", "insert_payloads": insert_payloads, "reason": f"append {len(insert_payloads)} item(s)"}
        return {"mode": "backfill", "insert_payloads": insert_payloads, "reason": f"backfill {len(insert_payloads)} item(s)"}

    return {"mode": "backfill", "insert_payloads": insert_payloads, "reason": f"backfill {len(insert_payloads)} item(s)"}


def _manual_alias_placeholder(alias: str) -> int:
    normalized = alias.strip()
    if not normalized:
        raise ValueError("alias cannot be empty")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big", signed=False)
    placeholder_base = (1 << 62) - 1
    placeholder_value = value % placeholder_base
    return -1 - placeholder_value


def _forward_card_keyboard(
    *,
    control_chat_id: int,
    target_chat_id: int | None,
    mode: str,
    known_chat_ids: list[int],
    manual_alias_label: str | None,
    manual_pending: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    has_resolved_target = isinstance(target_chat_id, int) and target_chat_id > 0
    if manual_alias_label is not None:
        has_resolved_target = True
    if has_resolved_target:
        if mode == "append":
            rows.append([InlineKeyboardButton(f"Append to /{target_chat_id}", callback_data=f"sc:fwd_insert:{control_chat_id}")])
        elif mode == "backfill":
            rows.append([InlineKeyboardButton(f"Backfill to /{target_chat_id}", callback_data=f"sc:fwd_insert:{control_chat_id}")])
        else:
            rows.append([InlineKeyboardButton("Insert blocked", callback_data="sc:nop")])
    else:
        if manual_pending:
            rows.append([InlineKeyboardButton("Manual override pending (enter alias)", callback_data="sc:nop")])
        else:
            rows.append([InlineKeyboardButton("Manual override alias", callback_data=f"sc:fwd_manual:{control_chat_id}")])
        for chat_id in known_chat_ids[:8]:
            rows.append([InlineKeyboardButton(f"/{chat_id}", callback_data=f"sc:fwd_selchat:{chat_id}")])
    rows.append([InlineKeyboardButton("Discard", callback_data=f"sc:fwd_discard:{control_chat_id}")])
    return InlineKeyboardMarkup(rows)


def _render_forward_card_text(
    *,
    control_chat_id: int,
    target_chat_id: int | None,
    payloads: list[dict[str, Any]],
    merge: dict[str, Any],
    manual_alias_label: str | None,
    manual_pending: bool,
) -> str:
    total = len(payloads)
    scammer = sum(1 for p in payloads if str(p.get("role") or "") == "scammer")
    manual = sum(1 for p in payloads if str(p.get("role") or "") != "scammer")
    missing_identity = sum(1 for p in payloads if not isinstance(_extract_forward_identity_key_from_payload(p), str))
    mode = str(merge.get("mode") or "unresolved")
    reason = str(merge.get("reason") or "-")
    target_text = f"/{target_chat_id}" if isinstance(target_chat_id, int) and target_chat_id > 0 else "(unresolved)"
    alias_label = manual_alias_label or "(none)"
    pending_note = " (waiting for entry)" if manual_pending else ""
    return (
        "Forward/Insert Card\n"
        f"control_chat: {control_chat_id}\n"
        f"target_chat: {target_text}\n"
        f"manual_alias: {alias_label}{pending_note}\n"
        f"batch_items: {total}\n"
        f"scammer_items: {scammer}\n"
        f"manual_items: {manual}\n"
        f"missing_forward_identity: {missing_identity}\n"
        f"merge_mode: {mode}\n"
        f"merge_reason: {reason}"
    )


def _clear_forward_session(application: Any, control_chat_id: int) -> None:
    from scambaiter.bot_state import (
        _forward_card_targets,
        _manual_override_labels,
        _manual_override_requests,
        _pending_forwards,
    )

    _pending_forwards(application)[control_chat_id] = []
    _forward_card_targets(application).pop(control_chat_id, None)
    _manual_override_requests(application).pop(control_chat_id, None)
    _manual_override_labels(application).pop(control_chat_id, None)


def _flush_pending_forwards(
    application: Any,
    store: Any,
    control_chat_id: int,
    target_chat_id: int,
) -> int:
    from scambaiter.bot_state import _pending_forwards

    pending = _pending_forwards(application)
    queue = pending.get(control_chat_id, [])
    if not queue:
        return 0
    imported = 0
    for payload in queue:
        _ingest_forward_payload(store=store, target_chat_id=target_chat_id, payload=payload)
        imported += 1
    pending[control_chat_id] = []
    return imported


def ingest_forwarded_message(store: Any, target_chat_id: int, message: Message) -> Any:
    role = _infer_role_from_forward(message, target_chat_id=target_chat_id)
    payload = _build_forward_payload(message, role=role)
    return _ingest_forward_payload(store=store, target_chat_id=target_chat_id, payload=payload)


def _update_forward_card(
    *,
    application: Any,
    message: Message | None,
    store: Any,
    control_chat_id: int,
) -> Any:
    """Build and send/edit the forward card.  Returns a coroutine (must be awaited)."""
    from scambaiter.bot_state import (
        _forward_card_messages,
        _forward_card_targets,
        _manual_override_labels,
        _manual_override_requests,
        _pending_forwards,
    )

    async def _run() -> None:
        pending = _pending_forwards(application)
        payloads = pending.get(control_chat_id, [])
        target_map = _forward_card_targets(application)
        target_chat_id = target_map.get(control_chat_id)
        allow_placeholder = isinstance(target_chat_id, int) and target_chat_id < 0
        merge = _plan_forward_merge(
            store,
            target_chat_id if isinstance(target_chat_id, int) else -1,
            payloads,
            allow_placeholder=allow_placeholder,
        )
        known_chat_ids = store.list_chat_ids(limit=30)
        manual_requests = _manual_override_requests(application)
        manual_alias_label = _manual_override_labels(application).get(control_chat_id)
        manual_pending = control_chat_id in manual_requests
        text = _render_forward_card_text(
            control_chat_id=control_chat_id,
            target_chat_id=target_chat_id,
            payloads=payloads,
            merge=merge,
            manual_alias_label=manual_alias_label,
            manual_pending=manual_pending,
        )
        keyboard = _forward_card_keyboard(
            control_chat_id=control_chat_id,
            target_chat_id=target_chat_id,
            mode=str(merge.get("mode") or "unresolved"),
            known_chat_ids=known_chat_ids,
            manual_alias_label=manual_alias_label,
            manual_pending=manual_pending,
        )
        message_ids = _forward_card_messages(application)
        current_id = message_ids.get(control_chat_id)
        if isinstance(current_id, int):
            try:
                await application.bot.edit_message_text(
                    chat_id=control_chat_id,
                    message_id=current_id,
                    text=text,
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass
        if message is not None:
            sent = await message.reply_text(text, reply_markup=keyboard)
        else:
            sent = await application.bot.send_message(chat_id=control_chat_id, text=text, reply_markup=keyboard)
        message_ids[control_chat_id] = int(getattr(sent, "message_id", None) or getattr(sent, "id", None) or 0)

    return _run()
