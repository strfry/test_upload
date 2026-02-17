#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class Msg:
    role: str
    text: str


STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "is",
    "are",
    "be",
    "that",
    "this",
    "it",
    "as",
    "at",
    "with",
    "i",
    "you",
    "we",
    "me",
    "my",
    "your",
    "our",
    "ich",
    "du",
    "wir",
    "der",
    "die",
    "das",
    "ein",
    "eine",
    "und",
    "oder",
    "zu",
    "im",
    "in",
    "ist",
    "sind",
    "mit",
}


ROLE_RE = re.compile(
    r"^\s*(user|assistant|u|a|scammer|bot|me|ich)\s*[:\-]\s*(.+?)\s*$",
    flags=re.IGNORECASE,
)
TG_HEADER_RE = re.compile(
    r"^\s*(?P<sender>.+?),\s*\[(?P<ts>[^\]]+)\]\s*$",
    flags=re.IGNORECASE,
)

INTENT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "validator_contact": [
        re.compile(r"\bvalidator\b", re.I),
        re.compile(r"\bgeorge\b", re.I),
        re.compile(r"\bcontact\b", re.I),
        re.compile(r"\breach\b", re.I),
    ],
    "wallet_address": [
        re.compile(r"\bwallet\b", re.I),
        re.compile(r"\baddress\b", re.I),
        re.compile(r"\bdeposit\b", re.I),
        re.compile(r"\beth\b", re.I),
    ],
    "fees": [
        re.compile(r"\bfee\b", re.I),
        re.compile(r"\bfees\b", re.I),
        re.compile(r"\bcost\b", re.I),
        re.compile(r"\bminimum\b", re.I),
    ],
    "next_step": [
        re.compile(r"\bnext\b", re.I),
        re.compile(r"\bproceed\b", re.I),
        re.compile(r"\bcontinue\b", re.I),
        re.compile(r"\bwhat do i need\b", re.I),
    ],
}


def _normalize_role(raw: str) -> str:
    text = raw.strip().lower()
    if text in {"assistant", "a", "bot", "me", "ich"}:
        return "assistant"
    return "user"


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9äöüÄÖÜß]{2,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def _extract_intents(text: str) -> set[str]:
    intents: set[str] = set()
    for intent, patterns in INTENT_PATTERNS.items():
        if any(pattern.search(text) for pattern in patterns):
            intents.add(intent)
    return intents


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _parse_from_json(path: Path) -> list[Msg]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("JSON muss ein Objekt sein.")
    items = raw.get("messages")
    if not isinstance(items, list):
        raise ValueError("JSON muss 'messages' als Array enthalten.")
    result: list[Msg] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"messages[{idx}] muss ein Objekt sein.")
        role = _normalize_role(str(item.get("role", "user")))
        text = str(item.get("text", "")).strip()
        if text:
            result.append(Msg(role=role, text=text))
    return result


def _parse_from_transcript(text: str, assistant_senders: set[str] | None = None) -> list[Msg]:
    # 1) Telegram copy format: "Sender, [date time]" followed by one or more message lines.
    lines = text.splitlines()
    result: list[Msg] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        header = TG_HEADER_RE.match(line)
        if header:
            sender = header.group("sender").strip()
            sender_norm = sender.casefold()
            body_lines: list[str] = []
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if TG_HEADER_RE.match(next_line):
                    break
                if next_line:
                    body_lines.append(next_line)
                i += 1
            content = " ".join(body_lines).strip()
            if content:
                role = "assistant" if assistant_senders and sender_norm in assistant_senders else "user"
                result.append(Msg(role=role, text=content))
            continue
        i += 1

    if result:
        return result

    # 2) Fallback format: "user: text" / "assistant: text"
    result: list[Msg] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        match = ROLE_RE.match(line)
        if match:
            role = _normalize_role(match.group(1))
            content = match.group(2).strip()
            if content:
                result.append(Msg(role=role, text=content))
            continue
        if result:
            result[-1].text = (result[-1].text + " " + line).strip()
    return result


