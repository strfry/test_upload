# Client API Reference

## Scope
This document defines the external control API between:
- the user-side agent/client, and
- the Scambaiter Bot API control layer (`ScamBaiterControl`).

This document does not define the internal LLM prompt/output contract.

## Important note for agent clients
- This command API is intentionally minimal and still evolving.
- Treat it as a control surface, not as a complete product protocol.
- Prefer intent-driven command usage (`/chats`, then `/analyse` or `/prompt`, then `/runonce`) instead of assuming strict workflow semantics.
- Keep client logic resilient to wording/card-layout changes in bot replies; rely on command success/error intent first.
- `ScamBaiterControl` is the Telegram bot name for the control channel, not a separate executor module.
- Telethon remains the automatic Telegram transport (receive/send). Control commands orchestrate decisions and state.

## Compatibility baseline
- The integration must stay compatible with first-level slash commands.
- The current bot command set remains valid and is treated as the canonical capability set.
- If command names change, a compatibility mapping must be preserved.

## API layers (explicit separation)

### External control API (this document)
- Command-style interaction used by client agents.
- Responsible for triggering runs, opening chat views, reading history, and updating analysis state.

### Internal model contract (not this document)
- `scambait.llm.v1` (`schema`, `analysis`, `message`, `actions`) is internal to core/model processing.
- External clients should treat it as engine output, not as command API.

## Command model

### Preferred first-level format
- `/<command> [args...]`

### Canonical command set for agent clients
- `/chats`
- `/prompt <chat_id>`
- `/analyse <chat_id>`
- `/analyse-set <chat_id> <json_object>`
- `/queue <suggestion_id>`
- `/runonce`
- `/runonce <chat_id[,chat_id2,...]>`
- `/history`
- `/retries`
- `/last`

Notes:
- Existing runtime commands remain valid (`/runonce`, `/chats`, `/analysisget`, `/analysisset`, `/promptpreview`).
- Agent-facing aliases map to runtime commands:
  - `/prompt` -> `/promptpreview`
  - `/analyse` -> `/analysisget`
  - `/analyse-set` -> `/analysisset`
- `/suggest` is intentionally not part of the command set; queued sending is done via `/queue`.
- `chat_id` is resolved server-side from the stored suggestion id.

## Request examples

### Run once for all enabled chats
```text
/runonce
```

### Run once for selected chats
```text
/runonce 7000000001,7000000004
```

### Read and update analysis
```text
/analyse 7000000001
/analyse-set 7000000001 {"language":"en","loop_guard_active":true}
```

### Queue a specific suggestion
```text
/queue 42
```

## Response model
- Responses are BotAPI messages and/or card updates, not a separate HTTP JSON envelope.
- Commands prepare/queue work; message delivery happens through the Telethon transport path after control decisions.
- For automation clients, the normalized interpretation should extract:
  - status (`ok`, `error`, `partial`)
  - target chat id(s)
  - text payload (human-readable summary)
  - optional structured JSON block (for analysis/history commands)

## Error behavior
- Invalid command usage returns usage hints (for example: missing chat id).
- Invalid JSON for `analyse-set` (mapped to `analysisset`) returns parse/validation error text.
- Unknown or stale `suggestion_id` in `/queue` returns a not-found or invalid-state message.
- Unauthorized chat access returns an authorization failure message.

## Client responsibilities
- Own orchestration strategy (sequence, retries, backoff, timing).
- Persist command/result correlation in client logs.
- Trigger relevant control commands and read status responses reliably.
- Avoid assuming hidden transport side effects beyond documented commands and states.

## Versioning policy
- Backward-incompatible command or argument changes require:
  - update of this document,
  - compatibility mapping for command aliases,
  - release note in architecture/implementation docs.
