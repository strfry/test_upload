# Telegram Scambaiter

Scambaiter wird aktuell von der Dokumentation aus neu implementiert.
Die Architekturreferenz liegt in:

- `docs/architecture.md`
- `docs/implementation_plan.md`
- `docs/client_api_reference.md`
- `docs/event_schema_draft.md`

## Zielbild

- `Telethon` ist der einzige automatische Sender und Empfaenger.
- `ScamBaiterControl` ist der Control-Channel (Slash-Kommandos, Inline-Buttons, Forward-Ingestion).
- `ScambaiterCore` erzeugt Analyse/Vorschlaege (`analysis`, `message`, `actions`) und sendet nicht selbst.
- `OpenClaw` bleibt Referenz fuer UI-Muster und `/history`-Darstellung.

## Eventmodell

- `event_type in {message, photo, forward, typing_interval}`
- `role in {manual, scammer, scambaiter, system}`
- Escalations werden als Control-Aktionen in `meta` protokolliert.

Forwarded User-Content bleibt im originalen `event_type` und wird als `role=manual` gespeichert, falls das Event noch nicht im Store vorhanden ist.

## Prompt Feeding

- Prompt bekommt zunaechst den gesamten Verlauf aus dem Store.
- Bei Tokenlimit wird vom Gespraechsbeginn gekuerzt, bis der Prompt passt.
- Zeitangaben werden fuer den Prompt immer auf `HH:MM` normalisiert.
- Der Store behaelt die vollstaendigen Rohdaten.

## Control Bot (Forward-Ingestion)

Aktueller Dev-Start fuer den Control-Channel:

```bash
export SCAMBAITER_BOT_TOKEN="123456:ABC..."
export SCAMBAITER_CONTROL_CHAT_ID="123456789"   # optional allowlist
python scripts/run_control_bot.py
```

Im Control-Chat:
- `/chat <chat_id>` setzt den Ziel-Chat fuer Forwards.
- Forwarded Nachrichten werden direkt in den Event-Store ingestiert.
- `/history` zeigt die zuletzt gespeicherten Events des aktiven Ziel-Chats.
