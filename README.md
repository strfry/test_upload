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
- `/startauto` – startet den Auto-Modus
- `/stopauto` – stoppt den Auto-Modus
- `/last` – zeigt die letzten Vorschläge (max. 5) für Analyse/Einblick

## Projektstruktur

Zur Trennung der Concerns wurde der Code aufgeteilt:

- `scam_baiter.py`: Einstieg und Modus-Umschaltung
- `scambaiter/config.py`: Umgebungsvariablen/Config
- `scambaiter/core.py`: Telegram- und HF-Kernlogik
- `scambaiter/service.py`: Hintergrund-Loop + Laufstatus
- `scambaiter/bot_api.py`: Telegram BotAPI-Kommandos
