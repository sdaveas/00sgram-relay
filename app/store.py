"""SQLite: the relay's own state, and nothing else.

All writes go through `write()` (one RLock, one transaction). Reads are
lock-free on WAL.

Only the relay's tables live here: the topic mapping, the Telegram messages it
polls, the sent map and the reaction queue. The DDL text is the same as the app's
and `init` adds the same columns, so an existing `/data/relay.sqlite3` from the
monolithic image keeps working.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

WRITE_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta     (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS tg_chats (
  chat_id TEXT PRIMARY KEY, title TEXT, kind TEXT, read_at INTEGER
);
CREATE TABLE IF NOT EXISTS tg_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, msg_id TEXT NOT NULL,
  at INTEGER NOT NULL, mine INTEGER NOT NULL DEFAULT 0, who TEXT,
  kind TEXT, text TEXT, links TEXT, thread_id INTEGER,
  UNIQUE (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS tg_messages_thread ON tg_messages (chat_id, at);

-- One Telegram forum topic per Instagram conversation. The name is the key the
-- inbox gives us; the topic itself can be renamed in Telegram without breaking
-- the mapping, because nothing reads the topic's title back.
CREATE TABLE IF NOT EXISTS ig_threads (
  name TEXT PRIMARY KEY, thread_id INTEGER NOT NULL,
  title TEXT, created_at INTEGER NOT NULL
);

-- Which Telegram message stands for which Instagram message. Written when the
-- relay sends, read when a reaction arrives in Telegram: a reaction names a
-- Telegram message, and Instagram wants the id of the Instagram one.
CREATE TABLE IF NOT EXISTS ig_sent_map (
  chat_id TEXT NOT NULL, tg_msg_id TEXT NOT NULL,
  ig_id TEXT NOT NULL, ig_msg_id TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY (chat_id, tg_msg_id)
);

-- Reactions typed in Telegram, waiting to be replayed on Instagram. A table and
-- not a queue in memory: a restart must not swallow one.
CREATE TABLE IF NOT EXISTS ig_reacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL, tg_msg_id TEXT NOT NULL,
  emoji TEXT NOT NULL, at INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# Columns added after the first deploy. CREATE TABLE IF NOT EXISTS cannot add a
# column to a file that already has the table, and the Pi has real data in it.
# Only the rows that touch the relay's own tables survive the split; the app's
# extras (`posts.liked`, `stories.*`) are not this process's business.
NEW_COLUMNS = (
    ("tg_messages", "thread_id", "INTEGER"),
    ("ig_threads", "ig_id", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in NEW_COLUMNS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init(conn: sqlite3.Connection) -> None:
    """Create the schema. No settings row: this process has no settings table."""
    with write(conn):
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)


@contextmanager
def write(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One transaction per outermost block; nested calls join the caller's.

    Explicit BEGIN IMMEDIATE so a writer takes the lock up front (no upgrade
    deadlock on WAL), and so a sync page is one atomic unit.
    """
    with WRITE_LOCK:
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


# --------------------------------------------------------------------- meta


def meta_json(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta_json(conn: sqlite3.Connection, key: str, value) -> None:
    with write(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))


# ------------------------------------------------------------------ telegram


def map_ig_message(conn: sqlite3.Connection, chat_id: str, tg_msg_id,
                   ig_id: str, ig_msg_id: str, now: int | None = None) -> None:
    """Remember which Telegram message carries which Instagram message."""
    if not tg_msg_id:
        return
    with write(conn):
        conn.execute(
            "INSERT OR REPLACE INTO ig_sent_map (chat_id, tg_msg_id, ig_id,"
            " ig_msg_id, created_at) VALUES (?,?,?,?,?)",
            (str(chat_id), str(tg_msg_id), str(ig_id), str(ig_msg_id),
             int(now if now is not None else time.time())))


