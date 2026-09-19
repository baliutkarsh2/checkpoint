"""Slack twin driven by slack_sdk, the official Python Slack SDK.

``WebClient`` POSTs every method (form-encoded or JSON), raises
``SlackApiError`` on ``ok: false`` and paginates by following ``next_cursor`` —
the exact behaviours an agent inherits for free and a twin has to earn.
"""
from __future__ import annotations

import time

import pytest

slack_sdk = pytest.importorskip("slack_sdk")

from slack_sdk.errors import SlackApiError  # noqa: E402

TWIN = "slack"


@pytest.fixture
def slack(twin):
    twin.seed("engineering-team")
    return slack_sdk.WebClient(token=twin.token, base_url=f"{twin.url}/api/")


@pytest.fixture
def empty_slack(twin):
    twin.seed("empty")
    return slack_sdk.WebClient(token=twin.token, base_url=f"{twin.url}/api/")


GENERAL = "C00000001"
ENGINEERING = "C00000002"
ALICE = "U00000001"


# --- identity and listing ------------------------------------------------

def test_auth_test_identifies_the_bot(slack):
    identity = slack.auth_test()
    assert identity["ok"] is True
    assert identity["user_id"].startswith("U")
    assert identity["bot_id"].startswith("B")
    assert identity["team_id"].startswith("T")


def test_list_channels_excludes_private_and_archived(slack):
    slack.conversations_create(name="secret-plans", is_private=True)
    slack.conversations_archive(channel="C00000005")
    names = [c["name"] for c in
             slack.conversations_list(types="public_channel", exclude_archived=True,
                                      limit=200)["channels"]]
    assert "general" in names
    assert "secret-plans" not in names
    assert "design" not in names


def test_conversations_info_and_members(slack):
    channel = slack.conversations_info(channel=GENERAL)["channel"]
    assert channel["name"] == "general"
    assert channel["is_member"] is True
    members = slack.conversations_members(channel=GENERAL)["members"]
    assert ALICE in members
    assert len(members) == channel["num_members"]


def test_users_list_info_and_lookup(slack):
    members = slack.users_list(limit=200)["members"]
    assert {m["id"] for m in members} >= {ALICE}
    assert slack.users_info(user=ALICE)["user"]["real_name"] == "Alice Anderson"
    email = slack.users_profile_get(user=ALICE)["profile"]["email"]
    assert slack.users_lookupByEmail(email=email)["user"]["id"] == ALICE


def test_users_conversations_lists_the_bots_channels(slack):
    names = {c["name"] for c in slack.users_conversations(limit=200)["channels"]}
    assert names == {"general", "engineering"}


# --- create -> read -> update -> delete ---------------------------------

def test_channel_lifecycle(slack, twin):
    created = slack.conversations_create(name="incident-4242")["channel"]
    assert created["is_member"] is True
    slack.conversations_setTopic(channel=created["id"], topic="SEV2: API latency")
    slack.conversations_invite(channel=created["id"], users=ALICE)
    info = slack.conversations_info(channel=created["id"])["channel"]
    assert info["topic"]["value"] == "SEV2: API latency"
    assert ALICE in slack.conversations_members(channel=created["id"])["members"]
    slack.conversations_archive(channel=created["id"])

    channels = {c["id"]: c for c in twin.views()["channels"]["items"]}
    assert channels[created["id"]]["is_archived"] is True
    assert channels[created["id"]]["topic"] == "SEV2: API latency"


def test_message_lifecycle(slack, twin):
    posted = slack.chat_postMessage(channel=ENGINEERING, text="Deploy starting :rocket:")
    ts = posted["ts"]
    slack.chat_update(channel=ENGINEERING, ts=ts, text="Deploy finished")
    assert slack.conversations_history(channel=ENGINEERING, limit=1)["messages"][0]["text"] \
        == "Deploy finished"

    messages = {m["id"]: m for m in twin.views()["messages"]["items"]}
    assert messages[f"{ENGINEERING}:{ts}"]["text"] == "Deploy finished"
    assert messages[f"{ENGINEERING}:{ts}"]["channel_name"] == "engineering"

    assert slack.chat_delete(channel=ENGINEERING, ts=ts)["ok"] is True
    assert f"{ENGINEERING}:{ts}" not in {m["id"] for m in twin.views()["messages"]["items"]}


def test_post_by_channel_name_and_blocks_only(slack):
    assert slack.chat_postMessage(channel="#general", text="by name")["channel"] == GENERAL
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Deploy* done"}}]
    assert slack.chat_postMessage(channel=GENERAL, blocks=blocks)["ok"] is True


