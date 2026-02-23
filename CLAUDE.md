# Scambaiter — Project Context

Scambaiter is a Telegram-based scam engagement automation system. It ingests scam conversations, uses an LLM to generate contextually appropriate baiting responses, and provides a human-in-the-loop control interface via a Telegram bot.

## Architecture

Three-layer design with strict separation:

- **Core** (`scambaiter/core.py`) — Prompt builder + LLM interface. Generates structured JSON output (`scambait.llm.v1` schema). Never sends messages itself.
- **Control Bot** (`scambaiter/bot_api.py`) — Telegram bot (`ScamBaiterControl`) for human oversight: inline buttons, prompt cards, forward ingestion, `/chats`, `/queue`, etc.
- **Telethon** (`scambaiter/telethon_executor.py`) — The sole automated sender/receiver. All delivery goes through here.
- **Storage** (`scambaiter/storage.py`) — SQLite event store (events, analyses, directives, memory context, profiles, generation attempts).
- **Service** (`scambaiter/service.py`) — Orchestration loop, pending state management.
- **Model Client** (`scambaiter/model_client.py`) — HuggingFace (primary) / OpenAI (fallback) API client.

## Operating Modes

Two modes, fixed at startup by available credentials:

- **Live Mode** — Telethon + Bot token present. Auto-receive, auto-send, profile enrichment, typing events. Single operator.
- **Relay Mode** — Bot token only. Ingestion via manual forwarding to control bot; operator copies and sends replies manually. Multi-operator capable.

Mode is exposed as `config.mode` (`"live"` / `"relay"`) and stored in `app.bot_data["mode"]`. Core and Store are mode-agnostic.

## Documentation

- `docs/architecture.md` — Component boundaries, data flow, prompt-building rules
- `docs/backlog.md` — Prioritized open items (LLM escalation, anti-loop guard, role consistency, forward-ingestion batch merge)
- `docs/client_api_reference.md` — Bot command reference, versioned
- `docs/implementation_plan.md` — Phase-based plan; currently Phase 3
- `docs/event_schema_draft.md` / `docs/profile_schema_draft.md` — Data model drafts
- `docs/prompt_sharpening_report.md` — Prompt quality observations
- `docs/prompt_cases/` — JSON test fixtures (escalation, no-repeat, topic-guard)

## Tests

`tests/` — pytest suite; run with `python3 -m pytest -q`

## Scripts

`scripts/` — CLI tools: `prompt_runner.py`, `prompt_cli.py`, `chat_repl.py`, `loop_analyzer.py`, `list_chat_ids.py`, `telethon_forward_helper.py`, `dry_run_cli.py`, and others.

## Notes

- All documentation is written in German.
- The LLM contract schema (`scambait.llm.v1`) is an internal Core interface, not the external bot API.
- Core must never send messages directly — only Telethon does.

---

## Default Orientation

When the user starts a session without a specific task, or says they're not sure where to continue:

1. Read `docs/backlog.md` for open items
2. Run `git status` and `git log --oneline -8` to see recent changes
3. Summarize: current documentation state, open backlog items, and what's currently modified
4. Ask the user what they'd like to work on next
