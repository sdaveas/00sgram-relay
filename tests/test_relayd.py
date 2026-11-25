"""`python -m app.relayd`: the relay as a client of the web app, offline.

No network: the API is an `httpx.MockTransport` standing in for the three chat
routes, Telegram is a fake with the two methods `relay_once` calls, and the store
is a temp file. What is pinned: the thread payload becomes the shape
`relay_once` reads, a new Instagram message is one Telegram message and only
once, a fresh database adopts the inbox instead of replaying it, and one
outbound row is exactly one `POST /api/chat/send` — with failure advancing the
cursor rather than retrying.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from app import relayd, store
from app.config import Settings

NOW = int(time.time())


class FakeTG:
    """The two calls `relay.topic_for` / `relay_once` make, and nothing else."""

    def __init__(self):
        self.sent = []
        self.topics = []

    def send(self, chat_id, text, thread_id=None):
        self.sent.append((thread_id, text))
        return {"message_id": len(self.sent)}

    def create_topic(self, chat_id, name):
        self.topics.append(name)
        return {"message_thread_id": 7}

    def call(self, *a, **k):
        return {"ok": True}


class Pages:
    """The web app's answers, and every request that asked for them."""

    def __init__(self, chats=None, threads=None, send_status=200):
        self.chats = chats or []
        self.threads = threads or {}
        self.send_status = send_status
        self.requests = []
        self.auth = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": request.url.path,
                              "params": dict(request.url.params), "body": body})
        self.auth.append((request.headers.get("x-api-token"),
                          request.headers.get("x-device")))
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"configured": True, "chats": self.chats})
        if request.url.path == "/api/chat/thread":
            tid = request.url.params.get("id")
            return httpx.Response(200, json={"chat_id": tid,
                                             "messages": self.threads.get(tid, [])})
        if request.url.path == "/api/chat/send":
            return httpx.Response(self.send_status, json={"message": {}})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def api(self) -> relayd.Api:
        return relayd.Api("http://web:8080", "tok",
                          client=httpx.Client(transport=httpx.MockTransport(self.handler)))

    def posts(self, path="/api/chat/send"):
        return [r for r in self.requests if (r["method"], r["path"]) == ("POST", path)]


def chat(chat_id="t1", title="Mary Levine", kind="dm", last_id="m3", ago_min=1):
    return {"chat_id": chat_id, "title": title, "kind": kind, "unread": 0,
            "last_at": NOW - ago_min * 60, "last_text": "", "last_kind": "text",
            "last_id": last_id}


def msg(msg_id, text, at):
    return {"msg_id": msg_id, "at": at, "mine": False, "who": "mary__levine",
            "text": text, "kind": "text", "url": "", "links": []}


def seed_outbound(conn, text="hi", thread_id=7, chat_id="-100", msg_id="1"):
    store.save_tg_message(conn, {
        "chat_id": chat_id, "msg_id": msg_id, "at": NOW, "mine": 0, "who": "me",
        "kind_msg": "text", "text": text, "links": [], "title": "Instagram",
        "kind": "group", "thread_id": thread_id})


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(tmp_path / "relay.sqlite3")
    store.init(c)
    yield c
    c.close()


def test_settings_are_explicit_and_defaulted():
    cfg = Settings.from_env({})
    assert (cfg.api_url, cfg.api_token, cfg.telegram_token, cfg.relay_chat,
            str(cfg.db), cfg.interval) == (
        "http://web:8080", "", "", "", "/data/relay.sqlite3", 20)

    cfg = Settings.from_env({"API_URL": "http://host:1/",
                                    "RELAY_INTERVAL": "5",
                                    "RELAY_DB": "/tmp/x.sqlite3",
                                    "TELEGRAM_RELAY_CHAT": " -100 "})
    assert (cfg.api_url, cfg.interval, str(cfg.db), cfg.relay_chat) == \
        ("http://host:1", 5, "/tmp/x.sqlite3", "-100")


def test_every_call_carries_the_app_pair_of_headers(conn):
    pages = Pages(chats=[chat()])
    pages.api().chats()
    # A bearer token would be a 401: the app authenticates on this pair.
    assert pages.auth == [("tok", "relay")]


def test_a_thread_becomes_the_shape_relay_once_reads(conn):
    pages = Pages(threads={"t1": [msg("m1", "γεια", NOW - 60)]})
    api = pages.api()

    assert api.messages("t1") == [{
        "id": "m1", "ts": NOW - 60, "mine": False, "sender": "mary__levine",
        "text": "γεια", "url": "", "kind": "text"}]
    assert pages.requests[0]["params"] == {"id": "t1", "read": "0"}
    assert pages.auth[-1] == ("tok", "relay")   # every call carries the pair


