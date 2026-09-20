"""The agent under test, and how to invoke it.

Checkpoint treats an agent as a black box it can start: any command in any
language, or an HTTP endpoint. It never imports or modifies the agent's code.

Command agents receive the task one of three ways (``task_via``):

    env    the task is in ``$CHECKPOINT_TASK`` (or ``task_env``)       [default]
    arg    the task is appended to the command (after ``task_arg`` if set)
    stdin  the task is written to the agent's stdin

and report their final answer on stdout (plain text, or JSON ``{"text": ...}``),
or — for agents that log to stdout — by writing it to ``$CHECKPOINT_ANSWER_FILE``.

An agent that wants its own reasoning shown alongside the result can append
JSON lines to ``$CHECKPOINT_AGENT_TRACE_FILE``: one object per event, with
whatever shape it already produces. Checkpoint stores them with the run and the
dashboard renders the messages and tool calls it recognizes. Writing nothing
there costs nothing; the twins' request log is captured either way.

HTTP agents receive ``POST {url}`` with ``{"task", "messages", "session_id"}``
and answer with text or JSON. OpenAI-compatible chat responses
(``choices[0].message.content``) are understood as well.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

TaskVia = Literal["env", "arg", "stdin"]

DEFAULT_TASK_ENV = "CHECKPOINT_TASK"
ANSWER_FILE_ENV = "CHECKPOINT_ANSWER_FILE"
TRACE_FILE_ENV = "CHECKPOINT_AGENT_TRACE_FILE"
MESSAGES_ENV = "CHECKPOINT_MESSAGES"
_MAX_CAPTURE = 8 * 1024 * 1024  # bytes of stdout/stderr kept per run

# Keys an agent's JSON output may use for its final answer, in priority order.
_ANSWER_KEYS = ("text", "answer", "output", "final_answer", "response", "content", "result")

LineSink = Callable[[str, str], None]  # (stream, line)


@dataclass
class AgentOutput:
    answer: str
    stdout: str
    stderr: str
    exit_code: int | None
    duration_s: float
    trace: list = field(default_factory=list)
    """What the agent said about itself, if it wrote to its trace file."""
    timed_out: bool = False
    error: str | None = None
    """Set when the agent could not be run at all (bad command, refused connection)."""

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0


@dataclass(frozen=True)
class Agent:
    """How to start the agent under test."""

    command: str | Sequence[str] = ()
    """Shell-style command string or argv list. Empty for a pure HTTP agent."""
    url: str | None = None
    """HTTP endpoint that answers tasks. With ``command`` set too, Checkpoint
    starts the command (inside the sandbox) and then talks to this URL."""
    task_via: TaskVia = "env"
    task_env: str = DEFAULT_TASK_ENV
    task_arg: str | None = None
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    name: str = ""
    ready_timeout: float = 60.0
    """Seconds to wait for a served agent's URL to accept requests."""

    def __post_init__(self) -> None:
        if not self.command and not self.url:
            raise ValueError("an agent needs a command, a url, or both")
        if self.task_via not in ("env", "arg", "stdin"):
            raise ValueError(f"task_via must be env, arg or stdin, not {self.task_via!r}")

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.command:
            argv = self.argv()
            for token in argv:
                if token.endswith((".py", ".js", ".ts", ".mjs", ".rb", ".go")):
                    return Path(token).stem
            return Path(argv[0]).stem if argv else "agent"
        return self.url or "agent"

    @property
    def served(self) -> bool:
        """A long-running service: started once, then sent tasks over HTTP."""
        return bool(self.url)

    def argv(self) -> list[str]:
        if isinstance(self.command, str):
            return split_command(self.command)
        return list(self.command)

    # -- one-shot command agents -------------------------------------------------

    def invoke(
        self,
        task: str,
        env: Mapping[str, str],
        timeout: float,
        *,
        messages: Sequence[Mapping[str, str]] | None = None,
        session_id: str | None = None,
        on_line: LineSink | None = None,
    ) -> AgentOutput:
        """Run the agent once on ``task`` and collect its answer.

        ``messages`` is the conversation so far (multi-turn runs); command agents
        receive it as JSON in ``$CHECKPOINT_MESSAGES``, HTTP agents in the body.
        """
        if self.served:
            return self._post(task, messages, session_id, timeout)
        argv = self.argv()
        if not argv:
            return AgentOutput("", "", "", None, 0.0, error="empty agent command")
        run_env = {**env, **self.env}
        stdin_data: str | None = None
        if self.task_via == "env":
            run_env[self.task_env] = task
            run_env[DEFAULT_TASK_ENV] = task
        elif self.task_via == "arg":
            argv = [*argv, *([self.task_arg] if self.task_arg else []), task]
        else:
            stdin_data = task
        if messages is not None:
            run_env[MESSAGES_ENV] = json.dumps(list(messages))
        with tempfile.TemporaryDirectory(prefix="checkpoint-agent-") as tmp:
            answer_file = Path(tmp) / "answer"
            trace_file = Path(tmp) / "trace.jsonl"
            run_env[ANSWER_FILE_ENV] = str(answer_file)
            run_env[TRACE_FILE_ENV] = str(trace_file)
            result = run_process(argv, env=run_env, cwd=self.cwd, timeout=timeout,
                                 stdin=stdin_data, on_line=on_line)
            if answer_file.is_file():
                result.answer = answer_file.read_text(encoding="utf-8", errors="replace").strip()
            else:
                result.answer = extract_answer(result.stdout)
            result.trace = read_agent_trace(trace_file)
        return result

    # -- HTTP agents -----------------------------------------------------------

    def _post(self, task: str, messages: Sequence[Mapping[str, str]] | None,
              session_id: str | None, timeout: float) -> AgentOutput:
        import httpx

        msgs = list(messages) if messages is not None else [{"role": "user", "content": task}]
        payload = {"task": task, "messages": msgs, "session_id": session_id}
        started = time.perf_counter()
        try:
            response = httpx.post(self.url, json=payload, timeout=timeout)  # type: ignore[arg-type]
        except httpx.TimeoutException:
            return AgentOutput("", "", "", None, time.perf_counter() - started, timed_out=True)
        except httpx.HTTPError as e:
            return AgentOutput("", "", "", None, time.perf_counter() - started,
                               error=f"could not reach agent at {self.url}: {e}")
        elapsed = time.perf_counter() - started
        body = response.text
        code = 0 if response.is_success else response.status_code
        return AgentOutput(extract_answer(body), body, "", code, elapsed,
                           error=None if response.is_success else f"agent returned HTTP {response.status_code}")


