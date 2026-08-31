"""Vendor-neutral LLM resolution + the Anthropic adapter."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from checkpoint.llm import get_client, provider_for
from checkpoint.llm.resolve import bare_model


def test_provider_inference():
    assert provider_for("gpt-4o-mini") == "openai"
    assert provider_for("o3-mini") == "openai"
    assert provider_for("claude-3-5-sonnet-latest") == "anthropic"
    assert provider_for("gemini-2.0-flash") == "gemini"
    assert provider_for("anthropic:claude-3-5-sonnet") == "anthropic"
    assert provider_for("google:gemini-2.0") == "gemini"
    assert provider_for("local:mixtral") == "compat"
    assert provider_for(None) == "openai"
    assert provider_for("") == "openai"


def test_bare_model_strips_prefix():
    assert bare_model("anthropic:claude-3-5-sonnet") == "claude-3-5-sonnet"
    assert bare_model("gpt-4o-mini") == "gpt-4o-mini"


def test_get_client_openai_uses_openai_sdk(monkeypatch):
    import openai as openai_mod
    sentinel = object()
    monkeypatch.setattr(openai_mod, "OpenAI", lambda **kw: sentinel)
    assert get_client("gpt-4o-mini") is sentinel


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
    with pytest.raises(RuntimeError):
        get_client("gemini-2.0-flash")


def test_base_url_override_forces_openai_compat(monkeypatch):
    import openai as openai_mod
    captured = {}
    monkeypatch.setattr(openai_mod, "OpenAI",
                        lambda **kw: captured.update(kw) or SimpleNamespace(**kw))
    monkeypatch.setenv("CHECKPOINT_LLM_BASE_URL", "http://localhost:1234/v1")
    get_client("claude-3-5-sonnet")  # even a claude name goes to the compat endpoint
    assert captured["base_url"] == "http://localhost:1234/v1"


def _install_fake_anthropic(monkeypatch, capture: dict):
    class _Messages:
        def create(self, **kw):
            capture.update(kw)
            # Return an Anthropic-shaped response continuing the JSON prefill.
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=' "passed": true}')])

    class _Anthropic:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    fake = SimpleNamespace(Anthropic=_Anthropic)
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def test_anthropic_adapter_translates_and_shapes(monkeypatch):
    cap: dict = {}
    _install_fake_anthropic(monkeypatch, cap)
    client = get_client("claude-3-5-sonnet-latest")
    resp = client.chat.completions.create(
        model="claude-3-5-sonnet-latest",
        messages=[
            {"role": "system", "content": "You judge criteria."},
            {"role": "user", "content": "criterion X"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    # System split out; user + JSON prefill assistant turn passed to Anthropic.
    assert cap["system"] == "You judge criteria."
    assert cap["messages"][-1] == {"role": "assistant", "content": "{"}
    # OpenAI-shaped response with a parseable JSON object (prefix restored).
    content = resp.choices[0].message.content
    assert json.loads(content) == {"passed": True}
