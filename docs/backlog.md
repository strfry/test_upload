# Backlog

## LLM Escalation statt Raten

- Bei fehlenden, entscheidenden Fakten soll das Modell bevorzugt `escalate_to_human` verwenden statt zu halluzinieren.
- Im Escalation-Fall soll `analysis` strukturierte Hinweise enthalten:
  - `missing_facts`: welche Informationen fehlen.
  - `suggested_analysis_keys`: welche Analysis-Keys zum Schließen der Lücke hilfreich wären.
- Optionaler Folgepunkt: UI-Unterstützung im Botchat, um vorgeschlagene Keys direkt in den Analysis-Store zu übernehmen.

## Allgemeine Anti-Loop-Regel

- Pro Turn den Kern-Intent der letzten Assistant-Nachricht in `analysis.last_assistant_intent` führen.
- Wenn der geplante neue Intent identisch oder semantisch gleich zu den letzten 2 Assistant-Intents ist:
  - Wiederholung als Hauptfrage verbieten.
  - Stattdessen muss die Nachricht primär auf die jüngste User-Aussage reagieren und einen neuen Fortschrittsschritt liefern
    (neues Subziel, neues Belegdetail oder konkrete nächste Aktion).
- Bei Trigger in `analysis` setzen:
  - `loop_guard_active=true`
  - `repeated_intent=<intent>`
  - `next_intent=<new_intent>`
  - `blocked_intents_next_turns=[<intent>]` (für 2 Turns)
- Spezialfall:
  - Wenn User signalisiert, dass ein angeforderter Nachweis nicht verfügbar ist (z. B. "steht nicht auf der Website"),
    darf genau dieser Nachweis nicht erneut als Hauptfrage angefordert werden.
  - Stattdessen alternatives verifizierbares Detail aus vorhandenem Material anfordern.

## Optionale Prompt-Regel: Rollenkonsistenz

- Rollen strikt trennen: Sender und Empfänger nicht verwechseln.
- Niemals behaupten, dass "ich" einen Link oder ein Dokument gesendet habe, wenn diese Information vom User kam.
- Antworten immer aus der Perspektive der Empfängerin formulieren.
- Vor dem finalen Antworttext prüfen:
  - Wer hat die letzte relevante Information geliefert?
  - Stimmen Pronomen und Besitzbezüge ("ich/du", "mein/dein Link") mit dem Verlauf überein?
- Guard für Re-Write:
  - Wenn Entwurf Formulierungen enthält wie "den Link, den du mir geschickt hast", aber der Link laut Verlauf vom User stammt,
    Antwort verwerfen und mit korrekter Rollenreferenz neu formulieren.

## Archiviertes Snippet (später)

- Escalation-Regel feiner kalibrieren: nur bei echten Faktenlücken, ansonsten kreativ weiterführen statt früh eskalieren.
- Mini-Testset für Prompt-Qualität: 5-10 reale JSON-Fixtures mit klaren Erwartungskriterien.
- Optionaler Guardrail/Post-Check (später): nur bei Bedarf automatische Repair-Iteration bei Duplikatfragen/Halluzinationsmustern.
