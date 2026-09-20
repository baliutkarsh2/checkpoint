"""Checkpoint must never kill itself while cleaning up after a run.

`kill_tree` stops a process *group*, which is the only reliable way an agent's
own children — a node CLI, a browser, a tool server — die when the agent does.
That is only safe if the child leads a group of its own. A POSIX child inherits
the caller's process group unless it is spawned with `own_process_group()`, and
signalling *that* group sends SIGKILL to Checkpoint, to the shell that ran it,
and to anything else sharing the group.

That is not hypothetical: the twin host was spawned without it, so on Linux and
macOS every command that started a twin killed itself during teardown —
`checkpoint doctor`, every sandboxed run, and pytest itself, which died at 36%
with no failure to report. Windows kills a tree by PID (`taskkill /T`) and was
unaffected, so the whole suite looked green on the machine it was written on and
every CI job that ran Python timed out instead of failing.

These run out-of-process on purpose: a test that gets SIGKILLed cannot report
its own death, so an in-process version of this would vanish rather than fail.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

PY = sys.executable


def run_child(body: str, timeout: float = 240.0) -> subprocess.CompletedProcess:
    """Run a snippet in a separate interpreter and hand back the whole result."""
    return subprocess.run(
        [PY, "-c", textwrap.dedent(body)],
        capture_output=True, text=True, timeout=timeout,
    )


def test_a_sandbox_teardown_leaves_the_process_that_started_it_alive():
    """The regression itself: start twins, stop them, and still be here."""
    result = run_child("""
        from checkpoint.engine import Sandbox
        with Sandbox(["github"], intercept=False, egress="none") as sandbox:
            assert sandbox.views()
        print("SURVIVED", flush=True)
    """)
    assert result.returncode == 0, (
        "starting and stopping a sandbox killed the process that did it "
        f"(exit {result.returncode}; negative or 137 means a signal). "
        f"stderr:\n{result.stderr[-2000:]}")
    assert "SURVIVED" in result.stdout


def test_doctor_exits_instead_of_being_killed():
    """`checkpoint doctor` is the first command anyone runs, and it starts a twin."""
    result = subprocess.run(
        [PY, "-m", "checkpoint.cli", "doctor"],
        capture_output=True, text=True, timeout=240,
    )
    # 0 = everything passed, 1 = something a run needs is broken and it said so.
    # Anything else — a signal, an empty exit — means it did not get to report.
    assert result.returncode in (0, 1), (
        f"doctor exited {result.returncode} instead of reporting. "
        f"stdout:\n{result.stdout[-1000:]}\nstderr:\n{result.stderr[-2000:]}")
    assert result.stdout.strip(), "doctor printed nothing at all"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_the_twin_host_leads_its_own_process_group():
    """The property that makes killing the group safe, asserted directly."""
    result = run_child("""
        import os
        from checkpoint.engine import Sandbox
        sandbox = Sandbox(["github"], intercept=False, egress="none")
        sandbox.start()
        try:
            host = sandbox._host
            print(os.getpgid(host.pid), os.getpgid(0), flush=True)
        finally:
            sandbox.stop()
    """)
    assert result.returncode == 0, result.stderr[-2000:]
    child_group, own_group = (int(n) for n in result.stdout.split()[:2])
    assert child_group != own_group, (
        "the twin host shares Checkpoint's process group, so killing its tree "
        "would kill Checkpoint — spawn it with own_process_group()")


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_kill_tree_refuses_to_signal_its_own_group():
    """Belt and braces: a future spawn site that forgets the flag must not be fatal.

    Deliberately spawns a child the wrong way — sharing this process's group —
    and asserts kill_tree still only takes the child.
    """
    result = run_child("""
        import os, subprocess, sys
        from checkpoint.engine.agent import kill_tree
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        assert os.getpgid(child.pid) == os.getpgid(0), "test needs a shared group"
        kill_tree(child)
        child.wait(timeout=30)
        print("SURVIVED", flush=True)
    """, timeout=120)
    assert result.returncode == 0, (
        f"kill_tree killed the process that called it (exit {result.returncode}). "
        f"stderr:\n{result.stderr[-2000:]}")
    assert "SURVIVED" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_every_process_checkpoint_starts_is_isolated():
    """Grep-level guard: a new Popen without the flag is the way this comes back."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "checkpoint"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"subprocess\.Popen\((.*?)\n\s*\)", text, re.S):
            call = match.group(1)
            if "own_process_group()" in call or "start_new_session" in call:
                continue
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(root.parent).as_posix()}:{line}")
    assert not offenders, (
        "these spawn a child in Checkpoint's own process group, so killing its "
        "tree would kill Checkpoint: " + ", ".join(offenders)
        + " — pass **own_process_group() to Popen")


def test_os_getpgid_is_what_we_think_it_is():
    """The mistaken assumption that caused this, pinned so it reads as a fact."""
    if sys.platform == "win32":
        pytest.skip("process groups are POSIX")
    child = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(child.pid) == os.getpgid(0), (
            "a plain Popen child is expected to INHERIT this process's group; "
            "if that ever stops being true, own_process_group() can be revisited")
    finally:
        child.kill()
        child.wait(timeout=30)
