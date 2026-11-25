"""The relay's one decision: which Instagram messages become a Telegram message.

The inbox is served by the app over HTTP, so these tests speak in thread ids,
message ids and timestamps — the things that actually identify a message.
"""
from __future__ import annotations

import sqlite3
import time

from app import relay, store


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, thread_id=None):
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    def create_topic(self, chat_id, name):
        return {"message_thread_id": 99}

    def call(self, *a, **k):
        return {"ok": True}


class FakeDirect:
    """`messages(ig_id)` as the app would answer it."""

    def __init__(self, messages):
        self._messages = messages

    def messages(self, ig_id):
        return self._messages.get(ig_id, [])


def conn_now():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init(conn)
    return conn


def thread(ig_id="1", name="Mary Levine", last="m3", group=False, ago_min=1):
    return {"id": ig_id, "name": name, "group": group, "users": {},
            "viewer": "me", "last_id": last,
            "last_ts": time.time() - ago_min * 60}


def msg(mid, text, ts=None, mine=False, sender="", url=""):
    return {"id": mid, "text": text, "ts": ts if ts is not None else time.time(),
            "mine": mine, "sender": sender, "url": url}


def test_first_look_only_records():
    conn = conn_now()
    tg = FakeTG()
    sent, _ = relay.relay_once(tg, conn, "1", [thread()],
                               FakeDirect({"1": [msg("m3", "γεια")]}))
    assert (sent, tg.sent) == (0, [])
    assert store.meta_json(conn, "ig_relay")["marks"]["1"]["last_id"] == "m3"


def test_a_new_message_id_is_sent():
    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "1": {"last_id": "m2", "ts": time.time() - 600}}})
    tg = FakeTG()
    sent, _ = relay.relay_once(tg, conn, "1", [thread(last="m3")],
                               FakeDirect({"1": [msg("m3", "στείλε μου το λινκ")]}))
    assert (sent, tg.sent) == (1, ["στείλε μου το λινκ"])


def test_the_same_message_id_is_not_sent_twice():
    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "1": {"last_id": "m3", "ts": time.time()}}})
    tg = FakeTG()
    assert relay.relay_once(tg, conn, "1", [thread()],
                            FakeDirect({"1": [msg("m3", "γεια")]})) == (0, False)
    assert tg.sent == []


def test_our_own_message_is_never_relayed():
    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "1": {"last_id": "m2", "ts": time.time() - 600}}})
    tg = FakeTG()
    relay.relay_once(tg, conn, "1", [thread(last="m3")],
                     FakeDirect({"1": [msg("m3", "τεστ", mine=True)]}))
    assert tg.sent == []
    # The watermark still moves, or the next cycle would look at it again.
    assert store.meta_json(conn, "ig_relay")["marks"]["1"]["last_id"] == "m3"


def test_old_messages_behind_the_watermark_are_not_sent():
    now = time.time()
    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "1": {"last_id": "m2", "ts": now - 60}}})
    tg = FakeTG()
    relay.relay_once(tg, conn, "1", [thread(last="m4")], FakeDirect({"1": [
        msg("m1", "πέρσι", ts=now - 86400 * 300),
        msg("m2", "χθες", ts=now - 86400),
        msg("m3", "πριν", ts=now - 120),
        msg("m4", "τώρα", ts=now - 5)]}))
    assert tg.sent == ["τώρα"]


def test_a_first_sight_sends_only_the_newest_and_only_if_recent():
    now = time.time()
    conn = conn_now()
    tg = FakeTG()
    relay.relay_once(tg, conn, "1", [thread()], FakeDirect({"1": [
        msg("m1", "παλιό", ts=now - 86400 * 30),
        msg("m2", "πιο παλιό", ts=now - 86400),
        msg("m3", "τώρα", ts=now - 60)]}))
    assert tg.sent == []      # nothing is primed on an empty state

    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "9": {"last_id": "x", "ts": now}}})
    tg = FakeTG()
    relay.relay_once(tg, conn, "1", [thread()], FakeDirect({"1": [
        msg("m1", "παλιό", ts=now - 86400 * 30),
        msg("m2", "χθες", ts=now - 86400),
        msg("m3", "τώρα", ts=now - 60)]}))
    assert tg.sent == ["τώρα"]      # the newcomer, not its history


def test_a_conversation_older_than_the_cutoff_is_not_read_at_all():
    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "9": {"last_id": "x", "ts": time.time()}}})

    class Explodes(FakeDirect):
        def messages(self, ig_id):
            raise AssertionError("an old conversation must never be opened")

    tg = FakeTG()
    sent, _ = relay.relay_once(tg, conn, "1",
                               [thread(last="m3", ago_min=60 * 24 * 200)],
                               Explodes({}), max_weeks=26)
    assert (sent, tg.sent) == (0, [])


