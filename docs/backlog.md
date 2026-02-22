# Backlog

Die wichtigsten offenen Guardrail-/Promptverbesserungen und ihre aktuellen Zustände.

## LLM-Eskalation statt Raten *(Actionable)*

- Bei fehlenden, entscheidenden Fakten soll das Modell bevorzugt `escalate_to_human` verwenden statt zu halluzinieren.
- Im Escalation-Fall soll `analysis` strukturierte Hinweise enthalten:
  - `missing_facts`: welche Informationen fehlen.
  - `suggested_analysis_keys`: welche Analysis-Keys zum Schließen der Lücke hilfreich wären.
- Optionaler Folgepunkt: UI-Unterstützung im Control-Chat, um vorgeschlagene Keys direkt in den Analysis-Store zu übernehmen.

### Nächste Schritte

1. Prompt-Contract so erweitern, dass `escalate_to_human` bevorzugt wird, wenn `missing_facts` leer oder `needing_fact` gemeldet wird.
2. Parser/Validator auf `analysis.missing_facts` + `analysis.suggested_analysis_keys` prüfen und ggf. neue Felder erzwingen.
3. Bot-API: zusätzliche Inline-Aktionen oder `/analysisset`-Hilfe anbieten, um vorgeschlagene Keys zu übernehmen.

## Allgemeine Anti-Loop-Regel *(Actionable)*

- Pro Turn die letzte Assistant-Intention in `analysis.last_assistant_intent` dokumentieren.
- Wenn die neue Intention mit den letzten zwei Assistant-Intents identisch ist:
  - Wiederholung als Hauptfrage verbieten.
  - Stattdessen Fokus auf die jüngste User-Aussage und einen klaren Fortschrittsschritt liefern.
- Bei Loop-Trigger:
  - `loop_guard_active=true`
  - `repeated_intent=<intent>`
  - `next_intent=<new_intent>`
  - `blocked_intents_next_turns=[<intent>]` für zwei Turns.
- Spezialfall: Wenn der User signalisiert, dass ein gewünschter Beleg fehlt, darf er nicht erneut als Hauptfrage kommen; stattdessen alternative überprüfbare Details anfordern.

### Nächste Schritte

1. Prompt/Validator: Tracking-Felder für `last_assistant_intent` + Guard-Logik einbauen und in der Response-Policy dokumentieren.
2. Bot-API/Dry-Run: Wiederholungserkennung bestätigen (z. B. mit Tests gegen `docs/prompt_cases/no_repeat_validator_contact.json`).
3. Report-Mechanismus: Wenn Guard triggern soll, kann `analysis` das explizit melden und die UI entsprechende Hinweise liefern.

## Rollenkonsistenz *(Actionable)*

- Sender vs. Empfänger eindeutig trennen. Keine Formulierungen wie "den Link, den du mir geschickt hast", wenn der Link vom User stammt.
- Antworten stets aus Sicht des Empfängers verfassen.
- Vor der finalen Ausgabe prüfen:
  - Wer hat die letzte relevante Information geliefert?
  - Stimmen Pronomen/ Besitzbezüge mit dem Verlauf?
- Guard für Rewrites: Wenn die Antwort falsche Pronomen benutzt, wird sie verworfen und neu generiert.

### Nächste Schritte

1. Prompt/Contract deutlich darauf hinweisen, aus welcher Perspektive geantwortet werden muss.
2. Post-Response-Check in `model_client` hinzufügen, der Pronomen/Kontext auf die letzten Erwähnungen abgleicht.
3. Bei Verletzungen das Guard-Flag setzen und ggf. ein Rewrite initiieren.

## Archivierte/deferrierte Ideen

- Escalation-Regel feiner kalibrieren: nur bei echten Faktenlücken eskalieren, sonst kreativ weiterführen.
- Mini-Testset für Prompt-Qualität: 5-10 reale JSON-Fixtures mit klaren Erwartungskriterien (siehe `docs/prompt_cases`).
- Optionaler Guardrail/Post-Check (später): Nur bei Bedarf automatische Repair-Iteration bei Duplikatfragen/Halluzinationen.
- Diese Ideen bleiben dokumentiert, werden aber aktuell nicht aktiv angegangen; sie können bei Bedarf reaktiviert werden.
