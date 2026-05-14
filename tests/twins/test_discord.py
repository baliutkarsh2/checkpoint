"""Discord twin REST surface — auth, guilds, channels, messages, reactions, roles, webhooks."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import discord as dc


@pytest.fixture(autouse=True)
def _reset_state():
    dc.STATE.clear()
    dc.STATE.update(dc._fresh_state())
    dc.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(dc.app)


TOKEN = dc.DEFAULT_BOOTSTRAP_TOKEN
# Discord auth uses "Bot <token>" — strip prefix for header construction
_RAW = TOKEN[4:] if TOKEN.startswith("Bot ") else TOKEN
H = {"Authorization": f"Bot {_RAW}"}


# --- auth -------------------------------------------------------------------

def test_missing_token_returns_401(client):
    r = client.get("/api/v10/users/@me")
    assert r.status_code == 401


def test_wrong_token_returns_401(client):
    r = client.get("/api/v10/users/@me", headers={"Authorization": "Bot wrongtoken"})
    assert r.status_code == 401


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


# --- bot user ---------------------------------------------------------------

def test_get_current_user(client):
    r = client.get("/api/v10/users/@me", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "id" in body
    assert "username" in body


# --- guild ------------------------------------------------------------------

def _make_guild(client) -> str:
    dc.STATE["guilds"]["g1"] = {
        "id": "g1",
        "name": "Test Server",
        "owner_id": "u1",
        "roles": [],
        "channels": [],
    }
    return "g1"


def test_get_guild(client):
    _make_guild(client)
    r = client.get("/api/v10/guilds/g1", headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "Test Server"


def test_get_guild_not_found(client):
    r = client.get("/api/v10/guilds/nope", headers=H)
    assert r.status_code == 404


# --- channels ---------------------------------------------------------------

def _make_channel(client, guild_id: str, name: str = "general") -> str:
    r = client.post(f"/api/v10/guilds/{guild_id}/channels", headers=H, json={
        "name": name, "type": 0
    })
    return r.json()["id"]


def test_create_channel(client):
    _make_guild(client)
    r = client.post("/api/v10/guilds/g1/channels", headers=H, json={"name": "general", "type": 0})
    assert r.status_code in (200, 201)
    body = r.json()
    assert body["name"] == "general"
    assert "id" in body


def test_list_guild_channels(client):
    _make_guild(client)
    _make_channel(client, "g1", "alpha")
    _make_channel(client, "g1", "beta")
    r = client.get("/api/v10/guilds/g1/channels", headers=H)
    assert r.status_code == 200
    names = [c["name"] for c in r.json()]
    assert "alpha" in names and "beta" in names


def test_get_channel(client):
    _make_guild(client)
    cid = _make_channel(client, "g1", "news")
    r = client.get(f"/api/v10/channels/{cid}", headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "news"


def test_modify_channel(client):
    _make_guild(client)
    cid = _make_channel(client, "g1", "old-name")
    r = client.patch(f"/api/v10/channels/{cid}", headers=H, json={"name": "new-name"})
    assert r.status_code == 200
    assert r.json()["name"] == "new-name"


def test_delete_channel(client):
    _make_guild(client)
    cid = _make_channel(client, "g1", "temp")
    r = client.delete(f"/api/v10/channels/{cid}", headers=H)
    assert r.status_code == 200
    assert cid not in dc.STATE["channels"]


# --- messages ---------------------------------------------------------------

def _make_message(client, channel_id: str, text: str = "hello") -> dict:
    return client.post(
        f"/api/v10/channels/{channel_id}/messages", headers=H, json={"content": text}
    ).json()


def test_send_message(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    r = client.post(f"/api/v10/channels/{cid}/messages", headers=H, json={"content": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "hi"
    assert "id" in body


def test_list_messages(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    _make_message(client, cid, "msg1")
    _make_message(client, cid, "msg2")
    r = client.get(f"/api/v10/channels/{cid}/messages", headers=H)
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_edit_message(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    msg = _make_message(client, cid, "original")
    r = client.patch(
        f"/api/v10/channels/{cid}/messages/{msg['id']}", headers=H,
        json={"content": "edited"}
    )
    assert r.status_code == 200
    assert r.json()["content"] == "edited"


def test_delete_message(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    msg = _make_message(client, cid, "gone")
    r = client.delete(f"/api/v10/channels/{cid}/messages/{msg['id']}", headers=H)
    assert r.status_code == 204
    msgs = dc.STATE["messages"].get(cid, [])
    assert not any(m["id"] == msg["id"] for m in msgs)


def test_bulk_delete_messages(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    ids = [_make_message(client, cid, f"m{i}")["id"] for i in range(3)]
    r = client.post(
        f"/api/v10/channels/{cid}/messages/bulk-delete", headers=H,
        json={"messages": ids[:2]},
    )
    assert r.status_code == 204
    remaining = dc.STATE["messages"].get(cid, [])
    assert len(remaining) == 1


# --- reactions --------------------------------------------------------------

def test_add_reaction(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    msg = _make_message(client, cid, "react me")
    r = client.put(
        f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/%F0%9F%91%8D/@me",
        headers=H,
    )
    assert r.status_code == 204


def test_get_reactions(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    msg = _make_message(client, cid, "react")
    client.put(
        f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/%F0%9F%91%8D/@me",
        headers=H,
    )
    r = client.get(
        f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/%F0%9F%91%8D",
        headers=H,
    )
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_remove_reaction(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    msg = _make_message(client, cid, "react")
    emoji = "%F0%9F%91%8D"
    client.put(f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/{emoji}/@me", headers=H)
    r = client.delete(
        f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/{emoji}/@me", headers=H
    )
    assert r.status_code == 204


# --- pins -------------------------------------------------------------------

def test_pin_and_list_pins(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    msg = _make_message(client, cid, "important")
    r = client.put(f"/api/v10/channels/{cid}/pins/{msg['id']}", headers=H)
    assert r.status_code == 204
    pins = client.get(f"/api/v10/channels/{cid}/pins", headers=H).json()
    assert any(p["id"] == msg["id"] for p in pins)


def test_unpin_message(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    msg = _make_message(client, cid, "unpin me")
    client.put(f"/api/v10/channels/{cid}/pins/{msg['id']}", headers=H)
    r = client.delete(f"/api/v10/channels/{cid}/pins/{msg['id']}", headers=H)
    assert r.status_code == 204


# --- roles ------------------------------------------------------------------

def test_list_roles(client):
    _make_guild(client)
    r = client.get("/api/v10/guilds/g1/roles", headers=H)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_create_role(client):
    _make_guild(client)
    r = client.post("/api/v10/guilds/g1/roles", headers=H, json={
        "name": "Moderator", "permissions": "8"
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Moderator"


def test_modify_role(client):
    _make_guild(client)
    role = client.post("/api/v10/guilds/g1/roles", headers=H, json={"name": "Admin"}).json()
    r = client.patch(f"/api/v10/guilds/g1/roles/{role['id']}", headers=H, json={"name": "SuperAdmin"})
    assert r.status_code == 200
    assert r.json()["name"] == "SuperAdmin"


def test_delete_role(client):
    _make_guild(client)
    role = client.post("/api/v10/guilds/g1/roles", headers=H, json={"name": "Temp"}).json()
    r = client.delete(f"/api/v10/guilds/g1/roles/{role['id']}", headers=H)
    assert r.status_code == 204


# --- members ----------------------------------------------------------------

def test_list_members(client):
    _make_guild(client)
    dc.STATE["members"]["g1"] = {
        "u1": {"user": {"id": "u1", "username": "alice"}, "roles": []},
        "u2": {"user": {"id": "u2", "username": "bob"}, "roles": []},
    }
    r = client.get("/api/v10/guilds/g1/members", headers=H)
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_assign_and_remove_role(client):
    _make_guild(client)
    dc.STATE["members"]["g1"] = {
        "u1": {"user": {"id": "u1", "username": "alice"}, "roles": []},
    }
    role = client.post("/api/v10/guilds/g1/roles", headers=H, json={"name": "Mod"}).json()
    r = client.put(f"/api/v10/guilds/g1/members/u1/roles/{role['id']}", headers=H)
    assert r.status_code == 204
    assert role["id"] in dc.STATE["members"]["g1"]["u1"]["roles"]

    r2 = client.delete(f"/api/v10/guilds/g1/members/u1/roles/{role['id']}", headers=H)
    assert r2.status_code == 204
    assert role["id"] not in dc.STATE["members"]["g1"]["u1"]["roles"]


# --- webhooks ---------------------------------------------------------------

def test_create_webhook(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    r = client.post(f"/api/v10/channels/{cid}/webhooks", headers=H, json={
        "name": "alerts-hook"
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "alerts-hook"
    assert "token" in body


def test_execute_webhook(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    wh = client.post(f"/api/v10/channels/{cid}/webhooks", headers=H, json={"name": "hook"}).json()
    r = client.post(
        f"/api/v10/webhooks/{wh['id']}/{wh['token']}",
        headers=H,
        json={"content": "webhook message"},
    )
    assert r.status_code in (200, 204)


def test_list_channel_webhooks(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    client.post(f"/api/v10/channels/{cid}/webhooks", headers=H, json={"name": "h1"})
    client.post(f"/api/v10/channels/{cid}/webhooks", headers=H, json={"name": "h2"})
    r = client.get(f"/api/v10/channels/{cid}/webhooks", headers=H)
    assert len(r.json()) == 2


def test_delete_webhook(client):
    _make_guild(client)
    cid = _make_channel(client, "g1")
    wh = client.post(f"/api/v10/channels/{cid}/webhooks", headers=H, json={"name": "del"}).json()
    r = client.delete(f"/api/v10/webhooks/{wh['id']}", headers=H)
    assert r.status_code == 204
    assert wh["id"] not in dc.STATE["webhooks"]


# --- seeds ------------------------------------------------------------------

def test_seed_small_server(client):
    r = client.post("/_seed/small-server")
    assert r.status_code == 200
    state = client.get("/_state").json()
    assert state["guilds"]
    assert state["channels"]


def test_seed_incident_response(client):
    r = client.post("/_seed/incident-response")
    assert r.status_code == 200
    state = client.get("/_state").json()
    assert state["guilds"]
    assert state["webhooks"]


def test_seed_unknown_returns_404(client):
    assert client.post("/_seed/nope").status_code == 404


def test_seed_empty(client):
    client.post("/_seed/small-server")
    client.post("/_seed/empty")
    state = client.get("/_state").json()
    assert not state["guilds"]
