# Implementierungsreferenz

## Zweck
Dieses Dokument beschreibt den vereinbarten Umsetzungsweg in lesbarer Form.  
Es ist kein Prompt, sondern die Referenz fuer Reihenfolge, Verantwortlichkeiten und Abnahmekriterien.

## Leitentscheidung
- Telethon ist separater Integrationsbaustein und der einzige automatische Sender/Empfaenger.
- `ScamBaiterControl` ist der Steuerbot fuer Slash/UI und History-Aufbau via User-Forwards.
- Core und Service bereiten Vorschlaege vor und protokollieren, senden aber nicht selbst.
- Der externe Integrationsvertrag bleibt stabil ueber Input-/Output-Schemas plus Feedback.

## Umsetzungsphasen

### Phase 1: Vertrag und Core-Capabilities festigen
Ergebnis:
- Core-Capabilities fuer Analyse, Prompt-Erzeugung und Vorschlagsgenerierung sind klar getrennt.
- Der externe Vertrag beschreibt Datenformate, nicht die Orchestrierungsreihenfolge.
- `docs/client_api_reference.md` ist die verbindliche Contract-Quelle.
- Externe Steuerung bleibt first-level Slash-kompatibel (`/prompt`, `/analyse`, `/queue`, `/chats`) und ist klar vom internen LLM-Schema getrennt.

Abnahme:
- Die dokumentierten Slash-Kommandos werden korrekt auf Runtime-Kommandos abgebildet.
- Analyse- und Prompt-Antworten bleiben fuer den Agenten maschinenlesbar interpretierbar.

### Phase 2: Telethon-kontext und Persistenz stabilisieren
Ergebnis:
- Kontextgewinnung (History, Typing, Medien) wird ueber Telethon und Forward-Ingestion in denselben Event-Store gefuehrt.
- Store deckt Analyse, Versuche, Event-Modell, Bildcache und Profilfoto-Referenzen ab.
- Logging in `logs/conversations/<conversation>.log` ist fuer Betrieb nutzbar.

Abnahme:
- Kontextdaten aus Telethon und User-Forwards erscheinen reproduzierbar im Prompt-Kontext.
- Bild-/Profilcaches koennen wiederverwendet werden (keine unnötigen Uploads).

### Phase 3: Control-Bot API konsolidieren
Ergebnis:
- `ScamBaiterControl`-Kommandos und Alias-Mapping sind stabil dokumentiert.
- `/suggest` wird nicht als Agent-Command verwendet; die Ausfuehrung referenziert konkrete Vorschlaege ueber `/queue <suggestion_id>`.
- Steueraktionen aus Chat und Inline-Menues sind deterministisch im Service abgebildet.
- Prompt-Aufbereitung nutzt immer den gesamten Verlauf und kuerzt bei Tokenlimit vom Gespraechsbeginn.
- Prompt-Zeitstempel sind immer in `HH:MM`; Store behaelt volle Zeitdaten.
- Scambaiter bleibt auf Analyse/Suggestion/Feedback-Verarbeitung fokussiert.

Abnahme:
- Agent/Benutzer kann den gesamten Lauf ausschliesslich ueber Bot-Steuerkommandos bedienen.
- Service-Zustaende und Actions bleiben nachvollziehbar im Store.
- Freigegebene Sends laufen nur ueber Telethon-Deliverypfad; Core/Service senden nicht direkt.

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
- Profilschema: `docs/profile_schema_draft.md` mit Telethon als kanonischer Quelle und BotAPI-Forward als Teilmengen-Quelle.

## Teststrategie
- Contract-Tests fuer Command-Kompatibilitaet und Alias-Mapping.
- Integrations-Tests fuer Core-Capabilities inklusive Provider-Fallback.
- Telethon-Integrationstest fuer Empfang und Sendepfad.
- Forward-Ingestion-Test: User-Forwards werden mit originalem `event_type` gespeichert und als `role=manual` markiert, falls noch unbekannt.
- Control-Bot-Simulation mit realistischem Feedback- und Zustands-Lifecycle.
- Prompt-Builder-Test: kompletter Verlauf bis Limit, danach Kuerzung vom Anfang; Zeitformat im Prompt immer `HH:MM`.
- Escalation-Test: Kontrollaktionen werden als `meta` protokolliert und sind auswertbar.
- Profilschema-Test: BotAPI-Forward fuellt nur Teilfelder, Telethon-Anreicherung setzt fehlende Felder (`bio`, `profile_media`) deterministisch.

## Geltungsbereich und Grenzen
In Scope:
- Core-Analyse und Vorschlagsgenerierung
- Persistenz und Nachvollziehbarkeit
- Stabiler Integrationsvertrag

Out of Scope:
- UI-Redesign ausserhalb des bestehenden Bot-Steuerkonzepts
- Plattformspezifische Orchestrierung ausserhalb des Repositories

## Aktueller Handover / Nächste Schritte

- Fokus bleibt auf **Phase 3 (Control-Bot API konsolidieren)**.
- Offene Arbeitspunkte:
  1. Slash-Contract vollständig konsistent halten (`/prompt`, `/analyse`, `/queue`, `/chats`, `/history`).
  2. `/suggest` nicht als Agent-Command verwenden; Ausführung nur über queue-/approval-basierte Pfade.
  3. Queue-/Action-Lifecycle je `trace_id` klar im Store sichtbar machen.
  4. Prompt-Ansicht stabil auf Reply-JSON-Sektionen halten (`schema`, `analysis`, `message`, `actions`, `raw`) und nur relevante Prompt-Events verwenden.
  5. Iterative Feinhärtung der Dry-Run-Fehlerdarstellung und Attempt-Diagnostik (`phase`, `accepted`, `reject_reason`).
  6. Forward-Ingestion als Batch-Append mit sequenzbasierter Deduplizierung stabilisieren:
     Hybrid-Identität verwenden (`channel_message_id` nur bei Channel-Origin, sonst `origin_signature`) und Sequenzabgleich darauf aufbauen.

- Übergabehinweis: Umsetzung läuft auf `branch/opencraw`; letzte Änderungen sind Prompt-Card/Attempt-Diagnostik/Cleanup für Profile-Noise.
