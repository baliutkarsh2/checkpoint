"""The sidecar image must contain everything that actually runs inside it.

That image does two jobs: it runs Checkpoint's intercept proxy
(``python -m checkpoint.proxy``), and it hosts the twin FastAPI apps under
uvicorn (_DOCKER_TWIN_APPS). It deliberately does not install the checkpoint
package with its full dependency set, so the list in its Dockerfile is
hand-maintained — and a missing entry only surfaces as a sidecar that never
prints "ready", or "Twin '<x>' failed to start in shared netns", during a real
Docker run.
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROXY_DIR = REPO_ROOT / "checkpoint" / "proxy"
DOCKERFILE = PROXY_DIR / "Dockerfile"
ENTRYPOINT = PROXY_DIR / "entrypoint.sh"
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _installed() -> dict[str, str]:
    """{package: floor} parsed from the Dockerfile's pip install line."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for spec in re.findall(r'"([A-Za-z0-9_.\-]+(?:\[[a-z]+\])?>=[0-9][^"]*)"', text):
        name = spec.split(">=")[0].split("[")[0]
        out[name] = spec.split(">=")[1]
    return out


def _third_party_imports(directory: Path) -> set[str]:
    """Top-level third-party modules imported anywhere in ``directory``'s Python files."""
    found: set[str] = set()
    for path in directory.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {m for m in found if m not in sys.stdlib_module_names and m != "checkpoint"}


def test_sidecar_installs_everything_the_proxy_imports():
    installed = _installed()
    missing = _third_party_imports(PROXY_DIR) - installed.keys()
    assert not missing, (
        f"checkpoint/proxy imports {sorted(missing)} but the sidecar image does not "
        "install them; the proxy would crash before printing 'ready'"
    )


def test_sidecar_installs_the_twin_runtimes():
    installed = _installed()
    # What a twin app imports at module scope (fastapi, and mcp via the mounted
    # MCP surface), plus the server that runs it.
    for required in ("fastapi", "uvicorn", "mcp"):
        assert required in installed, (
            f"the sidecar image does not install {required!r}; the twin apps will "
            "fail to start inside the container"
        )


def test_sidecar_floors_match_the_project():
    """A floor that drifts below the project's would install an untested version."""
    core = {
        d.split(">=")[0].split("[")[0]: d.split(">=")[1]
        for d in PYPROJECT["project"]["dependencies"] if ">=" in d
    }
    mismatched = [
        f"{pkg}: Dockerfile>={floor} vs project>={core.get(pkg)}"
        for pkg, floor in _installed().items()
        if floor != core.get(pkg)
    ]
    assert not mismatched, f"sidecar image floors drifted from pyproject: {mismatched}"


def test_sidecar_does_not_install_host_only_or_retired_dependencies():
    """openai is only used by the host-side judge; pulling it in here made the
    image's dependency graph unresolvable. mitmproxy was replaced by the
    in-tree proxy; its pins (h11, h2, typing-extensions) downgraded shared
    packages wherever it was installed."""
    installed = _installed()
    for unwanted in ("openai", "docker", "mitmproxy"):
        assert unwanted not in installed, f"{unwanted!r} does not belong in the sidecar image"


def test_image_is_stamped_with_the_runner_contract():
    """ensure_sidecar_image() rebuilds any image whose label differs from this."""
    from checkpoint.docker.sidecar import SIDECAR_CONTRACT, SIDECAR_CONTRACT_LABEL

    assert f'LABEL {SIDECAR_CONTRACT_LABEL}="{SIDECAR_CONTRACT}"' in DOCKERFILE.read_text(
        encoding="utf-8"
    )


def test_entrypoint_runs_the_proxy_in_transparent_mode():
    """The runner resolves SaaS domains to the sidecar's :443 and waits for 'ready'."""
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert "exec python -m checkpoint.proxy" in script
    assert '--transparent-port "${SIDECAR_PORT:-443}"' in script
    assert "--ca-dir" in script and "--routes" in script
    assert "EXPOSE 443" in DOCKERFILE.read_text(encoding="utf-8")
