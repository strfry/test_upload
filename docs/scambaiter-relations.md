# Scambaiter Deployment View

## Rolleschema

```
OpenClaw ←— Telegram Channel — Maid↔ScamBaiterControl —→ ScamBaiterCore ←— Telethon Client — Maid↔Jonathan
OpenClaw ←— Telegram Channel — Maid↔ScamBaiterControl —→ ScamBaiterCore ←— Telethon Client — Maid↔Julia (Scammer)
```

- **OpenClaw** bleibt der Agent/Gateway für Telegram, zeigt Buttons und gibt Beispiele (Inline-Views). Er ist nicht der Absender.
- **ScamBaiterControl** ist der interne Client, der die View‑Logik (Buttons, Typing, Inline-Keyboards) steuert und JSON-Payloads an OpenClaw sendet/empfängt.
- **Scambaiter Core** plus **Telethon Client** bilden gemeinsam die Analyse‑Engine: Core generiert `analysis/message/actions` (HF or OpenAI), Telethon liefert History/Media und erhält Feedback.
- Jonathan und Julia („Scammer“) sind jeweils die Nutzer, die über ScamBaiterControl an die Core/Telethon‑Schicht angeschlossen sind.

## Kommunikationskanäle

1. **Telegram ↔ OpenClaw** – Normaler Bot-Channel (Bot API). OpenClaw verweist auf Inline-Buttons und channelspezifische Config (allowlist, party stream). 
2. **OpenClaw ↔ ScamBaiterControl** – Skill-/tool-call (`tool.scambaiter.*`) mit JSON-Payloads für neue Nachrichten + Entscheidungen.
3. **ScamBaiterControl ↔ ScambaiterCore** – CLI/HTTP (`analyze`, `prompt`, `suggest`, `executor`). Der Core liefert strukturierte Ergebnisse, der Client gestaltet Telegram-JSONs.
4. **ScamBaiterControl ↔ Telethon** – History, typing/simulate_typing, Typing-Notifications, bei Bedarf auch Scheduler/Queue.

## Data Flow Example

```yaml
- Update: Scammer sagt etwas
- Telethon: liefert Kontext an Core (history, directives)
- Core: generiert JSON (analysis, message, actions)
- ScamBaiterControl: Verwendet Inline-Keyboard-UI + simulate_typing, verschickt Telegram-JSON an OpenClaw
- OpenClaw: sendet finale Telegram-Nachricht an Julia
- Feedback: Approve/Reject → ScamBaiterControl → Core/Store (pending, trace_id)
```

## Hinweise

- Typing-Notifications werden vom Client geloggt und in `analysis.typing_summary` zusammengefasst (pattern, sessions, gaps).
- Nur ScamBaiterControl + der Benutzer dürfen Nachrichten senden; OpenClaw bietet nur Beispiele, keine Verantwortung.
- Die CLI-Entrypoints (`analyze`, `suggest`, `executor`) sollten den Vertrag aus `docs/client_api_reference.md` erfüllen, damit der Skill über OpenClaw-Channel konsistent bleibt.

## Light deployment (future multiuser)

- Ohne Telethon gibt es keine native History; jeder neue Telegram-User liefert oder forwarded Chat-Nachrichten manuell in ScamBaiterControl.
- Der Client baut pro User eigene `history`-Stores (z. B. pro-ID-Dateien) und verwendet die gleichen prompt-Templates, damit Analysis/Suggestions konsistent bleiben.
- Nachrichten werden nur als Vorschläge generiert. Freigabe und manueller Send (z. B. `/start`, `/send1`) bleiben beim User, der die Buttons ausführt.
- Feedback wird lokal gespeichert (trace_id/analysis/pending) damit spätere Prompts die früheren Manual-Sends berücksichtigen.
