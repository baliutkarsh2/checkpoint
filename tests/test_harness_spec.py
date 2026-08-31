"""Tests for the v2 declarative harness spec (zero-code path)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from checkpoint.harness_spec import (
    DEFAULT_TASK_ENV,
    load_manifest,
    parse_inline,
    parse_manifest,
    template_manifest,
)


def test_parse_inline_env_default():
    s = parse_inline("python my_agent.py")
    assert s.argv == ["python", "my_agent.py"]
    assert s.task_via == "env"
    assert s.task_env == DEFAULT_TASK_ENV


def test_parse_inline_arg_mode():
    s = parse_inline("node agent.js", task_via="arg", task_arg="--prompt")
    assert s.argv == ["node", "agent.js"]
    assert s.task_via == "arg"
    assert s.task_arg == "--prompt"


def test_build_invocation_env():
    s = parse_inline("python my_agent.py")
    argv, env, stdin = s.build_invocation("do X")
    assert argv == ["python", "my_agent.py"]
    assert env["CHECKPOINT_TASK"] == "do X"
    assert stdin is None


def test_build_invocation_arg_with_flag():
    s = parse_inline("node agent.js", task_via="arg", task_arg="--prompt")
    argv, _, stdin = s.build_invocation("hello")
    assert argv == ["node", "agent.js", "--prompt", "hello"]
    assert stdin is None


def test_build_invocation_arg_no_flag_appends_value():
    s = parse_inline("./run.sh", task_via="arg")
    argv, _, _ = s.build_invocation("hi")
    assert argv == ["./run.sh", "hi"]


def test_build_invocation_stdin():
    s = parse_inline("python my_agent.py", task_via="stdin")
    argv, _, stdin = s.build_invocation("payload")
    assert argv == ["python", "my_agent.py"]
    assert stdin == "payload"


def test_parse_manifest_v2_command():
    s = parse_manifest({"command": "python my_agent.py", "task_via": "env"})
    assert s.argv == ["python", "my_agent.py"]
    assert s.task_via == "env"


def test_parse_manifest_v2_argv_list():
    s = parse_manifest({"argv": ["node", "agent.js"], "task_via": "stdin"})
    assert s.argv == ["node", "agent.js"]
    assert s.task_via == "stdin"


def test_parse_manifest_v1_path_legacy():
    """A bare {"path": "harness.py"} should still parse as python <path>."""
    s = parse_manifest({"path": "harness.py"})
    assert s.argv[-1] == "harness.py"
    assert s.task_via == "env"


def test_parse_manifest_docker_section():
    s = parse_manifest({
        "command": "python a.py",
        "docker": {"dockerfile": "./Dockerfile"},
    })
    assert s.is_docker()
    assert s.dockerfile == "./Dockerfile"


def test_parse_manifest_env_expansion(monkeypatch):
    monkeypatch.setenv("MY_KEY", "hunter2")
    s = parse_manifest({"command": "x", "env": {"FOO": "$MY_KEY"}})
    assert s.env["FOO"] == "hunter2"


def test_parse_manifest_rejects_bad_task_via():
    with pytest.raises(ValueError):
        parse_manifest({"command": "x", "task_via": "ftp"})


def test_parse_manifest_rejects_empty():
    with pytest.raises(ValueError):
        parse_manifest({})


def test_template_manifest_minimal():
    out = template_manifest(command="python a.py")
    assert out["command"] == "python a.py"
    # env-default fields should be omitted to keep the file tiny.
    assert "task_via" not in out
    assert "task_arg" not in out


def test_template_manifest_with_arg_and_docker():
    out = template_manifest(
        command="node x.js",
        task_via="arg",
        task_arg="--prompt",
        env={"K": "$K"},
        dockerfile="./Dockerfile",
        name="my-agent",
    )
    assert out["task_via"] == "arg"
    assert out["task_arg"] == "--prompt"
    assert out["docker"] == {"dockerfile": "./Dockerfile"}
    assert out["name"] == "my-agent"


def test_load_manifest_roundtrip(tmp_path):
    p = tmp_path / "harness.json"
    p.write_text(json.dumps({"command": "python my_agent.py", "name": "agent"}))
    s = load_manifest(p)
    assert s.argv == ["python", "my_agent.py"]
    assert s.name == "agent"
    assert s.source_path and Path(s.source_path) == p.resolve()
