"""pytest plugin for Checkpoint — auto-registered via the ``pytest11`` entry point.

After ``pip install checkpoint[dev]``, this plugin is discovered automatically.
It provides two fixtures and one marker:

Marker
------
``@pytest.mark.checkpoint(clones=["github"], seed="small-project")``
    Declares which twin clones a test needs and an optional named seed.

Fixtures
--------
``checkpoint_twin``
    Function-scoped. Starts one twin process per clone declared in the
    ``@pytest.mark.checkpoint`` marker, yields a ``{clone_id: TwinHandle}``
    dict, then stops all twins. Each test gets a fresh process.

``checkpoint_session``
    Session-scoped factory.  Call it with a clone list to start twins that
    persist for the whole test session::

        @pytest.fixture(scope="session")
        def gh_twin(checkpoint_session):
            return checkpoint_session(["github"])

Example
-------
::

    import pytest

    @pytest.mark.checkpoint(clones=["github"], seed="small-project")
    def test_agent_opens_issue(checkpoint_twin):
        gh = checkpoint_twin["github"]
        # gh.url, gh.mcp_url, gh.token
        import httpx, json
        r = httpx.post(
            f"{gh.url}/repos/acme/webapp/issues",
            json={"title": "oncall", "body": ""},
            headers={"Authorization": f"token {gh.token}"},
        )
        assert r.status_code == 201
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from checkpoint import clone_manager


@dataclass
class TwinHandle:
    """Live reference to a running twin process."""
    clone_id: str
    url: str
    mcp_url: str
    token: str


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "checkpoint(clones, seed): declare checkpoint twin clones needed by the test",
    )


@pytest.fixture
def checkpoint_twin(request: pytest.FixtureRequest, tmp_path: Path) -> dict[str, TwinHandle]:  # type: ignore[return]
    """Function-scoped fixture: starts twins for clones in @pytest.mark.checkpoint."""
    marker = request.node.get_closest_marker("checkpoint")
    clones: list[str] = marker.kwargs.get("clones", ["github"]) if marker else ["github"]
    seed: str | None = marker.kwargs.get("seed") if marker else None

    registry_path = tmp_path / "clones.json"
    started: list[str] = []
    handles: dict[str, TwinHandle] = {}

    try:
        for clone_id in clones:
            entry = clone_manager.start(clone_id, registry_path=registry_path)
            started.append(clone_id)
            handles[clone_id] = TwinHandle(
                clone_id=clone_id,
                url=entry["url"],
                mcp_url=entry["mcp_url"],
                token=entry["token"],
            )
            if seed:
                try:
                    httpx.post(f"{entry['url']}/_seed/{seed}", timeout=5)
                except Exception:
                    pass
    except Exception:
        for cid in started:
            try:
                clone_manager.stop(cid, registry_path=registry_path)
            except Exception:
                pass
        raise

    yield handles  # type: ignore[misc]

    for cid in started:
        try:
            clone_manager.stop(cid, registry_path=registry_path)
        except Exception:
            pass


class _SessionFactory:
    """Helper returned by checkpoint_session — call with a clone list."""

    def __init__(self) -> None:
        self._registry: Path = Path(tempfile.mkdtemp(prefix="checkpoint_pytest_")) / "clones.json"
        self._started: list[str] = []
        self._handles: dict[str, TwinHandle] = {}

    def __call__(self, clones: list[str], *, seed: str | None = None) -> dict[str, TwinHandle]:
        for clone_id in clones:
            if clone_id in self._handles:
                continue
            entry = clone_manager.start(clone_id, registry_path=self._registry)
            self._started.append(clone_id)
            self._handles[clone_id] = TwinHandle(
                clone_id=clone_id,
                url=entry["url"],
                mcp_url=entry["mcp_url"],
                token=entry["token"],
            )
            if seed:
                try:
                    httpx.post(f"{entry['url']}/_seed/{seed}", timeout=5)
                except Exception:
                    pass
        return self._handles

    def teardown(self) -> None:
        for cid in self._started:
            try:
                clone_manager.stop(cid, registry_path=self._registry)
            except Exception:
                pass
        import shutil
        shutil.rmtree(self._registry.parent, ignore_errors=True)


@pytest.fixture(scope="session")
def checkpoint_session() -> _SessionFactory:  # type: ignore[return]
    """Session-scoped factory fixture.

    Twins started via this factory persist for the whole pytest session.
    Use this when you want to share one twin across many tests to avoid the
    startup overhead of a new uvicorn process per test.

    Example::

        @pytest.fixture(scope="session")
        def gh(checkpoint_session):
            return checkpoint_session(["github"], seed="small-project")["github"]

        def test_list_repos(gh):
            r = httpx.get(f"{gh.url}/user/repos", ...)
            assert r.status_code == 200
    """
    factory = _SessionFactory()
    yield factory  # type: ignore[misc]
    factory.teardown()
