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
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=10)

    # -- control plane --------------------------------------------------------

    def reset(self) -> None:
        httpx.post(f"{self.url}/_reset", timeout=10).raise_for_status()

    def seed(self, name: str) -> None:
        httpx.post(f"{self.url}/_seed/{name}", timeout=10).raise_for_status()

    def configure(self, **config: object) -> None:
        httpx.post(f"{self.url}/_config", json=config, timeout=10).raise_for_status()

    def state(self) -> dict:
        return httpx.get(f"{self.url}/_state", timeout=10).json()

    def views(self) -> dict:
        return httpx.get(f"{self.url}/_views", timeout=10).json()["collections"]

    def trace(self) -> list[dict]:
        return httpx.get(f"{self.url}/_trace", timeout=10).json()

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
        if "tests/sdk/" in item.nodeid.replace("\\", "/") or "tests\sdk\\" in item.nodeid:
            item.add_marker(pytest.mark.sdk)