def test_thread_reply_and_replies(slack):
    parent = slack.chat_postMessage(channel=GENERAL, text="deploy thread")
    slack.chat_postMessage(channel=GENERAL, thread_ts=parent["ts"], text="step 1 ok")
    slack.chat_postMessage(channel=GENERAL, thread_ts=parent["ts"], text="step 2 ok")
    thread = slack.conversations_replies(channel=GENERAL, ts=parent["ts"])["messages"]
    assert [m["text"] for m in thread] == ["deploy thread", "step 1 ok", "step 2 ok"]
    # Replies stay out of the channel's top-level history.
    history = slack.conversations_history(channel=GENERAL)["messages"]
    assert [m["text"] for m in history] == ["deploy thread"]
    assert history[0]["reply_count"] == 2


def test_history_filters_by_timestamp(slack):
    first = slack.chat_postMessage(channel=GENERAL, text="one")["ts"]
    second = slack.chat_postMessage(channel=GENERAL, text="two")["ts"]
    newer = slack.conversations_history(channel=GENERAL, oldest=first, inclusive=False)
    assert [m["ts"] for m in newer["messages"]] == [second]
    inclusive = slack.conversations_history(channel=GENERAL, oldest=first, inclusive=True)
    assert [m["ts"] for m in inclusive["messages"]] == [second, first]


def test_reactions_add_get_remove(slack, twin):
    posted = slack.chat_postMessage(channel=GENERAL, text="ship it")
    slack.reactions_add(channel=GENERAL, timestamp=posted["ts"], name="white_check_mark")
    got = slack.reactions_get(channel=GENERAL, timestamp=posted["ts"])
    assert [r["name"] for r in got["message"]["reactions"]] == ["white_check_mark"]
    assert any(r["name"] == "white_check_mark" for r in twin.views()["reactions"]["items"])
    slack.reactions_remove(channel=GENERAL, timestamp=posted["ts"], name="white_check_mark")
    assert "reactions" not in slack.reactions_get(channel=GENERAL,
                                                  timestamp=posted["ts"])["message"]


def test_permalink_and_ephemeral_and_pin(slack, twin):
    posted = slack.chat_postMessage(channel=GENERAL, text="runbook here")
    link = slack.chat_getPermalink(channel=GENERAL, message_ts=posted["ts"])["permalink"]
    assert f"/archives/{GENERAL}/p" in link
    assert slack.chat_postEphemeral(channel=GENERAL, user=ALICE, text="psst")["ok"] is True
    assert slack.pins_add(channel=GENERAL, timestamp=posted["ts"])["ok"] is True
    assert [i["message"]["ts"] for i in slack.pins_list(channel=GENERAL)["items"]] == [posted["ts"]]
    assert twin.views()["ephemeral_messages"]["items"][0]["user_name"] == "alice"


def test_schedule_message(slack, twin):
    post_at = int(time.time()) + 600
    scheduled = slack.chat_scheduleMessage(channel=GENERAL, post_at=post_at, text="standup!")
    assert scheduled["ok"] is True
    listed = slack.chat_scheduledMessages_list()["scheduled_messages"]
    assert [r["id"] for r in listed] == [scheduled["scheduled_message_id"]]
    assert twin.views()["scheduled_messages"]["items"][0]["channel_name"] == "general"
    slack.chat_deleteScheduledMessage(channel=GENERAL,
                                      scheduled_message_id=scheduled["scheduled_message_id"])
    assert slack.chat_scheduledMessages_list()["scheduled_messages"] == []


def test_direct_messages(slack):
    opened = slack.conversations_open(users=ALICE)["channel"]["id"]
    assert opened.startswith("D")
    assert slack.chat_postMessage(channel=opened, text="hi via DM")["ok"] is True
    # Posting straight to a user id opens the same DM.
    assert slack.chat_postMessage(channel=ALICE, text="hi again")["channel"] == opened


def test_files_upload_v2_shares_the_file(slack, twin):
    result = slack.files_upload_v2(channel=GENERAL, content="deploy log line",
                                   filename="deploy.log", title="Deploy log",
                                   initial_comment="latest deploy")
    assert result["ok"] is True
    uploaded = twin.views()["files"]["items"]
    assert [f["title"] for f in uploaded] == ["Deploy log"]
    assert uploaded[0]["channel_names"] == ["general"]
    assert uploaded[0]["preview"] == "deploy log line"
    shared = slack.conversations_history(channel=GENERAL)["messages"][0]
    assert shared["text"] == "latest deploy"
    assert shared["files"][0]["name"] == "deploy.log"


