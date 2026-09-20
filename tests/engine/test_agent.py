"""Starting the agent under test: command parsing, task delivery, answers, timeouts."""
from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

from checkpoint.engine.agent import (
    Agent,
    _split_windows,
    extract_answer,
    split_command,
)

PY = sys.executable


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "agent.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


# -- command splitting ---------------------------------------------------------

def test_windows_split_keeps_backslashes_and_groups_quotes():
    assert _split_windows(r'C:\Python\python.exe "C:\my agents\a.py" --x 1') == [
        r"C:\Python\python.exe", r"C:\my agents\a.py", "--x", "1",
    ]
    assert _split_windows(r'a "b \"c\" d" e\f') == ["a", 'b "c" d', r"e\f"]
    assert _split_windows('""') == [""]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX quoting rules")
def test_posix_split_uses_shell_rules():
    assert split_command("python 'my agent.py' --task") == ["python", "my agent.py", "--task"]


# -- answers -------------------------------------------------------------------

@pytest.mark.parametrize(("stdout", "answer"), [
    ("", ""),
    ("plain answer", "plain answer"),
    ('{"text": "done"}', "done"),
    ('{"answer": "42"}', "42"),
    ('{"output": {"content": "nested"}}', "nested"),
    ('log line\n{"text": "final"}\n', "final"),
    ('{"choices": [{"message": {"content": "openai-style"}}]}', "openai-style"),
    ('"a json string"', "a json string"),
    ('{"unrelated": 1}', '{"unrelated": 1}'),
])
def test_extract_answer(stdout, answer):
    assert extract_answer(stdout) == answer


# -- task delivery ---------------------------------------------------------------

def test_task_via_env(tmp_path):
    agent = Agent(command=[PY, _script(tmp_path, """
        import os; print(os.environ["CHECKPOINT_TASK"].upper())
    """)])
    out = agent.invoke("hello", {"PATH": "", "SYSTEMROOT": _sysroot()}, timeout=30)
    assert out.ok and out.answer == "HELLO"


def test_task_via_custom_env_var(tmp_path):
    agent = Agent(command=[PY, _script(tmp_path, """
        import os; print(os.environ["MY_PROMPT"])
    """)], task_env="MY_PROMPT")
    assert agent.invoke("custom", _env(), timeout=30).answer == "custom"


def test_task_via_arg_with_flag(tmp_path):
    agent = Agent(command=[PY, _script(tmp_path, """
        import json, sys; print(json.dumps(sys.argv[1:]))
    """)], task_via="arg", task_arg="--prompt")
    out = agent.invoke("do it", _env(), timeout=30)
    assert json.loads(out.answer) == ["--prompt", "do it"]


def test_task_via_stdin(tmp_path):
    agent = Agent(command=[PY, _script(tmp_path, """
        import sys; print(sys.stdin.read().strip()[::-1])
    """)], task_via="stdin")
    assert agent.invoke("abc", _env(), timeout=30).answer == "cba"


def test_answer_file_wins_over_noisy_stdout(tmp_path):
    agent = Agent(command=[PY, _script(tmp_path, """
        import os
        print("lots of logging on stdout")
        open(os.environ["CHECKPOINT_ANSWER_FILE"], "w").write("the real answer")
    """)])
    assert agent.invoke("x", _env(), timeout=30).answer == "the real answer"


def test_messages_are_passed_as_json(tmp_path):
    agent = Agent(command=[PY, _script(tmp_path, """
        import json, os; print(len(json.loads(os.environ["CHECKPOINT_MESSAGES"])))
    """)])
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert agent.invoke("x", _env(), timeout=30, messages=msgs).answer == "2"


# -- failures ------------------------------------------------------------------------

def test_missing_command_is_reported_not_raised():
    out = Agent(command=["definitely-not-a-real-binary-xyz"]).invoke("x", _env(), timeout=10)
    assert not out.ok and "not found" in out.error


def test_nonzero_exit_is_captured(tmp_path):
    out = Agent(command=[PY, _script(tmp_path, """
        import sys; print("partial", file=sys.stderr); sys.exit(3)
    """)]).invoke("x", _env(), timeout=30)
    assert out.exit_code == 3 and not out.ok and "partial" in out.stderr


def test_timeout_kills_the_whole_process_tree(tmp_path):
    marker = tmp_path / "child-alive"
    child = _script(tmp_path, f"""
        import time, pathlib
        time.sleep(3)
        pathlib.Path({str(marker)!r}).write_text("still running")
    """)
    parent = tmp_path / "parent.py"
    parent.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, {child!r}])
        time.sleep(60)
    """), encoding="utf-8")
    started = time.perf_counter()
    out = Agent(command=[PY, str(parent)]).invoke("x", _env(), timeout=1)
    assert out.timed_out and out.exit_code is None
    assert time.perf_counter() - started < 20
    time.sleep(4)
    assert not marker.exists(), "the agent's child process outlived the timeout"


def test_agent_requires_command_or_url():
    with pytest.raises(ValueError):
        Agent()
    with pytest.raises(ValueError):
        Agent(command="x", task_via="carrier-pigeon")  # type: ignore[arg-type]


def test_display_name_prefers_script_stem():
    assert Agent(command="python agents/support_bot.py --fast").display_name == "support_bot"
    assert Agent(command=["node", "dist/main.js"]).display_name == "main"
    assert Agent(url="http://127.0.0.1:9/chat").display_name == "http://127.0.0.1:9/chat"


# -- HTTP agents -----------------------------------------------------------------------

def test_http_agent_round_trip():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            payload = json.dumps({"choices": [{"message": {"content": f"echo: {body['task']}"}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        agent = Agent(url=f"http://127.0.0.1:{server.server_address[1]}/chat")
        out = agent.invoke("ping", {}, timeout=10, session_id="s1")
        assert out.ok and out.answer == "echo: ping"
        assert seen[0]["session_id"] == "s1"
        assert seen[0]["messages"] == [{"role": "user", "content": "ping"}]
    finally:
        server.shutdown()


def test_http_agent_unreachable_is_an_error():
    out = Agent(url="http://127.0.0.1:9/chat").invoke("x", {}, timeout=5)
    assert not out.ok and "could not reach agent" in out.error


def _sysroot() -> str:
    import os
    return os.environ.get("SYSTEMROOT", "")


def _env() -> dict[str, str]:
    import os
    return dict(os.environ)


def test_bare_command_resolves_against_the_agents_path(tmp_path):
    # On Windows, CreateProcess looks in the *parent's* directory first, so a bare
    # "python" would run Checkpoint's interpreter instead of the agent's venv.
    # Resolution must use the PATH the agent is given.
    import os
    import stat

    bindir = tmp_path / "venv-bin"
    bindir.mkdir()
    if sys.platform == "win32":
        (bindir / "which-agent.bat").write_text("@echo resolved-from-agent-path\r\n", encoding="utf-8")
    else:
        tool = bindir / "which-agent"
        tool.write_text("#!/bin/sh\necho resolved-from-agent-path\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
    out = Agent(command=["which-agent"]).invoke("x", env, timeout=60)
    assert out.ok, out
    assert out.answer == "resolved-from-agent-path"
