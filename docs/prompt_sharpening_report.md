# Prompt Sharpening Report

## Scope
Dieser Report konsolidiert:
- den aktuellen Stand in `branch/opencraw`,
- relevante Prompt-/Output-Logik aus `master`,
- die bestehende Doku (`docs/backlog.md`, `docs/prompt_cases/`, `docs/event_schema_draft.md`, `docs/architecture.md`, `docs/implementation_plan.md`).

Ziel: eine manuell reviewbare Vorlage fuer die naechste Prompt-Nachschaerfung.

## Quellenbasis
- Historische Referenz:
  - `master:scambaiter/core.py`
  - `master:scambaiter/service.py`
  - `master:scambaiter/storage.py`
- Aktueller Code:
  - `scambaiter/core.py`
  - `scambaiter/bot_api.py`
  - `scambaiter/storage.py`
  - `scambaiter/model_client.py`
- Doku:
  - `docs/backlog.md`
  - `docs/prompt_cases/README.md` + JSON-Cases
  - `docs/event_schema_draft.md`
  - `docs/profile_schema_draft.md`
  - `docs/architecture.md`
  - `docs/implementation_plan.md`

## Was in `master` fuer Prompt-Qualitaet bereits existierte
- Sehr strikter `SYSTEM_PROMPT` mit klaren JSON-/Action-/Safety-Regeln (`scambait.llm.v1`).
- Struktur-Parser + Validator:
  - harte Top-Level-Whitelist,
  - strenge Action-Typen und Feldvalidierung,
  - Normalisierung von Kurzformen.
- Repair-Pfad fuer invalides JSON oder fehlerhafte Antworten (zweiter Modellaufruf mit Korrekturprompt).
- Heuristiken:
  - Timing-Normalisierung (`simulate_typing`, `wait`),
  - Reasoning-/Meta-Ausgaben erkennen und ablehnen.
- Bessere Prompt-Kontextstruktur:
  - Systemkontext + Verlauf mit `message_id`/`reply_to`,
  - optionale Mid-Trim-Logik statt nur stumpfem Anfangs-Cut.
- Umfangreiches Attempt-Tracking in `generation_attempts`:
  - Versuchsnummer, Phase (`initial`/`repair`), Accept/Reject-Grund,
  - Token-Metriken, Schema, Heuristik-Flags.

## Aktueller Stand (branch/opencraw)
- Positiv:
  - Event-basiertes Modell und klarer Control-Flow.
  - Prompt aus Store-Historie + Profil-Change-Systemevents.
  - Prompt Card + Dry Run + Persistenz in `generation_attempts`.
  - HF-Router ueber OpenAI-API-Client ist aktiv.
- Defizite gegenueber `master`:
  - `SYSTEM_PROMPT` aktuell zu duenn (nur grobe JSON-Anweisung).
  - Keine harte parserseitige Vertragsdurchsetzung fuer `analysis/message/actions`.
  - Kein Repair-Lauf bei invalider/unsauberer Modellausgabe.
  - Keine Guardrails fuer Loop-/Topic-Drift aus `docs/backlog.md`.
  - `generation_attempts` aktuell nur Basisfelder (ok/error + payload), ohne Diagnose-Tiefe.
  - Keine formale Zuordnung zu den bestehenden `docs/prompt_cases/*`-Erwartungen.

## Doku-Abgleich (Soll vs Ist)
- `docs/backlog.md` fordert u. a.:
  - strukturierte Eskalations-Analysefelder,
  - Loop-Guard-Felder (`loop_guard_active`, `last_user_topic_priority`).
  - Ist: nicht verbindlich im Prompt-/Parser-Vertrag implementiert.
- `docs/prompt_cases/*` erwarten:
  - reproduzierbares Verhalten fuer Eskalation/No-Repeat/Topic-Fokus.
  - Ist: keine contract-tests gegen strukturiertes Modell-Output-Schema vorhanden.
- `docs/implementation_plan.md` nennt:
  - contract-tests und robuste Prompt-Pipeline.
  - Ist: Teilabdeckung, aber nicht auf dem Niveau der alten `master`-Validierungslogik.

