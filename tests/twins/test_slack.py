"""Slack twin: methods, wire shapes, error codes, auth and state."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import slack as sl


@pytest.fixture(autouse=True)
def _reset_state():
    sl.TWIN.reset()
    yield


@pytest.fixture
def client():
    return TestClient(sl.app)


TOKEN = sl.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}
BOT = sl.BOT_USER_ID


def call(client, method, **args):
    """A method call the way an SDK makes it: POST with a form body."""
    return client.post(f"/api/{method}", headers=H, data=args).json()


# --- auth gate ----------------------------------------------------------

def test_missing_token_returns_not_authed(client):
    r = client.post("/api/chat.postMessage", json={"channel": "C1", "text": "hi"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "not_authed"}


def test_any_token_accepted_by_default(client):
    r = client.get("/api/conversations.list", headers={"Authorization": "Bearer xoxb-CHECKPOINTFAKE-agents-own"})
    assert r.json()["ok"] is True


def test_token_accepted_as_form_field(client):
    r = client.post("/api/chat.postMessage", data={"token": "xoxb-CHECKPOINTFAKE-form", "channel": "C404", "text": "hi"})
    # Authenticated: the failure is about the channel, not the credential.
    assert r.json()["error"] not in ("not_authed", "invalid_auth")


def test_wrong_token_returns_invalid_auth_under_strict_auth(client):
    client.post("/_config", json={"strict_auth": True})
    r = client.get("/api/conversations.list", headers={"Authorization": "Bearer xoxb-CHECKPOINTFAKE-wrong"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "invalid_auth"}


def test_env_override(monkeypatch, client):
    monkeypatch.setenv("SLACK_BOOTSTRAP_TOKEN", "xoxb-CHECKPOINTFAKE-env-override")
    client.post("/_config", json={"strict_auth": True})
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


# --- transport: verbs, encodings, unknown methods ------------------------

def test_every_method_answers_get_and_post(client):
    _seed_channel()
    for send in (
        lambda: client.get("/api/conversations.history?channel=C0001", headers=H),
        lambda: client.post("/api/conversations.history", headers=H, data={"channel": "C0001"}),
        lambda: client.post("/api/conversations.history", headers=H, json={"channel": "C0001"}),
    ):
        response = send()
        assert response.status_code == 200
        assert response.json()["ok"] is True


def test_unknown_method_is_an_application_error(client):
    r = client.post("/api/chat.unfurl", headers=H, json={"channel": "C1"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "unknown_method"}


def test_unknown_path_is_not_a_fastapi_404(client):
    r = client.get("/nope", headers=H)
    assert r.json() == {"ok": False, "error": "unknown_method"}
    assert "detail" not in r.json()


def test_auth_test_reports_the_bot_identity(client):
    body = call(client, "auth.test")
    assert body["ok"] is True
    assert body["user_id"] == BOT
    assert body["bot_id"] == sl.BOT_ID
    assert body["team_id"] == sl.TEAM_ID
    assert body["url"].startswith("http")


# --- chat.postMessage ---------------------------------------------------

def _seed_channel(channel_id: str = "C0001", name: str = "general"):
    sl.STATE["channels"][channel_id] = {
        "id": channel_id, "name": name, "is_channel": True, "members": [BOT], "num_members": 3,
    }
    sl.STATE["messages"][channel_id] = []


def test_post_message_happy_path(client):
    _seed_channel()
    body = call(client, "chat.postMessage", channel="C0001", text="hello team")
    assert body["ok"] is True
    assert body["channel"] == "C0001"
    assert "ts" in body
    assert body["message"]["text"] == "hello team"
    # A bot token posts as the bot user, with the bot's identity attached.
    assert body["message"]["user"] == BOT
    assert body["message"]["bot_id"] == sl.BOT_ID
    assert len(sl.STATE["messages"]["C0001"]) == 1


def test_post_message_resolves_by_name(client):
    _seed_channel()
    assert call(client, "chat.postMessage", channel="general", text="ping")["ok"] is True
    assert call(client, "chat.postMessage", channel="#general", text="ping")["ok"] is True


def test_post_message_accepts_blocks_without_text(client):
    _seed_channel()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Deploy* done"}}]
    body = client.post("/api/chat.postMessage", headers=H,
                       json={"channel": "C0001", "blocks": blocks}).json()
    assert body["ok"] is True
    assert body["message"]["blocks"] == blocks


def test_post_message_without_text_or_blocks_is_no_text(client):
    _seed_channel()
    assert call(client, "chat.postMessage", channel="C0001") == {"ok": False, "error": "no_text"}


def test_post_message_missing_channel(client):
    assert call(client, "chat.postMessage", text="hi") == {"ok": False, "error": "channel_not_found"}


def test_post_message_channel_not_found(client):
    assert call(client, "chat.postMessage", channel="C_nope", text="x")["error"] == "channel_not_found"


def test_post_message_to_archived_channel(client):
    _seed_channel()
    sl.STATE["channels"]["C0001"]["is_archived"] = True
    assert call(client, "chat.postMessage", channel="C0001", text="x")["error"] == "is_archived"


def test_post_message_to_user_id_opens_a_dm(client):
    sl.STATE["users"]["U00000001"] = {"id": "U00000001", "name": "alice"}
    body = call(client, "chat.postMessage", channel="U00000001", text="hi")
    assert body["ok"] is True
    assert body["channel"].startswith("D")
    im = sl.STATE["channels"][body["channel"]]
    assert im["is_im"] is True and im["user"] == "U00000001"


def test_post_message_thread_reply_bumps_parent(client):
    _seed_channel()
    parent = call(client, "chat.postMessage", channel="C0001", text="parent")
    reply = call(client, "chat.postMessage", channel="C0001", text="reply", thread_ts=parent["ts"])
    assert reply["ok"] is True
    assert reply["message"]["thread_ts"] == parent["ts"]
    parent_state = sl._find_message("C0001", parent["ts"])
    assert parent_state["reply_count"] == 1
    assert parent_state["reply_users"] == [BOT]
    assert parent_state["latest_reply"] == reply["ts"]


def test_post_message_unknown_thread(client):
    _seed_channel()
    body = call(client, "chat.postMessage", channel="C0001", text="x", thread_ts="1.000001")
    assert body == {"ok": False, "error": "thread_not_found"}


# --- chat.update / chat.delete ------------------------------------------

def test_update_rewrites_the_message_in_state(client):
    _seed_channel()
    posted = call(client, "chat.postMessage", channel="C0001", text="before")
    body = call(client, "chat.update", channel="C0001", ts=posted["ts"], text="after")
    assert body["ok"] is True and body["text"] == "after"
    stored = sl._find_message("C0001", posted["ts"])
    assert stored["text"] == "after"
    assert stored["edited"]["user"] == BOT


def test_update_of_someone_elses_message_is_refused(client):
    _seed_channel()
    sl.STATE["messages"]["C0001"].append(
        {"type": "message", "user": "U00000009", "text": "human", "ts": "1714521600.000001"})
    body = call(client, "chat.update", channel="C0001", ts="1714521600.000001", text="nope")
    assert body == {"ok": False, "error": "cant_update_message"}


def test_delete_removes_the_message(client):
    _seed_channel()
    posted = call(client, "chat.postMessage", channel="C0001", text="oops")
    assert call(client, "chat.delete", channel="C0001", ts=posted["ts"])["ok"] is True
    assert sl.STATE["messages"]["C0001"] == []
    assert call(client, "chat.delete", channel="C0001", ts=posted["ts"])["error"] == "message_not_found"


def test_delete_of_a_thread_parent_leaves_a_tombstone(client):
    _seed_channel()
    parent = call(client, "chat.postMessage", channel="C0001", text="parent")
    call(client, "chat.postMessage", channel="C0001", text="reply", thread_ts=parent["ts"])
    assert call(client, "chat.delete", channel="C0001", ts=parent["ts"])["ok"] is True
    stored = sl._find_message("C0001", parent["ts"])
    assert stored["subtype"] == "tombstone"
    views = client.get("/_views").json()["collections"]["messages"]
    assert any(m["id"] == f"C0001:{parent['ts']}" and m["deleted"] for m in views["items"])


def test_get_permalink(client):
    _seed_channel()
    posted = call(client, "chat.postMessage", channel="C0001", text="x")
    body = call(client, "chat.getPermalink", channel="C0001", message_ts=posted["ts"])
    assert body["permalink"].endswith("/archives/C0001/p" + posted["ts"].replace(".", ""))


def test_post_ephemeral_is_not_in_history(client):
    _seed_channel()
    sl.STATE["users"]["U00000001"] = {"id": "U00000001", "name": "alice"}
    body = call(client, "chat.postEphemeral", channel="C0001", user="U00000001", text="psst")
    assert body["ok"] is True and "message_ts" in body
    assert sl.STATE["messages"]["C0001"] == []
    assert sl.STATE["ephemeral_messages"][0]["user"] == "U00000001"


def test_schedule_message_lifecycle(client):
    _seed_channel()
    import time

    post_at = int(time.time()) + 3600
    body = call(client, "chat.scheduleMessage", channel="C0001", post_at=post_at, text="later")
    assert body["ok"] is True
    listed = call(client, "chat.scheduledMessages.list")["scheduled_messages"]
    assert [r["id"] for r in listed] == [body["scheduled_message_id"]]
    assert call(client, "chat.scheduleMessage", channel="C0001", post_at=1, text="x")["error"] == "time_in_past"
    assert call(client, "chat.deleteScheduledMessage", channel="C0001",
                scheduled_message_id=body["scheduled_message_id"])["ok"] is True
    assert sl.STATE["scheduled_messages"] == {}


# --- conversations.history / replies ------------------------------------

def test_conversations_history_returns_top_level_newest_first(client):
    _seed_channel()
    a = call(client, "chat.postMessage", channel="C0001", text="first")
    call(client, "chat.postMessage", channel="C0001", text="reply", thread_ts=a["ts"])
    call(client, "chat.postMessage", channel="C0001", text="second")
    body = client.get("/api/conversations.history?channel=C0001", headers=H).json()
    assert body["ok"] is True
    assert [m["text"] for m in body["messages"]] == ["second", "first"]
    assert body["has_more"] is False


def test_conversations_history_missing_channel(client):
    body = client.get("/api/conversations.history", headers=H).json()
    assert body["error"] == "invalid_arguments"
    assert "channel" in body["response_metadata"]["messages"][0]


def test_conversations_history_oldest_latest_inclusive(client):
    _seed_channel()
    first = call(client, "chat.postMessage", channel="C0001", text="one")
    second = call(client, "chat.postMessage", channel="C0001", text="two")
    after = call(client, "conversations.history", channel="C0001", oldest=first["ts"])
    assert [m["text"] for m in after["messages"]] == ["two"]
    inclusive = call(client, "conversations.history", channel="C0001",
                     oldest=first["ts"], inclusive="1")
    assert [m["text"] for m in inclusive["messages"]] == ["two", "one"]
    bounded = call(client, "conversations.history", channel="C0001", latest=second["ts"])
    assert [m["text"] for m in bounded["messages"]] == ["one"]
    assert call(client, "conversations.history", channel="C0001",
                oldest="not-a-ts")["error"] == "invalid_ts_oldest"


def test_conversations_history_paginates_with_opaque_cursors(client):
    _seed_channel()
    for i in range(5):
        call(client, "chat.postMessage", channel="C0001", text=f"m{i}")
    seen, cursor, pages = [], "", 0
    while True:
        page = call(client, "conversations.history", channel="C0001", limit=2, cursor=cursor)
        seen += [m["text"] for m in page["messages"]]
        cursor = page["response_metadata"]["next_cursor"]
        pages += 1
        if not cursor:
            break
    assert pages == 3 and len(seen) == 5 and len(set(seen)) == 5
    assert call(client, "conversations.history", channel="C0001",
                cursor="nonsense")["error"] == "invalid_cursor"


def test_conversations_replies_returns_parent_plus_replies(client):
    _seed_channel()
    p = call(client, "chat.postMessage", channel="C0001", text="p")
    call(client, "chat.postMessage", channel="C0001", text="r1", thread_ts=p["ts"])
    r2 = call(client, "chat.postMessage", channel="C0001", text="r2", thread_ts=p["ts"])
    body = client.get(f"/api/conversations.replies?channel=C0001&ts={p['ts']}", headers=H).json()
    assert body["ok"] is True
    assert [m["text"] for m in body["messages"]] == ["p", "r1", "r2"]
    # Any message in the thread resolves to the whole thread.
    from_reply = call(client, "conversations.replies", channel="C0001", ts=r2["ts"])
    assert [m["text"] for m in from_reply["messages"]] == ["p", "r1", "r2"]


def test_conversations_replies_missing_ts(client):
    _seed_channel()
    body = client.get("/api/conversations.replies?channel=C0001", headers=H).json()
    assert body["error"] == "invalid_arguments"


def test_conversations_replies_unknown_thread(client):
    _seed_channel()
    assert call(client, "conversations.replies", channel="C0001",
                ts="1.000001")["error"] == "thread_not_found"


# --- conversations.list / info ------------------------------------------

def test_conversations_list_cursor_pagination(client):
    for i in range(5):
        cid = f"C0000000{i+1}"
        sl.STATE["channels"][cid] = {"id": cid, "name": f"chan-{i}", "is_channel": True}
    body = client.get("/api/conversations.list?limit=2", headers=H).json()
    assert body["ok"] is True
    assert len(body["channels"]) == 2
    cursor = body["response_metadata"]["next_cursor"]
    assert base64.urlsafe_b64decode(cursor).decode() == "channels:2"
    body2 = client.get(f"/api/conversations.list?limit=2&cursor={cursor}", headers=H).json()
    assert len(body2["channels"]) == 2
    body3 = client.get(
        f"/api/conversations.list?limit=2&cursor={body2['response_metadata']['next_cursor']}",
        headers=H).json()
    assert len(body3["channels"]) == 1
    assert body3["response_metadata"]["next_cursor"] == ""


def test_conversations_list_filters_types_and_archived(client):
    client.post("/_seed/engineering-team")
    private = call(client, "conversations.create", name="secrets", is_private="1")["channel"]
    call(client, "conversations.archive", channel="C00000006")
    public = call(client, "conversations.list", types="public_channel", limit=100)["channels"]
    assert private["id"] not in [c["id"] for c in public]
    assert "random" in [c["name"] for c in public]
    live = call(client, "conversations.list", types="public_channel",
                exclude_archived="1", limit=100)["channels"]
    assert "random" not in [c["name"] for c in live]
    both = call(client, "conversations.list",
                types="public_channel,private_channel", limit=100)["channels"]
    assert private["id"] in [c["id"] for c in both]


def test_conversations_list_reports_membership(client):
    client.post("/_seed/engineering-team")
    channels = {c["name"]: c for c in call(client, "conversations.list", limit=100)["channels"]}
    assert channels["general"]["is_member"] is True
    assert channels["design"]["is_member"] is False
    assert channels["general"]["num_members"] == 13


def test_conversations_info_by_id_and_name(client):
    _seed_channel("C0001", "general")
    assert call(client, "conversations.info", channel="C0001")["channel"]["name"] == "general"
    assert call(client, "conversations.info", channel="general")["channel"]["id"] == "C0001"


def test_conversations_info_missing_channel_arg(client):
    assert client.get("/api/conversations.info", headers=H).json()["error"] == "invalid_arguments"


def test_conversations_info_not_found(client):
    assert call(client, "conversations.info", channel="C_NOPE")["error"] == "channel_not_found"


# --- conversations.create and friends -----------------------------------

def test_conversations_create_happy_path(client):
    ch = call(client, "conversations.create", name="engineering")["channel"]
    assert ch["name"] == "engineering"
    assert ch["id"].startswith("C")
    assert ch["is_channel"] is True
    assert ch["is_private"] is False
    assert ch["is_archived"] is False
    assert ch["is_member"] is True
    assert sl.STATE["channels"][ch["id"]]["name"] == "engineering"
    assert sl.STATE["messages"][ch["id"]] == []


def test_conversations_create_missing_name(client):
    assert call(client, "conversations.create") == {"ok": False, "error": "invalid_name_required"}


def test_conversations_create_name_taken(client):
    _seed_channel(name="general")
    assert call(client, "conversations.create", name="general")["error"] == "name_taken"


def test_conversations_create_invalid_name(client):
    assert call(client, "conversations.create", name="Bad Name!!")["error"] == "invalid_name"
    assert call(client, "conversations.create", name="x" * 81)["error"] == "invalid_name_maxlength"


def test_conversations_create_strips_hash_and_lowercases(client):
    body = call(client, "conversations.create", name="#Incident-2026")
    assert body["channel"]["name"] == "incident-2026"


def test_conversations_create_private(client):
    ch = call(client, "conversations.create", name="secrets", is_private="1")["channel"]
    assert ch["is_private"] is True
    # Private channels created through the conversations API are still channels.
    assert ch["is_channel"] is True


def test_conversations_create_then_post_and_list(client):
    ch = call(client, "conversations.create", name="incident-1")["channel"]
    assert call(client, "chat.postMessage", channel="incident-1", text="fire")["ok"] is True
    listed = call(client, "conversations.list")
    assert any(c["id"] == ch["id"] for c in listed["channels"])


def test_conversations_create_requires_auth(client):
    r = client.post("/api/conversations.create", json={"name": "nope"})
    assert r.json() == {"ok": False, "error": "not_authed"}


def test_conversations_create_recorded_in_trace(client):
    call(client, "conversations.create", name="traced")
    trace = client.get("/_trace").json()
    assert any(e["path"] == "/api/conversations.create" for e in trace)


def test_conversations_create_reset_clears(client):
    call(client, "conversations.create", name="temp")
    assert len(sl.STATE["channels"]) == 1
    client.post("/_reset")
    assert sl.STATE["channels"] == {}


def test_join_invite_kick_leave_track_membership(client):
    client.post("/_seed/engineering-team")
    joined = call(client, "conversations.join", channel="C00000003")
    assert joined["ok"] is True and joined["channel"]["is_member"] is True
    again = call(client, "conversations.join", channel="C00000003")
    assert again["warning"] == "already_in_channel"

    invited = call(client, "conversations.invite", channel="C00000003", users="U00000001")
    assert invited["ok"] is True
    assert "U00000001" in sl.STATE["channels"]["C00000003"]["members"]
    assert call(client, "conversations.invite", channel="C00000003",
                users="U00000001")["error"] == "already_in_channel"
    assert call(client, "conversations.invite", channel="C00000003",
                users="U404")["error"] == "user_not_found"

    members = call(client, "conversations.members", channel="C00000003")["members"]
    assert BOT in members and "U00000001" in members

    assert call(client, "conversations.kick", channel="C00000003", user="U00000001")["ok"] is True
    assert "U00000001" not in sl.STATE["channels"]["C00000003"]["members"]
    assert call(client, "conversations.leave", channel="C00000003")["ok"] is True
    assert BOT not in sl.STATE["channels"]["C00000003"]["members"]
    assert call(client, "conversations.leave", channel="C00000001")["error"] == "cant_leave_general"


def test_set_topic_and_purpose_and_rename(client):
    ch = call(client, "conversations.create", name="incident-9")["channel"]
    topic = call(client, "conversations.setTopic", channel=ch["id"], topic="SEV2: API latency")
    assert topic["channel"]["topic"]["value"] == "SEV2: API latency"
    assert topic["channel"]["topic"]["creator"] == BOT
    purpose = call(client, "conversations.setPurpose", channel=ch["id"], purpose="Coordination")
    assert purpose["channel"]["purpose"]["value"] == "Coordination"
    renamed = call(client, "conversations.rename", channel=ch["id"], name="incident-9-resolved")
    assert renamed["channel"]["name"] == "incident-9-resolved"
    assert renamed["channel"]["previous_names"] == ["incident-9"]
    assert sl.STATE["channels"][ch["id"]]["topic"]["value"] == "SEV2: API latency"


def test_archive_and_unarchive(client):
    ch = call(client, "conversations.create", name="tempus")["channel"]
    assert call(client, "conversations.archive", channel=ch["id"]) == {"ok": True}
    assert sl.STATE["channels"][ch["id"]]["is_archived"] is True
    assert call(client, "conversations.archive", channel=ch["id"])["error"] == "already_archived"
    assert call(client, "conversations.unarchive", channel=ch["id"]) == {"ok": True}
    assert call(client, "conversations.unarchive", channel=ch["id"])["error"] == "not_archived"


def test_conversations_open_is_idempotent(client):
    client.post("/_seed/engineering-team")
    first = call(client, "conversations.open", users="U00000001")["channel"]["id"]
    second = call(client, "conversations.open", users="U00000001", return_im="1")
    assert second["channel"]["id"] == first
    assert second["already_open"] is True
    group = call(client, "conversations.open", users="U00000001,U00000002")["channel"]["id"]
    assert sl.STATE["channels"][group]["is_mpim"] is True


# --- reactions ----------------------------------------------------------

def test_reactions_add_get_remove(client):
    _seed_channel()
    p = call(client, "chat.postMessage", channel="C0001", text="x")
    assert call(client, "reactions.add", channel="C0001", timestamp=p["ts"],
                name="thumbsup") == {"ok": True}
    msg = sl._find_message("C0001", p["ts"])
    assert msg["reactions"][0] == {"name": "thumbsup", "users": [BOT], "count": 1}
    assert call(client, "reactions.add", channel="C0001", timestamp=p["ts"],
                name="thumbsup")["error"] == "already_reacted"
    got = call(client, "reactions.get", channel="C0001", timestamp=p["ts"])
    assert got["message"]["reactions"][0]["name"] == "thumbsup"
    assert call(client, "reactions.remove", channel="C0001", timestamp=p["ts"],
                name="thumbsup") == {"ok": True}
    assert "reactions" not in sl._find_message("C0001", p["ts"])
    assert call(client, "reactions.remove", channel="C0001", timestamp=p["ts"],
                name="thumbsup")["error"] == "no_reaction"


def test_reactions_add_missing_name(client):
    _seed_channel()
    p = call(client, "chat.postMessage", channel="C0001", text="x")
    assert call(client, "reactions.add", channel="C0001",
                timestamp=p["ts"]) == {"ok": False, "error": "invalid_name"}


def test_reactions_add_message_not_found(client):
    _seed_channel()
    assert call(client, "reactions.add", channel="C0001", timestamp="9999.000001",
                name="fire") == {"ok": False, "error": "message_not_found"}


# --- pins ---------------------------------------------------------------

def test_pins_add_list_remove(client):
    _seed_channel()
    p = call(client, "chat.postMessage", channel="C0001", text="runbook")
    assert call(client, "pins.add", channel="C0001", timestamp=p["ts"]) == {"ok": True}
    assert call(client, "pins.add", channel="C0001", timestamp=p["ts"])["error"] == "already_pinned"
    items = call(client, "pins.list", channel="C0001")["items"]
    assert [i["message"]["ts"] for i in items] == [p["ts"]]
    assert call(client, "conversations.history", channel="C0001")["pin_count"] == 1
    assert call(client, "pins.remove", channel="C0001", timestamp=p["ts"]) == {"ok": True}
    assert call(client, "pins.list", channel="C0001")["items"] == []


# --- users --------------------------------------------------------------

def test_users_list_pagination(client):
    for i in range(3):
        uid = f"U0000000{i+1}"
        sl.STATE["users"][uid] = {"id": uid, "name": f"user-{i}", "real_name": f"User {i}"}
    body = client.get("/api/users.list?limit=2", headers=H).json()
    assert body["ok"] is True
    assert len(body["members"]) == 2
    assert base64.urlsafe_b64decode(body["response_metadata"]["next_cursor"]).decode() == "users:2"


def test_users_info_and_lookup_by_email(client):
    client.post("/_seed/engineering-team")
    assert call(client, "users.info", user="U00000001")["user"]["name"] == "alice"
    assert call(client, "users.info", user="U_NOPE")["error"] == "user_not_found"
    found = call(client, "users.lookupByEmail", email="alice@acme.com")
    assert found["user"]["id"] == "U00000001"
    assert call(client, "users.lookupByEmail", email="nobody@acme.com")["error"] == "users_not_found"


def test_users_conversations_lists_only_joined_channels(client):
    client.post("/_seed/engineering-team")
    mine = [c["name"] for c in call(client, "users.conversations", limit=100)["channels"]]
    assert set(mine) == {"general", "engineering"}
    theirs = [c["name"] for c in call(client, "users.conversations",
                                      user="U00000006", limit=100)["channels"]]
    assert set(theirs) == {"general", "design", "random"}


def test_users_profile_get_by_id(client):
    sl.STATE["users"]["U00000001"] = {
        "id": "U00000001", "name": "alice", "real_name": "Alice A",
        "profile": {"real_name": "Alice A", "display_name": "alice", "email": "a@x.com"},
    }
    body = client.get("/api/users.profile.get?user=U00000001", headers=H).json()
    assert body["ok"] is True
    assert body["profile"]["email"] == "a@x.com"


def test_users_profile_get_defaults_to_the_caller(client):
    body = client.get("/api/users.profile.get", headers=H).json()
    assert body["profile"]["display_name"] == "checkpoint"


def test_users_profile_get_user_not_found(client):
    assert client.get("/api/users.profile.get?user=U_NOPE",
                      headers=H).json()["error"] == "user_not_found"


# --- files and search ---------------------------------------------------

def test_external_upload_flow_shares_the_file(client):
    _seed_channel()
    start = call(client, "files.getUploadURLExternal", filename="deploy.log", length=9)
    assert start["ok"] is True and start["file_id"].startswith("F")
    upload = client.post(start["upload_url"].replace("http://testserver", ""), content=b"log line")
    assert upload.status_code == 200 and upload.text.startswith("OK - ")
    done = call(client, "files.completeUploadExternal",
                files=json.dumps([{"id": start["file_id"], "title": "Deploy log"}]),
                channel_id="C0001", initial_comment="latest deploy")
    assert done["ok"] is True
    file = done["files"][0]
    assert file["title"] == "Deploy log" and file["size"] == 8
    assert file["channels"] == ["C0001"]
    shared = sl.STATE["messages"]["C0001"][0]
    assert shared["subtype"] == "file_share" and shared["text"] == "latest deploy"
    assert call(client, "files.info", file=start["file_id"])["file"]["name"] == "deploy.log"
    assert [f["id"] for f in call(client, "files.list", channel="C0001")["files"]] == [start["file_id"]]
    assert call(client, "files.delete", file=start["file_id"]) == {"ok": True}
    assert call(client, "files.info", file=start["file_id"])["error"] == "file_not_found"


def test_search_messages_matches_terms_and_filters(client):
    client.post("/_seed/engineering-team")
    hits = call(client, "search.messages", query="sprint")
    assert hits["messages"]["total"] == 1
    assert hits["messages"]["matches"][0]["channel"]["name"] == "engineering"
    assert hits["messages"]["matches"][0]["permalink"].startswith("http")
    assert call(client, "search.messages",
                query="in:#backend latency")["messages"]["total"] == 1
    assert call(client, "search.messages",
                query="in:#design latency")["messages"]["total"] == 0
    assert call(client, "search.messages", query="from:@dave retro")["messages"]["total"] == 1
    assert call(client, "search.messages")["error"] == "no_query"


# --- views and classification -------------------------------------------

def test_views_expose_denormalized_collections(client):
    client.post("/_seed/incident-active")
    collections = client.get("/_views").json()["collections"]
    assert set(collections) >= {"channels", "messages", "users", "reactions", "files"}
    message = next(m for m in collections["messages"]["items"] if "P1 declared" in m["text"])
    assert message["channel_name"] == "incident-payments-2026-05-12"
    assert message["user_name"] == "erin"
    assert message["channel_type"] == "public_channel"
    assert message["pinned"] is True
    reaction = next(r for r in collections["reactions"]["items"] if r["name"] == "rotating_light")
    assert reaction["channel_name"] == "incident-payments-2026-05-12" and reaction["count"] == 3
    assert collections["channels"]["tombstone"] == "is_archived"
    assert collections["messages"]["nouns"][0] == "message"


def test_classification_of_rpc_methods():
    assert sl._classify("POST", "/api/chat.postMessage", None) == ("create", "messages")
    assert sl._classify("POST", "/api/chat.delete", None) == ("delete", "messages")
    assert sl._classify("POST", "/api/conversations.history", None) == ("read", "channels")
    assert sl._classify("POST", "/api/conversations.join", None) == ("update", "channels")
    assert sl._classify("POST", "/api/conversations.archive", None) == ("delete", "channels")
    assert sl._classify("POST", "/api/users.profile.get", None) == ("read", "users")
    assert sl._classify("POST", "/api/users.conversations", None) == ("read", "channels")
    assert sl._classify("POST", "/api/files.getUploadURLExternal", None) == ("create", "files")
    assert sl._classify("POST", "/upload/v1/F00000001", None) == ("update", "files")
    assert sl._classify("POST", "/api/search.messages", None) == ("read", "messages")
