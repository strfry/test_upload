# Profile Schema Draft (Telethon-Template)

## Zweck
Dieses Schema definiert ein einheitliches Profilobjekt fuer Scam-Kontakte.
Telethon ist die kanonische Vorlage (maximale Feldabdeckung).
BotAPI-Forward-Daten sind ein Teilmengen-Feed und fuellen nur vorhandene Felder.

## Envelope

```json
{
  "schema": "scambait.profile.v1",
  "chat_id": 1234567890,
  "profile": {}
}
```

## Canonical Profile Object (`profile`)

```json
{
  "identity": {
    "telegram_user_id": 1234567890,
    "telegram_chat_id": null,
    "display_name": "John Example",
    "first_name": "John",
    "last_name": "Example",
    "username": "johnexample",
    "usernames": ["johnexample", "john_example_old"],
    "phone": null
  },
  "account": {
    "is_bot": false,
    "is_premium": false,
    "is_verified": false,
    "is_scam": false,
    "is_fake": false,
    "lang_code": "en"
  },
  "presence": {
    "status": "online",
    "last_seen_utc": null
  },
  "profile_media": {
    "has_profile_photo": null,
    "profile_photo_count": null,
    "current_photo_file_id": null,
    "current_photo_unique_id": null
  },
  "about": {
    "bio": null
  },
  "provenance": {
    "primary_source": "telethon",
    "source_priority": ["telethon", "botapi_forward"],
    "first_seen_utc": "2026-02-21T19:00:00Z",
    "last_update_utc": "2026-02-21T19:10:00Z",
    "enrichment_status": {
      "bio": "pending",
      "profile_media": "pending"
    }
  }
}
```

## Source Mapping

### Telethon -> canonical (preferred)
- `User.id` -> `identity.telegram_user_id`
- `User.first_name`/`last_name` -> `identity.first_name`/`last_name`
- `User.username`/`usernames` -> `identity.username`/`identity.usernames`
- `User.phone` -> `identity.phone`
- `User.bot` -> `account.is_bot`
- `User.premium` -> `account.is_premium`
- `User.verified` -> `account.is_verified`
- `User.scam` -> `account.is_scam`
- `User.fake` -> `account.is_fake`
- `User.lang_code` -> `account.lang_code`
- `User.status` -> `presence.status`
- Telethon user/profile photo info -> `profile_media.*`
- `GetFullUser.about` -> `about.bio`

### BotAPI forward -> canonical (partial)
- `forward_origin.sender_user.id` -> `identity.telegram_user_id`
- `forward_origin.sender_chat.id` -> `identity.telegram_chat_id`
- `forward_origin.sender_user.first_name`/`last_name` -> `identity.first_name`/`last_name`
- `forward_origin.sender_user.username` or `sender_chat.username` -> `identity.username`
- `forward_origin.sender_chat.title` or `sender_user_name` -> `identity.display_name`
- `forward_origin.sender_user.is_bot` -> `account.is_bot`
- `forward_origin.sender_user.language_code` -> `account.lang_code`

Nicht aus BotAPI-Forward verlässlich vorhanden:
- `about.bio`
- `profile_media.*`
- verlässliche Presence (`last_seen`)
- komplette Username-Historie

## Merge Rules
- Telethon hat Prioritaet bei Konflikten.
- BotAPI-Forward setzt nur Felder, die leer/unbekannt sind oder als `pending` markiert sind.
- Jeder Merge schreibt `provenance.last_update_utc` und die genutzte Quelle.
- `enrichment_status` bleibt `pending`, bis Telethon-Felder erfolgreich aufgeloest sind.

## Card Usage
- Chat Card nutzt zunaechst `identity.display_name` + `identity.username`.
- Wenn `about.bio` oder `profile_media.*` fehlt: als `unknown` markieren, nicht raten.
- Sobald Telethon anreichert, zeigt Card die echten Werte an.
