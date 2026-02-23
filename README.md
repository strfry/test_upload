# Telegram Scambaiter

Scambaiter wird aktuell von der Dokumentation aus neu implementiert. Die Architektur- und Steuerreferenz liegt in einer Reihe von Dokumenten unter `docs/` und wird fortlaufend angepasst, während sich die Codebasis auf diesem Repo entwickelt.

## Dokumentation

- `docs/backlog.md` – Priorisiertes Backlog und Guardrail-Ideen; hier landen auch die laufenden Dokumentations-/Review-Aufgaben für Prompt, Memory und Bot-UX.
- `docs/architecture.md` – Architekturbeschreibung des Scambaiter-Systems und der Komponenten.
- `docs/implementation_plan.md` – Umsetzungsschritte und Meilensteine für die Neuentwicklung.
- `docs/client_api_reference.md` – Referenz für Bot-/Control-Channel-Befehle, Payloads und erlaubte Actions.
- `docs/event_schema_draft.md` – Entwurf für das eventbasierte Prompt/Inputmodell der Telegram-Verläufe.
- `docs/profile_schema_draft.md` – Schema, wie Chatprofile und Profiländerungen persistiert werden.
- `docs/prompt_sharpening_report.md` – Stand der Prompt-Nachscharfung, Beobachtungen und zu überprüfende Ideen.
- `docs/prompt_cases/README.md` und die zugehörigen JSON-Fixtures – strukturierte Prompt-Testfälle (Escalation, No-Repeat, Topic Guard usw.).
- `docs/card_prompt_rebuild.txt` – Notizen zum Prompt-Card/UIschicht-Relaunch.
- `docs/scambaiter-relations.md` – Beziehungsübersicht, z. B. welche Services, Skripte und Datenquellen beteiligt sind.
- `docs/snippets/prompt_konkretheit.txt` – Kürzere Richtlinie zur gewünschten Prompt-Konkretheit.
- `docs/skills.md` – Sammlung nützlicher Codex-Skills für dieses Projekt.

## Setup

```bash
pip install -r requirements.txt
```

Benötigte Umgebungsvariablen:

```bash
export TELEGRAM_API_ID="..."
export TELEGRAM_API_HASH="..."
export TELEGRAM_SESSION="scambaiter"   # optional

export HF_TOKEN="..."
export HF_MODEL="..."
export HF_VISION_MODEL="..."          # optional, fallback: HF_MODEL
export HF_BASE_URL="https://..."       # optional
export HF_MAX_TOKENS="350"             # optional
```

Optionale Laufzeit-Konfiguration:

```bash
export SCAMBAITER_FOLDER_NAME="Scammers"
export SCAMBAITER_HISTORY_LIMIT="20"
export SCAMBAITER_DEBUG="1"

export SCAMBAITER_SEND="1"
export SCAMBAITER_DELETE_OWN_AFTER_SECONDS="30"

export SCAMBAITER_INTERACTIVE="1"
export SCAMBAITER_ANALYSIS_DB_PATH="scambaiter.sqlite3"
```

## Batch-Modus

Wenn **kein** `SCAMBAITER_BOT_TOKEN` gesetzt ist, läuft das Tool einmal durch:

```bash
python scam_baiter.py
```

## BotAPI-Modus (Control Channel)

Setze einen Bot-Token, dann startet das Tool dauerhaft mit Polling:

```bash
export SCAMBAITER_BOT_TOKEN="123456:ABC..."
export SCAMBAITER_AUTO_INTERVAL_SECONDS="120"
python scam_baiter.py
```

Der Control-Chat wird automatisch über die Telegram-App-API ermittelt; nach dem Start wird dort eine Begrüßung mit verfügbaren Aktionen gepostet. Die wichtigsten Commands:

- `/whoami` – zeigt die aktuelle `chat_id`, `user_id`, konfigurierte `allowed_chat_id` und ob der aktuelle Chat autorisiert ist
- `/runonce` – Einzelnen Durchlauf starten
- `/runonce <chat_id[,chat_id2,...]>` – Gefilterter Lauf
- `/chats` – Menü für bekannte Chats mit Detailansicht (Generate, Send, Stop, Auto an/aus, Bilder, Analysis)
- `/last` – Zeigt die letzten Vorschläge (max. 5)
- `/history` – Listet die zuletzt gespeicherten Events und Analysen
- `/analysisget`/`/analysisset` – Zugriff auf gespeicherte Analysis-JSONs

Die Prompt Card im Control-Chat bietet jetzt zusätzlich eine eigene **Prompt**-Ansicht: dort sieht man die JSON-Nachricht, die an das Modell geschickt wird (Memory Summary + `model_messages`), neben den bisherigen Schema/Analysis/Message/Actions/Raw-Ansichten.

## Tests

Dev-Setup:

```bash
python3 -m pip install -r requirements-dev.txt
```

Tests ausführen:

```bash
python3 -m pytest -q
```

oder über den Repo-Runner:

```bash
python3 scripts/run_tests.py -q
```

## Chat-ID Scanner (Safe)

Nur Dialoge aus einem bestimmten Telegram-Ordner (Default `Scammers`) auslesen:

```bash
python scripts/list_chat_ids.py --folder Scammers
```

JSON-Ausgabe mit Filter:

```bash
python scripts/list_chat_ids.py --folder Scammers --filter scam --json
```

Alternativ mit semantischem Alias:

