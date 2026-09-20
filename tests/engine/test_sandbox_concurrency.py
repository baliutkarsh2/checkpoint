"""Several sandboxes starting at the same moment must not collide on a port.

The gate shards a scenario's runs across workers, and each worker holds its own
sandbox. That was correct arithmetic on top of a racy startup: ports were chosen
in the parent by binding to port 0, reading the number and closing the socket,
and the twin host bound it milliseconds later. Two sandboxes starting inside
that window were handed the same port.

At one worker it almost never lost. At four it lost immediately — a CI run
produced repeated `[Errno 98] address already in use`, four failed runs and an
INCONCLUSIVE verdict from a gate whose agent was fine.

The fix is to never name a port the parent does not hold: the host binds 0 and
reports back what the OS gave it. These tests pin both halves of that — the
behaviour under contention, and the absence of the pattern that caused it.
"""
from __future__ import annotations

import concurrent.futures
import inspect

import httpx
import pytest

from checkpoint.engine import Sandbox
from checkpoint.engine import sandbox as sandbox_module

pytestmark = [pytest.mark.integration, pytest.mark.slow]

WORKERS = 6


def _start_and_probe(_index: int) -> tuple[dict[str, int], int]:
    """Start a sandbox, prove its twin answers, and return the ports it got."""
    with Sandbox(["github"]) as box:
        ports = dict(box._ports)
        port = ports["github"]
        status = httpx.get(f"http://127.0.0.1:{port}/_health", timeout=20).status_code
        return ports, status


def test_concurrent_sandboxes_get_distinct_working_ports():
    """Six at once: every twin answers, and no two were given the same port."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(_start_and_probe, range(WORKERS)))

    assert len(results) == WORKERS
    for ports, status in results:
        assert status == 200, f"a twin on {ports} did not answer"

    chosen = [ports["github"] for ports, _ in results]
    assert len(set(chosen)) == WORKERS, (
        f"two sandboxes were handed the same port: {sorted(chosen)}"
    )


def test_the_parent_never_picks_a_port_it_does_not_hold():
    """The structural half: no bind-then-close port reservation in the sandbox.

    The behavioural test above is a race, so on a quiet machine it can pass
    against broken code. This one cannot: it fails the moment someone
    reintroduces the pattern, whatever the scheduler happens to do that day.
    """
    source = inspect.getsource(sandbox_module)
    assert "_free_port" not in source, (
        "the sandbox is choosing a port in the parent again. Binding to port 0 "
        "and closing the socket leaves a window another sandbox can bind in; "
        "pass NAME=0 and read the port back out of the host's ready line."
    )
    start = inspect.getsource(sandbox_module.Sandbox._start_twins)
    assert "dict.fromkeys(self.twins, 0)" in start, (
        "_start_twins must ask the host for port 0 per twin"
    )
    assert "_ready_ports(ready" in start, (
        "_start_twins must take its ports from the host's ready line"
    )


def test_a_host_that_reports_no_port_is_a_sandbox_error():
    """A missing or nonsense port must fail loudly, not route to nowhere.

    Routing to a port the host never confirmed makes every call to that twin a
    connection error, which reads like a broken agent rather than a sandbox
    that did not start.
    """
    from checkpoint.engine.sandbox import SandboxError, _ready_ports

    assert _ready_ports('{"ready": {"github": 8123}}', ["github"]) == {"github": 8123}

    for line in ('{"ready": {}}', '{"ready": {"github": 0}}',
                 '{"ready": {"github": "8123"}}', '{"ready": null}', "not json"):
        with pytest.raises(SandboxError):
            _ready_ports(line, ["github"])
