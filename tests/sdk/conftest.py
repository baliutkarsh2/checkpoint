"""Twin conformance: drive each twin with the vendor's own SDK, over real HTTP.

A twin is only as useful as the SDK calls it answers correctly — if PyGithub or
stripe-python trips over a twin response, a correct agent fails for reasons that
have nothing to do with the agent. These tests pin that down per twin.

The SDKs live in the ``twin-conformance`` dependency group
(``pip install --group twin-conformance``); without them the tests skip.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections.abc import Iterator

import httpx
import pytest

from checkpoint.twins import registry


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveTwin:
    """A twin served over real HTTP (SDKs use their own transports)."""

    def __init__(self, name: str) -> None:
        self.spec = registry.get(name)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._proc: subprocess.Popen[str] | None = None
        # One pooled client for the whole module: building a throwaway httpx
        # client per control call costs ~1.5s on Windows and dominated these suites.
        self._client = httpx.Client(base_url=self.url, timeout=30.0, trust_env=False)

    def start(self) -> LiveTwin:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "checkpoint.twins.host", f"{self.spec.name}={self.port}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        line = self._proc.stdout.readline() if self._proc.stdout else ""
        if not line or "ready" not in json.loads(line):
            err = self._proc.stderr.read() if self._proc.stderr else ""
            raise RuntimeError(f"twin {self.spec.name} failed to start: {err[-2000:]}")
        return self

    def stop(self) -> None:
        self._client.close()
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=10)

    # -- control plane --------------------------------------------------------

    def reset(self) -> None:
        self._client.post("/_reset").raise_for_status()

    def seed(self, name: str) -> None:
        self._client.post(f"/_seed/{name}").raise_for_status()

    def configure(self, **config: object) -> None:
        self._client.post("/_config", json=config).raise_for_status()

    def state(self) -> dict:
        return self._client.get("/_state").json()

    def views(self) -> dict:
        return self._client.get("/_views").json()["collections"]

    def trace(self) -> list[dict]:
        return self._client.get("/_trace").json()

    @property
    def token(self) -> str:
        return self.spec.token


@pytest.fixture(scope="module")
def live_twin(request: pytest.FixtureRequest) -> Iterator[LiveTwin]:
    """One twin process per test module; the module sets ``TWIN = "<name>"``."""
    twin = LiveTwin(request.module.TWIN).start()
    try:
        yield twin
    finally:
        twin.stop()


@pytest.fixture
def twin(live_twin: LiveTwin) -> LiveTwin:
    """A freshly reset twin for each test."""
    live_twin.reset()
    return live_twin


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/sdk/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.sdk)
