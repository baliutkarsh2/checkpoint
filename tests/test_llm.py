"""Vendor-neutral LLM resolution, the Anthropic adapter, and the one call path."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from checkpoint.llm import (
    DEFAULT_MODEL,
    LLMCredentialError,
    LLMResponseError,
    complete_json,
    complete_text,
    get_client,
    provider_for,
)
from checkpoint.llm import call as call_mod
from checkpoint.llm.resolve import bare_model


def test_provider_inference():
    assert provider_for("gpt-5.6-luna") == "openai"
    assert provider_for("o3-mini") == "openai"
    assert provider_for("claude-sonnet-5") == "anthropic"
    assert provider_for("gemini-2.0-flash") == "gemini"
    assert provider_for("anthropic:claude-opus-5") == "anthropic"
    assert provider_for("google:gemini-2.0") == "gemini"
    assert provider_for("local:mixtral") == "compat"
    assert provider_for(None) == "openai"
    assert provider_for("") == "openai"


def test_bare_model_strips_prefix():
    assert bare_model("anthropic:claude-sonnet-5") == "claude-sonnet-5"
    assert bare_model("gpt-5.6-luna") == "gpt-5.6-luna"
    assert bare_model("local:qwen/qwen3:8b") == "qwen/qwen3:8b"
    assert bare_model(None) == DEFAULT_MODEL


def test_default_model_is_a_current_id():
    """Nothing in the codebase may quietly reintroduce a retired default."""
    assert DEFAULT_MODEL == "gpt-5.6-luna"


def test_get_client_openai_uses_openai_sdk(monkeypatch):
    import openai as openai_mod
    sentinel = object()
    monkeypatch.setattr(openai_mod, "OpenAI", lambda **kw: sentinel)
    assert get_client("gpt-5.6-luna") is sentinel


def test_get_client_gemini_routes_to_openai_compat(monkeypatch):
    import openai as openai_mod
    captured = {}
    monkeypatch.setattr(openai_mod, "OpenAI",
                        lambda **kw: captured.update(kw) or SimpleNamespace(**kw))
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    get_client("gemini-2.0-flash")
    assert "generativelanguage.googleapis.com" in captured["base_url"]
    assert captured["api_key"] == "g-key"


def test_get_client_gemini_without_key_errors(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("CHECKPOINT_LLM_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        get_client("gemini-2.0-flash")


def test_missing_credential_says_what_to_do(monkeypatch):
    """The most common failure of a judged run must be self-explanatory."""
    import openai as openai_mod

    def _no_key(**kw):
        raise Exception("The api_key client option must be set")

    monkeypatch.delenv("CHECKPOINT_LLM_BASE_URL", raising=False)
    monkeypatch.setattr(openai_mod, "OpenAI", _no_key)
    with pytest.raises(LLMCredentialError) as excinfo:
        get_client("gpt-5.6-luna")
    message = str(excinfo.value)
    assert "OPENAI_API_KEY" in message
    assert "--model" in message            # the flag that judges with another provider
    assert "checkpoint.toml" in message    # ...and where to set it for good
    assert "CHECKPOINT_LLM_BASE_URL" in message


def test_base_url_override_forces_openai_compat(monkeypatch):
    import openai as openai_mod
    captured = {}
    monkeypatch.setattr(openai_mod, "OpenAI",
                        lambda **kw: captured.update(kw) or SimpleNamespace(**kw))
    monkeypatch.setenv("CHECKPOINT_LLM_BASE_URL", "http://localhost:1234/v1")
    get_client("claude-sonnet-5")  # even a claude name goes to the compat endpoint
    assert captured["base_url"] == "http://localhost:1234/v1"


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------

def _install_fake_anthropic(monkeypatch, capture: dict, text: str = '{"passed": true}'):
    class _Messages:
        def create(self, **kw):
            capture.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    class _Anthropic:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=_Anthropic))


def test_anthropic_adapter_uses_structured_outputs_not_a_prefill(monkeypatch):
    cap: dict = {}
    _install_fake_anthropic(monkeypatch, cap)
    monkeypatch.delenv("CHECKPOINT_LLM_BASE_URL", raising=False)
    client = get_client("anthropic:claude-sonnet-5")
    resp = client.chat.completions.create(
        model="anthropic:claude-sonnet-5",
        messages=[
            {"role": "system", "content": "You judge criteria."},
            {"role": "user", "content": "criterion X"},
        ],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "v", "strict": True,
                                         "schema": {"type": "object"}}},
    )
    assert cap["model"] == "claude-sonnet-5"        # prefix stripped
    assert cap["system"] == "You judge criteria."
    assert cap["messages"] == [{"role": "user", "content": "criterion X"}]
    assert cap["output_config"] == {"format": {"type": "json_schema",
                                               "schema": {"type": "object"}}}
    assert "temperature" not in cap                 # never sent unasked
    assert json.loads(resp.choices[0].message.content) == {"passed": True}


def test_anthropic_adapter_forwards_an_explicit_temperature(monkeypatch):
    cap: dict = {}
    _install_fake_anthropic(monkeypatch, cap)
    monkeypatch.delenv("CHECKPOINT_LLM_BASE_URL", raising=False)
    get_client("claude-sonnet-5").chat.completions.create(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
    )
    assert cap["temperature"] == 0.7
    assert "output_config" not in cap  # json_object has no Claude equivalent


# ---------------------------------------------------------------------------
# complete_json / complete_text
# ---------------------------------------------------------------------------

class FakeClient:
    """OpenAI-shaped client: replays responses or raises, and records every call."""

    def __init__(self, *responses, raises=None, raise_times: int = 0):
        self._responses = list(responses) or ["{}"]
        self._raises = raises
        self._raise_times = raise_times if raises is not None else 0
        self._unlimited = raises is not None and raise_times == 0
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls.append(kw)
        return self._respond(kw)

    def _respond(self, kw):
        if self._raises is not None and (self._unlimited or len(self.calls) <= self._raise_times):
            raise self._raises
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._responses[idx]))])


class _Status(Exception):
    """An SDK-shaped HTTP error: the classifier reads ``status_code``."""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(call_mod, "_sleep", lambda _s: None)


def test_complete_json_parses_and_strips_the_provider_prefix():
    client = FakeClient('{"ok": true}')
    assert complete_json(system="s", user="u", model="openai:gpt-5.6-luna",
                         client=client) == {"ok": True}
    assert client.calls[0]["model"] == "gpt-5.6-luna"


def test_temperature_is_absent_by_default_and_present_when_set():
    client = FakeClient('{"ok": true}')
    complete_json(system="s", user="u", model="m", client=client)
    assert "temperature" not in client.calls[0]

    complete_json(system="s", user="u", model="m", temperature=0.2, client=client)
    assert client.calls[1]["temperature"] == 0.2


def test_a_schema_becomes_a_strict_json_schema_request():
    client = FakeClient('{"ok": true}')
    complete_json(system="s", user="u", model="m", client=client,
                  schema={"type": "object"}, schema_name="thing")
    fmt = client.calls[0]["response_format"]
    assert fmt == {"type": "json_schema",
                   "json_schema": {"name": "thing", "schema": {"type": "object"},
                                   "strict": True}}


def test_no_schema_uses_json_mode():
    client = FakeClient('{"ok": true}')
    complete_json(system="s", user="u", model="m", client=client)
    assert client.calls[0]["response_format"] == {"type": "json_object"}


def test_a_mapping_payload_is_serialised_as_json():
    client = FakeClient('{"ok": true}')
    complete_json(system="s", user={"criterion": "x"}, model="m", client=client)
    body = next(m for m in client.calls[0]["messages"] if m["role"] == "user")
    assert json.loads(body["content"]) == {"criterion": "x"}


def test_complete_text_returns_stripped_prose_without_json_mode():
    client = FakeClient("  # A scenario\n")
    assert complete_text(system="s", user="u", model="m", client=client) == "# A scenario"
    assert "response_format" not in client.calls[0]


def test_samples_makes_k_independent_calls():
    client = FakeClient('{"n": 1}', '{"n": 2}', '{"n": 3}')
    out = complete_json(system="s", user="u", model="m", samples=3, client=client)
    assert out == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert len(client.calls) == 3


def test_malformed_and_empty_answers_raise_a_typed_error():
    with pytest.raises(LLMResponseError):
        complete_json(system="s", user="u", model="m", client=FakeClient("nope"))
    with pytest.raises(LLMResponseError, match="empty"):
        complete_json(system="s", user="u", model="m", client=FakeClient(""))


# -- retries ----------------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses_are_retried_then_succeed(status):
    client = FakeClient('{"ok": true}', raises=_Status(status), raise_times=2)
    assert complete_json(system="s", user="u", model="m", client=client) == {"ok": True}
    assert len(client.calls) == 3


def test_retries_stop_at_the_cap():
    client = FakeClient(raises=_Status(429))
    with pytest.raises(RuntimeError):
        complete_json(system="s", user="u", model="m", client=client, retries=3)
    assert len(client.calls) == 4  # the first attempt plus three retries


def test_timeouts_and_connection_errors_are_transient():
    class APITimeoutError(Exception):
        pass

    client = FakeClient('{"ok": true}', raises=APITimeoutError("timed out"), raise_times=1)
    complete_json(system="s", user="u", model="m", client=client)
    assert len(client.calls) == 2


def test_a_bad_request_is_not_retried():
    client = FakeClient(raises=_Status(400, "invalid model"))
    with pytest.raises(RuntimeError):
        complete_json(system="s", user="u", model="m", client=client)
    assert len(client.calls) == 1


def test_backoff_grows_and_is_jittered(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(call_mod, "_sleep", waits.append)
    monkeypatch.setattr(call_mod.random, "uniform", lambda lo, hi: hi)
    client = FakeClient('{"ok": true}', raises=_Status(500), raise_times=3)
    complete_json(system="s", user="u", model="m", client=client)
    assert waits == [0.5, 1.0, 2.0]  # the jitter window doubles, bounded by the cap


# -- degrading a rejected parameter -----------------------------------------

def test_a_rejected_schema_falls_back_to_json_mode_then_to_prompt_only():
    class _Picky(FakeClient):
        def _respond(self, kw):
            fmt = kw.get("response_format")
            if fmt and fmt["type"] == "json_schema":
                raise _Status(400, "Unsupported parameter: 'response_format.json_schema'")
            if fmt:
                raise _Status(400, "response_format is not supported by this endpoint")
            return super()._respond(kw)

    client = _Picky('{"ok": true}')
    assert complete_json(system="s", user="u", model="m", client=client,
                         schema={"type": "object"}) == {"ok": True}
    assert [c.get("response_format", {}).get("type") if c.get("response_format") else None
            for c in client.calls] == ["json_schema", "json_object", None]


def test_a_rejected_temperature_is_dropped_and_the_call_retried():
    class _Reasoning(FakeClient):
        def _respond(self, kw):
            if "temperature" in kw:
                raise _Status(400, "Unsupported value: 'temperature' does not support 0.7")
            return super()._respond(kw)

    client = _Reasoning('{"ok": true}')
    assert complete_json(system="s", user="u", model="m", temperature=0.7,
                         client=client) == {"ok": True}
    assert len(client.calls) == 2
    assert "temperature" not in client.calls[1]
