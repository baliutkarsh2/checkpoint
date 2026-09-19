"""The twin catalog and the multi-twin host process."""
from __future__ import annotations

import json
import socket
import subprocess
import sys

import httpx
import pytest

from checkpoint.twins import registry


def test_builtins_are_registered():
    assert registry.names() == [
        "discord", "github", "google-workspace", "linear", "slack", "stripe", "supabase",
    ]


def test_aliases_resolve():
    assert registry.get("GitHub").name == "github"
    assert registry.get("gmail").name == "google-workspace"


def test_unknown_twin_lists_available():
    with pytest.raises(registry.UnknownTwinError) as exc:
        registry.get("jira")
    assert "available: discord, github" in str(exc.value)


def test_agent_env_direct_points_sdk_urls_at_the_twin():
    env = registry.get("supabase").agent_env("http://127.0.0.1:9000", intercepted=False)
    assert env["CHECKPOINT_SUPABASE_URL"] == "http://127.0.0.1:9000"
    assert env["SUPABASE_URL"] == "http://127.0.0.1:9000"
    assert env["SUPABASE_KEY"] == registry.get("supabase").token


def test_agent_env_intercepted_keeps_production_urls():
    env = registry.get("supabase").agent_env("http://127.0.0.1:9000", intercepted=True)
    assert env["SUPABASE_URL"] == "https://checkpoint.supabase.co"
    assert env["CHECKPOINT_SUPABASE_URL"] == "http://127.0.0.1:9000"
    gh = registry.get("github").agent_env("http://127.0.0.1:9001", intercepted=True)
    assert gh["GITHUB_API_URL"] == "https://api.github.com"
    assert gh["GITHUB_TOKEN"] == gh["GH_TOKEN"] == registry.get("github").token


def test_register_rejects_replacing_a_builtin():
    with pytest.raises(ValueError):
        registry.register(registry.TwinSpec(name="github", title="x", app="x:app", builtin=False))


def test_every_twin_app_imports_with_a_control_plane():
    for spec in registry.all_specs():
        app = registry.load_app(spec)
        paths = {getattr(r, "path", None) for r in app.routes}
        assert {"/_health", "/_state", "/_trace", "/_reset", "/_config", "/_seeds"} <= paths, spec.name


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_host_serves_several_twins_from_one_process():
    ports = {"github": _free_port(), "slack": _free_port()}
    proc = subprocess.Popen(
        [sys.executable, "-m", "checkpoint.twins.host", *(f"{n}={p}" for n, p in ports.items())],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = proc.stdout.readline()
        assert json.loads(line) == {"ready": ports}
        for name, port in ports.items():
            health = httpx.get(f"http://127.0.0.1:{port}/_health", timeout=5).json()
            assert health == {"ok": True, "twin": name}
        # State is per twin, not shared.
        httpx.post(f"http://127.0.0.1:{ports['github']}/_seed/small-project", timeout=5)
        assert httpx.get(f"http://127.0.0.1:{ports['slack']}/_trace", timeout=5).json() == []
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_host_rejects_unknown_twin():
    proc = subprocess.run(
        [sys.executable, "-m", "checkpoint.twins.host", "jira=9"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "unknown twin 'jira'" in proc.stderr


def test_domains_are_owned_by_the_registry():
    # The proxy, the Docker runner and the sandbox all ask the registry which
    # hostnames to intercept; three hand-kept tables had already drifted apart.
    table = registry.domains()
    assert table["api.github.com"].name == "github"
    assert table["oauth2.googleapis.com"].name == "google-workspace"
    assert registry.for_domain("some-project.supabase.co").name == "supabase"
    assert registry.for_domain("example.com") is None


def test_auth_header_follows_each_service_scheme():
    assert registry.get("github").auth_header.startswith("token ")
    assert registry.get("slack").auth_header.startswith("Bearer ")
    assert registry.get("discord").auth_header.startswith("Bot ")
    # Linear sends a personal API key with no scheme at all.
    assert registry.get("linear").auth_header == registry.get("linear").token
