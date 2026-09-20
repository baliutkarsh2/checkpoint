"""Discord twin REST surface — auth, guilds, channels, messages, reactions, roles, webhooks."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import discord as dc


@pytest.fixture(autouse=True)
def _reset_state():
    dc.TWIN.reset()
    yield


@pytest.fixture
def client():
    return TestClient(dc.app)


TOKEN = dc.DEFAULT_BOOTSTRAP_TOKEN
# Discord auth uses "Bot <token>" — strip prefix for header construction
_RAW = TOKEN[4:] if TOKEN.startswith("Bot ") else TOKEN
H = {"Authorization": f"Bot {_RAW}"}

# Ids are snowflakes everywhere: SDKs parse them as 64-bit integers.
GUILD = "1214135245209600000"
USER = "1117518613708800000"


# --- auth -------------------------------------------------------------------

def test_missing_token_returns_401(client):
    r = client.get("/api/v10/users/@me")
    assert r.status_code == 401
    assert r.json() == {"code": 0, "message": "401: Unauthorized"}


def test_wrong_token_returns_401_under_strict_auth(client):
    client.post("/_config", json={"strict_auth": True})
    r = client.get("/api/v10/users/@me", headers={"Authorization": "Bot wrongtoken"})
    assert r.status_code == 401


def test_strict_auth_accepts_the_token_every_client_shape_sends(client):
    client.post("/_config", json={"strict_auth": True})
    # discord.py prefixes "Bot", the sandbox proxy rewrites to "Bearer", and an
    # agent may hand the SDK a token that already carries the scheme.
    for header in (f"Bot {_RAW}", f"Bearer {_RAW}", f"Bot Bot {_RAW}", f"Bearer Bot {_RAW}"):
        assert client.get("/api/v10/users/@me",
                          headers={"Authorization": header}).status_code == 200


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


def test_unknown_route_uses_discords_error_envelope(client):
    r = client.get("/api/v10/guilds/123/audit-logs", headers=H)
    assert r.status_code == 404
    assert r.json() == {"code": 0, "message": "404: Not Found"}


def test_every_route_is_served_under_each_api_version(client):
    client.post("/_seed/small-server")
    for prefix in ("/api/v10", "/api/v9", "/api"):
        assert client.get(f"{prefix}/users/@me", headers=H).status_code == 200


# --- bot user / application --------------------------------------------------

def test_get_current_user(client):
    r = client.get("/api/v10/users/@me", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["id"].isdigit() and body["bot"] is True
    assert body["username"] == "checkpoint-bot"


def test_application_info_matches_the_bot(client):
    """discord.py's login() fetches the application right after the user."""
    app = client.get("/api/v10/oauth2/applications/@me", headers=H).json()
    assert app["id"] == client.get("/api/v10/users/@me", headers=H).json()["id"]
    assert app["bot_public"] is False and len(app["verify_key"]) == 64


def test_get_user_and_unknown_user(client):
    client.post("/_seed/small-server")
    assert client.get(f"/api/v10/users/{USER}", headers=H).json()["username"] == "alice"
    r = client.get("/api/v10/users/1214135245209600123", headers=H)
    assert (r.status_code, r.json()["code"]) == (404, 10013)


# --- guild ------------------------------------------------------------------

def _make_guild(client, name: str = "Test Server") -> str:
    return client.post("/api/v10/guilds", headers=H, json={"name": name}).json()["id"]


def test_create_guild_seeds_everyone_role_and_bot_membership(client):
    guild = client.post("/api/v10/guilds", headers=H, json={"name": "Test Server"})
    assert guild.status_code == 201
    guild_id = guild.json()["id"]
    assert guild_id.isdigit()
    roles = client.get(f"/api/v10/guilds/{guild_id}/roles", headers=H).json()
    assert [r["name"] for r in roles] == ["@everyone"]
    members = client.get(f"/api/v10/guilds/{guild_id}/members",
                         headers=H, params={"limit": 100}).json()
    assert [m["user"]["username"] for m in members] == ["checkpoint-bot"]


def test_get_guild(client):
    guild_id = _make_guild(client)
    r = client.get(f"/api/v10/guilds/{guild_id}", headers=H, params={"with_counts": "true"})
    assert r.status_code == 200
    assert r.json()["name"] == "Test Server"
    assert r.json()["approximate_member_count"] == 1


def test_get_guild_not_found(client):
    r = client.get("/api/v10/guilds/1214135245209600123", headers=H)
    assert (r.status_code, r.json()["code"]) == (404, 10004)


def test_list_current_user_guilds(client):
    client.post("/_seed/small-server")
    guilds = client.get("/api/v10/users/@me/guilds", headers=H).json()
    assert [g["name"] for g in guilds] == ["Acme Engineering"]
    assert guilds[0]["permissions"]