def _collect_findings(messages: list[Msg]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    assistant_indices = [i for i, msg in enumerate(messages) if msg.role == "assistant"]
    user_indices = [i for i, msg in enumerate(messages) if msg.role == "user"]

    # 1) repeated assistant question pattern
    for idx_pos, i in enumerate(assistant_indices):
        text_i = messages[i].text
        if "?" not in text_i:
            continue
        tok_i = _tokens(text_i)
        if len(tok_i) < 3:
            continue
        for j in assistant_indices[:idx_pos]:
            text_j = messages[j].text
            if "?" not in text_j:
                continue
            score = _jaccard(tok_i, _tokens(text_j))
            if score >= 0.58:
                findings.append(
                    {
                        "type": "repeated_assistant_question",
                        "severity": "high",
                        "message_indexes": [j + 1, i + 1],
                        "similarity": round(score, 3),
                        "evidence": [text_j, text_i],
                        "why_it_loops": "Assistent stellt inhaltlich sehr ähnliche Frage erneut.",
                    }
                )
                break
            intents_i = _extract_intents(text_i)
            intents_j = _extract_intents(text_j)
            if intents_i and intents_j and (intents_i & intents_j):
                findings.append(
                    {
                        "type": "repeated_assistant_question_intent",
                        "severity": "high",
                        "message_indexes": [j + 1, i + 1],
                        "shared_intents": sorted(list(intents_i & intents_j)),
                        "evidence": [text_j, text_i],
                        "why_it_loops": "Assistent fragt dasselbe Intent-Thema erneut.",
                    }
                )
                break

    # 2) weak reaction to latest user turn
    for i, msg in enumerate(messages):
        if msg.role != "assistant":
            continue
        prev_user_idx = None
        for u in reversed(user_indices):
            if u < i:
                prev_user_idx = u
                break
        if prev_user_idx is None:
            continue
        user_text = messages[prev_user_idx].text
        user_tok = _tokens(user_text)
        asst_tok = _tokens(msg.text)
        user_intents = _extract_intents(user_text)
        asst_intents = _extract_intents(msg.text)
        if user_intents and asst_intents and not (user_intents & asst_intents):
            findings.append(
                {
                    "type": "intent_mismatch_latest_user_vs_assistant",
                    "severity": "high",
                    "message_indexes": [prev_user_idx + 1, i + 1],
                    "user_intents": sorted(list(user_intents)),
                    "assistant_intents": sorted(list(asst_intents)),
                    "evidence": [user_text, msg.text],
                    "why_it_loops": "Assistent springt thematisch weg von der letzten User-Intention.",
                }
            )
            continue
        if len(user_tok) < 3 or len(asst_tok) < 3:
            continue
        overlap = _jaccard(user_tok, asst_tok)
        if overlap < 0.07 and "?" in msg.text:
            findings.append(
                {
                    "type": "weak_reply_to_latest_user_topic",
                    "severity": "medium",
                    "message_indexes": [prev_user_idx + 1, i + 1],
                    "similarity": round(overlap, 3),
                    "evidence": [user_text, msg.text],
                    "why_it_loops": "Antwort greift die letzte User-Intention kaum auf und fällt auf generische Rückfrage zurück.",
                }
            )

    return findings


def _loop_risk(findings: list[dict[str, object]]) -> str:
    high = sum(1 for item in findings if item.get("severity") == "high")
    medium = sum(1 for item in findings if item.get("severity") == "medium")
    if high >= 2 or (high >= 1 and medium >= 1):
        return "high"
    if high >= 1 or medium >= 2:
        return "medium"
    return "low"


def _suggestions(findings: list[dict[str, object]]) -> dict[str, object]:
    has_repeat = any(
        item.get("type") in {"repeated_assistant_question", "repeated_assistant_question_intent"}
        for item in findings
    )
    has_weak = any(
        item.get("type") in {"weak_reply_to_latest_user_topic", "intent_mismatch_latest_user_vs_assistant"}
        for item in findings
    )

    analysis_update: dict[str, object] = {}
    directives: list[str] = []

    if has_repeat:
        analysis_update["loop_guard_active"] = True
        analysis_update["ask_again_guard"] = {"topic_from_last_turn": True}
        directives.append(
            "Stelle keine inhaltlich ähnliche Rückfrage zur letzten Assistant-Frage; liefere stattdessen den nächsten konkreten Schritt."
        )
    if has_weak:
        analysis_update["last_user_topic_priority"] = True
        directives.append(
            "Antworte zuerst auf das zuletzt vom User genannte Thema und formuliere nur dafür eine konkrete Anschlussfrage."
        )

    return {
        "analysis_update": analysis_update,
        "operator_directives": directives,
    }


def _build_prompt_case(
    messages: list[Msg],
    title: str,
    chat_id: int,
    language_hint: str,
    now_utc: str | None = None,
) -> dict[str, object]:
    if now_utc and isinstance(now_utc, str) and now_utc.strip():
        now_text = now_utc.strip()
    else:
        now_text = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    base_time = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=max(len(messages), 1))
    rendered_messages: list[dict[str, str]] = []
    for idx, msg in enumerate(messages):
        ts = (base_time + timedelta(minutes=idx)).isoformat().replace("+00:00", "Z")
        sender = "self" if msg.role == "assistant" else "User"
        rendered_messages.append(
            {
                "ts_utc": ts,
                "role": msg.role,
                "sender": sender,
                "text": msg.text,
            }
        )
    return {
        "chat_id": int(chat_id),
        "title": title,
        "messages": rendered_messages,
        "prompt_context": {
            "messenger": "telegram",
            "now_utc": now_text,
        },
        "language_hint": language_hint,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analysiert Gesprächs-Loop-Muster in pasted Verläufen.")
    parser.add_argument("--input-json", type=str, help="JSON-Datei mit messages[] (role/text).")
    parser.add_argument("--transcript-file", type=str, help="Textdatei mit gepastetem Verlauf (z.B. user:/assistant:).")
    parser.add_argument(
        "--transcript-stdin",
        action="store_true",
        help="Liest den gepasteten Verlauf aus stdin bis EOF (Ctrl+D).",
    )
    parser.add_argument(
        "--assistant-sender",
        action="append",
        default=[],
        help="Sendername aus Telegram-Kopfzeilen, der als Assistant gewertet wird (mehrfach möglich).",
    )
    parser.add_argument("--output-json", type=str, help="Optional: schreibt Ergebnis in Datei.")
    parser.add_argument(
        "--export-prompt-case",
        type=str,
        help="Optional: exportiert den geparsten Verlauf als prompt_runner-kompatibles JSON.",
    )
    parser.add_argument("--case-title", type=str, default="TRANSCRIPT-CASE", help="Titel für --export-prompt-case.")
    parser.add_argument("--case-chat-id", type=int, default=7999999999, help="Chat-ID für --export-prompt-case.")
    parser.add_argument("--case-language", type=str, default="en", help="language_hint für --export-prompt-case.")
    parser.add_argument("--case-now-utc", type=str, default="", help="Optionales now_utc für --export-prompt-case.")
    args = parser.parse_args()

    source_count = int(bool(args.input_json)) + int(bool(args.transcript_file)) + int(bool(args.transcript_stdin))
    if source_count == 0:
        parser.error("Bitte --input-json, --transcript-file oder --transcript-stdin angeben.")
    if source_count > 1:
        parser.error("Bitte nur eine Eingabequelle angeben.")

    try:
        if args.input_json:
            messages = _parse_from_json(Path(args.input_json))
        elif args.transcript_file:
            transcript = Path(str(args.transcript_file)).read_text(encoding="utf-8")
            assistant_senders = {str(item).strip().casefold() for item in args.assistant_sender if str(item).strip()}
            messages = _parse_from_transcript(transcript, assistant_senders=assistant_senders)
        else:
            transcript = sys.stdin.read()
            if not transcript.strip():
                raise ValueError("stdin ist leer. Bitte Verlauf einfügen und mit EOF (Ctrl+D) beenden.")
            assistant_senders = {str(item).strip().casefold() for item in args.assistant_sender if str(item).strip()}
            messages = _parse_from_transcript(transcript, assistant_senders=assistant_senders)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc

    findings = _collect_findings(messages)
    suggestion_block = _suggestions(findings)
    result = {
        "summary": {
            "message_count": len(messages),
            "assistant_count": sum(1 for m in messages if m.role == "assistant"),
            "user_count": sum(1 for m in messages if m.role == "user"),
            "loop_risk": _loop_risk(findings),
        },
        "findings": findings,
        "suggestions": suggestion_block,
    }

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        Path(args.output_json).write_text(rendered + "\n", encoding="utf-8")
    if args.export_prompt_case:
        case_obj = _build_prompt_case(
            messages=messages,
            title=str(args.case_title),
            chat_id=int(args.case_chat_id),
            language_hint=str(args.case_language),
            now_utc=str(args.case_now_utc or "").strip() or None,
        )
        Path(args.export_prompt_case).write_text(
            json.dumps(case_obj, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(rendered)


if __name__ == "__main__":
    main()
