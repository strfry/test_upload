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
# → Hook deployed den Code (git checkout -f master + reset --hard)
# → Bot wird NICHT automatisch neu gestartet — manuell:
ssh strfry.org supervisorctl restart scambaiter
```
GitHub (`origin`) ist nur Backup — kein Auto-Deploy dort.

**Auto-Restart im Hook:** Nur wenn der Bot vorher lief (`supervisorctl status | grep RUNNING`).  
War er gestoppt, bleibt er gestoppt. Der Restart läuft via `nohup ... &` im Hintergrund,  
weil `supervisorctl restart` ~30s blockiert (wartet auf RUNNING-State) — ohne `nohup`  
würde der Hintergrund-Job beim Hook-Exit SIGHUP kriegen und abbrechen.  
Restart-Log: `/tmp/scambaiter-restart.log` auf Uberspace.

**Früherer Bug:** Der Hook fehlte `unset GIT_DIR` und `cd ~/scambaiter`, weshalb  
`git reset --hard master` im falschen Verzeichnis lief und der Working-Tree nie  
aktualisiert wurde. `receive.denyCurrentBranch = ignore` ist gesetzt damit Pushes  
auf den checked-out Branch nicht abgewiesen werden.

**Server-Steuerung via SSH:**
```bash
ssh strfry.org supervisorctl stop scambaiter    # stoppen
ssh strfry.org supervisorctl start scambaiter   # starten
ssh strfry.org supervisorctl status scambaiter  # Status
```

**Datenbank vom Server kopieren:**
SQLite läuft im WAL-Modus — einfaches `scp` der `.sqlite3`-Datei ergibt eine leere/inkonsistente Kopie,
weil die eigentlichen Daten im WAL-File (`.sqlite3-wal`) stecken. Erst checkpointen:
```bash
ssh strfry.org "python3 -c \"
import sqlite3
sqlite3.connect('/home/strfry/scambaiter/scambaiter.sqlite3').execute('PRAGMA wal_checkpoint(TRUNCATE)')
\""
scp strfry.org:~/scambaiter/scambaiter.sqlite3 ./scambaiter.sqlite3
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

### Probe-Scripts — End-to-End Tests

#### ⚠️ Voraussetzungen (immer zuerst)

```bash
# 1. Secrets laden (Bot-Token, Telethon-Credentials, Chat-IDs, ...)
source secret.sh

# 2. Virtualenv aktivieren (hat telegram, telethon, alle Deps)
source /home/strfry/scambaiter-venv/bin/activate

# 3. Uberspace-Server stoppen — sonst Conflict: two getUpdates
ssh strfry.org supervisorctl stop scambaiter
```

> **Warum stoppen?** Telegram erlaubt nur eine aktive `getUpdates`-Verbindung pro Bot-Token.
> Läuft der Bot auf Uberspace noch, scheitert jede lokale Instanz mit `Conflict`.
> Nach dem Test: `ssh strfry.org supervisorctl start scambaiter` zum Wiederstarten.

#### Lokaler Server (für Tests)

```bash
# Server starten (im Hintergrund):
# -m scripts.run_control_bot setzt sys.path korrekt — kein PYTHONPATH nötig
python3 -m scripts.run_control_bot > /tmp/bot.log 2>&1 &

# Warten bis bereit:
until grep -q "Application started" /tmp/bot.log; do sleep 1; done && echo "bereit"

# Log beobachten:
tail -f /tmp/bot.log | grep -v "getUpdates\|HTTP Request"
```

#### Uberspace (produktiv)
```bash
# Status:
ssh strfry.org supervisorctl status scambaiter
# Neustart:
ssh strfry.org supervisorctl restart scambaiter
# Logs live:
ssh strfry.org "supervisorctl tail -f scambaiter stderr"
```

#### probe_control.py — Control-Bot Kommandos und Buttons

