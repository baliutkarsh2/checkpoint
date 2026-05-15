"""Sanity checks for the bundled example agents.

These agents exist for customers to copy. If a file is missing or doesn't
compile, the customer will hit a confusing failure 5 minutes into their
first run. Catch it at PR time instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO / "examples" / "agents"

AGENTS = ["openai-tools", "anthropic-tools", "langchain-react", "mcp-client"]


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_has_required_files(agent):
    d = AGENTS_DIR / agent
    for fname in ("harness.py", "Dockerfile", "entrypoint.sh", "requirements.txt", "README.md"):
        assert (d / fname).is_file(), f"{agent}/{fname} missing"


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_harness_parses(agent):
    """Harness must be valid Python — catches missing imports / typos."""
    src = (AGENTS_DIR / agent / "harness.py").read_text(encoding="utf-8")
    ast.parse(src)


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_entrypoint_has_lf_line_endings(agent):
    """Docker on Linux can't run scripts with CRLF — must be LF only.

    This is the bug that bit us in the original sidecar. Catch it on the
    bundled agents so customers don't hit it.
    """
    raw = (AGENTS_DIR / agent / "entrypoint.sh").read_bytes()
    assert b"\r\n" not in raw, (
        f"{agent}/entrypoint.sh has CRLF line endings; will fail to exec on Linux"
    )


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_entrypoint_has_shebang(agent):
    raw = (AGENTS_DIR / agent / "entrypoint.sh").read_bytes()
    assert raw.startswith(b"#!"), f"{agent}/entrypoint.sh missing shebang"


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_dockerfile_runs_entrypoint(agent):
    """Catch the classic mistake of forgetting `chmod +x` or wrong CMD."""
    df = (AGENTS_DIR / agent / "Dockerfile").read_text(encoding="utf-8")
    assert "entrypoint.sh" in df, f"{agent}/Dockerfile doesn't reference entrypoint.sh"
    assert "chmod +x" in df, f"{agent}/Dockerfile doesn't chmod +x entrypoint.sh"


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_readme_documents_run_command(agent):
    rdme = (AGENTS_DIR / agent / "README.md").read_text(encoding="utf-8")
    assert "checkpoint run" in rdme, f"{agent}/README.md missing example invocation"
    assert "--harness-dir" in rdme, f"{agent}/README.md should show --harness-dir usage"