def test_a_group_names_the_sender_and_a_direct_chat_does_not():
    now = time.time()
    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "1": {"last_id": "m1", "ts": now - 60}, "2": {"last_id": "m1", "ts": now - 60}}})
    tg = FakeTG()
    relay.relay_once(tg, conn, "1", [thread("1", "monday five", "m2", group=True),
                                     thread("2", "Mary Levine", "m2")],
                     FakeDirect({"1": [msg("m2", "1213", ts=now, sender="g.papadakis__")],
                                 "2": [msg("m2", "γεια", ts=now, sender="mary__levine")]}))
    assert tg.sent == ["g.papadakis__: 1213", "γεια"]


def test_a_shared_post_keeps_its_permalink():
    now = time.time()
    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "1": {"last_id": "m1", "ts": now - 60}}})
    tg = FakeTG()
    relay.relay_once(tg, conn, "1", [thread("1", "Mary Levine", "m2")],
                     FakeDirect({"1": [msg("m2", "", ts=now,
                                           url="https://www.instagram.com/p/ABC/")]}))
    assert tg.sent == ["https://www.instagram.com/p/ABC/"]


def test_each_message_gets_its_own_telegram_message():
    now = time.time()
    conn = conn_now()
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "1": {"last_id": "m1", "ts": now - 600}}})
    tg = FakeTG()
    relay.relay_once(tg, conn, "1", [thread("1", "Mary Levine", "m3")],
                     FakeDirect({"1": [msg("m2", "δες αυτό", ts=now - 2,
                                           url="https://www.instagram.com/p/xyz/"),
                                       msg("m3", "και αυτό", ts=now - 1)]}))
    assert tg.sent == ["δες αυτό\nhttps://www.instagram.com/p/xyz/", "και αυτό"]


def test_a_message_reports_its_own_topic_by_thread_id():
    conn = conn_now()
    store.save_ig_thread(conn, "Mary Levine", 14, ig_id="1742")
    assert store.ig_thread_id(conn, "Mary Levine", "1742") == 14
    assert store.ig_thread_id(conn, "Mary Levine") == 14
    # A renamed conversation keeps the topic: the id is the key.
    store.save_ig_thread(conn, "Mary L.", 14, ig_id="1742")
    assert store.ig_thread_id(conn, "Mary L.", "1742") == 14


def test_thread_id_survives_the_round_trip(tmp_path):
    conn = store.connect(tmp_path / "relay.sqlite3")
    store.init(conn)
    assert store.ig_thread_id(conn, "xara.gian") is None
    store.save_ig_thread(conn, "xara.gian", 42)
    assert store.ig_thread_id(conn, "xara.gian") == 42
    store.save_ig_thread(conn, "xara.gian", 43)         # a remake wins
    assert store.ig_thread_id(conn, "xara.gian") == 43


def test_a_tg_message_keeps_its_topic(tmp_path):
    conn = store.connect(tmp_path / "relay.sqlite3")
    store.init(conn)
    store.save_tg_message(conn, {"chat_id": "-100", "msg_id": "1", "at": 5,
                                 "title": "Instagram", "kind": "group",
                                 "text": "hi", "thread_id": 7})
    assert conn.execute("SELECT thread_id FROM tg_messages").fetchone()["thread_id"] == 7


def test_history_becomes_few_telegram_blocks():
    msgs = [{"text": "γεια", "mine": False}, {"text": "τι κάνεις", "mine": True}]
    assert relay.batches(msgs) == ["γεια\nεγώ: τι κάνεις"]
    long = [{"text": "x" * 2000, "mine": False} for _ in range(3)]
    blocks = relay.batches(long, limit=3000)
    assert len(blocks) == 3 and all(len(b) <= 3000 for b in blocks)


def test_an_outbox_only_sees_its_own_group(conn_now=None):
    """Topic ids are per group. A WhatsApp topic 7 and an Instagram topic 7 are
    different conversations, so a row must never cross the fence."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init(c)
    store.save_tg_message(c, {"chat_id": "-100ig", "msg_id": "1", "kind": "dm",
                              "thread_id": 7, "text": "στο Instagram",
                              "link": None, "at": 1, "mine": 0})
    store.save_tg_message(c, {"chat_id": "-100wa", "msg_id": "1", "kind": "dm",
                              "thread_id": 7, "text": "στο WhatsApp",
                              "link": None, "at": 2, "mine": 0})
    ig = relay.outbox_rows(c, 0, "-100ig")
    wa = relay.outbox_rows(c, 0, "-100wa")
    assert [r["text"] for r in ig] == ["στο Instagram"]
    assert [r["text"] for r in wa] == ["στο WhatsApp"]
    assert relay.outbox_rows(c, 0, "-100other") == []
