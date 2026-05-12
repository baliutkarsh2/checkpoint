import json
from pathlib import Path

from checkpoint.docker.runner import _build_env, _read_output, _write_hosts_file
from checkpoint.scenario import Scenario


def test_build_env_required_keys():
    s = Scenario(prompt="do thing")
    env = _build_env(s, "gpt-4o-mini")
    required = {
        "ARCHAL_ENGINE_TASK", "ARCHAL_ENGINE_MODE", "ARCHAL_METRICS_FILE",
        "ARCHAL_AGENT_TRACE_FILE", "ARCHAL_OUT_DIR", "ARCHAL_ENGINE_MODEL",
        "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "CHECKPOINT_TASK", "CHECKPOINT_MODE",
    }
    assert required <= env.keys()
    assert env["ARCHAL_ENGINE_TASK"] == "do thing"
    assert env["ARCHAL_ENGINE_MODE"] == "docker"
    assert env["ARCHAL_METRICS_FILE"] == "/archal-out/metrics.json"
    assert env["ARCHAL_AGENT_TRACE_FILE"] == "/archal-out/agent-trace.json"
    for k in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        assert env[k] == "/archal-out/ca.crt"


def test_build_env_forwards_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    env = _build_env(Scenario(prompt="x"), "gpt-4o-mini")
    assert env.get("OPENAI_API_KEY") == "sk-test-123"


def test_build_env_does_not_invent_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = _build_env(Scenario(prompt="x"), "gpt-4o-mini")
    assert "OPENAI_API_KEY" not in env


def test_read_output_missing_returns_none(tmp_path):
    assert _read_output(tmp_path, "metrics.json") is None


def test_read_output_valid_returns_dict(tmp_path):
    (tmp_path / "metrics.json").write_text(json.dumps({"inputTokens": 12, "version": 1}))
    out = _read_output(tmp_path, "metrics.json")
    assert out == {"inputTokens": 12, "version": 1}


def test_read_output_malformed_returns_none(tmp_path):
    (tmp_path / "metrics.json").write_text("{not valid json")
    assert _read_output(tmp_path, "metrics.json") is None


def test_write_hosts_file_has_api_github_com(tmp_path):
    from checkpoint.proxy.routes import register
    register("api.github.com", "http://host.docker.internal:8080")
    p = _write_hosts_file(tmp_path)
    text = p.read_text()
    assert "127.0.0.1 api.github.com" in text
    assert "127.0.0.1 localhost" in text
