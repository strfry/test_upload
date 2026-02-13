# Telegram Scambaiter

Das Tool hat jetzt zwei Betriebsarten:

1. **Batch-Modus (Standard):** läuft einmal durch und zeigt Vorschläge an (wie bisher).
2. **BotAPI-Modus:** läuft dauerhaft im Hintergrund und wird per Telegram-Bot gesteuert.

## Setup

```bash
pip install -r requirements.txt
```

Pflicht-Umgebungsvariablen:

```bash
export TELEGRAM_API_ID="..."
export TELEGRAM_API_HASH="..."
export TELEGRAM_SESSION="scambaiter"   # optional

export HF_TOKEN="..."
export HF_MODEL="..."
export HF_BASE_URL="https://..."       # optional
export HF_MAX_TOKENS="350"             # optional, längere Modellantworten erlauben
```

Optionale Laufzeit-Konfiguration:

```bash
export SCAMBAITER_FOLDER_NAME="Scammers"
export SCAMBAITER_HISTORY_LIMIT="20"
export SCAMBAITER_DEBUG="1"

export SCAMBAITER_SEND="1"
export SCAMBAITER_SEND_CONFIRM="SEND"          # Pflicht, wenn SEND aktiv
export SCAMBAITER_DELETE_OWN_AFTER_SECONDS="30" # optional

export SCAMBAITER_INTERACTIVE="1"              # nur Batch-Modus
export SCAMBAITER_ANALYSIS_DB_PATH="scambaiter.sqlite3"  # Persistenz für Analysen + Key-Value Store
```

## Batch-Modus

Wenn **kein** `SCAMBAITER_BOT_TOKEN` gesetzt ist, läuft das Tool einmal:

```bash
python scam_baiter.py
```

## BotAPI-Modus (Hintergrund + Steuerung per Telegram)

Setze zusätzlich einen Bot-Token, dann startet das Tool als dauerhafter Prozess mit Polling:

```bash
export SCAMBAITER_BOT_TOKEN="123456:ABC..."
export SCAMBAITER_BOT_ALLOWED_CHAT_ID="123456789"   # optionaler Zugriffsschutz
export SCAMBAITER_AUTO_INTERVAL_SECONDS="120"

python scam_baiter.py
```

Verfügbare Bot-Kommandos:

- `/status` – zeigt Auto-Status und letzten Lauf
- `/runonce` – startet sofort einen Einmaldurchlauf
- `/runonce <chat_id[,chat_id2,...]>` – Einmaldurchlauf nur für bestimmte Chat-IDs
- `/startauto` – startet den Auto-Modus
- `/stopauto` – stoppt den Auto-Modus
- `/last` – zeigt die letzten Vorschläge (max. 5) für Analyse/Einblick
- `/history` – zeigt die letzten persistent gespeicherten Analysen inkl. Metadaten (lange Ausgaben werden in mehrere Nachrichten aufgeteilt)
- `/kvset <scammer_chat_id> <key> <value>` – setzt/überschreibt einen Key für einen Scammer
- `/kvget <scammer_chat_id> <key>` – liest einen Key für einen Scammer
- `/kvdel <scammer_chat_id> <key>` – löscht einen Key für einen Scammer
- `/kvlist <scammer_chat_id>` – listet Keys für einen Scammer

Hinweis: Nach jedem Lauf werden `analyse`, `antwort` und alle Modell-Metadaten (z.B. `sprache`) automatisch als Keys für den jeweiligen Scammer aktualisiert.

## Projektstruktur

Zur Trennung der Concerns wurde der Code aufgeteilt:

- `scam_baiter.py`: Einstieg und Modus-Umschaltung
- `scambaiter/config.py`: Umgebungsvariablen/Config
- `scambaiter/core.py`: Telegram- und HF-Kernlogik
- `scambaiter/service.py`: Hintergrund-Loop + Laufstatus
- `scambaiter/bot_api.py`: Telegram BotAPI-Kommandos
- `scambaiter/storage.py`: SQLite-Persistenz für Analysen + Scammer-spezifischen Key-Value-Store
