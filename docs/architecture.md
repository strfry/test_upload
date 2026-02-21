# Architekturübersicht

## Einstieg & Betriebsmodi
- `scam_baiter.py` lädt die Umgebung, verbindet `AnalysisStore` und `ScambaiterCore` und entscheidet, ob ein einmaliger Batchlauf oder der BotAPI-Betrieb mit `BackgroundService` und `create_bot_app` startet. Der Batchlauf iteriert durch die Chats im konfigurierten Telegram-Ordner und speichert alle Modellantworten, während das Bot-Setup dauerhaft Polling, `/runonce`-befehle und automatische Intervalle steuert.

## Konfiguration
- `scambaiter/config.py` normalisiert alle Umgebungsvariablen und liefert den unveränderlichen `AppConfig`, der Telegram- und HuggingFace-Zugangsdaten, Flags für Send/Interaktiv, Prompt-Trimmparameter und den Pfad zur Analyse-DB bereitstellt.

## Core & Prompt-Pipeline
- `ScambaiterCore` liest Chats per Telethon, extrahiert Nachrichten inklusive Bildbeschreibungen über das HuggingFace-Vision-Modell, und ergänzt Kontext-Informationen aus `previous_analysis`, `operator.directives` und Live-Typing-Hints. Das Ergebnis sind strukturierte Prompt-Stacks plus Systeminstruktionen, die das Modell zu einem gültigen JSON-Schema (`scambait.llm.v1`) führen.
- Die Antwortverarbeitung validiert Actions, normalisiert Typing/Wait-Dauern, erkennt reasoning output und führt bei Bedarf Reparaturversuche durch. Hilfsfunktionen liefern Debug-Summaries, Bild- und Profilfunktionen sowie Typing-Hints für den Service.

## BackgroundService & Queue-Handling
- Der Service orchestriert Scanzyklen, bekannte Chats, Pending-Message-Zustände und Aktionstasks. Jede Chat-Zeitlinie besitzt genau einen aktiven Task (Generierung oder Sendung), Action-Queues enthalten `mark_read`, `simulate_typing`, `wait`, `send_message`, `edit_message`, `noop` oder `escalate_to_human`, und Pausen/Skip/Auto-Flags greifen über Helper.
- Er vermittelt zwischen Core, Storage und Bot: Er liefert Prompt-Kontext mit `previous_analysis`, `operator`, `planned_queue` und Typing-Hints, speichert Generationsergebnisse, inkrementiert `loop_guard`-Direktiven, verwaltet `StoredGenerationAttempt`-Logs und triggert automatische Sendungen oder manuelle `trigger_send`.

## Media- & Karteninfrastruktur
- Die neue `card_registry` verfolgt pro Chat/Kartentyp alle verschickten `message_id`s, damit das BotUI-System gezielt Karten (Kontrollkarten, Infoboxen, Bilder, Menüs) aufräumen und Fehlermeldungen beim Löschen protokollieren kann. Jede Karte ruft `_register_card_messages` nach dem Posting, und `_cleanup_card_messages` sorgt für konsistente Aufräumversuche.
- Die `🖼️ Bilder`-Taste liest stattdessen aus der zentralen `image_entries`-Tabelle (Cache aus Beschreibung + optionaler `file_id`), uploadet nur noch fehlende Karten, normalisiert Captions auf reine Beschreibungen und begleitet evtl. Tail-Nachrichten mit eigenen Registrierungen. Persistierte `image_entries` enthalten `chat_id`, `cache_key`, `caption`, optional `file_id` und `updated_at` und werden sowohl beim Core (Cache-Key via SHA-Hash plus Sprache) als auch beim Bot gepflegt.
- Profilbilder werden bei Scan oder Rendering in `profile_photos` mit Telegram-`file_id` gehalten, so dass User-Cards und Infoboxen nur gecachte Fotos posten können; bei Uploads wird der neue `file_id` zurückgeschrieben.

## Persistenz
- `AnalysisStore` bietet SQLite-Tabellen für Analysen, `directives`, `generation_attempts`, `image_entries` und `profile_photos`. Neben den bisherigen gespeicherten Modellantworten liefert sie CRUD-Operationen für Direktiven, Image-Cache-Zugriffe (`image_entry_get`, `image_entry_upsert`, `list_image_entries`, `has_image_entries`) und Profilfotos (`profile_photo_get/set`), um Core und Bot mit historisierten Kontextfarben zu versorgen.

## Policy-, Guardrail- & Prompt-Referenzen
- Das Backlog (Docs) enthält laufende Richtlinien: bei fehlenden Fakten `escalate_to_human` mit `analysis.missing_facts`/`suggested_analysis_keys`, Anti-Loop-Regel mit `loop_guard_active`/`blocked_intents_next_turns`, Rollenkonsistenz (Sender/Empfänger klarhalten) sowie Konkretheits-Hints (`docs/snippets/prompt_konkretheit.txt`). Diese Richtlinien fließen über `operator.directives` ebenso wie durch heuristische Guards im Prompt direkt ein.
- `docs/prompt_cases` beschreibt Smoke-Tests für Eskalationslogik (Continue möglich vs. Konflikt), Loop-Vermeidung (`no_repeat_validator_contact`) sowie Fokus auf die letzte User-Frage. Diese JSON-Fixtures plus die Guidelines dienen als Referenz dafür, welche `actions`, `analysis`-Felder und `message.text`-Verhalten erwartet werden.

## Kontextmodell & Tests
- Der Event-Schema-Entwurf (`docs/event_schema_draft.md`) legt nahe, Konversationen als strukturierte Events (`text_message`, `image_message`, `typing`, `read_receipt`) zu modellieren, was beim Aufbau des `prompt_context` und bei zukünftigen Telemetrie- oder Export-Funktionen helfen soll.
- `scripts/prompt_runner.py` repliziert die Prompt-Erstellung offline, exportiert Curl-Requests und erlaubt Tests via JSON-Fixtures, während `scripts/loop_analyzer.py` Transkripte scannt, Intents extrahiert (z. B. Validator, Wallet, Fees, Next Step) und Wiederholungen signalisiert.

## Datenfluss (vereinfacht)
1. `scam_baiter.py` lädt Config + Store, startet Core und entscheidet Modus.
2. BackgroundService scannt Chats, bereitet Prompt-Context vor (u. a. `previous_analysis`, `directives`, Typing-Hints) und ruft Core.
3. Core baut Prompt, ruft HF an, analysiert JSON, führt Reparaturen/Heuristiken aus und liefert Suggestion + Actions + Analysis.
4. Service persistiert Ergebnisse, steuert Action-Queues (inkl. Media-Karten, Waiting, Send) und aktualisiert `loop_guard`, `image_entries` und `profile_photos`.
5. Bot-App zeigt Menüs, Chat-Details, Bildkarten und Analysis-Infos, nutzt `card_registry`/`image_entries`/`profile_photos` und bietet Steuerbefehle (Chats, History, Prompt-Preview, Bild-Buttons).

## Weiterführende Hinweise
- Environment-Variablen (Telegram/HF-Authentisierung, Folder-Name, Flags) bestimmen Laufzeitverhalten; die Config-Schicht stellt sie in `AppConfig` bereit.
- Persistierte `analysis`-Objekte (Sprache, Loop-Indikatoren, Direktiven-IDs) werden von `BackgroundService.build_prompt_context_for_chat` wieder eingespielt.
