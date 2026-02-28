"""Directive panel rendering — pure view functions for directive lists and keyboards."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _render_directive_list_card(chat_id: int, directives: list[Any]) -> str:
    """Render a text card showing active directives for a chat.

    Args:
        chat_id: Target chat ID
        directives: List of Directive objects from storage

    Returns:
        Formatted text card for Telegram message
    """
    active_count = sum(1 for d in directives if getattr(d, "active", False))
    lines = [
        "Directives",
        f"chat_id: /{chat_id}",
        f"active: {active_count}",
        "---",
    ]
    if not active_count:
        lines.append("No active directives.")
    else:
        for d in directives[:10]:  # Cap at 10 for display
            if not getattr(d, "active", False):
                continue
            directive_id = getattr(d, "id", "?")
            text = str(getattr(d, "text", "") or "").strip()
            if len(text) > 120:
                text = text[:117] + "..."
            lines.append(f"#{directive_id} {text}")
    return "\n".join(lines)


def _directive_card_keyboard(chat_id: int, directives: list[Any]) -> InlineKeyboardMarkup:
    """Build keyboard for directive panel with toggle/delete buttons.

    Args:
        chat_id: Target chat ID
        directives: List of Directive objects

    Returns:
        InlineKeyboardMarkup with directive controls
    """
    rows: list[list[InlineKeyboardButton]] = []

    # Active directives with toggle and delete buttons
    for d in directives[:10]:
        if not getattr(d, "active", False):
            continue
        directive_id = getattr(d, "id", 0)
        text_snippet = str(getattr(d, "text", "") or "").strip()
        if len(text_snippet) > 30:
            text_snippet = text_snippet[:27] + "..."

        rows.append([
            InlineKeyboardButton(
                f"scope: once",
                callback_data=f"sc:dir_toggle:{directive_id}:{chat_id}",
            ),
            InlineKeyboardButton(
                "Delete",
                callback_data=f"sc:dir_delete:{directive_id}:{chat_id}",
            ),
        ])

    # Add / Close buttons
    rows.append([
        InlineKeyboardButton(
            "+ Add Directive",
            callback_data=f"sc:dir_add:{chat_id}",
        ),
        InlineKeyboardButton(
            "Close",
            callback_data=f"sc:dir_close:{chat_id}",
        ),
    ])

    return InlineKeyboardMarkup(rows)
