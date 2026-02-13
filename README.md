# Telegram Scambaiter

Das Tool hat jetzt zwei Betriebsarten:

1. **BotAPI-Modus (Standard):** läuft dauerhaft im Hintergrund und wird per Telegram-Bot gesteuert.
2. **Batch-Modus (optional):** läuft einmal durch und zeigt Vorschläge an.

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

Batch-Modus ist optional und nur aktiv, wenn du ihn explizit einschaltest:

```bash
export SCAMBAITER_BATCH_MODE="1"
python scam_baiter.py
```

## BotAPI-Modus (Hintergrund + Steuerung per Telegram)

Im Standard startet das Tool im BotAPI-Modus (dauerhaft mit Polling).
Bot-Token kann über `TELEGRAM_BOT_TOKEN`, `BOT_TOKEN` oder weiterhin optional über `SCAMBAITER_BOT_TOKEN` gesetzt werden:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export SCAMBAITER_BOT_ALLOWED_CHAT_ID="123456789"   # optionaler Zugriffsschutz
export SCAMBAITER_AUTO_INTERVAL_SECONDS="120"

python scam_baiter.py
```

Verfügbare Bot-Kommandos:

- `/start` – zeigt die Login-Anleitung
- `/login <telefonnummer>` – startet den Telethon-Login für deinen Bot-User und sendet den Telegram-Code
- `/code <PIN>` – bestätigt den Login-Code
- `/password <passwort>` – bestätigt optionales 2FA-Passwort
- `/logout` – meldet deinen Bot-User ab
- `/help` – zeigt die Hilfe mit allen Kommandos (erst nach Login)
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


Jede Bot-Anfrage wird einem Telegram-Bot-User (`effective_user.id`) zugeordnet.
Wenn dieser User noch nicht eingeloggt ist, sind nur Login-Kommandos nutzbar (`/start`, `/login`, `/code`, `/password`, `/logout`).
Wenn Telegram meldet, dass der Code abgelaufen ist, fordert der Bot jetzt automatisch einen neuen Code an und bittet erneut um `/code <PIN>`.
Die Telethon-Session wird pro Bot-User unter einem eigenen Session-Namen gespeichert.

Hinweis: Nach jedem Lauf werden `analyse`, `antwort` und alle Modell-Metadaten (z.B. `sprache`) automatisch als Keys für den jeweiligen Scammer aktualisiert.
Wenn `sprache` pro Scammer gesetzt ist (`de`/`en`), wird zusätzlich eine starke Sprach-Systeminstruktion erzwungen.

## Projektstruktur

Zur Trennung der Concerns wurde der Code aufgeteilt:

- `scam_baiter.py`: Einstieg und Modus-Umschaltung
- `scambaiter/config.py`: Umgebungsvariablen/Config
- `scambaiter/core.py`: Telegram- und HF-Kernlogik
- `scambaiter/service.py`: Hintergrund-Loop + Laufstatus
- `scambaiter/bot_api.py`: Telegram BotAPI-Kommandos
- `scambaiter/storage.py`: SQLite-Persistenz für Analysen + Scammer-spezifischen Key-Value-Store
