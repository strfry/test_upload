# Architekturübersicht

## Ziel und Rahmen
Scambaiter ist die Analyse- und Vorschlags-Engine fuer Scam-Dialoge.  
Die Engine erzeugt strukturierte Analyse, Antworttext und Action-Plan, fuehrt Nachrichten aber nicht selbst aus.

Die zentrale Architekturentscheidung ist:
- Telethon bleibt im Core fuer Telegram-Kontextgewinnung.
- Die Zustellung (Send/Queue/Delete) liegt bei einem externen API-Client als Executor/Nachrichtenempfaenger.

## Systemgrenzen

### Im Core (Scambaiter)
- Chat-Historie lesen, Ordner scannen, Typing-Hints aufnehmen (Telethon).
- Prompt-Kontext aufbauen aus Verlauf, `previous_analysis`, Direktiven und optionalen Live-Signalen.
- Modellaufrufe (HF-first, optional OpenAI-Fallback) fuer Analyse und Vorschlaege.
- JSON-Validierung und Normalisierung von Actions.
- Persistenz von Analyse, Generierungsversuchen, Bildcache und Profilfoto-Referenzen.

### Im externen Client (Executor)
- Action-Plan konsumieren und reale Delivery-Aktionen ausfuehren.
- Monitoring/UX bereitstellen (Bot API, Dashboard, Alerts).
- Feedback an Scambaiter zurueckmelden (gesendet, verworfen, Fehler).

## Hauptkomponenten

### `ScambaiterCore`
- Verantwortlich fuer Modellinteraktion und strukturierte Antwortgenerierung.
- Nutzt Telethon fuer Kontext- und Medienzugriff.
- Gibt strukturierte Ergebnisse zurueck (`analysis`, `message`, `actions`, Metadaten).

### `BackgroundService`
- Orchestriert Scans, Pending-Zustaende, Vorschlagsgenerierung und Statusverwaltung.
- Fuehrt Queue-Logik auf Core-Seite, ohne die externe Zustellungshoheit zu verletzen.
- Bindeglied zwischen Core, Store und Integrationsschicht.

### `AnalysisStore`
- SQLite-Store fuer:
  - Analysen und Modellausgaben
  - Direktiven
  - Generierungsversuche
  - `image_entries` (caption/file_id cache)
  - `profile_photos`

## API-Vertrag fuer Integrationen
Der externe Vertrag ist in `docs/client_api_reference.md` versioniert.

Wichtige Leitlinien:
- Scambaiter beschreibt Inputs/Outputs und Feedback-Formate, aber keine feste Client-Orchestrierung.
- Die Reihenfolge und Art der Aufrufe ist Aufgabe des externen Client-Agenten.
- Feedback ist verpflichtend, damit Traceability und Nachsteuerung funktionieren.
- Breaking Changes am Vertrag erfordern Schema-Versionssprung.

### Trennung der API-Ebenen
- Externe Client/BotAPI-Steuerung laeuft ueber first-level Slash-Kommandos wie `/prompt`, `/analyse`, `/chats` (siehe `docs/client_api_reference.md`).
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

Diese Quellen definieren die inhaltlichen Erwartungen an Analysefelder, Eskalationsverhalten, Loop-Vermeidung und Themenfokus.

## Referenz-Datenfluss
1. Core sammelt Kontext ueber Telethon und Store.
2. Core erzeugt Prompt und fragt das Modell.
3. Core liefert strukturiertes Ergebnis an den externen Client.
4. Externer Client fuehrt Delivery aus und sendet Feedback.
5. Store und Monitoring-Events bilden die Nachvollziehbarkeit fuer Betrieb und Verbesserung.

## Migrationsnotiz
Die Zielarchitektur ist kompatibel mit dem aktuellen Codezustand: Telethon bleibt, waehrend `python-telegram-bot` schrittweise in den externen Executor verlagert wird.
