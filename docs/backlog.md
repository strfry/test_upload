# Backlog

## LLM Escalation statt Raten

- Bei fehlenden, entscheidenden Fakten soll das Modell bevorzugt `escalate_to_human` verwenden statt zu halluzinieren.
- Im Escalation-Fall soll `analysis` strukturierte Hinweise enthalten:
  - `missing_facts`: welche Informationen fehlen.
  - `suggested_analysis_keys`: welche Analysis-Keys zum Schließen der Lücke hilfreich wären.
- Optionaler Folgepunkt: UI-Unterstützung im Botchat, um vorgeschlagene Keys direkt in den Analysis-Store zu übernehmen.

## Archiviertes Snippet (später)

- Escalation-Regel feiner kalibrieren: nur bei echten Faktenlücken, ansonsten kreativ weiterführen statt früh eskalieren.
- Mini-Testset für Prompt-Qualität: 5-10 reale JSON-Fixtures mit klaren Erwartungskriterien.
- Optionaler Guardrail/Post-Check (später): nur bei Bedarf automatische Repair-Iteration bei Duplikatfragen/Halluzinationsmustern.
