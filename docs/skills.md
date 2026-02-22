# Skills: ScamBaiterControl OpenClaw Channel

## Kontext
ScamBaiterControl steuert das View-Verhalten am Telegram-Channel. OpenClaw verbleibt als Referenz-User: es liefert die `/history`-Zusammenfassung und Beispiele für Inline-Buttons, aber **es verschickt selbst keine Nachrichten**. Die echten Sends passieren über den internen Client, der Wissen aus ScambaiterCore, Telethon-History und manuellen User-Aktionen (z. B. `/start`, `/send1`) zusammenführt.

## Skill-Aufrufsequenz (OpenClaw Seite)
1. **`tool.scambaiter.analyze`** – liefert `analysis`, `pending`, `typing_summary`. Wurde von OpenClaw in `/history` oder einer Automation getriggert.
2. **`tool.scambaiter.suggest`** – nimmt Prompt + history → erzeugt JSON `message.text`, `actions`, `trace_id`, `media`. Den Payload nutzt der Client, um eine Telegram-Nachricht zu bauen.
3. **`tools.message`** – sendet das fertige Telegram-JSON mit `chat_id`, `text`, `inline_keyboard`, eventuell `media`. Die Buttons haben Callback-Daten wie `sc:approve:<trace_id>`.
4. **`tool.scambaiter.executor`** – wird durch Button-Clicks (`Approve/Reject`) oder manuelle `/send`-Commands ausgelöst, aktualisiert `pending`, markiert `analysis`, loggt Feedback.

## Skill-View Notes
- Logging: Der Channel zeigt `analysis.typing_summary` (pattern/sessions/gaps) und das letzte `message.text`/`media`. Jede Historiezeile trägt ihre `message_id`, damit Jonathan später quick-referenced.
- History-Funktion: `/history` gibt dieselbe Summary wie Core-Prompt aus, ergänzt durch `message_id`s und `trace_id`s, damit der User durchs echte Telegram scrollen kann.
- Light Deployment: Ohne Telethon liefert der User Forwarded-Content; Buttons bleiben Vorschläge, das Sendetarget wird erst durch einen manuellen `/sendX` aktiviert.

## Beispielbuttons (Telegram-JSON)
```json
{
  "chat_id": -1001234567890,
  "text": "Alles klar, danke. Ich melde mich, falls ich etwas brauche.",
  "inline_keyboard": [
    [
      {"text": "Approve", "callback_data": "sc:approve:trace123"},
      {"text": "Reject", "callback_data": "sc:reject:trace123"}
    ]
  ]
}
```

## Hinweis
Die CLI/BotAPI-Entrypoints sollten den Vertrag aus `docs/client_api_reference.md` nutzen, damit Cmd+Skill am Channel weiterhin konsistent wird. Wenn du willst, kann ich daraus auch eine kleine `tool`-Definition in OpenClaw schreiben.
