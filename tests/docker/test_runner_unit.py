import json
import time

from checkpoint.docker.runner import (
    _build_env,
    _read_output,
    _wait_for_sidecar_listening,
    _write_hosts_file,
)
from checkpoint.scenario import Scenario


class _FakeSidecar:
    def __init__(self, stdout: bytes, status: str = "running"):
        self._stdout = stdout
        self.status = status

    def logs(self, stdout=True, stderr=True, tail=None):
        return self._stdout if stdout else b""

    def reload(self):
        pass


def test_build_env_required_keys():
    s = Scenario(prompt="do thing")
    env = _build_env(s, "gpt-4o-mini")
    required = {
        "CHECKPOINT_ENGINE_TASK", "CHECKPOINT_ENGINE_MODE", "CHECKPOINT_METRICS_FILE",
        "CHECKPOINT_AGENT_TRACE_FILE", "CHECKPOINT_OUT_DIR", "CHECKPOINT_ENGINE_MODEL",
        "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "CHECKPOINT_TASK", "CHECKPOINT_MODE",
    }
    assert required <= env.keys()
    assert env["CHECKPOINT_ENGINE_TASK"] == "do thing"
    assert env["CHECKPOINT_ENGINE_MODE"] == "docker"
    assert env["CHECKPOINT_METRICS_FILE"] == "/checkpoint-out/metrics.json"
    assert env["CHECKPOINT_AGENT_TRACE_FILE"] == "/checkpoint-out/agent-trace.json"
    for k in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        assert env[k] == "/checkpoint-out/ca.crt"


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


def test_sidecar_is_listening_once_it_prints_ready():
    sidecar = _FakeSidecar(b"ready\n")
    assert _wait_for_sidecar_listening(sidecar, timeout=1)


def test_sidecar_readiness_needs_the_exact_ready_line():
    # The CA banner mentions paths and routes; only the bare line counts.
    sidecar = _FakeSidecar(b"[sidecar] not ready yet\n")
    assert not _wait_for_sidecar_listening(sidecar, timeout=0.3)


def test_crashed_sidecar_fails_fast_instead_of_timing_out():
    sidecar = _FakeSidecar(b"", status="exited")
    started = time.monotonic()
    assert not _wait_for_sidecar_listening(sidecar, timeout=10)
    assert time.monotonic() - started < 1
