from __future__ import annotations

import re
import unicodedata
from typing import Any


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    return re.sub(r"\s+", " ", collapsed)


def _score_row(row: dict[str, Any], query_tokens: list[str]) -> tuple[int, int]:
    title = normalize_text(str(row.get("title") or ""))
    username = normalize_text(str(row.get("username") or ""))
    if not query_tokens:
        return (0, 0)
    token_score = 0
    username_hits = 0
    for token in query_tokens:
        if token and token in title:
            token_score += 1
        if token and token in username:
            token_score += 2
            username_hits += 1
    return (token_score, username_hits)


def match_dialogs(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    query_norm = normalize_text(query)
    if not query_norm:
        return []
    query_tokens = [token for token in query_norm.split(" ") if token]
    if not query_tokens:
        return []

    matched: list[tuple[dict[str, Any], int, int]] = []
    for row in rows:
        score, username_hits = _score_row(row, query_tokens)
        if score > 0:
            matched.append((row, score, username_hits))

    matched.sort(
        key=lambda item: (
            -item[1],
            -item[2],
            int(item[0].get("chat_id", 0)),
        )
    )
    return [item[0] for item in matched]


def resolve_unique_dialog(rows: list[dict[str, Any]], query: str) -> tuple[str, list[dict[str, Any]]]:
    matches = match_dialogs(rows=rows, query=query)
    if not matches:
        return ("none", [])
    if len(matches) == 1:
        return ("single", matches)
    return ("multiple", matches)
