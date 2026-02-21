# Implementierungsreferenz

## Zweck
Dieses Dokument beschreibt den vereinbarten Umsetzungsweg in lesbarer Form.  
Es ist kein Prompt, sondern die Referenz fuer Reihenfolge, Verantwortlichkeiten und Abnahmekriterien.

## Leitentscheidung
- Telethon bleibt im Core als Quelle fuer Telegram-Kontext.
- Die Ausfuehrung von Nachrichtenaktionen wird an einen externen Executor/Nachrichtenempfaenger abgegeben.
- Der externe Integrationsvertrag bleibt stabil ueber Input-/Output-Schemas plus Feedback.

## Umsetzungsphasen

### Phase 1: Vertrag und Core-Capabilities festigen
Ergebnis:
- Core-Capabilities fuer Analyse, Prompt-Erzeugung und Vorschlagsgenerierung sind klar getrennt.
- Der externe Vertrag beschreibt Datenformate, nicht die Orchestrierungsreihenfolge.
- `docs/client_api_reference.md` ist die verbindliche Contract-Quelle.
- Externe Steuerung bleibt first-level Slash-kompatibel (`/prompt`, `/analyse`, `/chats`) und ist klar vom internen LLM-Schema getrennt.

Abnahme:
- Die dokumentierten Slash-Kommandos werden korrekt auf Runtime-Kommandos abgebildet.
- Analyse- und Prompt-Antworten bleiben fuer den Agenten maschinenlesbar interpretierbar.

### Phase 2: Telethon-kontext und Persistenz stabilisieren
Ergebnis:
- Kontextgewinnung (History, Typing, Medien) bleibt in Telethon-basierten Core-Komponenten.
- Store deckt Analyse, Versuche, Bildcache und Profilfoto-Referenzen ab.
- Logging in `logs/conversations/<conversation>.log` ist fuer Betrieb nutzbar.

Abnahme:
- Kontextdaten aus Telethon erscheinen reproduzierbar im Prompt-Kontext.
- Bild-/Profilcaches koennen wiederverwendet werden (keine unnötigen Uploads).

### Phase 3: Executor-Split fuer Bot API
Ergebnis:
- `python-telegram-bot`-Ausfuehrung wird in externen Client verlagert.
- Externer Client uebernimmt Zustellung, Retry und sichtbare Monitoring-Oberflaeche.
- Scambaiter bleibt auf Analyse/Suggestion/Feedback-Verarbeitung fokussiert.

Abnahme:
- Externer Client kann Actions sicher ausfuehren und Feedback zurueckschreiben.
- Core laeuft ohne direkte Zustellabhaengigkeit.

### Phase 4: Monitoring und Feedback-Schleife
Ergebnis:
- Stage-Events und Feedback erlauben eindeutige Nachverfolgung pro `trace_id`.
- Provider-Wahl, Latenz und Fehlerpfade sind messbar.
- Prompt- und Richtlinienoptimierung ist auf Basis realer Rueckmeldungen moeglich.

Abnahme:
- Monitoring zeigt End-to-End-Lauf inkl. Fallback-Information.
- Gesendete vs. verworfene Vorschlaege sind sauber auswertbar.

## Technische Schwerpunkte
- Providerpfad: HF zuerst, optional OpenAI-Fallback.
- Promptquellen: `prompts/<channel>/<conversation>.json` + Richtlinien aus `docs/backlog.md` und `docs/prompt_cases/`.
- Datenhaltung: `analyses`, `directives`, `generation_attempts`, `image_entries`, `profile_photos`, Gespraechslogs.

## Teststrategie
- Contract-Tests fuer Command-Kompatibilitaet und Alias-Mapping.
- Integrations-Tests fuer Core-Capabilities inklusive Provider-Fallback.
- Telethon-Integrationstest fuer Kontextaufbereitung.
- Externer-Executor-Simulation mit realistischem Feedback-Lifecycle.

## Geltungsbereich und Grenzen
In Scope:
- Core-Analyse und Vorschlagsgenerierung
- Persistenz und Nachvollziehbarkeit
- Stabiler Integrationsvertrag

Out of Scope:
- UI-Design und Operationslogik des externen Clients
- Plattformspezifische Orchestrierung ausserhalb des Repositories
