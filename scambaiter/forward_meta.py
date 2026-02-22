from __future__ import annotations

from typing import Any


def _display_name_from_identity(identity: dict[str, Any] | None) -> str | None:
    if not isinstance(identity, dict):
        return None
    if value := identity.get("display_name"):
        return str(value).strip()
    first = str(identity.get("first_name") or "").strip()
    last = str(identity.get("last_name") or "").strip()
    if first or last:
        return " ".join(part for part in (first, last) if part)
    username = str(identity.get("username") or "").strip()
    if username:
        return f"@{username}"
    return None


def scammer_name_from_meta(meta: dict[str, Any] | None) -> str | None:
    if not isinstance(meta, dict):
        return None
    forward_profile = meta.get("forward_profile")
    if not isinstance(forward_profile, dict):
        return None
    sender_user = forward_profile.get("sender_user")
    if isinstance(sender_user, dict):
        if display := _display_name_from_identity(sender_user):
            return display
    sender_chat = forward_profile.get("sender_chat")
    if isinstance(sender_chat, dict):
        return _display_name_from_identity(sender_chat)
    return None


def baiter_name_from_meta(meta: dict[str, Any] | None) -> str | None:
    if not isinstance(meta, dict):
        return None
    control_sender = meta.get("control_sender")
    if not isinstance(control_sender, dict):
        return None
    return _display_name_from_identity(control_sender)
