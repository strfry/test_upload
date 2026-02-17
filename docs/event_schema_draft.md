# Event Schema Draft (Conversation Context)

This draft models Telegram interaction as explicit events instead of mixed text markers.
It is intended as prompt/input context (not necessarily persisted 1:1).

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

### `text_message`

```json
{
  "type": "text_message",
  "event_id": "msg_12345",
  "role": "counterparty",
  "ts_utc": "2026-02-17T18:10:00Z",
  "text": "Hi, install Coinbase first."
}
```

### `image_message`

```json
{
  "type": "image_message",
  "event_id": "msg_12346",
  "role": "counterparty",
  "ts_utc": "2026-02-17T18:11:00Z",
  "caption_original": "",
  "caption_generated": "A screenshot shows a login form with email and password fields.",
  "media_kind": "photo"
}
```

### `typing`

```json
{
  "type": "typing",
  "role": "counterparty",
  "ts_utc": "2026-02-17T18:11:05Z",
  "action": "SendMessageTypingAction",
  "ephemeral": true
}
```

### `read_receipt`

```json
{
  "type": "read_receipt",
  "role": "assistant",
  "ts_utc": "2026-02-17T18:11:10Z"
}
```

## Prompt Integration

- Pass `events[]` in chronological order.
- Typing/read events should be optional and short-lived.
- Typing events should not trigger model generation by themselves.
- Image events should use `caption_generated` in the conversation context.

