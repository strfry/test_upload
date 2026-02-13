# Telegram Scambaiter (Vorschlagsmodus)

Dieses Skript liest nur **unbeantwortete** Telegram-Konversationen aus dem Ordner `Scammers` und erstellt für jeden Chat einen Antwortvorschlag über die `huggingface_hub` Inference API.

## Setup

1. Python 3.10+ verwenden.
2. Abhängigkeiten installieren:

```bash
pip install -r requirements.txt
```

3. Umgebungsvariablen setzen:

```bash
export TELEGRAM_API_ID="..."
export TELEGRAM_API_HASH="..."
export TELEGRAM_SESSION="scambaiter"   # optional

export HF_TOKEN="..."
export HF_MODEL="..."
# optional, falls du ein eigenes Inference Endpoint nutzen willst
export HF_BASE_URL="https://..."

# optional: Debug-Logs für Folder/ID-Matching
export SCAMBAITER_DEBUG="1"
```

## Nutzung

```bash
python scam_baiter.py
```

Hinweis: `HF_MODEL` muss ein Chat-Completions-fähiges Modell sein.

## Logik

- Sucht den Telegram-Ordner `Scammers`.
- Nimmt nur Chats aus diesem Ordner.
- Berücksichtigt nur Chats, bei denen die letzte Nachricht **nicht** von dir stammt (also unbeantwortet).
- Baut aus den letzten 20 Nachrichten einen Prompt; der komplette Chatverlauf wird als User-Input übergeben.
- Verwendet den Systemprompt:

> Du bist eine Scambaiting-AI. Jemand versucht dir auf Telegram zu schreiben, du sollst kreative Gespräche aufbauen, um ihn so lange wie möglich hinzuhalten. Nutze nur den bereitgestellten Chatverlauf. Antworte mit genau einer sendefertigen Telegram-Nachricht auf Deutsch und ohne Zusatztexte. Vermeide KI-typische Ausgaben, insbesondere Emojis und den langen Gedankenstrich (—).

Standardmäßig werden Vorschläge nur in der Konsole ausgegeben (kein Auto-Senden).

Optional kannst du mit Sicherheitsbremse senden:

```bash
export SCAMBAITER_SEND="1"
export SCAMBAITER_SEND_CONFIRM="SEND"   # Pflicht, sonst wird nicht gesendet
# optional: eigene gesendete Nachricht nach X Sekunden löschen
export SCAMBAITER_DELETE_OWN_AFTER_SECONDS="30"
```

Hinweis: Das Skript verarbeitet jeden Chat einzeln und baut den Prompt nur aus dem Verlauf dieses einen Scammers auf.
Zusätzlich wird ein eventuell erzeugter `<think>...</think>`-Teil automatisch entfernt, bevor der Vorschlag angezeigt oder gesendet wird.

Interaktive Konsole (Standard):

```bash
# optional deaktivieren
export SCAMBAITER_INTERACTIVE="0"
```

Im Interaktiv-Modus fragt das Tool pro Chat: nicht senden, direkt senden oder Vorschlag manuell editieren und senden.


## Erweiterbarkeit (Callback)

Die zentrale Funktion `run(...)` akzeptiert optional einen `suggestion_callback`, mit dem die Modell-Ausgabe nachbearbeitet werden kann.
Standardmäßig wird die Modell-Ausgabe bereinigt (z. B. `<think>`, Meta-Labels wie `ANALYSE:`/`HINWEIS:` und umschließende Anführungszeichen), damit nur die sendefertige Nachricht übrig bleibt.
