"""The Instagram inbox, relayed into one Telegram topic per conversation.

Pure decisions, no I/O of its own: `relay_once` is handed the inbox listing and a
reader that answers one conversation, and decides what is new. Today the reader is
the app's `/api/chat/thread` (see `relayd`), so a conversation is fresh when its
*last message id* changes, which is exact, and the messages carry real
timestamps, senders and permalinks.

A conversation is identified by its Instagram thread id, so a rename on either
side does not orphan a topic.
"""
from __future__ import annotations

import logging
import time

from . import store
from .telegram import Telegram, TelegramError

log = logging.getLogger("relay")

MAX_PER_CYCLE = 20          # a burst is not a reason to be rate limited
# A thread seen for the first time is not a backlog to replay: only a message
# this recent is "arriving now" rather than "history we found".
CATCHUP_H = 6.0


def topic_for(tg: Telegram, conn, chat_id: str, name: str,
              create: bool = True, ig_id: str = "") -> int | None:
    """The topic's id, made on first sight. None means "stay in General"."""
    known = store.ig_thread_id(conn, name, ig_id)
    if known:
        if ig_id:
            # Bind the id to the topic once; after that a rename on either side
            # is just a label.
            store.save_ig_thread(conn, name, known, ig_id=ig_id)
        return known
    if not create:
        return None
    try:
        topic = tg.create_topic(chat_id, name)
    except TelegramError:
        # Not a forum, or no Manage Topics: a relay that posts to General beats
        # a relay that stops.
        return None
    thread_id = int((topic or {}).get("message_thread_id") or 0)
    if not thread_id:
        return None
    store.save_ig_thread(conn, name, thread_id, ig_id=ig_id)
    return thread_id


def new_messages(rows: list[dict], mark: dict,
                 catchup_h: float = CATCHUP_H) -> list[dict]:
    """What arrived since the watermark: everything newer, minus our own.

    A watermark is a timestamp, so a message deleted on the phone cannot shift
    anything. An unknown conversation has no watermark to trust, so it yields
    only the newest message and only if it is recent — never a backlog.
    """
    theirs = [m for m in rows if not m.get("mine")]
    if not mark:
        recent = [m for m in theirs if time.time() - m["ts"] <= catchup_h * 3600]
        return recent[-1:]
    since = float(mark.get("ts") or 0)
    return [m for m in theirs if m["ts"] > since]


def relay_once(tg: Telegram, conn, chat_id: str, threads: list[dict],
               direct, max_weeks: float | None = 26,
               catchup_h: float = CATCHUP_H) -> tuple[int, bool]:
    """One inbox listing -> one Telegram message per Instagram message.

    `threads` is the app's `/api/chat` list (`relayd.threads_for_relay`);
    `direct` reads one conversation over `/api/chat/thread`.
    """
    state = store.meta_json(conn, "ig_relay", {}) or {}
    marks: dict = dict(state.get("marks") or {})
    primed = bool(state.get("primed"))
    if not marks:
        # The state from the scraper has no watermarks: adopt everything on this
        # first look instead of replaying a day of it.
        primed = False
    sent = 0
    for thread in threads[:MAX_PER_CYCLE]:
        ig_id = thread["id"]
        if not thread.get("last_id"):
            continue
        mark = marks.get(ig_id) or {}
        if mark.get("last_id") == thread["last_id"]:
            continue
        if not primed:
            # The first look adopts the inbox instead of replaying it: no
            # conversation is read at all.
            marks[ig_id] = {"last_id": thread["last_id"],
                            "ts": thread.get("last_ts") or 0,
                            "name": thread.get("name")}
            continue
        if max_weeks is not None:
            age_weeks = (time.time() - (thread.get("last_ts") or 0)) / 604800
            if age_weeks > max_weeks:
                marks[ig_id] = {"last_id": thread["last_id"],
                                "ts": thread.get("last_ts") or 0,
                                "name": thread.get("name")}
                continue
        try:
            rows = direct.messages(ig_id)
        except Exception as exc:       # noqa: BLE001 — one bad thread, not a dead cycle
            log.warning("thread %s unreadable: %s: %s", ig_id, type(exc).__name__, exc)
            continue
        if not rows:
            continue
        fresh = new_messages(rows, mark, catchup_h)
        thread_tg = None
        for msg in fresh[:MAX_PER_CYCLE]:
            thread_tg = thread_tg or topic_for(tg, conn, chat_id,
                                               thread.get("name") or ig_id,
                                               ig_id=ig_id)
            block = line_for(msg, group=bool(thread.get("group")))
            try:
                sent_msg = tg.send(chat_id, block, thread_id=thread_tg) or {}
                # A reaction in Telegram names this message; the map is what
                # turns it back into the Instagram id.
                store.map_ig_message(conn, chat_id, sent_msg.get("message_id"),
                                     ig_id, msg["id"])
            except TelegramError:
                # A stale topic id: forget it so the next cycle can make a new
                # one. The message is not retried — a lost bubble beats a loop.
                if thread_tg:
                    store.save_ig_thread(conn, thread.get("name") or ig_id, 0,
                                         ig_id=ig_id)
                thread_tg = None
                continue
            record_sent(conn, thread.get("name") or ig_id, block)
            sent += 1
        marks[ig_id] = {"last_id": thread["last_id"],
                        "ts": max(m["ts"] for m in rows),
                        "name": thread.get("name")}
    store.set_meta_json(conn, "ig_relay", {"marks": marks, "primed": True})
    return sent, bool(sent)