# --- helpers ------------------------------------------------------------------

def split_command(command: str) -> list[str]:
    """Split a command line the way the platform's shell would.

    POSIX uses shell quoting rules. Windows has no argv-level quoting in the
    shell; programs parse their own command line, so splitting with POSIX rules
    would eat backslashes in paths. There we use the MS C runtime convention.
    """
    if sys.platform != "win32":
        return shlex.split(command)
    return _split_windows(command)


def _split_windows(command: str) -> list[str]:
    # CommandLineToArgvW rules: whitespace separates args; double quotes group;
    # backslashes are literal unless they precede a double quote.
    args: list[str] = []
    buf: list[str] = []
    in_quotes = False
    has_token = False
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c == "\\":
            j = i
            while j < n and command[j] == "\\":
                j += 1
            count = j - i
            if j < n and command[j] == '"':
                buf.append("\\" * (count // 2))
                if count % 2:
                    buf.append('"')
                    i = j + 1
                else:
                    i = j
                has_token = True
                continue
            buf.append("\\" * count)
            has_token = True
            i = j
            continue
        if c == '"':
            in_quotes = not in_quotes
            has_token = True
        elif c in " \t" and not in_quotes:
            if has_token:
                args.append("".join(buf))
                buf, has_token = [], False
        else:
            buf.append(c)
            has_token = True
        i += 1
    if has_token:
        args.append("".join(buf))
    return args


def read_agent_trace(path: Path) -> list:
    """Whatever the agent wrote about itself, as a list of events.

    Deliberately forgiving: this is an optional courtesy from the agent, and a
    malformed line in it must never turn a good run into a failed one. A file
    that is one JSON array is read as that array; otherwise each parseable line
    is an event and the rest are skipped.
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, list):
            return whole
    events = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def extract_answer(stdout: str) -> str:
    """Pull the agent's final answer out of its stdout.

    Prefers a JSON object with a known answer key — either the whole output or
    its last JSON line (agents often log first, answer last) — and falls back
    to the full text.
    """
    text = stdout.strip()
    if not text:
        return ""
    candidates = [text] + [ln.strip() for ln in reversed(text.splitlines()) if ln.strip().startswith("{")]
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        answer = _answer_from_json(obj)
        if answer is not None:
            return answer
    return text


def _answer_from_json(obj: object) -> str | None:
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return None
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    for key in _ANSWER_KEYS:
        value = obj.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("content"), str):
            return value["content"]
    return None


def run_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    timeout: float,
    stdin: str | None = None,
    on_line: LineSink | None = None,
) -> AgentOutput:
    """Run ``argv`` to completion (or timeout), killing its whole process tree.

    Agents routinely spawn children (a node CLI, a browser, a tool server); on
    timeout those must die too, or they keep mutating the sandbox and leak.
    """
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    argv = [_resolve_executable(argv[0], env, cwd), *argv[1:]]
    started = time.perf_counter()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=dict(env),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
        )
    except FileNotFoundError:
        return AgentOutput("", "", "", None, 0.0,
                           error=f"agent command not found: {argv[0]!r} (is it installed and on PATH?)")
    except OSError as e:
        return AgentOutput("", "", "", None, 0.0, error=f"could not start agent: {e}")

    out = _Collector("stdout", proc.stdout, on_line)
    err = _Collector("stderr", proc.stderr, on_line)
    if stdin is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
    out.join()
    err.join()
    return AgentOutput(
        answer="",
        stdout=out.text(),
        stderr=err.text(),
        exit_code=None if timed_out else proc.returncode,
        duration_s=time.perf_counter() - started,
        timed_out=timed_out,
    )


def _resolve_executable(program: str, env: Mapping[str, str], cwd: str | None) -> str:
    """Find a bare program name on the *agent's* PATH.

    Windows' CreateProcess searches the parent process's directory before PATH,
    so ``python`` would silently run Checkpoint's own interpreter instead of the
    agent's virtualenv. Resolving against the agent's PATH first avoids that.
    """
    if os.sep in program or (os.altsep and os.altsep in program):
        return program
    path = env.get("PATH") or env.get("Path") or os.environ.get("PATH", "")
    found = shutil.which(program, path=path)
    if found is None and cwd:
        found = shutil.which(program, path=cwd)
    return found or program


def kill_tree(proc: subprocess.Popen) -> None:
    """Terminate a process and every descendant."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


class _Collector:
    """Drain a pipe on a thread: keep the output (capped) and optionally echo lines."""

    def __init__(self, name: str, pipe, sink: LineSink | None) -> None:  # type: ignore[no-untyped-def]
        self._chunks: list[bytes] = []
        self._size = 0
        self._truncated = False
        self._thread = threading.Thread(target=self._run, args=(name, pipe, sink), daemon=True)
        self._thread.start()

    def _run(self, name: str, pipe, sink: LineSink | None) -> None:  # type: ignore[no-untyped-def]
        for raw in iter(pipe.readline, b""):
            if self._size < _MAX_CAPTURE:
                self._chunks.append(raw)
                self._size += len(raw)
            else:
                self._truncated = True
            if sink is not None:
                sink(name, raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        pipe.close()

    def join(self) -> None:
        self._thread.join(timeout=10)

    def text(self) -> str:
        data = b"".join(self._chunks).decode("utf-8", errors="replace")
        return data + ("\n[output truncated]" if self._truncated else "")
