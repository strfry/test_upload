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
export HF_VISION_MODEL="..."          # optional, fallback: HF_MODEL
export HF_BASE_URL="https://..."       # optional
export HF_MAX_TOKENS="350"             # optional, längere Modellantworten erlauben
```

Optionale Laufzeit-Konfiguration:

```bash
export SCAMBAITER_FOLDER_NAME="Scammers"
export SCAMBAITER_HISTORY_LIMIT="20"
export SCAMBAITER_DEBUG="1"

export SCAMBAITER_SEND="1"
export SCAMBAITER_DELETE_OWN_AFTER_SECONDS="30" # optional

export SCAMBAITER_INTERACTIVE="1"              # nur Batch-Modus
export SCAMBAITER_ANALYSIS_DB_PATH="scambaiter.sqlite3"  # Persistenz für Analysen
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
export SCAMBAITER_AUTO_INTERVAL_SECONDS="120"

python scam_baiter.py
```

Hinweis: Der Control-Chat wird beim Start automatisch über die Telegram-App-API ermittelt. Dazu wird ein Dialog mit dem Bot-Username gesucht; wenn gefunden, wird die eigene User-ID als erlaubter Bot-Chat verwendet. Wenn kein Dialog gefunden wird, bricht der Start mit Fehler ab.

Beim Start sendet der Bot außerdem automatisch eine Begrüßungs-/Befehlsübersicht in diesen erlaubten Chat.
Beim Start im BotAPI-Modus wird der Ordner asynchron über die Telegram-API eingelesen; Vorschlagsgenerierung läuft dabei separat im Hintergrund.

Verfügbare Bot-Kommandos:

- `/runonce` – startet sofort einen Einmaldurchlauf
- `/runonce <chat_id[,chat_id2,...]>` – Einmaldurchlauf nur für bestimmte Chat-IDs
- `/chats` – zeigt ein paginiertes Chat-Menü; pro Chat öffnet sich ein Detailmenü mit den neuesten Infos und Aktionen `Generate`, `Send`, `Stop`, `Auto an`, `Auto aus`, `Bilder`, `Analysis`
  - Beim Start im Bot-Modus wird dieses Menü automatisch gepostet
  - Beim Öffnen eines Chats wird eine Profil-Card mit Profilbild (falls vorhanden) als Bot-Nachricht gepostet
  - `Refresh` startet den Hintergrund-Refresh und aktualisiert die Liste ohne blockierenden Vollscan im Handler
  - Das Menü bleibt stabil: Aktionen aktualisieren diese Card-Ansicht (Caption + Buttons) und behalten Navigation (`Zurueck`, `Aktualisieren`) bei
  - **Generate** erzeugt immer einen neuen Antwort-Vorschlag und setzt den Nachrichtenprozess auf **Wartephase**
  - Fehlt ein Vorschlag, wird beim Öffnen der Detailansicht automatisch eine Generierung gestartet
  - **Send** löst das Senden aus (manueller Trigger)
  - **Stop** bricht den laufenden Prozess ab; falls die Nachricht bereits versendet wurde, wird sie gelöscht
  - **Auto an** aktiviert das automatische Senden nach Wartezeit nur für diesen Chat
  - **Auto aus** deaktiviert das automatische Senden für diesen Chat (Wartephase bleibt unbegrenzt)
  - **Bilder** postet die letzten Chat-Bilder mit KI-Caption in den Kontrollkanal
  - Mit **Auto an** läuft die Wartephase dieses Chats mit Timeout (`SCAMBAITER_AUTO_INTERVAL_SECONDS`), mit **Auto aus** unbegrenzt
- `/last` – zeigt die letzten Vorschläge (max. 5) für Analyse/Einblick
- `/history` – zeigt die letzten persistent gespeicherten Analysen inkl. Metadaten (lange Ausgaben werden in mehrere Nachrichten aufgeteilt)
- `/analysisget <scammer_chat_id>` – zeigt das zuletzt gespeicherte Analysis-JSON für einen Chat
- `/analysisset <scammer_chat_id> <json_objekt>` – überschreibt das zuletzt gespeicherte Analysis-JSON für einen Chat

Hinweis: Nach jedem Lauf wird das vom Modell gelieferte `analysis`-Objekt direkt als JSON gespeichert.
Eingehende Bildnachrichten vom Scammer werden automatisch mit `HF_VISION_MODEL` ausführlich und wohlwollend beschrieben und als Marker (`[Bild gesendet: ...]`) in den Chatverlauf für die Textgenerierung eingefügt.
Die Bildbeschreibung wird per Bild-Hash in der SQLite-DB (`image_descriptions`) gecacht, damit jedes identische Bild nur einmal an das Vision-Modell geschickt wird.
Wenn in der letzten gespeicherten Analyse ein `language`- oder `sprache`-Feld gesetzt ist (`de`/`en`), wird zusätzlich eine starke Sprach-Systeminstruktion erzwungen.

## Projektstruktur

Zur Trennung der Concerns wurde der Code aufgeteilt:

- `scam_baiter.py`: Einstieg und Modus-Umschaltung
- `scambaiter/config.py`: Umgebungsvariablen/Config
- `scambaiter/core.py`: Telegram- und HF-Kernlogik
- `scambaiter/service.py`: Hintergrund-Loop + Laufstatus
- `scambaiter/bot_api.py`: Telegram BotAPI-Kommandos
- `scambaiter/storage.py`: SQLite-Persistenz für Analysen und Bildbeschreibungen
- `scripts/prompt_runner.py`: lokaler Prompt-Runner (Prompt/Raw/Parsed) für schnelle Iteration
- `scripts/loop_analyzer.py`: analysiert gepastete Verläufe auf Loop-Muster (Wiederholungsfragen/Themen-Drift)


- Für unbeantwortete Chats wird beim Öffnen von `/chats` asynchron ein Vorschlag vorgezogen, damit die Chatliste sofort erscheint.

Nachrichtenzustände im BotInterface:
- `generating`: Vorschlag wird gerade erzeugt
- `waiting`: Vorschlag erzeugt, wartet auf manuellen Send-Trigger oder Auto-Timeout
- `sending_typing`: Senden läuft, inkl. Tippbenachrichtigung
- `sent`: Nachricht wurde gesendet
- `cancelled`: Vorgang per Stop abgebrochen (inkl. ggf. Löschung beim Empfänger)
- `error`: Senden fehlgeschlagen oder nicht möglich

## Prompt-Runner

Lokaler Testlauf für Prompt/Modelausgabe:

```bash
python scripts/prompt_runner.py --chat-id 123456789 --show-prompt
```

Ohne Telegram-Verbindung (kollidiert nicht mit laufendem Bot), mit Fixture-Datei:

```bash
python scripts/prompt_runner.py --input-json ./case.json --show-prompt
```

Nur Prompt ansehen, ohne Modell-Request:

```bash
python scripts/prompt_runner.py --input-json ./case.json --show-prompt --preview-only
```

Request-Body für externen Model-Call exportieren (z.B. auf einem Host mit Internet):

```bash
python scripts/prompt_runner.py \
  --input-json ./case.json \
  --preview-only \
  --dump-request-json /tmp/hf_request.json \
  --print-curl
```

Gesprächs-Loop analysieren (JSON-Case):

```bash
python scripts/loop_analyzer.py --input-json docs/prompt_cases/no_repeat_validator_contact.json
```

Gesprächs-Loop analysieren (Paste-Textdatei):

```bash
python scripts/loop_analyzer.py \
  --transcript-file ./transcript.txt \
  --assistant-sender "Me" \
  --output-json /tmp/loop_report.json
```

Hinweis: Für Telegram-Kopierformat (`Name, [Datum Uhrzeit]`) den eigenen Sendernamen per
`--assistant-sender` angeben (bei Bedarf mehrfach).

Direkt ins Tool pasten bis EOF (Ctrl+D):

```bash
python scripts/loop_analyzer.py --transcript-stdin --assistant-sender "Me"
```
