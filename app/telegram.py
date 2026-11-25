"""Telegram, as the thing that delivers links to the app.

A bot, not a person: the Bot API needs a token and nothing else, cannot get the
account limited, and can only ever see chats it was added to. The trade is that
a bot cannot join a conversation between two people — whoever sends the reel
sends it to the bot (or to a group the bot is in), and replies arrive from the
bot's own name. That is the price of not handing this program a login that can
act as you.

Two shapes come out of here, and only two:

- `normalize_update` — one Bot API update -> one row for `tg_messages`,
  including the Instagram links it carries. Nothing is interpreted beyond that;
  the UI decides what a link looks like.
- `Telegram` — the four calls this app makes. `getUpdates` is a long poll
  (25 s), so new messages land within a second or two without hammering.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any

import httpx

from . import iglink, store

API = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL = 25


class TelegramError(Exception):
    """Anything the Bot API refused. `description` is Telegram's own words."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Telegram:
    def __init__(self, token: str, timeout: int = LONG_POLL + 10) -> None:
        self.token = token
        self.client = httpx.Client(timeout=timeout)

    def call(self, method: str, **params: Any) -> Any:
        url = API.format(token=self.token, method=method)
        try:
            resp = self.client.post(url, json={k: v for k, v in params.items()
                                               if v is not None})
        except httpx.HTTPError as exc:
            raise TelegramError(f"{type(exc).__name__}: {exc}") from None
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"not json: {resp.status_code}") from None
        if not body.get("ok"):
            raise TelegramError(str(body.get("description") or "refused"),
                                resp.status_code)
        return body.get("result")

    def updates(self, offset: int | None = None) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=LONG_POLL,
                         allowed_updates=["message", "message_reaction"]) or []

    def send(self, chat_id: str, text: str, thread_id: int | None = None) -> dict:
        # No parse_mode on purpose: a message is shown as it was typed, and the
        # app renders no markup of its own.
        return self.call("sendMessage", chat_id=chat_id, text=text,
                         message_thread_id=thread_id) or {}

    def create_topic(self, chat_id: str, name: str) -> dict:
        """A forum topic, or a `TelegramError` when the group is not a forum."""
        return self.call("createForumTopic", chat_id=chat_id, name=name[:128]) or {}

    def me(self) -> dict:
        return self.call("getMe") or {}


def _kind(message: dict) -> str:
    for key, kind in (("text", "text"), ("photo", "photo"), ("video", "video"),
                      ("animation", "video"), ("video_note", "video"),
                      ("sticker", "sticker"), ("voice", "voice"),
                      ("audio", "voice"), ("document", "file"),
                      ("poll", "poll")):
        if key in message:
            return kind
    return "other"


def _title(chat: dict) -> str:
    if chat.get("title"):
        return chat["title"]
    name = " ".join(part for part in (chat.get("first_name"), chat.get("last_name"))
                    if part)
    return name or chat.get("username") or str(chat.get("id"))


def _text(message: dict) -> str:
    text = message.get("text") or message.get("caption") or ""
    # A link sticker or an inline keyboard carries a URL the text does not.
    extra = [e.get("url") for e in (message.get("entities") or []) + 
             (message.get("caption_entities") or [])
             if e.get("type") == "text_link" and e.get("url")]
    for url in extra:
        if url and url not in text:
            text = f"{text}\n{url}".strip()
    return text


def normalize_update(update: dict, me_id: str = "") -> dict | None:
    """One update -> one message row, or None for updates we do not show.

    `mine` is decided by the sender being the bot itself: the app can only send
    as the bot, so that is the only authorship there is.
    """
    message = update.get("message") or update.get("edited_message") or {}
    if not isinstance(message, dict) or not message:
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    msg_id = message.get("message_id")
    if chat_id is None or msg_id is None:
        return None
    text = _text(message)
    return {
        "chat_id": str(chat_id),
        "title": _title(chat),
        "kind": "group" if chat.get("type") in ("group", "supergroup") else "dm",
        "msg_id": str(msg_id),
        "at": int(message.get("date") or 0),
        "mine": bool(me_id) and str(sender.get("id") or "") == str(me_id),
        "who": " ".join(part for part in (sender.get("first_name"),
                                          sender.get("last_name")) if part)
               or sender.get("username") or "",
        "kind_msg": _kind(message),
        "text": text,
        "links": iglink.links_in(text),
        "thread_id": message.get("message_thread_id"),
    }


