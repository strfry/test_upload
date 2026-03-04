# Agent-Architektur: DebugAgentBot

## Idee

Ein zweiter Telegram-Bot (`DebugAgentBot`) läuft parallel zum `ScamBaiterControl`-Bot in einer gemeinsamen **Telegram-Gruppe**. Er übernimmt die Orchestrierungsrolle, die bisher der Operator manuell übernimmt: wann soll ein Prompt laufen, welche Direktive hilft, ist ein Vorschlag gut genug zum Senden?

Der Agent ersetzt nicht `scambait.llm.v1` – er entscheidet, **wann und wie** es aufgerufen wird.

---

## Zwei-Bot-Gruppe

```
┌─────────────────── Telegram Gruppe ───────────────────────┐
│                                                            │
│  [ScamBaiterControl]            [DebugAgentBot]            │
│   – Vorschlag #42 ready          – "Empfehlung: Senden.    │
│   [✓ Senden] [✗ Skip]              Scammer wartet 8min,    │
│   [↻ Retry]  [📋 Prompt]           Loop-Risiko gering."    │
│                                  [✓ Ausführen] [⏸ Warten] │
│                                  [✎ Direktive setzen]      │
│                                                            │
│  Operator sieht beides – entscheidet oder lässt laufen     │
└────────────────────────────────────────────────────────────┘
```

**Warum Gruppe:** Bots können sich in Telegram-Privatechats keine Nachrichten schicken. In einer Gruppe sehen beide Bots alle Nachrichten (Privacy Mode deaktiviert). Inline-Button-Callbacks bleiben beim jeweiligen Bot.

---

## Architektur

```
[Scammer-Nachricht]
       ↓
[Telethon / Forward-Ingestion → SQLite Event Store]
       ↓
[Service-Layer: Trigger-Event an DebugAgentBot]
       ↓
[DebugAgentBot liest State-Snapshot aus SQLite]
       ↓
[Agent-LLM: Was ist jetzt die beste Aktion?]
       ↓
[Agent postet Recommendation-Card mit Inline-Buttons in Gruppe]
       ↓
[Operator klickt – oder Agent-Auto-Approval bei Konfiguration]
       ↓
[DebugAgentBot führt Aktion aus: schreibt in SQLite / triggert Service]
```

---

## State Snapshot (Minimaler Lesekontext)

Was der Agent aus SQLite lesen muss, um eine Entscheidung zu treffen:

```python
{
  "chat_id": 123456,
  "last_inbound_ts": "2026-03-04T17:00:00Z",   # wann kam letzte Scammer-Nachricht
  "last_outbound_ts": "2026-03-04T16:52:00Z",  # wann wurde zuletzt gesendet
  "pending_suggestion": {                        # liegt ein Vorschlag vor?
      "attempt_id": 42,
      "message": "Interessant, erzähl mir mehr...",
      "actions": ["wait_medium"],
      "loop_risk": "low"
  },
  "active_directives": ["focus: money topic"],
  "recent_events": [...],                        # letzte N Events für Kontext
  "analysis_summary": {...},                     # letzte gespeicherte Analyse
  "loop_indicator": false                        # wurde Loop erkannt?
}
```

---

## Agent-Prompt-Struktur

Der Agent bekommt einen kurzen, fokussierten Prompt – kein Gesprächskontext (das ist Sache von `scambait.llm.v1`), sondern **Orchestrierungsentscheidung**:

```
System:
  Du bist der Orchestrierungs-Agent für einen Scambaiter.
  Deine Aufgabe: Entscheide welche Aktion jetzt sinnvoll ist.
  Du siehst den aktuellen State. Du generierst KEINE Antworten an Scammer –
  das übernimmt das interne Modell.

State: {state_snapshot}

Mögliche Aktionen:
  - run_prompt       – Neuen Vorschlag generieren
  - queue_suggestion – Vorliegenden Vorschlag zur Sendung freigeben
  - set_directive    – Direktive setzen (mit Text)
  - wait             – Nichts tun, warten
  - escalate         – Operator explizit um Entscheidung bitten

Antworte als JSON: {"action": "...", "reason": "...", "params": {...}}
```

Kurzer Output, klar strukturiert – minimale Token-Kosten, gut validierbar.

---

## Temporale Verantwortung: Agent vs. Service

LLMs denken schlecht in Zeit. Deshalb klare Trennung:

| Zuständigkeit | Wer |
|---|---|
| „Jetzt senden oder warten?" | Agent (einmalige Entscheidung) |
| „Warte 3 Minuten, dann trigger" | Service-Layer (deterministischer Timer) |
| „Scammer tippt gerade" | Telethon (Live-Signal) |
| „Burst-Erkennung (≥3 in 120s)" | Service-Layer (Statistik) |

Der Agent sagt `"action": "wait"` – der Service entscheidet wie lange, basierend auf konfigurierten Pacing-Regeln.

---

## Activation / Auto-Modus

Drei klar getrennte Modi, die die bisherige Inkonsistenz auflösen:

```
MANUAL   – Agent postet nur Empfehlung, Operator muss klicken
SEMI     – Agent führt "sichere" Aktionen selbst aus (run_prompt, wait),
           fragt bei Senden nach
AUTO     – Agent führt alle Aktionen selbst aus (nur mit explizitem Opt-in)
```

Modus ist pro Chat konfigurierbar, nicht global. Default: `SEMI`.

---

## Was sich an der Hauptarchitektur ändert

**Wenig:**
- `scambait.llm.v1` bleibt unverändert
- SQLite-Schema bleibt kompatibel, ggf. neue Read-Only-Views
- `ScamBaiterControl`-Bot bleibt vollständig benutzbar ohne Agent

**Neu:**
- `DebugAgentBot` – separater Prozess, eigener Token
- `agent/state_reader.py` – State-Snapshot aus SQLite
- `agent/orchestrator.py` – Agent-LLM-Aufruf + Action-Dispatch
- Service-Layer: Trigger-Hooks an Agent bei neuen Events

**Nicht ändern:**
- Telethon als einziger Sender
- Operator bleibt immer override-fähig

---

## Warum "Debug" im Namen

Der Bot heißt bewusst `DebugAgentBot` in der ersten Phase:
- Kann jederzeit abgeschaltet werden ohne System-Impact
- Empfehlungen sind zunächst rein advisory (MANUAL-Modus)
- Dient zum Beobachten: trifft der Agent sinnvolle Entscheidungen?
- Erst wenn das Vertrauen gewachsen ist: SEMI / AUTO aktivieren

---

## Offene Fragen

1. **Trigger-Mechanismus:** Wie informiert der Service-Layer den DebugAgentBot über neue Events? (Polling vs. SQLite-Watch vs. direkter Funktionsaufruf bei shared process)
2. **Gruppen-Setup:** Wer ist in der Gruppe? Nur Operator(en) + beide Bots?
3. **Agent-Modell:** Welches LLM für den Orchestrierungs-Agent? Muss schnell und günstig sein – kein großes Reasoning nötig.
4. **Directive-Feedback-Loop:** Kann der Agent sehen, ob eine gesetzte Direktive gewirkt hat?
