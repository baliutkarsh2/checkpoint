"""Public Python SDK for the Checkpoint agent evaluation platform.

Enables programmatic scenario construction and evaluation without shelling
out to the CLI. All heavy lifting is delegated to the existing runner and
clone_manager internals.

Usage::

    from checkpoint.sdk import Checkpoint, CriterionSpec, RunConfig

    client = Checkpoint()

    result = client.run_file(
        "scenarios/github-happy-path.md",
        config=RunConfig(evaluator_model="gpt-4o"),
    )
    print(result.score, result.criteria)

    # Or build a scenario inline:
    result = client.run_scenario(
        title="Create issue",
        prompt="Create an issue titled 'oncall' in acme/webapp",
        criteria=[
            CriterionSpec("At least 1 issue exists", kind="D"),
            CriterionSpec("Issue title contains 'oncall'", kind="P"),
        ],
        config=RunConfig(clones=["github"], seed="small-project"),
    )

    # Or manage twins directly (no harness — useful for notebooks/REPL):
    with client.twin_session(["github", "slack"]) as twins:
        github_url = twins["github"].url
        github_token = twins["github"].token
        # ... call the twin REST API or MCP server directly
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, Literal

from .runner import RunResult, run_once
from .scenario import Criterion, Scenario
from .run_record import build_record, write_record


@dataclass
class TwinHandle:
    """Live reference to a running twin process."""
    clone_id: str
    url: str
    mcp_url: str
    token: str


@dataclass
class CriterionSpec:
    """Inline criterion for use with :meth:`Checkpoint.run_scenario`."""
    text: str
    kind: Literal["D", "P"] = "D"


@dataclass
class RunConfig:
    """Configuration overrides for a programmatic run."""
    clones: list[str] = field(default_factory=lambda: ["github"])
    seed: str | None = None
    harness_cmd: list[str] = field(
        default_factory=lambda: [sys.executable, "harness.py"]
    )
    evaluator_model: str = "gpt-4o-mini"
    timeout: int = 120
    cwd: str | Path | None = None


class Checkpoint:
    """Programmatic entrypoint for the Checkpoint evaluation SDK."""

    def run_scenario(
        self,
        *,
        title: str,
        prompt: str,
        criteria: list[CriterionSpec],
        config: RunConfig | None = None,
    ) -> RunResult:
        """Build a :class:`~checkpoint.scenario.Scenario` inline and run it.

        Returns a :class:`~checkpoint.runner.RunResult` with `.score` (0-100)
        and `.criteria` (list of per-criterion verdicts).
        """
        cfg = config or RunConfig()
        raw_config: dict = {
            "clones": ",".join(cfg.clones),
            "timeout": str(cfg.timeout),
        }
        if cfg.seed:
            raw_config["seed"] = cfg.seed

        scenario = Scenario(
            title=title,
            prompt=prompt,
            criteria=[Criterion(text=c.text, kind=c.kind) for c in criteria],
            config=raw_config,
        )
        result = run_once(
            scenario=scenario,
            harness_cmd=cfg.harness_cmd,
            cwd=str(cfg.cwd) if cfg.cwd else None,
            judge_model=cfg.evaluator_model,
        )
        record = build_record(
            scenario_name=title,
            scenario_path=None,
            satisfaction=result.score,
            criteria=result.criteria,
            evaluator_model=cfg.evaluator_model,
            evaluator_model_source="sdk",
            final_answer=result.final_answer,
            trace=result.trace,
            state=result.state,
            error=result.error,
            exit_code=result.exit_code,
        )
        write_record(record)
        return result

    def run_file(
        self, path: str | Path, config: RunConfig | None = None
    ) -> RunResult:
        """Load a ``.md`` scenario file and run it.

        ``config`` overrides take precedence over values baked into the file
        only for ``harness_cmd``, ``evaluator_model``, ``timeout``, and
        ``cwd`` — clone list and seed come from the file.
        """
        from .scenario import parse_file

        cfg = config or RunConfig()
        scenario = parse_file(Path(path))
        result = run_once(
            scenario=scenario,
            harness_cmd=cfg.harness_cmd,
            cwd=str(cfg.cwd) if cfg.cwd else None,
            judge_model=cfg.evaluator_model,
        )
        record = build_record(
            scenario_name=scenario.title,
            scenario_path=str(path),
            satisfaction=result.score,
            criteria=result.criteria,
            evaluator_model=cfg.evaluator_model,
            evaluator_model_source="sdk",
            final_answer=result.final_answer,
            trace=result.trace,
            state=result.state,
            error=result.error,
            exit_code=result.exit_code,
        )
        write_record(record)
        return result

    @contextmanager
    def twin_session(
        self,
        clones: list[str],
        *,
        seed: str | None = None,
        registry_path: Path | None = None,
    ) -> Generator[dict[str, TwinHandle], None, None]:
        """Context manager: start named clones, yield handles, stop on exit.

        Useful for REPL exploration and notebook workflows where you want
        direct access to the twin REST/MCP endpoints without running a
        full harness scenario.

        Example::

            with client.twin_session(["github"], seed="small-project") as twins:
                import httpx
                r = httpx.get(twins["github"].url + "/repos/acme/webapp")
        """
        from . import clone_manager
        import httpx

        tmp: Path | None = None
        if registry_path is None:
            tmp = Path(tempfile.mkdtemp(prefix="checkpoint_sdk_"))
            registry_path = tmp / "clones.json"

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
                        httpx.post(
                            f"{entry['url']}/_seed/{seed}", timeout=5
                        )
                    except Exception:
                        pass
            yield handles
        finally:
            for cid in started:
                try:
                    clone_manager.stop(cid, registry_path=registry_path)
                except Exception:
                    pass
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