# --- channels ---------------------------------------------------------------

def _make_channel(client, guild_id: str, name: str = "general") -> str:
    r = client.post(f"/api/v10/guilds/{guild_id}/channels", headers=H, json={
        "name": name, "type": 0
    })
    return r.json()["id"]


def test_create_channel(client):
    guild_id = _make_guild(client)
    r = client.post(f"/api/v10/guilds/{guild_id}/channels", headers=H,
                    json={"name": "general", "type": 0})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "general"
    assert body["id"].isdigit()


def test_create_channel_without_a_name_is_rejected(client):
    guild_id = _make_guild(client)
    r = client.post(f"/api/v10/guilds/{guild_id}/channels", headers=H, json={"type": 0})
    assert (r.status_code, r.json()["code"]) == (400, 50035)


def test_list_guild_channels(client):
    guild_id = _make_guild(client)
    _make_channel(client, guild_id, "alpha")
    _make_channel(client, guild_id, "beta")
    r = client.get(f"/api/v10/guilds/{guild_id}/channels", headers=H)
    assert r.status_code == 200
    names = [c["name"] for c in r.json()]
    assert "alpha" in names and "beta" in names


def test_get_channel(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id, "news")
    r = client.get(f"/api/v10/channels/{cid}", headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "news"


def test_modify_channel(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id, "old-name")
    r = client.patch(f"/api/v10/channels/{cid}", headers=H, json={"name": "new-name"})
    assert r.status_code == 200
    assert r.json()["name"] == "new-name"


def test_delete_channel(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id, "temp")
    r = client.delete(f"/api/v10/channels/{cid}", headers=H)
    assert r.status_code == 200
    assert cid not in dc.STATE["channels"]


def test_typing_indicator(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    assert client.post(f"/api/v10/channels/{cid}/typing", headers=H).status_code == 204


# --- messages ---------------------------------------------------------------

def _make_message(client, channel_id: str, text: str = "hello") -> dict:
    return client.post(
        f"/api/v10/channels/{channel_id}/messages", headers=H, json={"content": text}
    ).json()


def test_send_message(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    r = client.post(f"/api/v10/channels/{cid}/messages", headers=H, json={"content": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "hi"
    assert body["id"].isdigit()
    assert body["author"]["bot"] is True
    # A plain message carries no reference at all; a null one breaks SDK parsing.
    assert "message_reference" not in body
    assert dc.STATE["channels"][cid]["last_message_id"] == body["id"]


def test_send_empty_message_is_rejected(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    r = client.post(f"/api/v10/channels/{cid}/messages", headers=H, json={})
    assert (r.status_code, r.json()["code"]) == (400, 50006)


def test_reply_carries_the_reference_and_resolves_it(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    original = _make_message(client, cid, "deploy?")
    reply = client.post(f"/api/v10/channels/{cid}/messages", headers=H, json={
        "content": "yes", "message_reference": {"message_id": original["id"]},
    }).json()
    assert reply["type"] == 19
    assert reply["message_reference"]["message_id"] == original["id"]
    assert reply["referenced_message"]["content"] == "deploy?"


def test_list_messages(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    _make_message(client, cid, "msg1")
    _make_message(client, cid, "msg2")
    r = client.get(f"/api/v10/channels/{cid}/messages", headers=H)
    assert r.status_code == 200
    assert [m["content"] for m in r.json()] == ["msg2", "msg1"]  # newest first


def test_get_single_message(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "find me")
    r = client.get(f"/api/v10/channels/{cid}/messages/{msg['id']}", headers=H)
    assert r.status_code == 200 and r.json()["content"] == "find me"
    missing = client.get(f"/api/v10/channels/{cid}/messages/1214135245209600123", headers=H)
    assert (missing.status_code, missing.json()["code"]) == (404, 10008)


def test_edit_message(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "original")
    r = client.patch(
        f"/api/v10/channels/{cid}/messages/{msg['id']}", headers=H,
        json={"content": "edited"}
    )
    assert r.status_code == 200
    assert r.json()["content"] == "edited"
    assert r.json()["edited_timestamp"]


def test_delete_message(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "gone")
    r = client.delete(f"/api/v10/channels/{cid}/messages/{msg['id']}", headers=H)
    assert r.status_code == 204
    msgs = dc.STATE["messages"].get(cid, [])
    assert not any(m["id"] == msg["id"] for m in msgs)


def test_bulk_delete_messages(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    ids = [_make_message(client, cid, f"m{i}")["id"] for i in range(3)]
    r = client.post(
        f"/api/v10/channels/{cid}/messages/bulk-delete", headers=H,
        json={"messages": ids[:2]},
    )
    assert r.status_code == 204
    remaining = dc.STATE["messages"].get(cid, [])
    assert len(remaining) == 1


def test_bulk_delete_needs_at_least_two_messages(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "only one")
    r = client.post(f"/api/v10/channels/{cid}/messages/bulk-delete", headers=H,
                    json={"messages": [msg["id"]]})
    assert (r.status_code, r.json()["code"]) == (400, 50016)


# --- reactions --------------------------------------------------------------

THUMBS_UP = "%F0%9F%91%8D"


def test_add_reaction_is_idempotent(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "react me")
    url = f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/{THUMBS_UP}/@me"
    assert client.put(url, headers=H).status_code == 204
    assert client.put(url, headers=H).status_code == 204  # same user, same emoji
    reactions = client.get(f"/api/v10/channels/{cid}/messages/{msg['id']}",
                           headers=H).json()["reactions"]
    assert reactions[0]["count"] == 1 and reactions[0]["me"] is True


def test_get_reactions(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "react")
    client.put(f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/{THUMBS_UP}/@me",
               headers=H)
    r = client.get(f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/{THUMBS_UP}",
                   headers=H)
    assert r.status_code == 200
    assert [u["id"] for u in r.json()] == [dc._bot_user_id()]


def test_remove_reaction(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "react")
    url = f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/{THUMBS_UP}/@me"
    client.put(url, headers=H)
    assert client.delete(url, headers=H).status_code == 204
    assert client.get(f"/api/v10/channels/{cid}/messages/{msg['id']}",
                      headers=H).json()["reactions"] == []


def test_clear_all_reactions(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "react")
    client.put(f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions/{THUMBS_UP}/@me",
               headers=H)
    r = client.delete(f"/api/v10/channels/{cid}/messages/{msg['id']}/reactions", headers=H)
    assert r.status_code == 204
    assert client.get(f"/api/v10/channels/{cid}/messages/{msg['id']}",
                      headers=H).json()["reactions"] == []


# --- pins -------------------------------------------------------------------

def test_pin_and_list_pins(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "important")
    r = client.put(f"/api/v10/channels/{cid}/pins/{msg['id']}", headers=H)
    assert r.status_code == 204
    pins = client.get(f"/api/v10/channels/{cid}/pins", headers=H).json()
    assert any(p["id"] == msg["id"] for p in pins)


def test_unpin_message(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "unpin me")
    client.put(f"/api/v10/channels/{cid}/pins/{msg['id']}", headers=H)
    r = client.delete(f"/api/v10/channels/{cid}/pins/{msg['id']}", headers=H)
    assert r.status_code == 204
    assert client.get(f"/api/v10/channels/{cid}/pins", headers=H).json() == []


def test_message_pins_endpoint_returns_items_and_cursor(client):
    """The current pins route SDKs use: {"items": [{pinned_at, message}], "has_more"}."""
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    msg = _make_message(client, cid, "runbook")
    assert client.put(f"/api/v10/channels/{cid}/messages/pins/{msg['id']}",
                      headers=H).status_code == 204
    pins = client.get(f"/api/v10/channels/{cid}/messages/pins", headers=H).json()
    assert pins["has_more"] is False
    assert pins["items"][0]["message"]["id"] == msg["id"]
    assert pins["items"][0]["pinned_at"]


# --- roles ------------------------------------------------------------------

def test_list_roles(client):
    guild_id = _make_guild(client)
    r = client.get(f"/api/v10/guilds/{guild_id}/roles", headers=H)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_create_role(client):
    guild_id = _make_guild(client)
    r = client.post(f"/api/v10/guilds/{guild_id}/roles", headers=H, json={
        "name": "Moderator", "permissions": "8", "color": 16711680,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Moderator"
    assert body["color"] == body["colors"]["primary_color"] == 16711680


def test_modify_role(client):
    guild_id = _make_guild(client)
    role = client.post(f"/api/v10/guilds/{guild_id}/roles",
                       headers=H, json={"name": "Admin"}).json()
    r = client.patch(f"/api/v10/guilds/{guild_id}/roles/{role['id']}",
                     headers=H, json={"name": "SuperAdmin"})
    assert r.status_code == 200
    assert r.json()["name"] == "SuperAdmin"


def test_delete_role(client):
    guild_id = _make_guild(client)
    role = client.post(f"/api/v10/guilds/{guild_id}/roles",
                       headers=H, json={"name": "Temp"}).json()
    r = client.delete(f"/api/v10/guilds/{guild_id}/roles/{role['id']}", headers=H)
    assert r.status_code == 204
    assert client.get(f"/api/v10/guilds/{guild_id}/roles/{role['id']}",
                      headers=H).json()["code"] == 10011


# --- members ----------------------------------------------------------------

def test_list_members(client):
    client.post("/_seed/small-server")
    r = client.get(f"/api/v10/guilds/{GUILD}/members", headers=H, params={"limit": 100})
    assert r.status_code == 200
    assert {m["user"]["username"] for m in r.json()} >= {"alice", "bob", "carol"}


def test_list_members_defaults_to_one_like_the_real_api(client):
    client.post("/_seed/small-server")
    assert len(client.get(f"/api/v10/guilds/{GUILD}/members", headers=H).json()) == 1


def test_get_member_fills_the_fields_sdks_require(client):
    client.post("/_seed/small-server")
    member = client.get(f"/api/v10/guilds/{GUILD}/members/{USER}", headers=H).json()
    assert member["user"]["username"] == "alice"
    assert member["flags"] == 0 and member["roles"] and member["joined_at"]


def test_modify_and_kick_member(client):
    client.post("/_seed/small-server")
    renamed = client.patch(f"/api/v10/guilds/{GUILD}/members/{USER}", headers=H,
                           json={"nick": "alice-oncall"})
    assert renamed.json()["nick"] == "alice-oncall"
    assert client.delete(f"/api/v10/guilds/{GUILD}/members/{USER}", headers=H).status_code == 204
    gone = client.get(f"/api/v10/guilds/{GUILD}/members/{USER}", headers=H)
    assert (gone.status_code, gone.json()["code"]) == (404, 10007)


def test_assign_and_remove_role(client):
    client.post("/_seed/small-server")
    role = client.post(f"/api/v10/guilds/{GUILD}/roles", headers=H, json={"name": "Mod"}).json()
    r = client.put(f"/api/v10/guilds/{GUILD}/members/{USER}/roles/{role['id']}", headers=H)
    assert r.status_code == 204
    assert role["id"] in dc.STATE["members"][GUILD][USER]["roles"]

    r2 = client.delete(f"/api/v10/guilds/{GUILD}/members/{USER}/roles/{role['id']}", headers=H)
    assert r2.status_code == 204
    assert role["id"] not in dc.STATE["members"][GUILD][USER]["roles"]


# --- webhooks ---------------------------------------------------------------

def test_create_webhook(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    r = client.post(f"/api/v10/channels/{cid}/webhooks", headers=H, json={
        "name": "alerts-hook"
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "alerts-hook"
    # SDKs only accept webhook URLs whose token is at least 60 characters.
    assert len(body["token"]) >= 60
    assert body["url"] == f"https://discord.com/api/webhooks/{body['id']}/{body['token']}"


def test_execute_webhook(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    wh = client.post(f"/api/v10/channels/{cid}/webhooks",
                     headers=H, json={"name": "hook"}).json()
    r = client.post(f"/api/v10/webhooks/{wh['id']}/{wh['token']}",
                    json={"content": "webhook message"})
    assert r.status_code == 204  # no ?wait=true: acknowledged, no body
    waited = client.post(f"/api/v10/webhooks/{wh['id']}/{wh['token']}",
                         params={"wait": "true"}, json={"content": "with wait"})
    assert waited.status_code == 200
    assert waited.json()["content"] == "with wait"
    assert waited.json()["webhook_id"] == wh["id"]


def test_execute_webhook_on_the_unversioned_url_without_a_bot_token(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    wh = client.post(f"/api/v10/channels/{cid}/webhooks",
                     headers=H, json={"name": "hook"}).json()
    path = wh["url"].split("discord.com", 1)[1]
    r = client.post(path, params={"wait": "true"}, json={"content": "from the url"})
    assert r.status_code == 200 and r.json()["content"] == "from the url"


def test_unknown_webhook_token_is_rejected(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    wh = client.post(f"/api/v10/channels/{cid}/webhooks",
                     headers=H, json={"name": "hook"}).json()
    r = client.post(f"/api/v10/webhooks/{wh['id']}/wrong-token", json={"content": "nope"})
    assert (r.status_code, r.json()["code"]) == (404, 10015)


def test_list_channel_webhooks(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    client.post(f"/api/v10/channels/{cid}/webhooks", headers=H, json={"name": "h1"})
    client.post(f"/api/v10/channels/{cid}/webhooks", headers=H, json={"name": "h2"})
    r = client.get(f"/api/v10/channels/{cid}/webhooks", headers=H)
    assert len(r.json()) == 2


def test_delete_webhook(client):
    guild_id = _make_guild(client)
    cid = _make_channel(client, guild_id)
    wh = client.post(f"/api/v10/channels/{cid}/webhooks",
                     headers=H, json={"name": "del"}).json()
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
