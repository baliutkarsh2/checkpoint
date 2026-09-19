"""Discord twin: threads, DMs, moderation, uploads, pagination, views and classification."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import discord as dc

TOKEN = dc.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": TOKEN}

GUILD = "1214135245209600000"
GENERAL = "1214135496867840000"
ENGINEERING = "1214135748526080000"
ALICE = "1117518613708800000"
BOB = "1125019287552000000"


@pytest.fixture(autouse=True)
def _reset_state():
    dc.TWIN.reset()
    yield


@pytest.fixture
def client():
    client = TestClient(dc.app)
    client.post("/_seed/small-server")
    return client


def _send(client, channel_id: str, content: str) -> dict:
    return client.post(f"/api/v10/channels/{channel_id}/messages", headers=H,
                       json={"content": content}).json()


# --- ids ---------------------------------------------------------------------

def test_seeded_ids_are_snowflakes_and_counters_continue_after_them(client):
    state = client.get("/_state").json()
    ids = [*state["guilds"], *state["channels"], *state["users"],
           *state["roles"][GUILD]]
    assert all(i.isdigit() and len(i) >= 17 for i in ids), ids
    new_id = _send(client, GENERAL, "first")["id"]
    assert int(new_id) > max(int(i) for i in ids)


def test_generated_ids_are_monotonic_and_carry_their_timestamp(client):
    ids = [_send(client, GENERAL, f"m{i}")["id"] for i in range(3)]
    assert ids == sorted(ids, key=int)
    message = client.get(f"/api/v10/channels/{GENERAL}/messages/{ids[0]}", headers=H).json()
    # discord.py dates a message from its id; the timestamp has to agree.
    assert message["timestamp"] == dc._snowflake_time(ids[0])


def test_seed_makes_the_bot_a_member_of_every_guild(client):
    members = client.get(f"/api/v10/guilds/{GUILD}/members",
                         headers=H, params={"limit": 100}).json()
    bot_id = client.get("/api/v10/users/@me", headers=H).json()["id"]
    assert bot_id in [m["user"]["id"] for m in members]
    roles = client.get(f"/api/v10/guilds/{GUILD}/roles", headers=H).json()
    everyone = next(r for r in roles if r["name"] == "@everyone")
    assert everyone["id"] == GUILD  # @everyone always shares the guild id


# --- pagination --------------------------------------------------------------

def test_message_pagination_compares_snowflakes_numerically(client):
    ids = [_send(client, GENERAL, f"m{i}")["id"] for i in range(5)]

    def listed(**params):
        return [m["id"] for m in client.get(f"/api/v10/channels/{GENERAL}/messages",
                                            headers=H, params=params).json()]

    assert listed(limit=2) == ids[::-1][:2]
    assert listed(before=ids[2], limit=10) == ids[:2][::-1]
    assert listed(after=ids[2], limit=10) == ids[3:][::-1]
    assert listed(around=ids[2], limit=3) == ids[1:4][::-1]


def test_message_pagination_orders_by_value_not_string(client):
    """"9" sorts after "10" as text; snowflakes have to compare as integers."""
    client.post("/_seed-file", json={"state": {
        "guilds": {"1": {"id": "1", "name": "g"}},
        "channels": {"10": {"id": "10", "guild_id": "1", "name": "c", "type": 0}},
        "messages": {"10": [{"id": str(i), "channel_id": "10", "content": f"m{i}",
                             "timestamp": "2025-01-01T00:00:00+00:00", "type": 0,
                             "author": {"id": "2", "username": "u"}, "reactions": []}
                            for i in (5, 9, 10, 11, 100)]},
    }})
    listed = client.get("/api/v10/channels/10/messages", headers=H,
                        params={"before": "11", "limit": 10}).json()
    assert sorted((m["id"] for m in listed), key=int) == ["5", "9", "10"]


# --- uploads -----------------------------------------------------------------

def test_multipart_upload_becomes_an_attachment(client):
    response = client.post(
        f"/api/v10/channels/{GENERAL}/messages", headers=H,
        data={"payload_json": json.dumps(
            {"content": "logs", "attachments": [{"id": 0, "filename": "run.log",
                                                 "description": "the failing run"}]})},
        files={"files[0]": ("run.log", b"boom\n", "text/plain")},
    )
    assert response.status_code == 200
    attachment = response.json()["attachments"][0]
    assert (attachment["filename"], attachment["size"]) == ("run.log", 5)
    assert attachment["description"] == "the failing run"
    assert attachment["url"].endswith(f"/attachments/{GENERAL}/{attachment['id']}/run.log")


def test_uploaded_file_is_downloadable_from_the_cdn_path_without_auth(client):
    response = client.post(
        f"/api/v10/channels/{GENERAL}/messages", headers=H,
        data={"payload_json": json.dumps({"content": "report"})},
        files={"files[0]": ("report.csv", b"a,b\n1,2\n", "text/csv")},
    )
    url = response.json()["attachments"][0]["url"]
    downloaded = client.get(url.split("cdn.discordapp.com", 1)[1])
    assert downloaded.status_code == 200
    assert downloaded.content == b"a,b\n1,2\n"


# --- threads -----------------------------------------------------------------

def test_thread_from_message_shares_its_id_and_accepts_messages(client):
    message = _send(client, ENGINEERING, "RFC: drop Python 3.10")
    thread = client.post(
        f"/api/v10/channels/{ENGINEERING}/messages/{message['id']}/threads",
        headers=H, json={"name": "rfc-310", "auto_archive_duration": 1440})
    assert thread.status_code == 201
    body = thread.json()
    assert body["id"] == message["id"] and body["parent_id"] == ENGINEERING
    assert body["thread_metadata"]["archived"] is False

    _send(client, body["id"], "+1")
    assert client.get(f"/api/v10/channels/{body['id']}",
                      headers=H).json()["message_count"] == 1
    # The parent message advertises the thread, as the real API does.
    refetched = client.get(f"/api/v10/channels/{ENGINEERING}/messages/{message['id']}",
                           headers=H).json()
    assert refetched["thread"]["name"] == "rfc-310"


def test_standalone_thread_join_leave_and_active_listing(client):
    thread = client.post(f"/api/v10/channels/{GENERAL}/threads", headers=H,
                         json={"name": "standup", "type": 11}).json()
    assert client.put(f"/api/v10/channels/{thread['id']}/thread-members/@me",
                      headers=H).status_code == 204
    active = client.get(f"/api/v10/guilds/{GUILD}/threads/active", headers=H).json()
    assert [t["name"] for t in active["threads"]] == ["standup"]
    assert client.delete(f"/api/v10/channels/{thread['id']}/thread-members/@me",
                         headers=H).status_code == 204

    client.patch(f"/api/v10/channels/{thread['id']}", headers=H, json={"archived": True})
    assert client.get(f"/api/v10/guilds/{GUILD}/threads/active",
                      headers=H).json()["threads"] == []
    archived = client.get(f"/api/v10/channels/{GENERAL}/threads/archived/public",
                          headers=H).json()
    assert [t["name"] for t in archived["threads"]] == ["standup"]


def test_threads_are_not_listed_as_guild_channels(client):
    client.post(f"/api/v10/channels/{GENERAL}/threads", headers=H,
                json={"name": "side-quest", "type": 11})
    names = [c["name"] for c in client.get(f"/api/v10/guilds/{GUILD}/channels",
                                           headers=H).json()]
    assert "side-quest" not in names


# --- direct messages ---------------------------------------------------------

def test_open_dm_is_reused_and_accepts_messages(client):
    first = client.post("/api/v10/users/@me/channels", headers=H,
                        json={"recipient_id": BOB}).json()
    again = client.post("/api/v10/users/@me/channels", headers=H,
                        json={"recipient_id": BOB}).json()
    assert first["id"] == again["id"] and first["type"] == 1
    assert [r["username"] for r in first["recipients"]] == ["bob"]
    _send(client, first["id"], "shift starts tomorrow")
    assert dc.STATE["messages"][first["id"]][0]["content"] == "shift starts tomorrow"


# --- moderation --------------------------------------------------------------

def test_ban_removes_the_member_and_is_listed_then_lifted(client):
    assert client.put(f"/api/v10/guilds/{GUILD}/bans/{BOB}", headers=H,
                      json={"delete_message_seconds": 0},
                      ).status_code == 204
    assert client.get(f"/api/v10/guilds/{GUILD}/members/{BOB}",
                      headers=H).status_code == 404
    bans = client.get(f"/api/v10/guilds/{GUILD}/bans", headers=H).json()
    assert [b["user"]["username"] for b in bans] == ["bob"]
    assert client.get(f"/api/v10/guilds/{GUILD}/bans/{BOB}", headers=H).status_code == 200
    assert client.delete(f"/api/v10/guilds/{GUILD}/bans/{BOB}", headers=H).status_code == 204
    missing = client.get(f"/api/v10/guilds/{GUILD}/bans/{BOB}", headers=H)
    assert (missing.status_code, missing.json()["code"]) == (404, 10026)


def test_ban_records_the_audit_log_reason(client):
    client.put(f"/api/v10/guilds/{GUILD}/bans/{BOB}", headers={
        **H, "X-Audit-Log-Reason": "spam"}, json={})
    assert client.get(f"/api/v10/guilds/{GUILD}/bans/{BOB}",
                      headers=H).json()["reason"] == "spam"


def test_member_search_matches_username_and_nick(client):
    found = client.get(f"/api/v10/guilds/{GUILD}/members/search", headers=H,
                       params={"query": "ali", "limit": 10}).json()
    assert [m["user"]["username"] for m in found] == ["alice"]
    by_nick = client.get(f"/api/v10/guilds/{GUILD}/members/search", headers=H,
                         params={"query": "carol", "limit": 10}).json()
    assert [m["nick"] for m in by_nick] == ["Carol"]


def test_deleting_a_role_detaches_it_from_members(client):
    role = client.post(f"/api/v10/guilds/{GUILD}/roles", headers=H,
                       json={"name": "temp"}).json()
    client.put(f"/api/v10/guilds/{GUILD}/members/{ALICE}/roles/{role['id']}", headers=H)
    client.delete(f"/api/v10/guilds/{GUILD}/roles/{role['id']}", headers=H)
    member = client.get(f"/api/v10/guilds/{GUILD}/members/{ALICE}", headers=H).json()
    assert role["id"] not in member["roles"]


def test_channel_permission_overwrites(client):
    assert client.put(f"/api/v10/channels/{GENERAL}/permissions/{ALICE}", headers=H,
                      json={"type": 1, "allow": "2048", "deny": "0"}).status_code == 204
    overwrites = client.get(f"/api/v10/channels/{GENERAL}", headers=H
                            ).json()["permission_overwrites"]
    assert overwrites == [{"id": ALICE, "type": 1, "allow": "2048", "deny": "0"}]
    assert client.delete(f"/api/v10/channels/{GENERAL}/permissions/{ALICE}",
                         headers=H).status_code == 204
    assert client.get(f"/api/v10/channels/{GENERAL}",
                      headers=H).json()["permission_overwrites"] == []


# --- slash commands ----------------------------------------------------------

def test_bulk_overwriting_slash_commands_replaces_the_set(client):
    application = client.get("/api/v10/oauth2/applications/@me", headers=H).json()["id"]
    first = client.put(f"/api/v10/applications/{application}/commands", headers=H, json=[
        {"name": "status", "description": "Report status"},
        {"name": "page", "description": "Page the on-call"},
    ]).json()
    assert sorted(c["name"] for c in first) == ["page", "status"]
    second = client.put(f"/api/v10/applications/{application}/commands", headers=H,
                        json=[{"name": "status", "description": "Report status"}]).json()
    assert [c["name"] for c in second] == ["status"]
    assert [c["name"] for c in client.get(
        f"/api/v10/applications/{application}/commands", headers=H).json()] == ["status"]


# --- views and classification ------------------------------------------------

def test_views_denormalize_every_collection(client):
    message = _send(client, GENERAL, "hello world")
    client.put(f"/api/v10/channels/{GENERAL}/messages/pins/{message['id']}", headers=H)
    client.post(f"/api/v10/channels/{GENERAL}/webhooks", headers=H, json={"name": "ci"})
    client.put(f"/api/v10/guilds/{GUILD}/bans/{BOB}", headers=H, json={})
    client.post(f"/api/v10/channels/{GENERAL}/threads", headers=H,
                json={"name": "triage", "type": 11})

    views = client.get("/_views").json()["collections"]
    assert set(views) == {"guilds", "channels", "threads", "messages", "members", "roles",
                          "bans", "webhooks", "users", "commands"}

    posted = next(m for m in views["messages"]["items"] if m["content"] == "hello world")
    assert posted["channel"] == "general" and posted["guild"] == "Acme Engineering"
    assert posted["author"] == "checkpoint-bot" and posted["pinned"] is True

    channels = {c["name"]: c for c in views["channels"]["items"]}
    assert channels["general"]["kind"] == "text" and channels["general"]["message_count"] == 1
    assert [t["parent"] for t in views["threads"]["items"]] == ["general"]

    members = {m["username"]: m for m in views["members"]["items"]}
    assert members["alice"]["roles"] == ["Admin"] and members["alice"]["display_name"] == "Alice"
    assert "bob" not in members  # the ban removed them

    assert [b["username"] for b in views["bans"]["items"]] == ["bob"]
    assert [w["channel"] for w in views["webhooks"]["items"]] == ["general"]
    assert {r["name"] for r in views["roles"]["items"]} >= {"@everyone", "Admin", "Member"}
    assert next(r for r in views["roles"]["items"] if r["name"] == "Admin")["member_count"] == 1
    assert views["messages"]["nouns"] == ["message", "messages"]


def test_views_key_and_tombstone_contract(client):
    views = client.get("/_views").json()["collections"]
    for name, view in views.items():
        assert view["key"] == "id", name
        assert view["tombstone"] is None, name  # Discord deletes for real
        assert all("id" in item for item in view["items"]), name


@pytest.mark.parametrize(("method", "path", "expected"), [
    ("POST", f"/api/v10/channels/{GENERAL}/messages", ("create", "messages")),
    ("POST", f"/api/v10/channels/{GENERAL}/messages/1/threads", ("create", "threads")),
    ("POST", f"/api/v10/channels/{GENERAL}/threads", ("create", "threads")),
    ("POST", f"/api/v10/channels/{GENERAL}/messages/bulk-delete", ("delete", "messages")),
    ("PUT", f"/api/v10/channels/{GENERAL}/messages/1/reactions/x/@me", ("create", "reactions")),
    ("DELETE", f"/api/v10/channels/{GENERAL}/messages/1/reactions/x/@me",
     ("delete", "reactions")),
    ("PUT", f"/api/v10/channels/{GENERAL}/messages/pins/1", ("create", "pins")),
    ("DELETE", f"/api/v10/channels/{GENERAL}/pins/1", ("delete", "pins")),
    ("PUT", f"/api/v10/guilds/{GUILD}/members/{ALICE}/roles/2", ("update", "members")),
    ("DELETE", f"/api/v10/guilds/{GUILD}/members/{ALICE}", ("delete", "members")),
    ("PUT", f"/api/v10/guilds/{GUILD}/bans/{ALICE}", ("create", "bans")),
    ("DELETE", f"/api/v10/guilds/{GUILD}/bans/{ALICE}", ("delete", "bans")),
    ("GET", f"/api/v10/guilds/{GUILD}/members/search", ("read", "members")),
    ("POST", "/api/v10/webhooks/1/token", ("create", "messages")),
    ("PATCH", "/api/v10/webhooks/1/token/messages/2", ("update", "messages")),
    ("POST", f"/api/v10/channels/{GENERAL}/typing", ("other", "typing")),
    ("POST", f"/api/v10/guilds/{GUILD}/channels", ("create", "channels")),
    ("GET", f"/api/v10/channels/{GENERAL}/messages", ("read", "messages")),
])
def test_classification_maps_routes_to_op_and_resource(method, path, expected):
    assert dc.TWIN.classify_call(method, path, None) == expected


def test_trace_classifies_a_bulk_delete_as_a_delete(client):
    ids = [_send(client, GENERAL, f"m{i}")["id"] for i in range(2)]
    client.post(f"/api/v10/channels/{GENERAL}/messages/bulk-delete", headers=H,
                json={"messages": ids})
    entry = client.get("/_trace").json()[-1]
    assert (entry["op"], entry["resource"], entry["ok"]) == ("delete", "messages", True)
