"""Phase 3 Plan 01: Slack twin core endpoints + auth + error shapes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import slack as sl


@pytest.fixture(autouse=True)
def _reset_state():
    sl.STATE.clear()
    sl.STATE.update(sl._fresh_state())
    sl.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(sl.app)


TOKEN = sl.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}


# --- auth gate ----------------------------------------------------------

def test_missing_token_returns_not_authed(client):
    r = client.post("/api/chat.postMessage", json={"channel": "C1", "text": "hi"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "not_authed"}


def test_wrong_token_returns_invalid_auth(client):
    r = client.get("/api/conversations.list", headers={"Authorization": "Bearer xoxb-CHECKPOINTFAKE-wrong"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "invalid_auth"}


def test_env_override(monkeypatch, client):
    monkeypatch.setenv("SLACK_BOOTSTRAP_TOKEN", "xoxb-CHECKPOINTFAKE-env-override")
    r = client.get("/api/conversations.list", headers=H)
    assert r.json() == {"ok": False, "error": "invalid_auth"}
    r = client.get("/api/conversations.list", headers={"Authorization": "Bearer xoxb-CHECKPOINTFAKE-env-override"})
    assert r.json()["ok"] is True


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


def test_introspection_not_in_trace(client):
    client.get("/_health")
    client.get("/_state")
    assert client.get("/_trace").json() == []
    client.get("/api/conversations.list", headers=H)
    trace = client.get("/_trace").json()
    assert len(trace) == 1
    assert trace[0]["path"] == "/api/conversations.list"


# --- chat.postMessage ---------------------------------------------------

def _seed_channel(channel_id: str = "C0001", name: str = "general"):
    sl.STATE["channels"][channel_id] = {
        "id": channel_id, "name": name, "is_channel": True, "num_members": 3,
    }
    sl.STATE["messages"][channel_id] = []


def test_post_message_happy_path(client):
    _seed_channel()
    r = client.post(
        "/api/chat.postMessage",
        headers=H,
        json={"channel": "C0001", "text": "hello team"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["channel"] == "C0001"
    assert "ts" in body
    assert body["message"]["text"] == "hello team"
    assert len(sl.STATE["messages"]["C0001"]) == 1


def test_post_message_resolves_by_name(client):
    _seed_channel()
    r = client.post(
        "/api/chat.postMessage",
        headers=H,
        json={"channel": "general", "text": "ping"},
    )
    assert r.json()["ok"] is True


def test_post_message_missing_text(client):
    _seed_channel()
    r = client.post("/api/chat.postMessage", headers=H, json={"channel": "C0001"})
    assert r.json() == {"ok": False, "error": "missing required arguments: text"}


def test_post_message_missing_channel(client):
    r = client.post("/api/chat.postMessage", headers=H, json={"text": "hi"})
    assert r.json() == {"ok": False, "error": "missing required arguments: channel"}


def test_post_message_channel_not_found(client):
    r = client.post("/api/chat.postMessage", headers=H, json={"channel": "C_nope", "text": "x"})
    assert r.json() == {"ok": False, "error": "channel_not_found"}


def test_post_message_thread_reply_bumps_parent(client):
    _seed_channel()
    parent = client.post(
        "/api/chat.postMessage", headers=H,
        json={"channel": "C0001", "text": "parent"},
    ).json()
    pts = parent["ts"]
    reply = client.post(
        "/api/chat.postMessage", headers=H,
        json={"channel": "C0001", "text": "reply", "thread_ts": pts},
    ).json()
    assert reply["ok"] is True
    assert reply["message"]["thread_ts"] == pts
    parent_state = sl._find_message("C0001", pts)
    assert parent_state["reply_count"] == 1
    assert parent_state["latest_reply"] == reply["ts"]


# --- conversations.history ----------------------------------------------

def test_conversations_history_returns_top_level(client):
    _seed_channel()
    a = client.post("/api/chat.postMessage", headers=H,
                    json={"channel": "C0001", "text": "first"}).json()
    client.post("/api/chat.postMessage", headers=H,
                json={"channel": "C0001", "text": "reply", "thread_ts": a["ts"]})
    client.post("/api/chat.postMessage", headers=H,
                json={"channel": "C0001", "text": "second"})
    r = client.get("/api/conversations.history?channel=C0001", headers=H)
    body = r.json()
    assert body["ok"] is True
    # Two top-level (first + second), no thread reply.
    texts = {m["text"] for m in body["messages"]}
    assert texts == {"first", "second"}


def test_conversations_history_missing_channel(client):
    r = client.get("/api/conversations.history", headers=H)
    assert r.json() == {"ok": False, "error": "missing required arguments: channel"}


# --- conversations.replies ----------------------------------------------

def test_conversations_replies_returns_parent_plus_replies(client):
    _seed_channel()
    p = client.post("/api/chat.postMessage", headers=H,
                    json={"channel": "C0001", "text": "p"}).json()
    client.post("/api/chat.postMessage", headers=H,
                json={"channel": "C0001", "text": "r1", "thread_ts": p["ts"]})
    client.post("/api/chat.postMessage", headers=H,
                json={"channel": "C0001", "text": "r2", "thread_ts": p["ts"]})
    r = client.get(f"/api/conversations.replies?channel=C0001&ts={p['ts']}", headers=H)
    body = r.json()
    assert body["ok"] is True
    assert [m["text"] for m in body["messages"]] == ["p", "r1", "r2"]


def test_conversations_replies_missing_ts(client):
    _seed_channel()
    r = client.get("/api/conversations.replies?channel=C0001", headers=H)
    assert r.json() == {"ok": False, "error": "missing required arguments: ts"}


# --- conversations.list -------------------------------------------------

def test_conversations_list_cursor_pagination(client):
    for i in range(5):
        cid = f"C0000000{i+1}"
        sl.STATE["channels"][cid] = {"id": cid, "name": f"chan-{i}", "is_channel": True}
    r = client.get("/api/conversations.list?limit=2", headers=H)
    body = r.json()
    assert body["ok"] is True
    assert len(body["channels"]) == 2
    cursor = body["response_metadata"]["next_cursor"]
    assert cursor == "2"
    r2 = client.get(f"/api/conversations.list?limit=2&cursor={cursor}", headers=H)
    body2 = r2.json()
    assert len(body2["channels"]) == 2
    r3 = client.get(f"/api/conversations.list?limit=2&cursor={body2['response_metadata']['next_cursor']}", headers=H)
    body3 = r3.json()
    assert len(body3["channels"]) == 1
    assert body3["response_metadata"]["next_cursor"] == ""


# --- reactions.add ------------------------------------------------------

def test_reactions_add_creates_reaction(client):
    _seed_channel()
    p = client.post("/api/chat.postMessage", headers=H,
                    json={"channel": "C0001", "text": "x"}).json()
    r = client.post("/api/reactions.add", headers=H, json={
        "channel": "C0001", "timestamp": p["ts"], "name": "thumbsup",
    })
    assert r.json() == {"ok": True}
    msg = sl._find_message("C0001", p["ts"])
    assert msg["reactions"][0]["name"] == "thumbsup"
    assert msg["reactions"][0]["count"] == 1


def test_reactions_add_missing_name(client):
    _seed_channel()
    p = client.post("/api/chat.postMessage", headers=H,
                    json={"channel": "C0001", "text": "x"}).json()
    r = client.post("/api/reactions.add", headers=H, json={
        "channel": "C0001", "timestamp": p["ts"],
    })
    assert r.json() == {"ok": False, "error": "missing required arguments: name"}


def test_reactions_add_message_not_found(client):
    _seed_channel()
    r = client.post("/api/reactions.add", headers=H, json={
        "channel": "C0001", "timestamp": "9999.000001", "name": "fire",
    })
    assert r.json() == {"ok": False, "error": "message_not_found"}


# --- conversations.create -----------------------------------------------

def test_conversations_create_happy_path(client):
    r = client.post("/api/conversations.create", headers=H, json={"name": "engineering"})
    body = r.json()
    assert body["ok"] is True
    ch = body["channel"]
    assert ch["name"] == "engineering"
    assert ch["id"].startswith("C")
    assert ch["is_channel"] is True
    assert ch["is_private"] is False
    assert ch["is_archived"] is False
    # Recorded in state, message list initialized.
    assert sl.STATE["channels"][ch["id"]]["name"] == "engineering"
    assert sl.STATE["messages"][ch["id"]] == []


def test_conversations_create_missing_name(client):
    r = client.post("/api/conversations.create", headers=H, json={})
    assert r.json() == {"ok": False, "error": "missing required arguments: name"}


def test_conversations_create_name_taken(client):
    _seed_channel(name="general")
    r = client.post("/api/conversations.create", headers=H, json={"name": "general"})
    assert r.json() == {"ok": False, "error": "name_taken"}


def test_conversations_create_invalid_name(client):
    r = client.post("/api/conversations.create", headers=H, json={"name": "Bad Name!!"})
    assert r.json() == {"ok": False, "error": "invalid_name"}


def test_conversations_create_strips_hash_and_lowercases(client):
    r = client.post("/api/conversations.create", headers=H, json={"name": "#Incident-2026"})
    body = r.json()
    assert body["ok"] is True
    assert body["channel"]["name"] == "incident-2026"


def test_conversations_create_private(client):
    r = client.post("/api/conversations.create", headers=H, json={"name": "secrets", "is_private": True})
    ch = r.json()["channel"]
    assert ch["is_private"] is True
    assert ch["is_channel"] is False


def test_conversations_create_then_post_and_list(client):
    ch = client.post("/api/conversations.create", headers=H, json={"name": "incident-1"}).json()["channel"]
    r = client.post("/api/chat.postMessage", headers=H, json={"channel": "incident-1", "text": "fire"})
    assert r.json()["ok"] is True
    listed = client.get("/api/conversations.list", headers=H).json()
    assert any(c["id"] == ch["id"] for c in listed["channels"])


def test_conversations_create_requires_auth(client):
    r = client.post("/api/conversations.create", json={"name": "nope"})
    assert r.json() == {"ok": False, "error": "not_authed"}


def test_conversations_create_recorded_in_trace(client):
    client.post("/api/conversations.create", headers=H, json={"name": "traced"})
    trace = client.get("/_trace").json()
    assert any(e["path"] == "/api/conversations.create" for e in trace)


def test_conversations_create_reset_clears(client):
    client.post("/api/conversations.create", headers=H, json={"name": "temp"})
    assert len(sl.STATE["channels"]) == 1
    client.post("/_reset")
    assert sl.STATE["channels"] == {}


# --- conversations.info -------------------------------------------------

def test_conversations_info_by_id(client):
    _seed_channel("C0001", "general")
    r = client.get("/api/conversations.info?channel=C0001", headers=H)
    body = r.json()
    assert body["ok"] is True
    assert body["channel"]["name"] == "general"


def test_conversations_info_by_name(client):
    _seed_channel("C0001", "general")
    r = client.get("/api/conversations.info?channel=general", headers=H)
    assert r.json()["channel"]["id"] == "C0001"


def test_conversations_info_missing_channel_arg(client):
    r = client.get("/api/conversations.info", headers=H)
    assert r.json() == {"ok": False, "error": "missing required arguments: channel"}


def test_conversations_info_not_found(client):
    r = client.get("/api/conversations.info?channel=C_NOPE", headers=H)
    assert r.json() == {"ok": False, "error": "channel_not_found"}


# --- users.list & users.profile.get -------------------------------------

def test_users_list_pagination(client):
    for i in range(3):
        uid = f"U0000000{i+1}"
        sl.STATE["users"][uid] = {"id": uid, "name": f"user-{i}", "real_name": f"User {i}"}
    r = client.get("/api/users.list?limit=2", headers=H)
    body = r.json()
    assert body["ok"] is True
    assert len(body["members"]) == 2
    assert body["response_metadata"]["next_cursor"] == "2"


def test_users_profile_get_by_id(client):
    sl.STATE["users"]["U00000001"] = {
        "id": "U00000001", "name": "alice", "real_name": "Alice A",
        "profile": {"real_name": "Alice A", "display_name": "alice", "email": "a@x.com"},
    }
    r = client.get("/api/users.profile.get?user=U00000001", headers=H)
    body = r.json()
    assert body["ok"] is True
    assert body["profile"]["email"] == "a@x.com"


def test_users_profile_get_missing_user_arg(client):
    r = client.get("/api/users.profile.get", headers=H)
    assert r.json() == {"ok": False, "error": "missing required arguments: user"}


def test_users_profile_get_user_not_found(client):
    r = client.get("/api/users.profile.get?user=U_NOPE", headers=H)
    assert r.json() == {"ok": False, "error": "user_not_found"}