```bash
python scripts/list_chat_ids.py --folder Scammers --find julia --json
```

Sicherheitsverhalten: Wenn der Ordner nicht existiert, bricht das Skript mit Fehler ab und gibt keine IDs aus.

One-step Forward mit Query-Auflösung (nur innerhalb `Scammers`):

```bash
source secret.sh && python3 scripts/telethon_forward_helper.py --source-query julia --limit 20 --delay 0.4
```

Bei 0 oder mehreren Treffern wird nicht geforwardet; stattdessen gibt das Skript eine Treffer-/Fehlerliste aus.
Das Ziel wird standardmäßig über `@ScamBaiterControlBot` angesprochen (optional überschreibbar per `--target` oder `SCAMBAITER_CONTROL_TARGET`).

## Projektstruktur

- `scam_baiter.py`: Einstieg und Modusumschaltung
- `scambaiter/config.py`: Umgebungskonfiguration
- `scambaiter/core.py`: Kernlogik für Telegram/Model
- `scambaiter/service.py`: Laufzeit-Loop und Scheduling
- `scambaiter/bot_api.py`: Telegram-Bot, Prompt Card und Inline-Views
- `scambaiter/storage.py`: SQLite-Store für Events, Analysen, Memory Context
- `scambaiter/forward_meta.py`: Helfer für Namen/Metadaten aus Forward-Messages
- `scambaiter/model_client.py`: HF/OpenAI-Client und Ergebnisparser
- `scripts/prompt_runner.py`: Prompt-Interpretation und künftige C2-Tests
- `scripts/prompt_cli.py` / `scripts/history_cli.py`: CLI-Inspektion für Prompt/Memory/History (`--history`, `--model-view`, `--memory`, `--refresh-memory`, `--max-tokens`) inklusive profilgestützter Chatliste
- `scripts/forward_profile_cli.py`: CLI zum Extrahieren von Profilinformationen aus Beispielen
- `scripts/run_control_bot.py`: Startet den BotAPI-Modus
- `scripts/run_tests.py`: Wrapper für Entwicklertests
- `scripts/dry_run_cli.py`: Führt einen read-only Live-Dry-Run gegen das Modell aus (keine DB-Schreibvorgänge)
- `scripts/telethon_forward_helper.py`: Telethon-Helfer zum automatisierten Weiterleiten kompletter Chats (für Langzeit-Tests)
- `scripts/list_chat_ids.py`: Liest Chat-IDs ausschließlich aus einem expliziten Telegram-Ordner (Default `Scammers`)
- `scripts/loop_analyzer.py`: Analysiert Verläufe auf Loops/Wiederholungen

## Prompt-Runner

Lokale Prompt/Modellausgabe testen:

```bash
python scripts/prompt_runner.py --chat-id 123456789 --show-prompt
```

```bash
python scripts/prompt_runner.py --input-json ./case.json --show-prompt
```

Prompt ohne Modell-Request anzeigen:

```bash
python scripts/prompt_runner.py --input-json ./case.json --show-prompt --preview-only
```

Request-Body exportieren:

```bash
python scripts/prompt_runner.py \
  --input-json ./case.json \
  --preview-only \
  --dump-request-json /tmp/hf_request.json \
  --print-curl
```

Loop-Analyse (JSON-Case):

```bash
python scripts/loop_analyzer.py --input-json docs/prompt_cases/no_repeat_validator_contact.json
```

Loop-Analyse (Paste-Text):

```bash
python scripts/loop_analyzer.py \
  --transcript-file ./transcript.txt \
  --assistant-sender "Me" \
  --output-json /tmp/loop_report.json
```

Direkt ins Tool bis EOF (Ctrl+D):

```bash
python scripts/loop_analyzer.py --transcript-stdin --assistant-sender "Me"
```

## Interactive ScamBaiter REPL

Für schnelle Experimente oder manuelle Overrides gibt es jetzt eine REPL, die denselben HF-Prompt-Flow wie der Control-Chat nutzt:

```bash
python scripts/chat_repl.py
```

Jede Zeile, die du eintippst (mit Enter abgeschickt), wird als neue Scammer-Nachricht gespeichert; die REPL baut prompt/builds, ruft `HF_TOKEN`/`HF_MODEL` auf und zeigt nur ScamBaiters vorgeschlagene Antwort. Die Eingabe wird erst dann abgeschickt, wenn du eine ganze Zeile bestätigt hast, und `Ctrl+C`/EOF beendet die Session.

Konfigurierbare Flags:

- `--db PATH`: Persistiere die Konversation im angegebenen SQLite-File (Standard `:memory:` bleibt flüchtig).
- `--chat-id ID`: Nutze eine bekannte Chat-ID oder gib eine negative Test-ID vor.
- `--max-tokens N`: Begrenze Prompt + Generation.
- `--include-memory`: Lasse den Memory-Summary in die Prompt-Kette einfließen, ähnlich wie in der Control-Card.
- `--prompt-path FILE`: Schreibe das erzeugte Prompt-JSON in eine Datei, um den Payload mit der Control-Prompt-Card vergleichen zu können.

Diese REPL ergänzt die neuen manuellen Override-Features (Control-Card, Prompt-Ansicht etc.), weil sie dieselbe Prompt-, Memory- und Analysis-Pipeline nutzt. So können Operator:innen neue Dialogstränge erproben oder ungültige Responses reproduzieren, ohne die Telegram-UI zu benutzen.