## Konkrete Nachschaerfungsfelder

### 1) Vertragsfester Prompt
- Einen versionierten `SYSTEM_PROMPT` wieder einfuehren, der:
  - exakt `schema=scambait.llm.v1` verlangt,
  - `analysis` als Objekt erzwingt,
  - `message.text`/`actions` konsistent bindet,
  - erlaubte Action-Typen + Parameter klar begrenzt,
  - Sicherheitsregeln und Eskalationsregeln explizit macht.

### 2) Strikter Output-Parser + Validator
- Ausgabepfad in drei Schichten:
  1. JSON parse,
  2. schema/shape validation,
  3. action/value validation + normalization.
- Invalides Ergebnis darf nicht direkt weiterlaufen.

### 3) Repair-Retry
- Wenn parse/validation fehlschlaegt:
  - Korrektur-Request mit kompaktem Repair-Systemprompt.
- Attempts als `initial` vs `repair` getrennt loggen.

### 4) Backlog-Guardrails operationalisieren
- In Prompt und Validator verankern:
  - Loop-Guard,
  - Last-User-Topic-Priority,
  - Eskalationsstruktur (`missing_facts`, `suggested_analysis_keys`).

### 5) Erweiterte Attempt-Telemetrie
- `generation_attempts` um Diagnosefelder erweitern:
  - `attempt_no`, `phase`, `accepted`, `reject_reason`,
  - `schema`, `prompt_tokens`, `completion_tokens`, `total_tokens`,
  - optional `reasoning_tokens`.
- Das ist wichtig fuer manuelles Prompt-Tuning in Iterationen.

## Iterationsplan (manuell reviewbar)

### Iteration A: Contract-First
- Promptvertrag + Parser/Validator einziehen.
- Ziel: Nur gueltige strukturierte Outputs kommen durch.
- Exit-Kriterium:
  - alle bestehenden unit-tests gruen,
  - neue parser tests fuer invalid/valid cases.

### Iteration B: Repair + Diagnostik
- Repair-Lauf und Attempt-Phasen implementieren.
- Exit-Kriterium:
  - bei absichtlich kaputter Modellausgabe: repair wird versucht und protokolliert,
  - `generation_attempts` zeigt Phase/Reject-Grund.

### Iteration C: Backlog-Policy
- Loop-/Topic-/Escalation-Regeln aus `docs/backlog.md` harden.
- Exit-Kriterium:
  - `docs/prompt_cases/*` als tests/fixtures laufen reproduzierbar gegen Soll.

### Iteration D: Prompt Card QA
- Prompt/Dry-Run UX stabilisieren:
  - klare, kurze Fehlermeldungen,
  - attempt-id verlinkbar,
  - optional `/attempts <chat_id>` fuer schnelle manuelle Diagnose.
- Exit-Kriterium:
  - manuelles QA-Protokoll im Control-Chat ohne „silent fail“.

## Offene Entscheidungen fuer manuellen Review
- Soll der `SYSTEM_PROMPT` wieder zentral als langer Vertrag in Code liegen oder in eine versionierte Prompt-Datei ausgelagert werden?
- Wie strikt wollen wir Action-Normalisierung halten (harte Rejection vs sanfte Normalisierung)?
- Wollen wir Mid-Trim wieder einfuehren (head+tail+marker) oder beim aktuellen "drop oldest" bleiben?
- Welche Mindestfelder muessen in `analysis` verpflichtend sein, bevor `send_message` erlaubt ist?

## Kurzfazit
Die aktuelle Pipeline ist funktional, aber fuer robustes Prompt-Tuning noch zu weich.
Die wichtigsten Hidden-Qualitaetsmechanismen aus `master` (strikter Vertrag, Repair, Diagnostik) sollten kontrolliert zurueckkehren.
Dieser Schritt ist die Voraussetzung, um Prompt-Nachschaerfung iterativ und messbar zu machen statt rein heuristisch.
