# Event Schema Draft (Conversation Context)

This draft models Telegram interaction as explicit events.
Storage keeps full source fields. Prompt payload is a lightweight projection.

## Envelope

```json
{
  "schema": "scambait.context.v1",
  "chat_id": 6998054071,
  "language": "de",
  "events": []
}
```

## Event Types

### `message`

```json
{
  "event_type": "message",
  "event_id": "msg_12345",
  "role": "scammer",
  "ts_utc": "2026-02-17T18:10:00Z",
  "text": "Hi, install Coinbase first."
}
```

### `photo`

```json
{
  "event_type": "photo",
  "event_id": "msg_12346",
  "role": "scammer",
  "ts_utc": "2026-02-17T18:11:00Z",
  "caption_original": "",
  "caption_generated": "A screenshot shows a login form with email and password fields.",
  "media_kind": "photo"
}
```

### `typing_interval`

```json
{
  "event_type": "typing_interval",
  "role": "system",
  "ts_utc": "2026-02-17T18:11:05Z",
  "duration_ms": 4000
}
```

### `forward`

```json
{
  "event_type": "forward",
  "role": "manual",
  "ts_utc": "2026-02-17T18:11:10Z",
  "text": "Forwarded from Telegram user...",
  "source_message_id": "fwd:v2:origin_signature:ab12cd34ef56:message:112233445566",
  "meta": {
    "forward_identity": {
      "strategy": "origin_signature",
      "key": "sig:9ff3..."
    },
    "forward_profile": {
      "origin_kind": "MessageOriginUser"
    }
  }
}
```

## Prompt Integration

- Persisted enum set:
  - `event_type in {message, photo, forward, typing_interval}`
  - `role in {manual, scammer, scambaiter, system}`
- User forwards keep their original content type (`message`/`photo`/`forward`) and become `role=manual` when not yet known in store.
- Pass `events[]` to the model in chronological order.
- Prompt builder starts with full history; when token limit is reached, remove oldest events first.
- Prompt timestamps are normalized to `HH:MM`; storage keeps full `ts_utc`.
- `typing_interval` stays optional in prompt and should not trigger generation by itself.
- Escalations are control actions, logged in event `meta` (not as separate top-level event type).
