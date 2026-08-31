"""Phase 8 / Plan 05: cold-start performance gate (QA-02).

Wall-clock budget for `checkpoint run` cold-start in local mode:
  spec   : <5s
  ci     : <8s (per execution_rules note 5 — generous 20% headroom)

We use the existing `example/smoke-scenario.md` (which has only a [D]
criterion, so the LLM judge is never called) and the existing
`example/harness_fake.py` (no LLM, just `requests`).

Docker-mode timing is intentionally out of scope here — it would
require a docker daemon + image build + a totally different threshold
(60s). The Phase 1 docker tests already cover that path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_SCENARIO = REPO_ROOT / "examples" / "smoke" / "smoke-scenario.md"
FAKE_HARNESS = REPO_ROOT / "examples" / "smoke" / "harness_fake.py"

# Generous threshold per execution_rules note 5. The spec says <5s on a
# normal dev box; we allow up to 8s to absorb CI/slow-disk variance.
COLD_START_BUDGET_SECONDS = 8.0


@pytest.mark.skipif(
    not SMOKE_SCENARIO.is_file() or not FAKE_HARNESS.is_file(),
    reason="example smoke scenario or fake harness missing",
)
def test_cold_start_local_under_budget() -> None:
    """Time a fresh `checkpoint run` invocation against the smoke scenario."""
    env = dict(os.environ)
    # The fake harness doesn't use OpenAI, so the key is irrelevant. But the
    # CLI's dotenv hook can pick up an OPENAI_API_KEY from .env in the repo,
    # which is fine — we don't strip it.
    #
    # Force UTF-8 I/O and disable Rich colour output so that Unicode symbols
    # (✓/✗) don't trigger UnicodeEncodeError on Windows cp1252 consoles when
    # stdout is piped.
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"

    cmd = [
        sys.executable,
        "-m",
        "checkpoint.cli",
        "run",
        str(SMOKE_SCENARIO),
        "--no-docker",
        "--harness",
        f"{sys.executable} {FAKE_HARNESS}",
    ]
    started = time.monotonic()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=COLD_START_BUDGET_SECONDS + 30,
    )
    elapsed = time.monotonic() - started

    # The run should at least produce output. Don't strictly require a 100/100
    # because environment drift (no .env, missing OpenAI key) can affect the
    # full scoring — but the score line must appear, meaning the harness ran
    # and the evaluator completed.
    assert "Score:" in proc.stdout, (
        f"no Score line in stdout (elapsed={elapsed:.2f}s):\n"
        f"stdout: {proc.stdout[-500:]}\nstderr: {proc.stderr[-500:]}"
    )
    assert elapsed < COLD_START_BUDGET_SECONDS, (
        f"Cold-start took {elapsed:.2f}s (budget {COLD_START_BUDGET_SECONDS}s)"
    )


def test_cold_start_docker_skipped_without_daemon() -> None:
    """Docker-mode cold-start is covered by Phase 1's docker tests.

    This placeholder just records the docker-mode threshold for the record.
    If docker is available the test imports the docker runner module to
    confirm the wiring is still present, but does not actually run it.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH; Phase 1 docker tests cover the threshold")
    # Just confirm the docker runner module is importable.
    from checkpoint.docker import runner as _docker_runner  # noqa: F401
