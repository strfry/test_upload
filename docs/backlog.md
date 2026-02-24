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

## Forward-Ingestion: Batch-Merge & Sequenz-Dedupe *(Actionable)*

- Für Nutzer ohne Telethon sollen weitergeleitete Nachrichten als Batch betrachtet werden.
- Ein Batch darf nur hinten angehängt werden, wenn die enthaltenen Scam-Nachrichten nicht bereits in derselben Reihenfolge in der DB stehen.
- Deduplizierung soll nicht nur auf einzelner Message-ID basieren, sondern auf geordneter Sequenz-Erkennung über den Batch.

### Nächste Schritte

1. Batch-Identifikation definieren (zusammenhängende Forward-Importe pro ingest-Lauf).
2. Sequenzvergleich gegen vorhandene Scam-Nachrichten implementieren (ordered subsequence / suffix match).
3. Append nur bei neuer Sequenz; ansonsten Import überspringen und im Control-Text transparent melden.
4. Tests ergänzen: identischer Batch doppelt forwarded -> kein zweites Append; Reihenfolgeabweichung -> neues Append erlaubt.

## Timing-Orchestration & Pacing-Logik *(Deferred)*

- Neue orchestrated Prompt (`rag_prompt.txt`) erwartet ein strukturiertes `timing`-Objekt mit:
  - `now_ts` (current timestamp)
  - `secs_since_last_inbound` (latency from scammer)
  - `secs_since_last_outbound` (our response latency)
  - `inbound_burst_count_120s` (activity burst in 120s window)
  - `avg_inbound_latency_s` (average response time)
- Detaillierte Pacing-Regeln in `docs/snippets/prompt_timing.txt`:
  - Immediate inbound (<10s): prefer wait/typing, no send
  - Burst detection (≥3 in 120s): hold with wait
  - Long silence (>600s): respond normally
  - Urgency signals: introduce artificial delay
  - Rapport phase: minimal delay
- Latency-Klasse-Mapping: `"short"` = 30s, `"medium"` = 3min, `"long"` = 15min.
- Fehlende Komponenten:
  - Timing-Collector im Service (berechnet die Statistiken)
  - Timing-Injector in Core (fügt `timing` zum Prompt hinzu)
  - Wait-Executor in Telethon (führt Verzögerungen aus)

### Nächste Schritte

1. Architektur-Entscheidung: Timing als Service-Layer oder Core-responsibility?
2. Proto-Datenmodell: `timing_stats` in Store oder ephemeral im Service?
3. Integration mit `rag_prompt.txt` und bestehender `SYSTEM_PROMPT_CONTRACT`.

---

## Archivierte/deferrierte Ideen

- Escalation-Regel feiner kalibrieren: nur bei echten Faktenlücken eskalieren, sonst kreativ weiterführen.
- Mini-Testset für Prompt-Qualität: 5-10 reale JSON-Fixtures mit klaren Erwartungskriterien (siehe `docs/prompt_cases`).
- Optionaler Guardrail/Post-Check (später): Nur bei Bedarf automatische Repair-Iteration bei Duplikatfragen/Halluzinationen.
- Prompt-Modi als konfigurierbare Chat-Card-Option (`lead_extract`, `play_light`, `hard_challenge`) einführen; aktuell bleibt `play_light` Default.
- Diese Ideen bleiben dokumentiert, werden aber aktuell nicht aktiv angegangen; sie können bei Bedarf reaktiviert werden.
