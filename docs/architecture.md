# Architekturübersicht

## Ziel und Rahmen
Scambaiter ist die Analyse- und Vorschlags-Engine fuer Scam-Dialoge.  
Die Engine erzeugt strukturierte Analyse, Antworttext und Action-Plan, fuehrt Nachrichten aber nicht selbst aus.

Die zentrale Architekturentscheidung ist:
- Telethon ist ein eigener Teil und der einzige automatische Telegram-Sender plus Empfaenger.
- Der Telegram-Bot `ScamBaiterControl` ist die Steueroberflaeche fuer Slash-Kommandos, Inline-Aktionen und Monitoring.
- Benutzer plus Control-Kanal entscheiden ueber Freigabe; Core bereitet nur vor und protokolliert.
- OpenClaw ist nur eine Referenz fuer UI-Muster (z. B. Inline-Buttons), nicht fuer echten Versand.

## Systemgrenzen

### Im Core (ScambaiterCore)
- Prompt-Kontext aufbauen aus Verlauf, `previous_analysis`, Direktiven und optionalen Live-Signalen.
- Modellaufrufe (HF-first, optional OpenAI-Fallback) fuer Analyse und Vorschlaege.
- JSON-Validierung und Normalisierung von Actions.
- Persistenz von Analyse, Generierungsversuchen, Event-Store, Bildcache und Profilfoto-Referenzen.

### Im Telethon-Teil
- Live-Nachrichten empfangen und als Events persistieren.
- Telegram-Delivery als einziger automatischer Sender ausfuehren.
- Volle Rohdaten fuer Store erfassen (nicht prompt-optimiert kuerzen).

### Im Control-Kanal (`ScamBaiterControl`)
- Benutzer und Agent steuern den Lauf ueber Slash-Kommandos und Inline-Aktionen.
- Der Bot liefert Status, Analysen, Prompt-Preview und Bedienaktionen.
- Der Bot kapselt Telegram-spezifische Darstellung (`chat_id`, `text`, `reply_to`, `inline_keyboard`).
- Entscheidungen (z. B. Run, Stop, Analyse setzen, Queue) werden in den Core-Service rueckgespielt.
- Feedback (approve/reject, send_state) fliesst zurueck in Core und Store.
- Der Bot kann History aus User-Forwards aufbauen; das ist der primaere Aufbauweg fuer neue Chats.

### Externe Gateways (Referenz, nicht Sender)
- Externe Systeme koennen UI-Muster oder Workflows inspirieren.
- Sie duerfen keine finale Versandrolle fuer Scambaiting-Nachrichten uebernehmen.

## Betriebsmodi

Scambaiter kennt zwei Betriebsmodi, die beim Start durch die vorhandene Konfiguration festgelegt werden. Ein Wechsel zur Laufzeit ist nicht vorgesehen.

### Live-Modus *(Telethon verbunden)*

Erfordert vollen Telegram-Kontozugang per Telethon. Nur ein Operator gleichzeitig.

- Eingehende Scammer-Nachrichten werden **automatisch** empfangen und gespeichert; der Backend-Pipeline wird **sofort** getriggert.
- Tipp-Events des Scammers werden überwacht, was das realistische „Tippen und Pausieren" ermöglicht.
- Eingehende Nachrichten können sofort als gelesen markiert werden, um Ungelesene-Badges im Telegram-Client zu unterdrücken.
- Antworten werden per Telethon **direkt** vom Konto des Operators gesendet.
- Profilmetadaten (Fotos, Bio, Username) werden aus Telegram abgerufen und gespeichert.
- Kein manuelles Weiterleiten oder Kopieren erforderlich.

**Voraussetzungen:** `TELETHON_API_ID`, `TELETHON_API_HASH` und `SCAMBAITER_BOT_TOKEN` müssen gesetzt sein.

### Relay-Modus *(nur Bot-API)*

Erfordert nur ein Telegram-Bot-Token. Kann von beliebig vielen unabhängigen Operatoren genutzt werden.

- Eingehende Scammer-Nachrichten werden durch **Weiterleiten** an den Control-Bot ingested.
- Antworten werden im Control-Bot präsentiert; der Operator **kopiert und sendet** sie manuell aus dem eigenen Telegram-Client.
- Profilmetadaten beschränken sich auf das, was Telegram in Bot-API-Forward-Origins ausgibt (keine Fotos, keine Bio).
- Die Forward-Ingestion nutzt sequenzbewusste Merge-Logik für Deduplizierung und Reihenfolge-Rekonstruktion.

**Voraussetzung:** Nur `SCAMBAITER_BOT_TOKEN` muss gesetzt sein.

### Moduserkennung