def normalize_reaction(update: dict) -> dict | None:
    """One `message_reaction` update -> `{chat_id, msg_id, emoji}`, or None.

    Telegram sends the whole reaction set each time, so the newest emoji is the
    one that was just pressed. An empty list is a removal: the relay replays it
    as `deleted`.
    """
    reaction = update.get("message_reaction") or {}
    if not isinstance(reaction, dict) or not reaction:
        return None
    chat_id = (reaction.get("chat") or {}).get("id")
    msg_id = reaction.get("message_id")
    if chat_id is None or msg_id is None:
        return None
    fresh = [r.get("emoji") for r in (reaction.get("new_reaction") or [])
             if r.get("type") == "emoji" and r.get("emoji")]
    return {"chat_id": str(chat_id), "msg_id": str(msg_id),
            "emoji": fresh[-1] if fresh else "",
            "at": int(reaction.get("date") or 0)}


def ingest(tg: Telegram, conn: sqlite3.Connection, me_id: str,
           wa_chat: str = "", allow: tuple[str, ...] = ()) -> int:
    """One long poll: store what arrived, remember the offset. Returns count.

    A reaction is filed in the queue of the relay that owns the group it was
    pressed in: topic ids are per group, and there is no way to learn afterwards
    which relay a `thread_id` belongs to.

    `allow` is the chats this app speaks in — anyone can find a bot and message
    it, and a stranger's DM has no business in the table or on the screen. An
    empty `allow` is no filter, which is the plain-inbox mode: a token and no
    relay chat yet.
    """
    allowed = {str(c) for c in allow if c}
    offset = store.meta_json(conn, "tg_offset", None)
    updates = tg.updates(offset=int(offset) if offset is not None else None)
    saved = 0
    last = None
    for update in updates:
        row = normalize_update(update, me_id)
        if row and (not allowed or str(row["chat_id"]) in allowed):
            store.save_tg_message(conn, row)
            saved += 1
        elif not row:
            reaction = normalize_reaction(update)
            if reaction and (not allowed or str(reaction["chat_id"]) in allowed):
                own = str(reaction["chat_id"]) == str(wa_chat or "")
                store.queue_reaction(conn, reaction["chat_id"],
                                     reaction["msg_id"], reaction["emoji"],
                                     reaction["at"] or None,
                                     table="wa_reacts" if own else "ig_reacts")
                saved += 1
        last = update.get("update_id", last)
    if last is not None:
        store.set_meta_json(conn, "tg_offset", int(last) + 1)
    return saved


def start_poller(config, conn: sqlite3.Connection) -> threading.Event:
    """A daemon that keeps the store current. Silent when there is no token."""
    stop = threading.Event()
    if not getattr(config, "telegram_token", ""):
        return stop

    def loop() -> None:
        tg = Telegram(config.telegram_token)
        me_id = ""
        first = True
        while not stop.is_set():
            try:
                if not me_id:
                    me_id = str((tg.me() or {}).get("id") or "")
                ingest(tg, conn, me_id,
                       getattr(config, "wa_relay_chat", "") or "",
                       allow=(getattr(config, "relay_chat", "") or "",
                              getattr(config, "wa_relay_chat", "") or ""))
                first = False
            except TelegramError:
                # A refused token or a blip: say nothing, try again shortly.
                if first:
                    stop.wait(30)
                    continue
                stop.wait(5)
            except Exception:                      # noqa: BLE001 — never die
                stop.wait(5)

    threading.Thread(target=loop, name="telegram-poll", daemon=True).start()
    return stop
