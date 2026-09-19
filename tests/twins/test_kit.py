"""The shared twin runtime: every twin gets the same control plane and fault model."""
from __future__ import annotations

import importlib
import time

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import registry

# A cheap authenticated read and a write for each twin, used to exercise faults.
PROBES = {
    "github": (("GET", "/user", None), ("POST", "/user/repos", {"name": "kit-probe"})),
    "slack": (("GET", "/api/conversations.list", None),
              ("POST", "/api/conversations.create", {"name": "kit-probe"})),
    "stripe": (("GET", "/v1/balance", None), ("POST", "/v1/customers", {"email": "k@x.io"})),
    "linear": (("GET", "/v1/issues", None), ("POST", "/v1/teams", {"name": "Kit", "key": "KIT"})),
    # A fresh Supabase project has only the tables its seed declares, so the
    # probe uses auth, which exists in every project.
    "supabase": (("GET", "/auth/v1/admin/users", None),
                 ("POST", "/auth/v1/admin/users", {"email": "kit-probe@acme.test"})),
    "discord": (("GET", "/api/v10/users/@me", None),
                ("POST", "/api/v10/guilds", {"name": "kit-probe"})),
    "google-workspace": (("GET", "/gmail/v1/users/me/profile", None),
                         ("POST", "/gmail/v1/users/me/labels", {"name": "kit-probe"})),
}

TWINS = sorted(PROBES)


def _module(name: str):
    return importlib.import_module(registry.get(name).app.partition(":")[0])


@pytest.fixture(params=TWINS)
def twin(request):
    module = _module(request.param)
    module.TWIN.reset()
    client = TestClient(module.app)
    headers = {"Authorization": f"Bearer {registry.get(request.param).token}",
               "apikey": registry.get(request.param).token}
    read, write = PROBES[request.param]

    def call(spec):
        method, path, body = spec
        return client.request(method, path, json=body, headers=headers)

    yield request.param, module, client, lambda: call(read), lambda: call(write)
    module.TWIN.reset()


def test_every_builtin_twin_is_covered():
    assert set(TWINS) == set(registry.names())


def test_control_plane(twin):
    name, module, client, read, _ = twin
    assert client.get("/_health").json()["ok"] is True
    assert "seeds" in client.get("/_seeds").json()
    assert client.get("/_config").json()["rate_limit"] is None
    assert read().status_code < 400
    trace = client.get("/_trace").json()
    assert len(trace) == 1 and trace[0]["via"] == "rest" and "duration_ms" in trace[0]
    assert client.post("/_reset").json() == {"ok": True}
    assert client.get("/_trace").json() == []


def test_unknown_config_key_is_rejected(twin):
    _, _, client, _, _ = twin
    r = client.post("/_config", json={"rate_limt": 3})
    assert r.status_code == 400
    assert "rate_limt" in r.json()["error"]


def test_invalid_fault_values_are_rejected(twin):
    _, _, client, _, _ = twin
    assert client.post("/_config", json={"error_rate": 2}).status_code == 400
    assert client.post("/_config", json={"fail": [{"status": 200}]}).status_code == 400
    assert client.post("/_config", json={"fail": [{"path": "("}]}).status_code == 400


def test_rate_limit(twin):
    _, _, client, read, _ = twin
    client.post("/_config", json={"rate_limit": 2})
    assert read().status_code < 400
    assert read().status_code < 400
    assert read().status_code == 429
    assert client.get("/_trace").json()[-1]["fault"] is True


def test_read_only_blocks_writes_but_not_reads(twin):
    name, _, client, read, write = twin
    client.post("/_config", json={"read_only": True})
    assert read().status_code < 400
    r = write()
    # Slack reports permission problems as HTTP 200 + ok:false, like the real API.
    if name == "slack":
        assert r.json() == {"ok": False, "error": "restricted_action"}
    else:
        assert r.status_code == 403


def test_permissions_denied_blocks_writes(twin):
    name, _, client, _, write = twin
    client.post("/_config", json={"permissions_denied": True})
    r = write()
    if name == "slack":
        assert r.json()["error"] == "missing_scope"
    else:
        assert r.status_code == 403


def test_targeted_fail_rule_fires_then_expires(twin):
    _, _, client, read, _ = twin
    client.post("/_config", json={"fail": [{"method": "GET", "path": ".", "status": 503, "times": 1}]})
    assert read().status_code == 503
    assert read().status_code < 400


def test_error_rate_is_reproducible(twin):
    _, _, client, read, _ = twin

    def outcomes():
        client.post("/_reset")
        client.post("/_config", json={"error_rate": 0.5, "fault_seed": 7})
        return [read().status_code for _ in range(12)]

    first = outcomes()
    assert 500 in first and any(s < 400 for s in first)
    assert outcomes() == first