def ig_message_for(conn: sqlite3.Connection, chat_id: str,
                   tg_msg_id) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT ig_id, ig_msg_id FROM ig_sent_map WHERE chat_id=? AND tg_msg_id=?",
        (str(chat_id), str(tg_msg_id))).fetchone()
    return (row["ig_id"], row["ig_msg_id"]) if row else None


def queue_reaction(conn: sqlite3.Connection, chat_id: str, tg_msg_id,
                   emoji: str, now: int | None = None,
                   table: str = "ig_reacts") -> None:
    _reaction_table(table)
    with write(conn):
        conn.execute(
            f"INSERT INTO {table} (chat_id, tg_msg_id, emoji, at, done)"
            " VALUES (?,?,?,?,0)",
            (str(chat_id), str(tg_msg_id), emoji or "",
             int(now if now is not None else time.time())))


def next_reaction(conn: sqlite3.Connection, table: str = "ig_reacts"):
    _reaction_table(table)
    return conn.execute(
        f"SELECT * FROM {table} WHERE done=0 ORDER BY id LIMIT 1").fetchone()


def finish_reaction(conn: sqlite3.Connection, row_id: int,
                    error: str = "", table: str = "ig_reacts") -> None:
    _reaction_table(table)
    with write(conn):
        conn.execute(f"UPDATE {table} SET done=1, error=? WHERE id=?",
                     (error or None, int(row_id)))


def _reaction_table(table: str) -> None:
    """The table name comes from a caller, so it is a two-name whitelist, not a
    string to format into SQL. `wa_reacts` stays listed because
    `telegram.ingest` names it, but this database has no WhatsApp queue."""
    if table not in ("ig_reacts", "wa_reacts"):
        raise ValueError(f"unknown reaction table {table!r}")


def save_tg_message(conn: sqlite3.Connection, row: dict) -> None:
    """One message plus its chat row. An edit is the same message (chat_id, msg_id)."""
    links = json.dumps(row.get("links") or [], ensure_ascii=False)
    with write(conn):
        conn.execute(
            "INSERT INTO tg_messages (chat_id, msg_id, at, mine, who, kind, text,"
            " links, thread_id) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(chat_id, msg_id) DO UPDATE SET text=excluded.text,"
            " links=excluded.links, at=excluded.at, kind=excluded.kind,"
            " who=excluded.who, mine=excluded.mine,"
            " thread_id=COALESCE(excluded.thread_id, thread_id)",
            (row["chat_id"], row["msg_id"], int(row["at"]), int(row.get("mine") or 0),
             row.get("who"), row.get("kind_msg"), row.get("text"), links,
             row.get("thread_id")))
        # Never touches read_at: saving a message must not mark a chat unread.
        conn.execute(
            "INSERT INTO tg_chats (chat_id, title, kind) VALUES (?,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, kind=excluded.kind",
            (row["chat_id"], row.get("title"), row.get("kind")))


def ig_thread_id(conn: sqlite3.Connection, name: str,
                 ig_id: str = "") -> int | None:
    """The Telegram topic for a conversation, by Instagram thread id first.

    The id survives a rename on either side; the name is only a fallback for
    rows written before the id was known.
    """
    if ig_id:
        row = conn.execute("SELECT thread_id FROM ig_threads WHERE ig_id=?",
                           (str(ig_id),)).fetchone()
        if row:
            return int(row["thread_id"])
    row = conn.execute("SELECT thread_id FROM ig_threads WHERE name=?",
                       (name,)).fetchone()
    return int(row["thread_id"]) if row else None


def save_ig_thread(conn: sqlite3.Connection, name: str, thread_id: int,
                   title: str = "", now: int | None = None,
                   ig_id: str = "") -> None:
    with write(conn):
        conn.execute(
            "INSERT INTO ig_threads (name, thread_id, title, created_at, ig_id)"
            " VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
            " thread_id=excluded.thread_id, title=excluded.title,"
            " ig_id=COALESCE(NULLIF(excluded.ig_id,''), ig_threads.ig_id)",
            (name, int(thread_id), title or name,
             int(now if now is not None else time.time()),
             str(ig_id or "")))
