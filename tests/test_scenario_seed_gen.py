"""Phase 4 plan 03 — seeds-from-English (SCN-08).

Unit tests for `checkpoint.scenario_seed_gen`. Uses a fake OpenAI client so
we never hit the network. The cache-hit path is fully testable without any
client at all.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from checkpoint import scenario_seed_gen as sgs


def _fake_client(content: str, call_counter: list[int]):
    """Build a minimal stand-in for `openai.OpenAI()`.

    `call_counter` is mutated each time .create() is invoked so tests can
    assert "exactly one LLM call" / "zero LLM calls".
    """
    class _Chat:
        class _Completions:
            def create(_self, **kwargs):
                call_counter.append(1)
                msg = SimpleNamespace(content=content)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        completions = _Completions()
    return SimpleNamespace(chat=_Chat())


SAMPLE_GITHUB_STATE = {
    "repos": {"acme/web": {"name": "web", "owner": "acme"}},
    "issues": {"1": {"number": 1, "title": "existing", "state": "open"}},
    "_config": {"rate_limit": None},
    "_counters": {"issue": 1},
}


def test_cache_key_stable_across_whitespace():
    a = sgs.cache_key("github", "Hello\nworld")
    b = sgs.cache_key("github", "Hello world")
    assert a == b


def test_cache_key_differs_per_clone():
    a = sgs.cache_key("github", "x")
    b = sgs.cache_key("slack", "x")
    assert a != b


def test_state_schema_sample_drops_private_keys():
    out = sgs._state_schema_sample(SAMPLE_GITHUB_STATE)
    assert "_config" not in out
    assert "_counters" not in out
    assert "repos" in out and "issues" in out


def test_generate_seed_calls_llm_once(tmp_path: Path):
    calls: list[int] = []
    seed_payload = {"state": {"issues": {"99": {"number": 99, "title": "From LLM", "state": "open"}}}}
    client = _fake_client(json.dumps(seed_payload), calls)
    out = sgs.generate_seed("github", "A repo with one issue", SAMPLE_GITHUB_STATE,
                            client=client, cache_root=tmp_path)
    assert len(calls) == 1
    assert out["state"]["issues"]["99"]["title"] == "From LLM"


def test_cache_hit_returns_zero_llm_calls(tmp_path: Path):
    # Prime the cache with one call.
    calls: list[int] = []
    seed_payload = {"state": {"issues": {"99": {"number": 99, "title": "Cached", "state": "open"}}}}
    client = _fake_client(json.dumps(seed_payload), calls)
    sgs.generate_seed("github", "setup text", SAMPLE_GITHUB_STATE,
                      client=client, cache_root=tmp_path)
    assert len(calls) == 1
    # Second call with same args -> no new client invocation.
    out2 = sgs.generate_seed("github", "setup text", SAMPLE_GITHUB_STATE,
                             client=client, cache_root=tmp_path)
    assert len(calls) == 1, "cache miss: LLM was called a second time"
    assert out2["state"]["issues"]["99"]["title"] == "Cached"


def test_cache_per_clone(tmp_path: Path):
    """github and slack seeds for the same setup_text get cached separately."""
    calls: list[int] = []
    seed = {"state": {"x": {}}}
    client = _fake_client(json.dumps(seed), calls)
    sgs.generate_seed("github", "same prose", {}, client=client, cache_root=tmp_path)
    sgs.generate_seed("slack", "same prose", {}, client=client, cache_root=tmp_path)
    assert len(calls) == 2  # cache key includes clone


def test_normalize_unwrapped_state(tmp_path: Path):
    """If the model returns raw state (no `state` key), we wrap it."""
    calls: list[int] = []
    client = _fake_client(json.dumps({"issues": {"7": {"title": "raw"}}}), calls)
    out = sgs.generate_seed("github", "x", {}, client=client, cache_root=tmp_path)
    assert "state" in out
    assert out["state"]["issues"]["7"]["title"] == "raw"


def test_private_keys_stripped_from_model_output(tmp_path: Path):
    calls: list[int] = []
    payload = {"state": {"issues": {}, "_config": {"evil": True}, "_counters": {"n": 1}}}
    client = _fake_client(json.dumps(payload), calls)
    out = sgs.generate_seed("github", "x", {}, client=client, cache_root=tmp_path)
    assert "_config" not in out["state"]
    assert "_counters" not in out["state"]


def test_missing_api_key_raises(monkeypatch, tmp_path: Path):
    """No client supplied + no OPENAI_API_KEY -> RuntimeError (soft-fail path)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        sgs.generate_seed("github", "setup text", {}, cache_root=tmp_path)


def test_runner_soft_fail_when_setup_without_key(monkeypatch, tmp_path: Path):
    """A scenario with `## Setup` text but no OPENAI_API_KEY and no explicit
    seed should still complete the run — the twin keeps its default state."""
    import sys, textwrap
    from checkpoint.runner import run_once
    from checkpoint.scenario import Scenario

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # so .checkpoint/cache/ lands in tmp

    harness = tmp_path / "h.py"
    harness.write_text(textwrap.dedent("""
        import json, sys
        sys.stdout.write(json.dumps({"text": "ok"}))
    """).strip())

    s = Scenario(
        prompt="ok",
        setup="A repo with two open issues.",
        config={"clones": "github", "timeout": "30"},
    )
    r = run_once(s, [sys.executable, str(harness)])
    # Soft-fall-through: run completes; twin state stays fresh (no issues).
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"


def test_runner_uses_cached_seed(monkeypatch, tmp_path: Path):
    """If the seed cache already has an entry for (clone, setup_text), the
    runner uses it without needing OPENAI_API_KEY."""
    import sys, textwrap
    from checkpoint.runner import run_once
    from checkpoint.scenario import Scenario

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    setup_text = "Two issues exist; both open."
    # Pre-seed the cache (mimicking a prior successful LLM call).
    cached = {"state": {"issues": {
        "1": {"number": 1, "title": "From cache", "state": "open", "labels": []},
        "2": {"number": 2, "title": "Another cached", "state": "open", "labels": []},
    }}}
    sgs.save_cached("github", setup_text, cached, root=tmp_path)

    harness = tmp_path / "h.py"
    harness.write_text(textwrap.dedent("""
        import json, sys
        sys.stdout.write(json.dumps({"text": "ok"}))
    """).strip())

    s = Scenario(
        prompt="ok",
        setup=setup_text,
        config={"clones": "github", "timeout": "30"},
    )
    r = run_once(s, [sys.executable, str(harness)])
    assert r.complete, f"runner failed: {r.error} / {r.stderr}"
    issues = r.state.get("issues") or {}
    assert any(i.get("title") == "From cache" for i in issues.values()), \
        f"cache wasn't applied; got issues={issues}"