def test_latency_is_added(twin):
    _, _, client, read, _ = twin
    client.post("/_config", json={"latency_ms": 60})
    t0 = time.perf_counter()
    read()
    assert time.perf_counter() - t0 >= 0.05


def test_missing_credentials_are_rejected(twin):
    _, _, client, _, _ = twin
    method, path, body = PROBES[twin[0]][0]
    r = client.request(method, path, json=body)
    if twin[0] == "slack":
        assert r.json() == {"ok": False, "error": "not_authed"}
    else:
        assert r.status_code == 401


def test_seed_file_applies_config(twin):
    _, _, client, read, _ = twin
    r = client.post("/_seed-file", json={"state": {}, "config": {"rate_limit": 0}})
    assert r.status_code == 200
    assert read().status_code == 429


def test_unknown_seed_lists_available(twin):
    name, _, client, _, _ = twin
    r = client.post("/_seed/does-not-exist")
    assert r.status_code == 404
    assert r.json()["available"] == registry.get(name).seed_names()


def test_seed_names_match_bundled_files():
    for spec in registry.all_specs():
        assert "empty" in spec.seed_names(), spec.name


def test_trace_entries_are_classified(twin):
    name, _, client, read, write = twin
    read()
    write()
    first, second = client.get("/_trace").json()
    assert first["twin"] == name and first["op"] == "read"
    assert second["op"] in ("create", "update")
    assert isinstance(first["resource"], str) and first["resource"]
    assert first["ok"] is True


def test_views_expose_collections(twin):
    _, _, client, _, write = twin
    write()
    views = client.get("/_views").json()["collections"]
    assert views, "every twin exposes at least one collection"
    for coll in views.values():
        assert {"key", "tombstone", "nouns", "items"} <= set(coll)
        assert isinstance(coll["items"], list)


def test_slack_ok_false_is_a_failed_call():
    from checkpoint.twins import slack

    slack.TWIN.reset()
    client = TestClient(slack.app)
    headers = {"Authorization": f"Bearer {registry.get('slack').token}"}
    r = client.post("/api/chat.postMessage", json={"channel": "C404", "text": "hi"}, headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is False
    entry = client.get("/_trace").json()[-1]
    assert entry["ok"] is False
    assert (entry["op"], entry["resource"]) == ("create", "messages")


def test_default_classify_uses_http_semantics():
    from checkpoint.twins.kit import default_classify

    assert default_classify("POST", "/repos/acme/webapp/issues") == ("create", "issues")
    assert default_classify("PATCH", "/repos/acme/webapp/issues/12") == ("update", "issues")
    assert default_classify("DELETE", "/api/v10/channels/1/messages/2") == ("delete", "messages")
    assert default_classify("GET", "/v1/customers/cus_abc123") == ("read", "customers")


def test_read_only_uses_the_twins_own_idea_of_a_write():
    # RPC and GraphQL APIs POST everything, reads included (Slack's SDK does), so
    # deciding "is this a write?" from the HTTP verb made read-only mode refuse reads.
    from fastapi import FastAPI

    from checkpoint.twins import kit

    app = FastAPI()
    state: dict = {}

    def classify(method: str, path: str, body: object):
        return ("read", "things") if path.endswith(".list") else ("create", "things")

    kit.install(app, kit.Twin(name="rpc", state=state, trace=[], fresh_state=dict,
                              classify=classify))

    @app.post("/api/things.list")
    def _list() -> dict:
        return {"ok": True}

    @app.post("/api/things.create")
    def _create() -> dict:
        return {"ok": True}

    client = TestClient(app)
    client.post("/_config", json={"read_only": True})
    assert client.post("/api/things.list").status_code == 200
    assert client.post("/api/things.create").status_code == 403


def test_public_paths_skip_authentication():
    # Real APIs serve some URLs without a credential: webhook endpoints, CDN
    # assets, payment links. A twin declares them instead of special-casing auth.
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from checkpoint.twins import kit

    app = FastAPI()

    def deny(request):
        return JSONResponse(status_code=401, content={"error": "no credential"})

    kit.install(app, kit.Twin(name="pub", state={}, trace=[], fresh_state=dict,
                              authenticate=deny, public_paths=("/webhooks/*",)))

    @app.get("/webhooks/{token}")
    def _hook(token: str) -> dict:
        return {"ok": True}

    @app.get("/private")
    def _private() -> dict:
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/webhooks/abc").status_code == 200
    assert client.get("/private").status_code == 401
