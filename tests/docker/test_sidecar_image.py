"""The sidecar image must contain everything that actually runs inside it.

That image does two jobs: it runs mitmdump with the route addon, and it hosts
the twin FastAPI apps under uvicorn (_DOCKER_TWIN_APPS). It deliberately does
not install the checkpoint package with its full dependency set, so the list in
its Dockerfile is hand-maintained — and a missing entry only surfaces as
"Twin '<x>' failed to start in shared netns" during a real Docker run.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = REPO_ROOT / "checkpoint" / "proxy" / "Dockerfile"
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _installed() -> dict[str, str]:
    """{package: floor} parsed from the Dockerfile's pip install line."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for spec in re.findall(r'"([A-Za-z0-9_.\-]+(?:\[[a-z]+\])?>=[0-9][^"]*)"', text):
        name = spec.split(">=")[0].split("[")[0]
        out[name] = spec.split(">=")[1]
    return out


def test_sidecar_installs_the_proxy_and_twin_runtimes():
    installed = _installed()
    # mitmproxy runs the addon; the rest are what a twin app imports at module
    # scope (fastapi, and mcp via the mounted MCP surface).
    for required in ("mitmproxy", "fastapi", "uvicorn", "mcp"):
        assert required in installed, (
            f"the sidecar image does not install {required!r}; the twin apps or "
            "the proxy addon will fail to start inside the container"
        )


def test_sidecar_floors_match_the_project():
    """A floor that drifts below the project's would install an untested version."""
    core = {
        d.split(">=")[0].split("[")[0]: d.split(">=")[1]
        for d in PYPROJECT["project"]["dependencies"] if ">=" in d
    }
    core["mitmproxy"] = PYPROJECT["project"]["optional-dependencies"]["proxy"][0].split(">=")[1]
    mismatched = [
        f"{pkg}: Dockerfile>={floor} vs project>={core[pkg]}"
        for pkg, floor in _installed().items()
        if pkg in core and floor != core[pkg]
    ]
    assert not mismatched, f"sidecar image floors drifted from pyproject: {mismatched}"


def test_sidecar_does_not_install_host_only_dependencies():
    """openai is only used by the host-side judge; pulling it in here made the
    image's dependency graph unresolvable."""
    installed = _installed()
    for host_only in ("openai", "docker"):
        assert host_only not in installed, (
            f"{host_only!r} is not used inside the sidecar and bloats its graph"
        )
