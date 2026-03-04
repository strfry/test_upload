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

## Deployment

**Deploy = push nach Uberspace:**
```bash
git push uberspace master
```
Der git hook (`hooks/post-receive`) auf Uberspace startet den Bot automatisch neu.  
GitHub (`origin`) ist nur Backup — kein Auto-Deploy dort.

**Server-Steuerung via SSH:**
```bash
ssh strfry.org supervisorctl stop scambaiter    # stoppen
ssh strfry.org supervisorctl start scambaiter   # starten
ssh strfry.org supervisorctl status scambaiter  # Status
```

## Scripts

`scripts/` — CLI tools: `prompt_runner.py`, `prompt_cli.py`, `chat_repl.py`, `loop_analyzer.py`, `list_chat_ids.py`, `telethon_forward_helper.py`, `dry_run_cli.py`, and others.

## Notes

- All documentation is written in German.
- The LLM contract schema (`scambait.llm.v1`) is an internal Core interface, not the external bot API.
- Core must never send messages directly — only Telethon does.

## ScamBaiterDebugBot — Nächstes Experiment

Ein zweiter Telegram-Bot als Orchestrierungs-Agent. Ziel: die manuelle Operator-Rolle beim "wann soll ein Prompt laufen / welche Direktive hilft / ist der Vorschlag gut genug" durch einen LLM-Agenten ersetzen, ohne `scambait.llm.v1` anzufassen.

**Kernidee:**
- `ScamBaiterControl` und `ScamBaiterDebugBot` laufen in einer **Telegram-Gruppe** (löst Bot-zu-Bot-Kommunikationsproblem)
- Agent liest State-Snapshot aus SQLite (read-only), postet Recommendation-Cards mit eigenen Inline-Buttons
- Agent ersetzt nicht den Core — er entscheidet nur *wann* und *wie* er aufgerufen wird
- Timing (Pacing, Wartezeiten) bleibt deterministischer Code im Service-Layer; Agent sagt nur `"wait"` oder `"send"`
- Drei Modi pro Chat: `MANUAL` (nur Empfehlung) → `SEMI` (sichere Aktionen auto) → `AUTO` (alles auto, opt-in)

**Agent-Prompt ist minimal:**
- Kein Gesprächskontext (das macht `scambait.llm.v1`)
- Input: State-Snapshot (last_inbound_ts, pending_suggestion, directives, loop_indicator, ...)
- Output: `{"action": "run_prompt|queue_suggestion|set_directive|wait|escalate", "reason": "...", "params": {...}}`

**Warum "Debug" im Namen:**
Erst beobachten ob der Agent sinnvolle Empfehlungen macht (MANUAL-Modus), dann Autonomie schrittweise erhöhen.

**Vollständiger Entwurf:** `docs/agent_architecture_draft.md`

**Status:** Bot-Handle wird gerade bei @BotFather registriert. Implementierung startet in neuer Session.

---

## Live-Testing mit dem DebugAgentBot (Probe)

Der `@ScamBaiterDebugBot` läuft in der Gruppe **"ScamBaiter Control Crew"** und dient als
**direktes Testinstrument für den Coding Agent** — kein manuelles Operator-Feedback nötig.

### Wie es funktioniert

Zwei Gruppen, zwei Rollen:

```
[Test-Scammer-Gruppe]          [Control-Gruppe "ScamBaiter Control Crew"]
  DebugAgentBot postet           ScamBaiterControl postet Vorschläge
  → Telethon empfängt als        → DebugAgentBot sieht Reaktionen
    echtes Scammer-Event           Operator klickt Buttons
  → Control Bot generiert
    Antwort
```

- **Test-Scammer-Gruppe** (`SCAMBAITER_TEST_CHAT_ID`): DebugAgentBot ist Mitglied,
  Gruppe liegt im "Scammers"-Telegram-Folder → Telethon behandelt sie wie einen echten Scammer-Chat.
  DebugAgentBot-Nachrichten kommen beim User-Account (Telethon) als `incoming=True` an.
- **Control-Gruppe** (`SCAMBAITER_GROUP_CHAT_ID`): Beide Bots, Operator sieht alles.
  DebugAgentBot beobachtet hier die Antworten des Control Bots.

**Warum das funktioniert:** Telegram liefert Bot-Nachrichten NICHT an andere Bots via getUpdates
(Bot-zu-Bot blockiert). Aber der Telethon-Client ist der *User-Account* — der empfängt alle
Gruppennnachrichten inkl. Bot-Nachrichten als normale Updates.

### Einmaliges Setup (manuell)

1. Neue Gruppe anlegen, z.B. "ScamBaiter Test Scammer"
2. `@ScamBaiterDebugBot` hinzufügen
3. Gruppe in Telegram-Folder **"Scammers"** verschieben (damit Telethon lauscht)
4. Chat-ID als `SCAMBAITER_TEST_CHAT_ID` in `secret.sh` eintragen

### Wann benutzen

- Nach Änderungen an `bot_api.py` / `service.py`: Testkonversation starten, Vorschlag prüfen
- Um den vollen Loop zu testen: Ingestion → Generation → Card → Send
- Um Regressions zu erkennen ohne echte Scammer zu benötigen

### Wichtige Regeln

- **Nicht in `tests/`** — Live-Tests mit echtem Telegram gehören nicht in die pytest-Suite
- DebugAgentBot registriert **keine eigenen Kommandos** die mit ScamBaiterControl kollidieren
  (`/chats`, `/status` etc. gehören ausschließlich dem Control Bot)
- Control-Chats (persönlicher Chat + Control-Gruppe) sind in `allowed_chat_ids` und werden von
  Telethon nie als Scammer-Chats behandelt (`control_chat_ids` in `start_listener`)
- Die Test-Scammer-Gruppe ist **kein** Control-Chat — sie darf nicht in `allowed_chat_ids`
- Der Server kann jederzeit gestoppt werden (`pkill -f run_control_bot`) um lokal mit
  beiden Bots zu debuggen — DebugAgentBot hat eigenen Token, kein Konflikt

### Umgebungsvariablen (alle in `secret.sh`)

| Variable | Bedeutung |
|---|---|
| `SCAMBAITER_BOT_TOKEN` | ScamBaiterControl |
| `SCAMBAITER_DEBUG_BOT_TOKEN` | DebugAgentBot (Probe) |
| `SCAMBAITER_GROUP_CHAT_ID` | Control-Gruppe "ScamBaiter Control Crew" (negativ) |
| `SCAMBAITER_CONTROL_CHAT_ID` | Persönlicher Operator-Chat |
| `SCAMBAITER_TEST_CHAT_ID` | Test-Scammer-Gruppe (DebugAgentBot sendet hier rein) |

### probe.py — End-to-End Test

```bash
source secret.sh
# Sendet Nachricht als DebugAgentBot in Test-Scammer-Gruppe,
# sammelt Antworten des Control Bots aus der Control-Gruppe
PYTHONPATH=. /home/strfry/scambaiter-venv/bin/python3 scripts/probe.py "Hallo, wer bist du?"
```

---

## Default Orientation

When the user starts a session without a specific task, or says they're not sure where to continue:

1. Read `docs/backlog.md` for open items
2. Run `git status` and `git log --oneline -8` to see recent changes
3. Summarize: current documentation state, open backlog items, and what's currently modified
4. Ask the user what they'd like to work on next
