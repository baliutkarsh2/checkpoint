"""Linear twin driven by @linear/sdk, the vendor's own (TypeScript) SDK.

Linear publishes no Python client, so the only way to prove the twin answers
the official SDK is to run it: this shells out to Node and drives the twin
through ``linear_sdk_probe.mjs``.

It skips unless Node and the SDK are both available:

    npm install @linear/sdk            # anywhere
    CHECKPOINT_NODE=/path/to/node \\
    CHECKPOINT_LINEAR_SDK_DIR=/dir/with/node_modules pytest tests/sdk
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

TWIN = "linear"

PROBE = Path(__file__).parent / "linear_sdk_probe.mjs"


def _node() -> str | None:
    return os.environ.get("CHECKPOINT_NODE") or shutil.which("node")


def _sdk_module(node: str) -> str | None:
    """The resolved @linear/sdk entry point, or None if it is not installed."""
    directory = os.environ.get("CHECKPOINT_LINEAR_SDK_DIR") or os.getcwd()
    result = subprocess.run(
        [node, "--input-type=module", "-e", "console.log(import.meta.resolve('@linear/sdk'))"],
        cwd=directory, capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip() if result.returncode == 0 else None


@pytest.fixture(scope="module")
def sdk_probe():
    node = _node()
    if not node:
        pytest.skip("node is not available (set CHECKPOINT_NODE)")
    module = _sdk_module(node)
    if not module:
        pytest.skip("@linear/sdk is not installed (set CHECKPOINT_LINEAR_SDK_DIR)")
    return node, module


@pytest.mark.slow
def test_official_typescript_sdk_round_trip(twin, sdk_probe):
    node, module = sdk_probe
    twin.seed("small-project")
    result = subprocess.run(
        [node, str(PROBE)],
        env={**os.environ, "LINEAR_SDK_MODULE": module, "LINEAR_API_KEY": twin.token,
             "LINEAR_API_URL": twin.url},
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert out["viewer"]["email"]
    assert out["team"]["key"] == "ENG"
    assert set(out["issues"]) <= {"ENG-1", "ENG-2", "ENG-3", "ENG-42"}
    assert out["issue"] == {"identifier": "ENG-2", "title": "Login page crashes on mobile",
                            "state": "In Progress", "assignee": "Bob Smith"}
    assert out["created"] == {"success": True, "identifier": "ENG-43", "priorityLabel": "High"}
    assert out["comments"] == ["SDK comment"]
    assert out["updated"]["state"] == "Done"
    assert "ENG-43" in out["completed"]
    assert "ENG-43" not in out["afterArchive"]
    # The SDK parses Linear's error envelope into a typed LinearError.
    assert out["error"]["type"] == "InvalidInput"
    assert out["error"]["message"] == "Entity not found: Issue - Could not find referenced Issue."

    issues = twin.views()["issues"]["items"]
    filed = next(i for i in issues if i["identifier"] == "ENG-43")
    assert filed["title"] == "Filed by @linear/sdk" and filed["archivedAt"]