def test_search_messages(slack):
    slack.chat_postMessage(channel=GENERAL, text="release 4.2 is out")
    hits = slack.search_messages(query="release")
    assert hits["messages"]["total"] == 1
    assert hits["messages"]["matches"][0]["channel"]["name"] == "general"
    assert slack.search_messages(query="in:#design release")["messages"]["total"] == 0


# --- pagination ----------------------------------------------------------

def test_cursor_pagination_terminates(slack, twin):
    ids, pages = [], 0
    for page in slack.conversations_list(limit=2):
        pages += 1
        ids += [c["id"] for c in page["channels"]]
        assert pages < 20, "pagination does not terminate"
    assert len(set(ids)) == len(twin.state()["channels"])
    assert pages == 3


def test_history_pagination_covers_every_message(slack):
    for i in range(5):
        slack.chat_postMessage(channel=GENERAL, text=f"message {i}")
    seen = []
    for page in slack.conversations_history(channel=GENERAL, limit=2):
        seen += [m["text"] for m in page["messages"]]
    assert sorted(seen) == [f"message {i}" for i in range(5)]


# --- typed errors --------------------------------------------------------

def test_unknown_channel_raises_typed_error(slack):
    with pytest.raises(SlackApiError) as caught:
        slack.chat_postMessage(channel="C_NOPE", text="x")
    assert caught.value.response["error"] == "channel_not_found"


def test_unimplemented_method_reports_unknown_method(slack):
    with pytest.raises(SlackApiError) as caught:
        slack.api_call("chat.unfurl", json={"channel": GENERAL, "ts": "1.2", "unfurls": {}})
    assert caught.value.response["error"] == "unknown_method"


def test_editing_someone_elses_message_is_refused(slack):
    human = slack.conversations_history(channel=ENGINEERING)["messages"][0]
    with pytest.raises(SlackApiError) as caught:
        slack.chat_update(channel=ENGINEERING, ts=human["ts"], text="rewritten")
    assert caught.value.response["error"] == "cant_update_message"


def test_duplicate_reaction_and_missing_user(slack):
    posted = slack.chat_postMessage(channel=GENERAL, text="x")
    slack.reactions_add(channel=GENERAL, timestamp=posted["ts"], name="eyes")
    with pytest.raises(SlackApiError) as caught:
        slack.reactions_add(channel=GENERAL, timestamp=posted["ts"], name="eyes")
    assert caught.value.response["error"] == "already_reacted"
    with pytest.raises(SlackApiError) as caught:
        slack.users_lookupByEmail(email="nobody@acme.com")
    assert caught.value.response["error"] == "users_not_found"


def test_name_taken_on_duplicate_channel(slack):
    with pytest.raises(SlackApiError) as caught:
        slack.conversations_create(name="general")
    assert caught.value.response["error"] == "name_taken"


def test_strict_auth_rejects_a_foreign_token(twin, empty_slack):
    twin.configure(strict_auth=True)
    stranger = slack_sdk.WebClient(token="xoxb-CHECKPOINTFAKE-not-ours", base_url=f"{twin.url}/api/")
    with pytest.raises(SlackApiError) as caught:
        stranger.auth_test()
    assert caught.value.response["error"] == "invalid_auth"


# --- fault injection -----------------------------------------------------

def test_rate_limit_surfaces_as_429_without_hanging(twin, slack):
    twin.configure(rate_limit=1)
    started = time.monotonic()
    with pytest.raises(SlackApiError) as caught:
        for _ in range(4):
            slack.conversations_list()
    assert caught.value.response.status_code == 429
    assert caught.value.response.headers["retry-after"] == "30"
    assert time.monotonic() - started < 30, "the SDK must not sleep through the retry window"


# --- the trace the engine scores ----------------------------------------

def test_trace_classifies_rpc_calls(slack, twin):
    posted = slack.chat_postMessage(channel=GENERAL, text="traced")
    slack.conversations_history(channel=GENERAL)
    slack.reactions_add(channel=GENERAL, timestamp=posted["ts"], name="eyes")
    slack.chat_delete(channel=GENERAL, ts=posted["ts"])
    ops = [(e["op"], e["resource"]) for e in twin.trace()]
    assert ("create", "messages") in ops
    assert ("read", "channels") in ops
    assert ("create", "reactions") in ops
    assert ("delete", "messages") in ops
    assert all(e["ok"] for e in twin.trace())
