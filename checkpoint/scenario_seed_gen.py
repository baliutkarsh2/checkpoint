"""Generate twin seeds from `## Setup` plain-English prose (SCN-08).

One LLM call per (setup_text, clone) pair, cached at
`.checkpoint/cache/seeds/<sha256>.json` so the second run does zero LLM work.

The output is the same shape the twins' `/_seed-file` endpoint accepts:
`{"state": {<twin state keys>}, "config": {...}}`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("checkpoint.scenario_seed_gen")


SYSTEM_PROMPT = """You generate JSON seed data for a synthetic SaaS twin (one of: github, slack, stripe).
You will be given:
- A plain-English description of the desired starting world.
- The clone ID (github/slack/stripe).
- A real sample of the twin's current state (so you know exactly which keys
  exist and what shape each entry takes).

Return STRICT JSON with this shape:
{
  "state": { <state keys matching the schema sample> }
}

Rules:
- Use ONLY the top-level state keys present in the schema sample.
- Match the shape of each entry exactly (same keys, same value types).
- Generate plausible data that matches the description.
- Do NOT include `_config`, `_counters`, or any key starting with `_`.
- Keep the seed compact: a handful of entries per key is enough.
"""


def cache_dir(root: Path | None = None) -> Path:
    """Return the seed cache directory, creating it if needed."""
    root = root or Path.cwd()
    p = root / ".checkpoint" / "cache" / "seeds"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_key(clone: str, setup_text: str) -> str:
    """Stable cache key for (clone, setup_text). Whitespace-normalized."""
    norm = " ".join((setup_text or "").split())
    h = hashlib.sha256(f"{clone}\n{norm}".encode()).hexdigest()
    return h


def _cache_path(clone: str, setup_text: str, root: Path | None = None) -> Path:
    return cache_dir(root) / f"{cache_key(clone, setup_text)}.json"


def load_cached(clone: str, setup_text: str, root: Path | None = None) -> dict | None:
    p = _cache_path(clone, setup_text, root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_cached(clone: str, setup_text: str, seed: dict, root: Path | None = None) -> None:
    p = _cache_path(clone, setup_text, root)
    try:
        p.write_text(json.dumps(seed, indent=2))
    except OSError as e:
        log.warning("could not write seed cache to %s: %s", p, e)


def _state_schema_sample(twin_state: dict) -> dict:
    """Trim a /_state response into a structural sample for the LLM prompt.

    Keep up to 2 entries per dict key, drop keys starting with `_`. Keeps
    shapes intact while bounding token usage.
    """
    out: dict[str, Any] = {}
    for k, v in twin_state.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            # Truncate to first 2 entries.
            items = list(v.items())[:2]
            out[k] = dict(items)
        elif isinstance(v, list):
            out[k] = v[:2]
        else:
            out[k] = v
    return out


def generate_seed(
    clone: str,
    setup_text: str,
    twin_state: dict,
    *,
    model: str = "gpt-4o-mini",
    client: Any | None = None,
    cache_root: Path | None = None,
) -> dict:
    """Return a seed payload (`{"state": {...}}`) for the given clone.

    Cache hits short-circuit before any OpenAI client is constructed.
    Raises `RuntimeError` if `OPENAI_API_KEY` is unset and no `client` was
    supplied (callers should treat that as a soft-fall-through: just skip).
    """
    cached = load_cached(clone, setup_text, cache_root)
    if cached is not None:
        log.debug("seed cache hit for clone=%s", clone)
        return cached

    if client is None:
        from .llm import get_client, provider_for
        if provider_for(model) == "openai" and not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY not set; cannot generate seed from `## Setup` text."
            )
        client = get_client(model)

    schema_sample = _state_schema_sample(twin_state)
    user_payload = {
        "clone": clone,
        "setup": setup_text,
        "state_schema_sample": schema_sample,
    }
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw)

    # Normalize: tolerate models that return raw state at the top level.
    if "state" not in parsed:
        parsed = {"state": parsed}
    # Strip any private keys the model invented.
    state = parsed.get("state") or {}
    parsed["state"] = {k: v for k, v in state.items() if not k.startswith("_")}

    save_cached(clone, setup_text, parsed, cache_root)
    return parsed