Der Modus ergibt sich beim Start automatisch: Sind `TELETHON_API_ID` und `TELETHON_API_HASH` gesetzt, läuft das System im Live-Modus; andernfalls im Relay-Modus. Core und Store sind modusunabhängig – sie lesen immer aus dem Event-Store, unabhängig davon, wie Ereignisse dort hineinkamen.

---

## Hauptkomponenten

### `ScambaiterCore`
- Verantwortlich fuer Modellinteraktion und strukturierte Antwortgenerierung.
- Nutzt den Event-Store als Wahrheit fuer Prompt-Kontext.
- Gibt strukturierte Ergebnisse zurueck (`analysis`, `message`, `actions`, Metadaten), ohne selbst zu senden.

### `BackgroundService`
- Orchestriert Scans, Pending-Zustaende, Vorschlagsgenerierung und Statusverwaltung.
- Fuehrt Queue- und Freigabelogik auf Core-Seite, ohne selbst Delivery auszufuehren.
- Bindeglied zwischen Core, Store und Integrationsschicht.

### `AnalysisStore`
- SQLite-Store fuer:
  - Analysen und Modellausgaben
  - Direktiven
  - Generierungsversuche
  - Events (`event_type`, `role`, Voll-Metadaten)
  - `image_entries` (caption/file_id cache)
  - `profile_photos`

## API-Vertrag fuer Integrationen
Der externe Vertrag ist in `docs/client_api_reference.md` versioniert.

Wichtige Leitlinien:
- Scambaiter beschreibt Inputs/Outputs und Feedback-Formate der Steuerkommandos.
- Reihenfolge und Art der Aufrufe erfolgen im Control-Kanal durch Benutzer/Agent.
- Feedback ist verpflichtend, damit Traceability und Nachsteuerung funktionieren.
- Breaking Changes am Vertrag erfordern Schema-Versionssprung.

### Trennung der API-Ebenen
- Externe Client/BotAPI-Steuerung laeuft ueber first-level Slash-Kommandos wie `/prompt`, `/analyse`, `/queue`, `/chats` (siehe `docs/client_api_reference.md`).
- Das interne LLM-Vertragsschema `scambait.llm.v1` bleibt eine Core-Engine-Schnittstelle und ist nicht die externe Agent-API.

## Medien- und Kartenmodell
- Bildbeschreibungen und File-Referenzen werden in `image_entries` gehalten.
- Profilbilder werden als wiederverwendbare `file_id` in `profile_photos` gepflegt.
- Falls interne Kartenlogik noch genutzt wird (Uebergangsphase), muessen alle geposteten Nachrichten registriert und kontrolliert geloescht werden.

## Prompt- und Policy-Basis
Fachregeln und Guardrails liegen in:
- `docs/backlog.md`
- `docs/prompt_cases/`
- `docs/snippets/prompt_konkretheit.txt`
- `docs/event_schema_draft.md`
- `docs/profile_schema_draft.md`

Diese Quellen definieren die inhaltlichen Erwartungen an Analysefelder, Eskalationsverhalten, Loop-Vermeidung und Themenfokus.

## Referenz-Datenfluss
1. Telethon und/oder User-Forwards liefern Ereignisse in den Event-Store.
2. Core erzeugt Prompt und fragt das Modell.
3. Core liefert strukturiertes Ergebnis und Zustandsinfos an den Control-Bot `ScamBaiterControl`.
4. Control erzeugt die Telegram-Darstellung (Text, Buttons, Reply-Bezug) und steuert Freigaben.
5. Telethon fuehrt die eigentliche Zustellung aus; Feedback und Escalations landen als Store-Metadaten.

## Prompt-Aufbereitung
- Prompt bekommt grundsaetzlich den gesamten Verlauf aus dem Store.
- Wenn das Token-Limit erreicht wird, werden alte Event-Anfaenge entfernt, bis der Prompt passt.
- Zeitangaben werden fuer den Prompt immer auf `HH:MM` reduziert.
- Store behaelt immer die vollstaendige Zeit-/Telegram-Information.

## Kommunikationskanaele
- Telegram Live-Transport laeuft ueber Telethon (Senden/Empfangen).
- `ScamBaiterControl` ist der Control-Kanal fuer Slash/UI und Forward-basierte History-Ergaenzung.
- Core bleibt zustandsfuehrende Analyse-/Vorschlags-Engine ohne eigene UI-Interpretation und ohne direkte Sendelogik.

## Migrationsnotiz
Die Zielarchitektur priorisiert eine Neuimplementierung vom Event-Store nach oben: zuerst History-Ingestion (inkl. Forwards), dann Prompt-Build, dann Control-Flow, danach Delivery-Integrationen.
