from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .forward_meta import baiter_name_from_meta, scammer_name_from_meta
from .model_client import call_hf_openai_chat, extract_reasoning_details, extract_result_text, extract_tool_calls

_log = logging.getLogger(__name__)

# Re-export everything from core_schema so existing ``from scambaiter.core import X`` keeps working.
from .core_schema import (  # noqa: F401 — re-exports
    ALLOWED_ACTION_TYPES,
    ALLOWED_TOP_LEVEL_KEYS,
    DISALLOWED_STYLE_PHRASES,
    MEMORY_SUMMARY_PROMPT_CONTRACT,
    META_TURN_PROMPT_CONTRACT,
    SEMANTIC_CONFLICT_REASON_HINTS,
    SYSTEM_PROMPT_CONTRACT,
    TIMING_PROMPT_RULES,
    TOOL_DEFINITIONS,
    ChatContext,
    ChatEvent,
    EventType,
    ModelOutput,
    ParseResult,
    RoleType,
    ValidationIssue,
    _WAIT_LATENCY_MAP,
    _build_repair_messages,
    _validate_actions,
    normalize_action_shape,
    normalize_iso_utc,
    parse_structured_model_output,
    parse_structured_model_output_detailed,
    parse_tool_calls_to_model_output,
    strip_think_segments,
    violates_scambait_style_policy,
)