Schickt Kommandos via Telethon an den Control Bot, klickt Buttons, liest Antworten.

```bash
# /whoami testen:
PYTHONPATH=. python3 scripts/probe_control.py --cmd "/whoami"

# Chat-Card öffnen:
PYTHONPATH=. python3 scripts/probe_control.py --chat $SCAMBAITER_TEST_CHAT_ID

# Dry-Run (Prompt + LLM, kein Senden):
PYTHONPATH=. python3 scripts/probe_control.py --chat $SCAMBAITER_TEST_CHAT_ID --dryrun

# Kompletter Flow: Dry-Run + Senden in Test-Gruppe:
PYTHONPATH=. python3 scripts/probe_control.py --chat $SCAMBAITER_TEST_CHAT_ID --dryrun --send
```

#### probe_scammer.py — Scammer-Nachrichten simulieren

Schickt Nachrichten als DebugAgentBot in die Test-Scammer-Gruppe.

```bash
PYTHONPATH=. python3 scripts/probe_scammer.py "Hallo, ich bin Lisa!"
PYTHONPATH=. python3 scripts/probe_scammer.py "Hallo" "Ich habe ein Angebot" --wait 30
```

#### probe_autosend.py — Auto-Send End-to-End Test

Testet den vollautomatischen Modus: Toggle ON → Scammer-Nachricht → Bot antwortet selbstständig.

```bash
# Beide Szenarien (empfohlen nach Änderungen an bot_api.py / service.py):
PYTHONPATH=. python3 scripts/probe_autosend.py

# Nur Szenario A (Live-Event triggert Auto-Send):
PYTHONPATH=. python3 scripts/probe_autosend.py --scenario a

# Nur Szenario B (Auto-Send bei vorhandenen Nachrichten einschalten):
PYTHONPATH=. python3 scripts/probe_autosend.py --scenario b
```

**Szenario A:** Auto-Send wird eingeschaltet, dann kommt eine neue Scammer-Nachricht rein → Telethon-Listener triggert `_cancel_and_restart_auto_send` → Bot antwortet automatisch.

**Szenario B:** Scammer-Nachrichten existieren schon im DB, dann wird Auto-Send eingeschaltet → `_start_auto_send_task` läuft sofort an (letzter Event = scammer) → Bot antwortet automatisch.

Erwartete Dauer pro Szenario: ~75–90s (Reading-Phase + LLM + Typing).

#### Bekannte Fallstricke

- **PYTHONPATH-Falle:** `python3 scripts/run_control_bot.py` scheitert mit `ModuleNotFoundError: No module named 'scambaiter'` wenn PYTHONPATH nicht gesetzt ist. Besser immer `python3 -m scripts.run_control_bot` benutzen — `-m` setzt sys.path automatisch auf das CWD.
- **Zwei Bot-Instanzen:** Wenn lokaler Server läuft UND Uberspace aktiv ist, gibt es `Conflict: terminated by other getUpdates`. Uberspace vorher stoppen: `ssh strfry.org supervisorctl stop scambaiter`
- **SQLite Lock:** Probe-Scripts brauchen `probe_session.session` (Kopie), nicht `scambaiter.session` (die der Server hält)
- **Uberspace dirty working tree:** Der git hook macht `git reset --hard master`. Falls auf Uberspace manuelle Edits gemacht wurden, vorher `ssh strfry.org "cd ~/scambaiter && git reset --hard HEAD"` ausführen
- **Negative Chat-IDs:** Alle Callback-Patterns in `bot_api.py` verwenden `-?[0-9]+` — bei Änderungen prüfen dass das erhalten bleibt

---

## Default Orientation

When the user starts a session without a specific task, or says they're not sure where to continue:

1. Read `docs/backlog.md` for open items
2. Run `git status` and `git log --oneline -8` to see recent changes
3. Summarize: current documentation state, open backlog items, and what's currently modified
4. Ask the user what they'd like to work on next
