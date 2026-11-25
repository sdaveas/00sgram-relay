"""The relay as its own process: Instagram DMs in, Telegram topics, and back.

Its own repo, its own image, no ports. This process is a client: it talks HTTP to
the app (`API_URL`, `API_TOKEN`) and to the Bot API (`TELEGRAM_TOKEN`), and talks
to nothing else. The browser, the Instagram session and the message store stay in
the web app, which never learns what a Telegram topic is.

Its own SQLite (`RELAY_DB`) holds only the relay's state: the topic mapping
(`ig_threads`), the per-conversation watermarks and the outbound cursor
(`meta.ig_relay`, `meta.ig_outbox`).

Restart behaviour — nothing is replayed. `meta.ig_relay` in *this* database is
the only "live" mark, so a fresh volume adopts the inbox instead of replaying it
(`relay_once`'s primed rule), and the outbox starts at the newest row already in
`tg_messages`. A send is one attempt: success and failure both advance the
cursor, so a dead API never becomes a loop of duplicate DMs.

Two loops:
- inbound, every `RELAY_INTERVAL` s: `/api/chat` -> `relay_once` -> one Telegram
  message per new Instagram message, in the conversation's topic.
- outbound, every 5 s: new `tg_messages` rows in our own group -> the topic's
  Instagram id -> `POST /api/chat/send`.

`--import-topics <app.sqlite3>` copies the topic mapping the in-process relay
already made, so a migration does not create a second topic per conversation.
"""
from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx

from . import relay, store, telegram
from .config import Settings
from .telegram import Telegram

log = logging.getLogger("relayd")

OUTBOUND_INTERVAL = 5