def test_a_new_instagram_message_lands_once(conn):
    pages = Pages(chats=[chat(last_id="m3")],
                  threads={"t1": [msg("m2", "δες αυτό", NOW - 120),
                                  msg("m3", "τώρα", NOW - 5)]})
    store.set_meta_json(conn, "ig_relay", {"primed": True, "marks": {
        "t1": {"last_id": "m2", "ts": NOW - 120}}})
    tg = FakeTG()

    assert relayd.inbound_pass(tg, conn, pages.api(), "-100") == 1
    assert tg.sent == [(7, "τώρα")]          # in the conversation's topic

    # Same listing again: `last_id` is unchanged, so nothing is even opened.
    assert relayd.inbound_pass(tg, conn, pages.api(), "-100") == 0
    assert tg.sent == [(7, "τώρα")]
    assert len([r for r in pages.requests if r["path"] == "/api/chat/thread"]) == 1


def test_the_first_pass_adopts_the_inbox_and_sends_nothing(conn):
    pages = Pages(chats=[chat(last_id="m3")],
                  threads={"t1": [msg("m3", "τώρα", NOW - 5)]})
    tg = FakeTG()

    assert relayd.inbound_pass(tg, conn, pages.api(), "-100") == 0
    assert (tg.sent, tg.topics) == ([], [])
    assert [r["path"] for r in pages.requests] == ["/api/chat"]
    state = store.meta_json(conn, "ig_relay")
    assert (state["primed"], state["marks"]["t1"]["last_id"]) == (True, "m3")


def test_one_outbound_row_is_one_post(conn):
    pages = Pages()
    api = pages.api()
    assert relayd.outbound_pass(conn, api, "-100") == 0      # nothing to replay

    store.save_ig_thread(conn, "Mary Levine", 7, ig_id="t1")
    seed_outbound(conn, "hi there")

    assert relayd.outbound_pass(conn, api, "-100") == 1
    assert pages.posts() == [{"method": "POST", "path": "/api/chat/send",
                              "params": {}, "body": {"chat_id": "t1",
                                                     "text": "hi there"}}]
    assert relayd.outbound_pass(conn, api, "-100") == 0
    assert len(pages.posts()) == 1          # the cursor, not a second send


def test_a_quiet_outbox_makes_no_request(conn):
    pages = Pages()
    api = pages.api()
    assert relayd.outbound_pass(conn, api, "-100") == 0
    assert relayd.outbound_pass(conn, api, "-100") == 0
    assert pages.requests == []


def test_a_refused_send_advances_the_cursor_and_is_not_retried(conn):
    pages = Pages(send_status=502)
    api = pages.api()
    assert relayd.outbound_pass(conn, api, "-100") == 0

    store.save_ig_thread(conn, "Mary Levine", 7, ig_id="t1")
    seed_outbound(conn, "hi")

    assert relayd.outbound_pass(conn, api, "-100") == 0
    assert relayd.outbound_pass(conn, api, "-100") == 0
    assert len(pages.posts()) == 1
    row = conn.execute("SELECT id FROM tg_messages").fetchone()
    assert store.meta_json(conn, "ig_outbox") == row["id"]


def test_a_topic_we_did_not_make_is_not_sent_to(conn):
    pages = Pages()
    api = pages.api()
    assert relayd.outbound_pass(conn, api, "-100") == 0

    seed_outbound(conn, "hi", thread_id=9)   # no ig_threads row owns topic 9
    assert relayd.outbound_pass(conn, api, "-100") == 0
    assert pages.requests == []
    # Skipped is still considered: the cursor moves past it.
    assert store.meta_json(conn, "ig_outbox") == 1


def test_the_topics_the_old_relay_made_are_adopted_once(conn, tmp_path):
    old = store.connect(tmp_path / "app.sqlite3")
    store.init(old)
    store.save_ig_thread(old, "Mary Levine", 7, ig_id="t1")
    store.save_ig_thread(old, "the group", 12, ig_id="t2")
    old.close()

    assert relayd.import_topics(tmp_path / "app.sqlite3", conn) == 2
    assert conn.execute("SELECT thread_id FROM ig_threads WHERE ig_id='t2'"
                        ).fetchone()["thread_id"] == 12
    # A second run is a no-op: the same conversation keeps its topic.
    assert relayd.import_topics(tmp_path / "app.sqlite3", conn) == 0