class ScambaiterCore:
    """Core analysis/prompt component.

    This class intentionally does not send any messages. It only prepares
    context and generates structured model outputs.
    """

    def __init__(self, config: Any, store: Any) -> None:
        self.config = config
        self.store = store

    @staticmethod
    def _default_memory_summary() -> dict[str, Any]:
        return {
            "schema": "scambait.memory.v1",
            "claimed_identity": {"name": "", "role_claim": "", "confidence": "low"},
            "narrative": {"phase": "unknown", "short_story": "", "timeline_points": []},
            "current_intent": {"scammer_intent": "", "baiter_intent": "", "latest_topic": ""},
            "key_facts": {},
            "risk_flags": [],
            "open_questions": [],
            "next_focus": [],
        }

    async def start(self) -> None:  # pragma: no cover - lifecycle hook
        return

    async def close(self) -> None:  # pragma: no cover - lifecycle hook
        return

    async def build_chat_context(self, chat_id: int) -> ChatContext | None:
        events = self.store.list_events(chat_id=chat_id, limit=500)
        if not events:
            return None
        messages: list[dict[str, Any]] = []
        for event in events:
            messages.append(
                {
                    "event_type": event.event_type,
                    "role": event.role,
                    "text": event.text,
                    "ts_utc": event.ts_utc,
                    "meta": event.meta,
                }
            )
        return ChatContext(chat_id=chat_id, title=f"chat-{chat_id}", messages=messages)

    def get_recent_typing_hint(self, chat_id: int, max_age_seconds: int = 120) -> dict[str, Any] | None:
        _ = (chat_id, max_age_seconds)
        return None

    def build_prompt_events(self, chat_id: int, token_limit: int | None = None) -> list[dict[str, Any]]:
        token_budget = token_limit if token_limit is not None else int(getattr(self.config, "hf_max_tokens", 1500))
        events = self.store.list_events(chat_id=chat_id, limit=5000)
        prompt_events: list[dict[str, Any]] = []
        for event in events:
            # Legacy rows may contain synthetic profile_update system messages from older builds.
            # Skip these in prompt context to avoid profile noise amplification.
            if str(getattr(event, "role", "")) == "system":
                text_value = getattr(event, "text", None)
                if isinstance(text_value, str) and text_value.strip().startswith("profile_update:"):
                    continue
            prompt_events.append(
                {
                    "event_type": event.event_type,
                    "role": event.role,
                    "text": event.text,
                    "time": self._as_hhmm(event.ts_utc),
                    "meta": event.meta,
                }
            )
        return self._trim_prompt_events(prompt_events, token_budget)

    def compute_timing_stats(self, chat_id: int) -> dict[str, Any]:
        """Compute timing statistics for pacing decisions.

        Returns:
        {
            "now_ts": int,              # Current UTC timestamp (seconds since epoch)
            "secs_since_last_inbound": int | None,    # Seconds since last scammer message
            "secs_since_last_outbound": int | None,   # Seconds since last scambaiter message
            "inbound_burst_count_120s": int,          # Number of scammer messages in last 120s
            "avg_inbound_latency_s": float | None,    # Average response latency (scammer->scambaiter)
        }
        """
        events = self.store.list_events(chat_id=chat_id, limit=200)
        if not events:
            return {
                "now_ts": int(datetime.now(timezone.utc).timestamp()),
                "secs_since_last_inbound": None,
                "secs_since_last_outbound": None,
                "inbound_burst_count_120s": 0,
                "avg_inbound_latency_s": None,
            }

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # Parse all events with valid timestamps
        timestamped_events: list[tuple[int, str, str]] = []  # (ts, role, event_type)
        for event in events:
            ts_utc = event.ts_utc
            if not ts_utc:
                continue
            try:
                # Parse ISO 8601 timestamp to seconds since epoch
                dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
                ts = int(dt.timestamp())
                timestamped_events.append((ts, event.role, event.event_type))
            except (ValueError, AttributeError):
                continue

        if not timestamped_events:
            return {
                "now_ts": now_ts,
                "secs_since_last_inbound": None,
                "secs_since_last_outbound": None,
                "inbound_burst_count_120s": 0,
                "avg_inbound_latency_s": None,
            }

        # Compute secs_since_last_inbound and secs_since_last_outbound
        secs_since_last_inbound: int | None = None
        secs_since_last_outbound: int | None = None

        for ts, role, _ in reversed(timestamped_events):
            if secs_since_last_inbound is None and role == "scammer":
                secs_since_last_inbound = max(0, now_ts - ts)
            if secs_since_last_outbound is None and role == "scambaiter":
                secs_since_last_outbound = max(0, now_ts - ts)
            if secs_since_last_inbound is not None and secs_since_last_outbound is not None:
                break

        # Count inbound messages in last 120 seconds
        inbound_burst_count_120s = 0
        for ts, role, _ in timestamped_events:
            if role == "scammer" and (now_ts - ts) <= 120:
                inbound_burst_count_120s += 1

        # Calculate average inbound latency (scammer -> scambaiter response time)
        latencies: list[float] = []
        for i, (inbound_ts, inbound_role, _) in enumerate(timestamped_events):
            if inbound_role != "scammer":
                continue
            # Find the next scambaiter response
            for j in range(i + 1, len(timestamped_events)):
                response_ts, response_role, _ = timestamped_events[j]
                if response_role == "scambaiter":
                    latency = response_ts - inbound_ts
                    if latency > 0:
                        latencies.append(float(latency))
                    break

        avg_inbound_latency_s: float | None = None
        if latencies:
            avg_inbound_latency_s = sum(latencies) / len(latencies)

        return {
            "now_ts": now_ts,
            "secs_since_last_inbound": secs_since_last_inbound,
            "secs_since_last_outbound": secs_since_last_outbound,
            "inbound_burst_count_120s": inbound_burst_count_120s,
            "avg_inbound_latency_s": avg_inbound_latency_s,
        }

    def build_memory_events(self, chat_id: int, after_event_id: int = 0) -> list[dict[str, Any]]:
        events = self.store.list_events(chat_id=chat_id, limit=5000)
        out: list[dict[str, Any]] = []
        for event in events:
            event_id = int(getattr(event, "id", 0))
            if event_id <= int(after_event_id):
                continue
            meta = getattr(event, "meta", None)
            if not isinstance(meta, dict):
                meta = {}
            media_type = str(getattr(event, "event_type", "") or "")
            caption = None
            if media_type == "photo":
                text_value = getattr(event, "text", None)
                if isinstance(text_value, str) and text_value.strip():
                    caption = text_value.strip()
            out.append(
                {
                    "event_id": event_id,
                    "ts_utc": getattr(event, "ts_utc", None),
                    "role": getattr(event, "role", None),
                    "scammer_username": scammer_name_from_meta(meta),
                    "baiter_username": baiter_name_from_meta(meta),
                    "text": getattr(event, "text", None),
                    "caption": caption,
                    "citation": meta.get("citation"),
                    "media_type": media_type,
                }
            )
        return out

    def _build_memory_messages(
        self,
        *,
        chat_id: int,
        cursor_event_id: int,
        existing_memory: dict[str, Any] | None,
        events: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        payload = {
            "schema": "scambait.memory.input.v1",
            "chat_id": chat_id,
            "memory_cursor_event_id": int(cursor_event_id),
            "existing_memory": existing_memory,
            "events": events,
        }
        return [
            {"role": "system", "content": MEMORY_SUMMARY_PROMPT_CONTRACT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
        ]

    @staticmethod
    def _parse_memory_summary_output(text: str) -> dict[str, Any] | None:
        cleaned = strip_think_segments(text)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        if str(value.get("schema") or "").strip() != "scambait.memory.v1":
            return None
        required = {
            "claimed_identity",
            "narrative",
            "current_intent",
            "key_facts",
            "risk_flags",
            "open_questions",
            "next_focus",
        }
        if not required.issubset(value.keys()):
            return None
        return value

    def ensure_memory_context(self, chat_id: int, force_refresh: bool = False) -> dict[str, Any]:
        current = self.store.get_summary(chat_id=chat_id)
        latest_events = self.store.list_events(chat_id=chat_id, limit=5000)
        latest_id = int(latest_events[-1].id) if latest_events else 0

        if current is not None and not force_refresh and int(current.cursor_event_id) >= latest_id:
            return {"summary": current.summary, "cursor_event_id": current.cursor_event_id, "updated": False}

        cursor = 0
        existing_summary = None
        if current is not None:
            existing_summary = current.summary
            if not force_refresh:
                cursor = int(current.cursor_event_id)
        token = (getattr(self.config, "hf_token", None) or "").strip()
        model = (getattr(self.config, "hf_memory_model", None) or "").strip() or "openai/gpt-oss-120b"
        max_tokens = int(getattr(self.config, "hf_memory_max_tokens", 150000))
        if not token:
            # Offline-safe fallback when HF token is unavailable.
            fallback_summary = self._default_memory_summary()
            if current is None or force_refresh or int(current.cursor_event_id) < latest_id:
                saved = self.store.upsert_summary(
                    chat_id=chat_id,
                    summary=fallback_summary,
                    cursor_event_id=latest_id,
                    model=model,
                )
                return {"summary": saved.summary, "cursor_event_id": saved.cursor_event_id, "updated": True}
            return {"summary": current.summary, "cursor_event_id": current.cursor_event_id, "updated": False}

        events = self.build_memory_events(chat_id=chat_id, after_event_id=cursor)
        if not events:
            if current is not None:
                return {"summary": current.summary, "cursor_event_id": current.cursor_event_id, "updated": False}
            empty_summary = self._default_memory_summary()
            saved = self.store.upsert_summary(
                chat_id=chat_id,
                summary=empty_summary,
                cursor_event_id=0,
                model=model,
            )
            return {"summary": saved.summary, "cursor_event_id": saved.cursor_event_id, "updated": True}

        latest_cursor = int(events[-1]["event_id"])
        messages = self._build_memory_messages(
            chat_id=chat_id,
            cursor_event_id=cursor,
            existing_memory=existing_summary,
            events=events,
        )
        try:
            response = call_hf_openai_chat(
                token=token,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                base_url=(getattr(self.config, "hf_base_url", None) or None),
            )
            result_text = extract_result_text(response)
            parsed = self._parse_memory_summary_output(result_text)
            if parsed is None:
                raise RuntimeError("invalid memory summary contract (expected scambait.memory.v1)")
        except Exception as exc:
            _log.warning(
                "ensure_memory_context: memory summary failed for chat_id=%d model=%s: %s",
                chat_id,
                model,
                exc,
            )
            if current is not None:
                return {"summary": current.summary, "cursor_event_id": current.cursor_event_id, "updated": False}
            fallback_summary = self._default_memory_summary()
            saved = self.store.upsert_summary(
                chat_id=chat_id,
                summary=fallback_summary,
                cursor_event_id=latest_cursor,
                model=model,
            )
            return {"summary": saved.summary, "cursor_event_id": saved.cursor_event_id, "updated": True}
        saved = self.store.upsert_summary(
            chat_id=chat_id,
            summary=parsed,
            cursor_event_id=latest_cursor,
            model=model,
        )
        return {"summary": saved.summary, "cursor_event_id": saved.cursor_event_id, "updated": True}

    def build_model_messages(
        self,
        chat_id: int,
        token_limit: int | None = None,
        force_refresh_memory: bool = False,
        include_memory: bool = True,
        timing: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        prompt_events = self.build_prompt_events(chat_id=chat_id, token_limit=token_limit)
        memory_state: dict[str, Any] = {"summary": {}, "cursor_event_id": 0, "updated": False}
        if include_memory:
            memory_state = self.ensure_memory_context(chat_id=chat_id, force_refresh=force_refresh_memory)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT_CONTRACT},
            {
                "role": "system",
                "content": (
                    "Memory summary for chat_id="
                    f"{chat_id}: {json.dumps(memory_state.get('summary') or {}, ensure_ascii=True)}"
                ),
            },
        ]
        if timing is not None:
            messages.append(
                {
                    "role": "system",
                    "content": TIMING_PROMPT_RULES + "\n\nTiming data:\n" + json.dumps(timing, ensure_ascii=True),
                }
            )
        for event in prompt_events:
            payload = {
                "time": event.get("time"),
                "role": event.get("role"),
                "event_type": event.get("event_type"),
                "text": event.get("text"),
            }
            messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=True)})
        return messages

    @staticmethod
    def _extract_analysis_reason_from_result_text(result_text: str) -> str:
        cleaned = strip_think_segments(result_text or "")
        if not cleaned:
            return ""
        try:
            loaded = json.loads(cleaned)
        except Exception:
            return ""
        if not isinstance(loaded, dict):
            return ""
        analysis = loaded.get("analysis")
        if not isinstance(analysis, dict):
            return ""
        reason = analysis.get("reason")
        if not isinstance(reason, str):
            return ""
        return reason.strip()

    @staticmethod
    def _classify_conflict_code(reason: str) -> str:
        text = (reason or "").strip().lower()
        if not text:
            return "operator_required"
        if "insufficient" in text or "not enough context" in text or "unclear" in text or "uncertain" in text:
            return "insufficient_context"
        if "policy" in text or "cannot" in text or "can't" in text or "unable" in text:
            return "policy_tension"
        if "stall" in text:
            return "conversation_stall"
        if "target" in text:
            return "uncertain_target"
        return "operator_required"

    def _detect_semantic_conflict(self, parsed: ModelOutput | None, result_text: str) -> tuple[bool, dict[str, Any] | None]:
        if parsed is not None and isinstance(parsed.conflict, dict):
            return True, parsed.conflict
        analysis_reason = ""
        if parsed is not None and isinstance(parsed.analysis, dict):
            candidate = parsed.analysis.get("reason")
            if isinstance(candidate, str):
                analysis_reason = candidate.strip()
        if not analysis_reason:
            analysis_reason = self._extract_analysis_reason_from_result_text(result_text)
        reason_lower = analysis_reason.lower()
        has_reason_hint = bool(reason_lower) and any(hint in reason_lower for hint in SEMANTIC_CONFLICT_REASON_HINTS)
        has_escalate = False
        if parsed is not None:
            for action in parsed.actions:
                if isinstance(action, dict) and str(action.get("type") or "") == "escalate_to_human":
                    has_escalate = True
                    break
        if has_escalate or has_reason_hint:
            reason = analysis_reason or "Semantic conflict signaled by model output."
            return True, {
                "type": "semantic_conflict",
                "code": self._classify_conflict_code(reason),
                "reason": reason,
                "requires_human": True,
                "suggested_mode": "hold",
            }
        return False, None

    @staticmethod
    def _parse_meta_turn_output(result_text: str) -> dict[str, Any] | None:
        cleaned = strip_think_segments(result_text or "")
        if not cleaned:
            return None
        try:
            loaded = json.loads(cleaned)
        except Exception:
            return None
        if not isinstance(loaded, dict):
            return None
        if str(loaded.get("schema") or "").strip() != "scambait.meta.turn.v1":
            return None
        recommended_text = loaded.get("recommended_text")
        turn_options = loaded.get("turn_options")
        if not isinstance(recommended_text, str) or not recommended_text.strip():
            return None
        if not isinstance(turn_options, list):
            return None
        normalized_options: list[dict[str, str]] = []
        for item in turn_options[:3]:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            strategy = item.get("strategy")
            risk = item.get("risk")
            if not isinstance(text, str) or not text.strip():
                continue
            if not isinstance(strategy, str):
                strategy = ""
            risk_value = str(risk or "").strip().lower()
            if risk_value not in {"low", "med", "high"}:
                risk_value = "med"
            normalized_options.append(
                {
                    "text": text.strip(),
                    "strategy": strategy.strip(),
                    "risk": risk_value,
                }
            )
        if not normalized_options:
            normalized_options.append({"text": recommended_text.strip(), "strategy": "", "risk": "med"})
        return {
            "schema": "scambait.meta.turn.v1",
            "recommended_text": recommended_text.strip(),
            "turn_options": normalized_options,
        }

    def _build_semantic_pivot(self, chat_id: int, conflict: dict[str, Any] | None) -> dict[str, Any] | None:
        token = (getattr(self.config, "hf_token", None) or "").strip()
        if not token:
            return None
        model = (getattr(self.config, "hf_memory_model", None) or "").strip() or (getattr(self.config, "hf_model", None) or "").strip()
        if not model:
            return None
        max_tokens = int(getattr(self.config, "hf_memory_max_tokens", 150000))
        prompt_events = self.build_prompt_events(chat_id=chat_id, token_limit=int(getattr(self.config, "hf_max_tokens", 1500)))
        memory_state = self.store.get_summary(chat_id=chat_id)
        payload = {
            "schema": "scambait.meta.turn.input.v1",
            "chat_id": chat_id,
            "conflict": conflict or {},
            "recent_messages": prompt_events[-20:],
            "memory_summary": memory_state.summary if memory_state is not None else {},
        }
        messages = [
            {"role": "system", "content": META_TURN_PROMPT_CONTRACT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
        ]
        response = call_hf_openai_chat(
            token=token,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            base_url=None,
        )
        result_text = extract_result_text(response)
        parsed = self._parse_meta_turn_output(result_text)
        if parsed is None:
            raise RuntimeError("invalid meta turn contract (expected scambait.meta.turn.v1)")
        parsed["model"] = model
        return parsed

    def run_hf_dry_run(self, chat_id: int, include_timing: bool = True) -> dict[str, Any]:
        token = (getattr(self.config, "hf_token", None) or "").strip()
        model = (getattr(self.config, "hf_model", None) or "").strip()
        if not token or not model:
            raise RuntimeError("HF_TOKEN/HF_MODEL missing")
        max_tokens = int(getattr(self.config, "hf_max_tokens", 1500))

        attempts: list[dict[str, Any]] = []
        timing = self.compute_timing_stats(chat_id) if include_timing else None
        initial_messages = self.build_model_messages(chat_id=chat_id, include_memory=False, timing=timing)
        initial_prompt = {"messages": initial_messages, "max_tokens": max_tokens}
        reasoning_cycles = 0
        reasoning_snippet: str = ""

        try:
            initial_response = call_hf_openai_chat(
                token=token,
                model=model,
                messages=initial_messages,
                max_tokens=max_tokens,
                # Dry run is pinned to HF router to avoid accidental provider drift via HF_BASE_URL.
                base_url=None,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
            )
            initial_text = extract_result_text(initial_response)
            initial_tool_calls = extract_tool_calls(initial_response)
            if not initial_text and initial_tool_calls:
                initial_text = json.dumps(initial_tool_calls, ensure_ascii=True)
            reasoning_cycles, reasoning_snippet = extract_reasoning_details(initial_response)
        except Exception as exc:
            attempts.append(
                {
                    "phase": "initial",
                    "status": "error",
                    "accepted": False,
                    "reject_reason": "provider_error",
                    "error_message": str(exc),
                    "prompt_json": initial_prompt,
                    "response_json": {},
                    "result_text": "",
                }
            )
            return {
                "provider": "huggingface_openai_compat",
                "model": model,
                "prompt_json": initial_prompt,
                "response_json": {},
                "result_text": "",
                "valid_output": False,
                "parsed_output": None,
                "error_message": str(exc),
                "outcome_class": "provider_error",
                "semantic_conflict": False,
                "conflict": None,
                "pivot": None,
                "repair_available": False,
                "repair_context": None,
                "attempts": attempts,
                "reasoning_cycles": reasoning_cycles,
                "reasoning_snippet": reasoning_snippet,
            }

        parsed_result, memory_pairs = parse_tool_calls_to_model_output(initial_tool_calls, raw_response=initial_text)
        parsed = parsed_result.output
        initial_reject_reason: str | None = None
        if parsed is None:
            initial_reject_reason = "no_tool_calls"
        elif violates_scambait_style_policy(parsed.suggestion):
            initial_reject_reason = "style_policy_violation"
            parsed = None
        else:
            for k, v in memory_pairs:
                self.store.set_memory_kv(chat_id, k, v)
        attempts.append(
            {
                "phase": "initial",
                "status": "ok" if parsed is not None else "invalid",
                "accepted": parsed is not None,
                "reject_reason": initial_reject_reason,
                "error_message": None,
                "contract_issues": [item.as_dict() for item in parsed_result.issues] if parsed is None else [],
                "prompt_json": initial_prompt,
                "response_json": initial_response,
                "result_text": initial_text,
            }
        )

        final_reasoning_cycles = reasoning_cycles
        final_reasoning_snippet = reasoning_snippet
        final_prompt = initial_prompt
        final_response = initial_response
        final_text = initial_text

        if parsed is None and not initial_tool_calls:
            follow_messages = initial_messages + [
                {"role": "user", "content": "Please use the available tools to respond."}
            ]
            follow_prompt = {"messages": follow_messages, "max_tokens": max_tokens}
            try:
                follow_response = call_hf_openai_chat(
                    token=token,
                    model=model,
                    messages=follow_messages,
                    max_tokens=max_tokens,
                    base_url=None,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="required",
                )
                follow_text = extract_result_text(follow_response)
                follow_tool_calls = extract_tool_calls(follow_response)
                if not follow_text and follow_tool_calls:
                    follow_text = json.dumps(follow_tool_calls, ensure_ascii=True)
            except Exception as exc:
                attempts.append(
                    {
                        "phase": "tool_retry",
                        "status": "error",
                        "accepted": False,
                        "reject_reason": "provider_error",
                        "error_message": str(exc),
                        "prompt_json": follow_prompt,
                        "response_json": {},
                        "result_text": "",
                    }
                )
                return {
                    "provider": "huggingface_openai_compat",
                    "model": model,
                    "prompt_json": follow_prompt,
                    "response_json": {},
                    "result_text": "",
                    "valid_output": False,
                    "parsed_output": None,
                    "contract_issues": [],
                    "outcome_class": "provider_error",
                    "semantic_conflict": False,
                    "conflict": None,
                    "pivot": None,
                    "repair_available": False,
                    "repair_context": None,
                    "error_message": str(exc),
                    "attempts": attempts,
                    "reasoning_cycles": final_reasoning_cycles,
                    "reasoning_snippet": final_reasoning_snippet,
                }
            follow_result, follow_memory_pairs = parse_tool_calls_to_model_output(follow_tool_calls, raw_response=follow_text)
            parsed = follow_result.output
            parsed_result = follow_result
            if parsed is not None and not violates_scambait_style_policy(parsed.suggestion):
                for k, v in follow_memory_pairs:
                    self.store.set_memory_kv(chat_id, k, v)
            else:
                if parsed is not None:
                    parsed = None
            attempts.append(
                {
                    "phase": "tool_retry",
                    "status": "ok" if parsed is not None else "invalid",
                    "accepted": parsed is not None,
                    "reject_reason": None,
                    "error_message": None,
                    "contract_issues": [item.as_dict() for item in follow_result.issues] if parsed is None else [],
                    "prompt_json": follow_prompt,
                    "response_json": follow_response,
                    "result_text": follow_text,
                }
            )
            final_prompt = follow_prompt
            final_response = follow_response
            final_text = follow_text

        contract_issues: list[dict[str, Any]] = []
        if parsed is None:
            for attempt in reversed(attempts):
                candidate = attempt.get("contract_issues")
                if isinstance(candidate, list) and candidate:
                    contract_issues = candidate
                    break
        first_issue = contract_issues[0] if contract_issues else None
        first_issue_str = ""
        if isinstance(first_issue, dict):
            issue_path = str(first_issue.get("path") or "").strip()
            issue_reason = str(first_issue.get("reason") or "").strip()
            if issue_path or issue_reason:
                first_issue_str = f" ({issue_path}: {issue_reason})"

        semantic_conflict, conflict_payload = self._detect_semantic_conflict(parsed=parsed, result_text=final_text)
        pivot_payload: dict[str, Any] | None = None
        if semantic_conflict:
            try:
                pivot_payload = self._build_semantic_pivot(chat_id=chat_id, conflict=conflict_payload)
            except Exception as exc:
                pivot_payload = {"error": str(exc)}
            # Keep a deterministic trail in attempts: conflict means operator decision is still required.
            if attempts:
                attempts[-1]["accepted"] = False
                attempts[-1]["reject_reason"] = "semantic_conflict"
                attempts[-1]["status"] = "invalid"

        if semantic_conflict:
            outcome_class = "semantic_conflict"
        elif parsed is None and any(str(item.get("reject_reason") or "") == "style_policy_violation" for item in attempts):
            outcome_class = "style_violation"
        elif parsed is None:
            outcome_class = "contract_invalid"
        else:
            outcome_class = "ok"

        return {
            "provider": "huggingface_openai_compat",
            "model": model,
            "prompt_json": final_prompt,
            "response_json": final_response,
            "result_text": final_text,
            "valid_output": parsed is not None,
            "parsed_output": {
                "analysis": parsed.analysis,
                "message": {"text": parsed.suggestion},
                "actions": parsed.actions,
                "metadata": parsed.metadata,
                "conflict": parsed.conflict,
            }
            if parsed is not None
            else None,
            "contract_issues": contract_issues,
            "outcome_class": outcome_class,
            "semantic_conflict": semantic_conflict,
            "conflict": conflict_payload,
            "pivot": pivot_payload,
            "repair_available": parsed is None,
            "repair_context": (
                {
                    "chat_id": chat_id,
                    "reject_reason": initial_reject_reason or "no_tool_calls",
                    "failed_generation_excerpt": final_text[:2000],
                }
                if parsed is None
                else None
            ),
            "error_message": (
                None
                if parsed is not None and not semantic_conflict
                else (
                    "semantic conflict detected (operator decision required)"
                    if semantic_conflict
                    else (
                        "model output violates scambait style policy"
                        if any(str(item.get("reject_reason") or "") == "style_policy_violation" for item in attempts)
                        else "no tool calls in model output"
                        + first_issue_str
                    )
                )
            ),
            "attempts": attempts,
            "reasoning_cycles": final_reasoning_cycles,
            "reasoning_snippet": final_reasoning_snippet,
        }

    def run_hf_dry_run_repair(
        self,
        chat_id: int,
        failed_generation: str,
        reject_reason: str = "contract_validation_failed",
    ) -> dict[str, Any]:
        # `failed_generation` and `reject_reason` are kept for API compatibility but no longer
        # embedded in the prompt — the repair simply forces tool use on a fresh context rebuild.
        _ = (failed_generation, reject_reason)
        token = (getattr(self.config, "hf_token", None) or "").strip()
        model = (getattr(self.config, "hf_model", None) or "").strip()
        if not token or not model:
            raise RuntimeError("HF_TOKEN/HF_MODEL missing")
        max_tokens = int(getattr(self.config, "hf_max_tokens", 1500))
        repair_messages = self.build_model_messages(chat_id=chat_id, include_memory=False)
        repair_prompt = {"messages": repair_messages, "max_tokens": max_tokens}
        attempts: list[dict[str, Any]] = []
        try:
            repair_response = call_hf_openai_chat(
                token=token,
                model=model,
                messages=repair_messages,
                max_tokens=max_tokens,
                base_url=None,
                tools=TOOL_DEFINITIONS,
                tool_choice="required",
            )
            repair_text = extract_result_text(repair_response)
            repair_tool_calls = extract_tool_calls(repair_response)
            if not repair_text and repair_tool_calls:
                repair_text = json.dumps(repair_tool_calls, ensure_ascii=True)
        except Exception as exc:
            attempts.append(
                {
                    "phase": "repair",
                    "status": "error",
                    "accepted": False,
                    "reject_reason": "provider_error",
                    "error_message": str(exc),
                    "prompt_json": repair_prompt,
                    "response_json": {},
                    "result_text": "",
                }
            )
            return {
                "provider": "huggingface_openai_compat",
                "model": model,
                "prompt_json": repair_prompt,
                "response_json": {},
                "result_text": "",
                "valid_output": False,
                "parsed_output": None,
                "error_message": str(exc),
                "outcome_class": "provider_error",
                "semantic_conflict": False,
                "conflict": None,
                "pivot": None,
                "repair_available": False,
                "repair_context": None,
                "attempts": attempts,
            }

        repaired_result, repair_memory_pairs = parse_tool_calls_to_model_output(repair_tool_calls, raw_response=repair_text)
        repaired = repaired_result.output
        repair_reject_reason: str | None = None
        if repaired is None:
            repair_reject_reason = "no_tool_calls"
        elif violates_scambait_style_policy(repaired.suggestion):
            repair_reject_reason = "style_policy_violation"
            repaired = None
        else:
            for k, v in repair_memory_pairs:
                self.store.set_memory_kv(chat_id, k, v)
        attempts.append(
            {
                "phase": "repair",
                "status": "ok" if repaired is not None else "invalid",
                "accepted": repaired is not None,
                "reject_reason": repair_reject_reason,
                "error_message": None,
                "contract_issues": [item.as_dict() for item in repaired_result.issues] if repaired is None else [],
                "prompt_json": repair_prompt,
                "response_json": repair_response,
                "result_text": repair_text,
            }
        )

        contract_issues: list[dict[str, Any]] = []
        if repaired is None:
            contract_issues = [item.as_dict() for item in repaired_result.issues]
        first_issue = contract_issues[0] if contract_issues else None
        first_issue_str = ""
        if isinstance(first_issue, dict):
            issue_path = str(first_issue.get("path") or "").strip()
            issue_reason = str(first_issue.get("reason") or "").strip()
            if issue_path or issue_reason:
                first_issue_str = f" ({issue_path}: {issue_reason})"

        semantic_conflict, conflict_payload = self._detect_semantic_conflict(parsed=repaired, result_text=repair_text)
        pivot_payload: dict[str, Any] | None = None
        if semantic_conflict:
            try:
                pivot_payload = self._build_semantic_pivot(chat_id=chat_id, conflict=conflict_payload)
            except Exception as exc:
                pivot_payload = {"error": str(exc)}
            attempts[-1]["accepted"] = False
            attempts[-1]["reject_reason"] = "semantic_conflict"
            attempts[-1]["status"] = "invalid"

        if semantic_conflict:
            outcome_class = "semantic_conflict"
        elif repaired is None and repair_reject_reason == "style_policy_violation":
            outcome_class = "style_violation"
        elif repaired is None:
            outcome_class = "contract_invalid"
        else:
            outcome_class = "ok"

        return {
            "provider": "huggingface_openai_compat",
            "model": model,
            "prompt_json": repair_prompt,
            "response_json": repair_response,
            "result_text": repair_text,
            "valid_output": repaired is not None,
            "parsed_output": {
                "analysis": repaired.analysis,
                "message": {"text": repaired.suggestion},
                "actions": repaired.actions,
                "metadata": repaired.metadata,
                "conflict": repaired.conflict,
            }
            if repaired is not None
            else None,
            "contract_issues": contract_issues,
            "outcome_class": outcome_class,
            "semantic_conflict": semantic_conflict,
            "conflict": conflict_payload,
            "pivot": pivot_payload,
            "repair_available": repaired is None,
            "repair_context": (
                {
                    "chat_id": chat_id,
                    "reject_reason": repair_reject_reason or "no_tool_calls",
                    "failed_generation_excerpt": repair_text[:2000],
                }
                if repaired is None
                else None
            ),
            "error_message": (
                None
                if repaired is not None and not semantic_conflict
                else (
                    "semantic conflict detected (operator decision required)"
                    if semantic_conflict
                    else (
                        "model output violates scambait style policy"
                        if repair_reject_reason == "style_policy_violation"
                        else "no tool calls in repair output"
                        + first_issue_str
                    )
                )
            ),
            "attempts": attempts,
        }

    def generate_output(
        self,
        context: ChatContext,
        language_hint: str | None = None,
        prompt_context: dict[str, Any] | None = None,
    ) -> ModelOutput:
        _ = (language_hint, prompt_context)
        last_text = ""
        for message in reversed(context.messages):
            if isinstance(message, dict):
                candidate = message.get("text")
                if isinstance(candidate, str) and candidate.strip():
                    last_text = candidate.strip()
                    break
            elif isinstance(message, ChatEvent) and isinstance(message.text, str) and message.text.strip():
                last_text = message.text.strip()
                break
        suggestion = "Noted."
        if last_text:
            suggestion = f"Noted: {last_text[:120]}"

        candidate_payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {"text": suggestion},
            "actions": [{"type": "send_message", "message": {"text": suggestion}}],
        }
        raw = json.dumps(candidate_payload, ensure_ascii=True)
        parsed = parse_structured_model_output(raw)
        if parsed is not None:
            return parsed

        # Safety fallback (should be unreachable unless parser contract changes unexpectedly).
        return ModelOutput(
            raw=raw,
            suggestion=suggestion,
            analysis={},
            metadata={"schema": "scambait.llm.v1", "fallback": True},
            actions=[{"type": "noop"}],
        )

    @staticmethod
    def _as_hhmm(ts_utc: str | None) -> str | None:
        if not ts_utc:
            return None
        try:
            cleaned = ts_utc.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(cleaned)
            return parsed.astimezone(timezone.utc).strftime("%H:%M")
        except ValueError:
            return ts_utc[-8:-3] if len(ts_utc) >= 5 else ts_utc

    @classmethod
    def _trim_prompt_events(cls, events: list[dict[str, Any]], token_limit: int) -> list[dict[str, Any]]:
        if token_limit <= 0:
            return []
        kept: list[dict[str, Any]] = []
        running = 0
        # Events come in chronological order (oldest→newest, from list_events ORDER BY id ASC).
        # Keep newest events by walking backwards, then reverse to maintain chronological order.
        for event in reversed(events):
            estimated = cls._estimate_tokens(event)
            if kept and running + estimated > token_limit:
                break
            if not kept and estimated > token_limit:
                # Ensure we keep at least one newest event.
                kept.append(event)
                break
            kept.append(event)
            running += estimated
        kept.reverse()  # Reverse to chronological order (oldest → newest)
        return kept

    @staticmethod
    def _estimate_tokens(event: dict[str, Any]) -> int:
        text = str(event.get("text") or "")
        meta = str(event.get("meta") or "")
        base = len(text) + len(meta) + 24
        return max(1, base // 4)