def record_sent(conn, name: str, text: str) -> None:
    """What actually left for Telegram, so "did it arrive?" is answerable.

    The Bot API cannot read back the bot's own messages, so the relay keeps its
    own log: the last 50 sends, by conversation and text.
    """
    log = list(store.meta_json(conn, "ig_sent", []) or [])
    log.append({"at": int(time.time()), "name": name, "text": text})
    store.set_meta_json(conn, "ig_sent", log[-50:])


def line_for(msg: dict, group: bool = False, limit: int = 3500) -> str:
    """One Instagram message as one Telegram message, its link under it.

    A shared post is two facts — what was said and what was sent — and Telegram
    has nowhere else to put the second, so the permalink goes under the text.
    A group writes who is speaking; a one-to-one chat has no one else it could be.
    """
    text = " ".join((msg.get("text") or "").split())
    if msg.get("mine"):
        text = f"εγώ: {text}"
    sibling = (msg.get("sender") or "").strip()
    if group and not msg.get("mine") and sibling:
        text = f"{sibling}: {text}"
    url = (msg.get("url") or (msg.get("hrefs") or [""])[0] or "").strip()
    if url and url not in text:
        text = f"{text}\n{url}" if text else url
    return text[:limit]


def batches(messages: list[dict], limit: int = 3000,
            group: bool = False) -> list[str]:
    """A transcript as Telegram-sized blocks: several bubbles per message.

    One Telegram message per bubble would be hundreds of requests per
    conversation; history does not need that fidelity. Ours are marked, theirs
    are bare — the same shape the live relay sends.
    """
    out: list[str] = []
    buf = ""
    for msg in messages or []:
        line = line_for(msg, group=group)
        if not line:
            continue
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n{line}".strip()
    if buf:
        out.append(buf)
    return out


def outbox_rows(conn, cursor: int, chat_id: str, limit: int = 5):
    """What is waiting to be written back, for one group only.

    Topic ids are per group and start low, so an Instagram topic and a WhatsApp
    topic can both be `7`. Without the chat filter this query would hand a
    WhatsApp topic id to the Instagram relay, which would find an `ig_threads`
    row that happens to own that number and write into the wrong conversation.
    """
    return list(conn.execute(
        "SELECT * FROM tg_messages WHERE id > ? AND mine=0"
        " AND thread_id IS NOT NULL AND chat_id=?"
        " ORDER BY id LIMIT ?", (int(cursor), chat_id, int(limit))))
