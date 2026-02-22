# Scambaiter Deployment View

## Rolleschema

```
OpenClaw (reference only)
        \
         \ UI examples + /history style
          \
ScamBaiterControl (BotAPI control channel) <-> ScamBaiterCore <-> Event Store <-> Telethon
                                                                       ^
                                                                       |
                                                           Telegram live transport
```

- **OpenClaw** bleibt Referenz fuer UI-Muster und Summaries; kein aktiver Sender.
- **ScamBaiterControl** steuert Slash-Kommandos, Inline-Buttons, Monitoring und Forward-Ingestion.
- **Scambaiter Core** erzeugt `analysis/message/actions` und bleibt ohne direkte Sendelogik.
- **Telethon** ist der einzige automatische Sender und Empfaenger fuer Telegram.

## Kommunikationskanäle

1. **Telegram ↔ Telethon** - Live-Empfang und Delivery.
2. **Telegram User ↔ ScamBaiterControl** - Control-Chat, Slash-Kommandos, Inline-Aktionen, Forwards.
3. **ScamBaiterControl ↔ ScambaiterCore** - Analyse/Prompt/Queue/History APIs.
4. **Core ↔ Store** - Events, Analysen, Direktiven, Escalation-Meta.

## Data Flow Example

```yaml
- Update: User forwardet eine Scammer-Nachricht in den Control-Chat
- ScamBaiterControl: validiert und speichert Event (gleicher event_type, role=manual falls neu)
- Core: baut Prompt aus voller Store-History, kuerzt bei Tokenlimit vom Anfang
- Core: generiert analysis/message/actions
- Control: zeigt Vorschlag + Inline-Buttons (Queue/Approve/Reject)
- Telethon: sendet erst nach Freigabe/Queue-Entscheidung
- Store: schreibt Feedback und Escalation unter meta
```

## Hinweise

- Typing-Intervalle sind optionale Systemevents und triggern alleine keine Generation.
- Nur Telethon sendet automatisch; Benutzerfreigaben kommen ueber ScamBaiterControl.
- OpenClaw bleibt dokumentarische Referenz ohne operative Sendekompetenz.

## Light deployment (future multiuser)

- Ohne Telethon gibt es keine native History; jeder neue Telegram-User liefert oder forwarded Chat-Nachrichten manuell in ScamBaiterControl.
- Der Client baut pro User eigene `history`-Stores (z. B. pro-ID-Dateien) und verwendet die gleichen prompt-Templates, damit Analysis/Suggestions konsistent bleiben.
- Nachrichten werden nur als Vorschläge generiert. Freigabe und manueller Send (z. B. `/start`, `/send1`) bleiben beim User, der die Buttons ausführt.
- Feedback wird lokal gespeichert (trace_id/analysis/pending) damit spätere Prompts die früheren Manual-Sends berücksichtigen.

## Event-Modell

- `event_type` deckt die eigentlichen Chat-Ereignisse ab (`message`, `photo`, `sticker`, `forward`, `typing_interval`).
- `role` ist auf die Klarrollen `manual`, `scammer`, `scambaiter`, `system` begrenzt; `typing_interval` und ähnliche technische Events gehen als `role=system` in die History.
- Escalation ist kein separates History-Event, sondern eine Control-Action, die in `meta` (z. B. `actions`) oder im Pending-Log vermerkt wird.
- Beim Prompt-Build wird zuerst der gesamte Verlauf verwendet; bei Limit werden die aeltesten Events entfernt. Zeitangaben gehen als `HH:MM` in den Prompt.