class Api:
    """The app's chat routes: the only Instagram this process can see."""

    def __init__(self, url: str, token: str,
                 client: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        self.client = client or httpx.Client(timeout=60)
        # The app's own header pair, not OAuth: a token without a device is a
        # 401 by design (`resolve_auth`).
        self.client.headers["x-api-token"] = token
        self.client.headers["x-device"] = "relay"

    def chats(self) -> list[dict]:
        resp = self.client.get(f"{self.url}/api/chat")
        resp.raise_for_status()
        return resp.json().get("chats") or []

    def messages(self, ig_id: str) -> list[dict]:
        """One conversation, in the shape `relay_once` reads."""
        resp = self.client.get(f"{self.url}/api/chat/thread",
                               params={"id": ig_id, "read": "0"})
        resp.raise_for_status()
        return [{"id": m["msg_id"], "ts": int(m.get("at") or 0),
                 "mine": bool(m.get("mine")), "sender": m.get("who") or "",
                 "text": m.get("text") or "", "url": m.get("url") or "",
                 "kind": m.get("kind") or ""}
                for m in resp.json().get("messages") or []]

    def send(self, ig_id: str, text: str) -> bool:
        resp = self.client.post(f"{self.url}/api/chat/send",
                                json={"chat_id": ig_id, "text": text})
        return resp.status_code < 400


class Reader:
    """`messages(ig_id)` as `relay_once` wants it: HTTP, not a browser page."""

    def __init__(self, api: Api) -> None:
        self.api = api

    def messages(self, ig_id: str) -> list[dict]:
        return self.api.messages(ig_id)


def threads_for_relay(chats: list[dict]) -> list[dict]:
    """`/api/chat` rows -> the list `relay_once` walks (the inbox's own shape)."""
    return [{
        "id": c["chat_id"],
        "last_id": c.get("last_id") or "",
        "last_ts": c.get("last_at") or 0,
        "name": c.get("title") or c["chat_id"],
        "group": c.get("kind") == "group",
    } for c in chats]


def ig_id_for(conn: sqlite3.Connection, thread_id) -> str | None:
    """The Instagram conversation behind a topic. None: not one we made."""
    if not thread_id:
        return None
    row = conn.execute("SELECT ig_id FROM ig_threads WHERE thread_id=?",
                       (int(thread_id),)).fetchone()
    return (row["ig_id"] or None) if row else None


def inbound_pass(tg, conn: sqlite3.Connection, api: Api, chat_id: str) -> int:
    """One inbox listing -> Telegram. Raises when the API refused the listing."""
    sent, _ = relay.relay_once(tg, conn, chat_id, threads_for_relay(api.chats()),
                               Reader(api))
    return sent


def _outbox_cursor(conn: sqlite3.Connection) -> int:
    """The row id of the last message considered; the first run starts at the newest."""
    cursor = store.meta_json(conn, "ig_outbox", None)
    if cursor is not None:
        return int(cursor)
    row = conn.execute("SELECT MAX(id) AS id FROM tg_messages").fetchone()
    cursor = int((row and row["id"]) or 0)
    store.set_meta_json(conn, "ig_outbox", cursor)
    return cursor


def outbound_pass(conn: sqlite3.Connection, api: Api, chat_id: str) -> int:
    """New Telegram messages in our group -> Instagram. One attempt each.

    The cursor advances for a skipped row too: an unknown topic is a row that
    will never be sendable, not a queue to retry.
    """
    cursor = _outbox_cursor(conn)
    sent = 0
    for row in relay.outbox_rows(conn, cursor, chat_id):
        text = (row["text"] or "").strip()
        ig_id = ig_id_for(conn, row["thread_id"])
        if ig_id and text:
            try:
                if api.send(ig_id, text):
                    sent += 1
                else:
                    log.warning("outbound %s: the API refused it", row["id"])
            except Exception as exc:   # noqa: BLE001 — one message, not a dead loop
                log.warning("outbound %s failed: %s: %s", row["id"],
                            type(exc).__name__, exc)
        cursor = int(row["id"])
        store.set_meta_json(conn, "ig_outbox", cursor)
    return sent


def inbound_loop(stop: threading.Event, tg, conn: sqlite3.Connection, api: Api,
                 chat_id: str, interval: int) -> None:
    while not stop.is_set():
        try:
            sent = inbound_pass(tg, conn, api, chat_id)
            if sent:
                log.info("relayed %d message(s)", sent)
        except Exception as exc:       # noqa: BLE001 — one bad pass, not a dead process
            log.warning("inbox pass failed: %s: %s", type(exc).__name__, exc)
        stop.wait(interval)


def outbound_loop(stop: threading.Event, conn: sqlite3.Connection, api: Api,
                  chat_id: str) -> None:
    while not stop.is_set():
        try:
            outbound_pass(conn, api, chat_id)
        except Exception as exc:       # noqa: BLE001
            log.warning("outbound pass failed: %s: %s", type(exc).__name__, exc)
        stop.wait(OUTBOUND_INTERVAL)


def import_topics(src: Path | str, conn: sqlite3.Connection) -> int:
    """Adopt the topics the in-process relay already made, before its first pass.

    Without this the service's empty database looks like a fresh install: the
    primed rule adopts the inbox (so nothing is replayed) but `topic_for` then
    creates a *second* topic for every conversation. Read-only on the source,
    idempotent on the target.
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"no such database: {src}")
    other = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    rows = other.execute("SELECT name, thread_id, ig_id FROM ig_threads").fetchall()
    other.close()
    saved = 0
    for name, thread_id, ig_id in rows:
        known = conn.execute("SELECT thread_id FROM ig_threads WHERE ig_id=?",
                             (ig_id,)).fetchone() if ig_id else None
        if known:
            continue
        store.save_ig_thread(conn, name, int(thread_id), ig_id=ig_id or "")
        saved += 1
    return saved


def main(env: dict[str, str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every URL at INFO, and the Bot API carries the token in it.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if len(sys.argv) == 3 and sys.argv[1] == "--import-topics":
        cfg = Settings.from_env(env)
        cfg.db.parent.mkdir(parents=True, exist_ok=True)
        conn = store.connect(cfg.db)
        store.init(conn)
        try:
            count = import_topics(sys.argv[2], conn)
        except FileNotFoundError as exc:
            log.error("%s", exc)
            return 1
        print(f"imported {count} topic(s) into {cfg.db}")
        conn.close()
        return 0
    cfg = Settings.from_env(env)
    missing = [key for key in ("api_token", "telegram_token", "relay_chat")
               if not getattr(cfg, key)]
    if missing:
        log.error("relayd needs API_TOKEN, TELEGRAM_TOKEN and TELEGRAM_RELAY_CHAT;"
                  " missing: %s", ", ".join(missing))
        return 1
    cfg.db.parent.mkdir(parents=True, exist_ok=True)
    conn = store.connect(cfg.db)
    store.init(conn)

    api = Api(cfg.api_url, cfg.api_token)
    tg = Telegram(cfg.telegram_token)
    # What `start_poller` asks of a config, and nothing else: no second Config.
    poll_cfg = SimpleNamespace(telegram_token=cfg.telegram_token,
                               relay_chat=cfg.relay_chat, wa_relay_chat="")

    stop = threading.Event()

    def finish(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)

    poller = telegram.start_poller(poll_cfg, conn)
    workers = [
        threading.Thread(target=inbound_loop, name="relay-in", daemon=True,
                         args=(stop, tg, conn, api, cfg.relay_chat, cfg.interval)),
        threading.Thread(target=outbound_loop, name="relay-out", daemon=True,
                         args=(stop, conn, api, cfg.relay_chat)),
    ]
    for worker in workers:
        worker.start()
    log.info("relayd up: %s -> chat %s, every %ss", cfg.api_url, cfg.relay_chat,
             cfg.interval)
    stop.wait()
    poller.set()
    for worker in workers:
        worker.join(timeout=5)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
