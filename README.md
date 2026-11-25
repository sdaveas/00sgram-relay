# 00sgram-relay

An addon for **[00sgram](https://github.com/sdaveas/00sgram)**: Instagram DMs,
relayed into one Telegram forum topic per conversation — and back.

It is a **client of the app**: it owns no Instagram session, no browser
and no message store. It asks the app over HTTP what arrived, posts each new
message into that conversation's Telegram topic, polls Telegram for what you
typed, and posts it back. Its own SQLite holds only what the relay must
remember: the topic mapping, the per-conversation watermarks and the outbound
cursor. The app never learns what a Telegram topic is.

## The HTTP contract

- `GET /api/chat` — the inbox listing: `chat_id`, `title`, `kind`, `last_id`, `last_at`.
- `GET /api/chat/thread?id=<chat_id>&read=0` — one conversation, newest messages.
- `POST /api/chat/send` — `{chat_id, text}`, one message out.
- `GET /api/chat/changes` — stored inbound messages since a cursor; not used yet.

Auth is the app's own pair of headers, not OAuth: `x-api-token: $API_TOKEN` and
`x-device: relay`.

## Topics

One forum topic per Instagram conversation, keyed by the Instagram thread id, so
a rename on either side is just a label. **That mapping lives in this process's
database** (`ig_threads`), never in the app: a topic id means nothing to the app,
an Instagram id means nothing to Telegram. Lose the mapping and each conversation
gets a second topic while the old ones go quiet. Attachments arrive as
permalinks, not files.

## Run

```sh
cp .env.example .env      # API_TOKEN, TELEGRAM_TOKEN, TELEGRAM_RELAY_CHAT
docker compose up -d --build
```

The compose file joins the app's network (`00sgram_default`) and mounts the
volume `00sgram_relay` — both external on purpose, so reachability and state
survive changes to either repo. The app is expected to be up as `web`.

## Migrating from the in-process relay

The old relay kept its topics in the app's database. Adopt them once, or every
conversation gets a second topic:

```sh
docker compose run --rm relay python -m app.relayd --import-topics /data/00sgram.sqlite3
```

(with the app's data mounted, or pointed at a copy). Read-only on the source,
idempotent on the target.

## Limits

- No history backfill: it relays what arrives after it is up, and a new database
  adopts the inbox instead of replaying it.
- No reaction relay yet: `ig_reacts` is written and nothing replays it.
- No attachments: a photo or reel is a link, not a file.
- No browser, no Playwright, no Pillow. `httpx` is the only dependency.
