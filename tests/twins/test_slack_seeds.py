"""Slack twin: seed loading and the shape of what a seed contains."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import slack as sl

SEEDS_DIR = Path(sl.__file__).parent / "slack_seeds"

EXPECTED_SEEDS = {"empty", "engineering-team", "busy-workspace", "incident-active"}


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


def test_all_expected_seeds_present_on_disk():
    found = {p.stem for p in SEEDS_DIR.glob("*.json")}
    assert EXPECTED_SEEDS.issubset(found), f"missing: {EXPECTED_SEEDS - found}"


@pytest.mark.parametrize("seed_name", sorted(EXPECTED_SEEDS))
def test_seed_loads_via_endpoint(client, seed_name):
    r = client.post(f"/_seed/{seed_name}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["seed"] == seed_name


def test_empty_seed_has_no_channels(client):
    r = client.post("/_seed/empty")
    assert r.status_code == 200
    state = client.get("/_state").json()
    assert state["channels"] == {}
    # The credential belongs to an app, so its bot user exists in every workspace.
    assert list(state["users"]) == [sl.BOT_USER_ID]


def test_engineering_team_seed_shape(client):
    client.post("/_seed/engineering-team")
    state = client.get("/_state").json()
    # 5-8 channels (spec said 5-8, we ship 6).
    assert 5 <= len(state["channels"]) <= 8
    # 10-15 users (we ship 12).
    assert 10 <= len(state["users"]) <= 15
    # Conversation history in at least 2 channels.
    assert sum(1 for v in state["messages"].values() if v) >= 2


def test_engineering_team_general_channel_exists(client):
    client.post("/_seed/engineering-team")
    r = client.get("/api/conversations.list", headers=H)
    names = {c["name"] for c in r.json()["channels"]}
    assert "general" in names
    assert "engineering" in names


def test_busy_workspace_has_more_channels_than_engineering(client):
    client.post("/_seed/engineering-team")
    eng_channels = len(client.get("/_state").json()["channels"])
    client.post("/_seed/busy-workspace")
    busy_channels = len(client.get("/_state").json()["channels"])
    assert busy_channels > eng_channels


def test_incident_active_has_incident_channel(client):
    client.post("/_seed/incident-active")
    r = client.get("/api/conversations.list", headers=H)
    names = [c["name"] for c in r.json()["channels"]]
    incident_channels = [n for n in names if n.startswith("incident-")]
    assert len(incident_channels) >= 1


def test_incident_active_has_messages_with_reactions(client):
    client.post("/_seed/incident-active")
    state = client.get("/_state").json()
    # Find any message with reactions.
    has_reactions = False
    for msgs in state["messages"].values():
        for m in msgs:
            if m.get("reactions"):
                has_reactions = True
                break
    assert has_reactions, "incident-active should have at least one message with reactions"


def test_seeds_are_normalized_into_full_api_records(client):
    client.post("/_seed/engineering-team")
    state = client.get("/_state").json()
    general = state["channels"]["C00000001"]
    # A seed writes a name, a topic and a member list; the twin fills in the rest.
    assert general["is_channel"] is True
    assert general["is_general"] is True
    assert general["is_private"] is False
    assert general["name_normalized"] == "general"
    assert general["topic"]["last_set"] == 0
    assert general["num_members"] == len(general["members"])
    assert sl.BOT_USER_ID in general["members"]


def test_generated_ids_continue_after_the_seed(client):
    client.post("/_seed/engineering-team")
    created = client.post("/api/conversations.create", headers=H, data={"name": "after-seed"})
    assert created.json()["channel"]["id"] == "C00000007"


def test_seed_resets_state(client):
    # Add a channel then load seed; original channel should be gone.
    sl.STATE["channels"]["C99999999"] = {"id": "C99999999", "name": "pre-seed"}
    client.post("/_seed/empty")
    state = client.get("/_state").json()
    assert "C99999999" not in state["channels"]


def test_unknown_seed_returns_404(client):
    r = client.post("/_seed/no-such-seed")
    assert r.status_code == 404


def test_all_seeds_parse_as_valid_json():
    for seed in EXPECTED_SEEDS:
        data = json.loads((SEEDS_DIR / f"{seed}.json").read_text())
        assert "state" in data, f"{seed}: missing state"
