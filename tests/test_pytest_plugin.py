"""Tests for the checkpoint pytest plugin and init --template CLI option."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from checkpoint.cli import main as cli_main
from checkpoint.fake_credentials import FAKE_GITHUB_TOKEN

# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def test_plugin_registers_marker(pytestconfig):
    """The checkpoint marker should be registered via pytest_configure."""
    # _inicache stores markers as raw strings like "checkpoint(clones, seed): ..."
    marker_strings: list[str] = pytestconfig._inicache.get("markers", [])
    assert any("checkpoint" in m for m in marker_strings), (
        f"checkpoint marker not found in {marker_strings}"
    )


def test_plugin_exports_twin_handle():
    from checkpoint.pytest_plugin import TwinHandle
    h = TwinHandle(
        clone_id="github",
        url="http://127.0.0.1:9001",
        mcp_url="http://127.0.0.1:9001/mcp/",
        token=FAKE_GITHUB_TOKEN,
    )
    assert h.clone_id == "github"
    assert h.mcp_url.endswith("/mcp/")


def test_plugin_exports_session_factory():
    from checkpoint.pytest_plugin import _SessionFactory
    assert callable(_SessionFactory)


# ---------------------------------------------------------------------------
# checkpoint_twin fixture (mocked clone_manager)
# ---------------------------------------------------------------------------

def _make_entry(clone_id: str, port: int = 19001) -> dict:
    return {
        "pid": 99999,
        "port": port,
        "host": "127.0.0.1",
        "started_at": "2026-05-13T00:00:00Z",
        "url": f"http://127.0.0.1:{port}",
        "mcp_url": f"http://127.0.0.1:{port}/mcp/",
        "token": FAKE_GITHUB_TOKEN,
    }


@pytest.fixture
def mock_clone_manager(monkeypatch, tmp_path):
    """Patch clone_manager start/stop to avoid real subprocess."""
    stopped: list[str] = []

    monkeypatch.setattr(
        "checkpoint.clone_manager.start",
        lambda clone_id, **kw: _make_entry(clone_id),
    )
    monkeypatch.setattr(
        "checkpoint.clone_manager.stop",
        lambda clone_id, **kw: stopped.append(clone_id) or True,
    )
    return stopped


def test_checkpoint_twin_default_clones(mock_clone_manager, checkpoint_twin):
    """Without @pytest.mark.checkpoint, default clone is github."""
    assert "github" in checkpoint_twin
    gh = checkpoint_twin["github"]
    assert gh.clone_id == "github"
    assert "127.0.0.1" in gh.url


@pytest.mark.checkpoint(clones=["slack"])
def test_checkpoint_twin_marker_clones(mock_clone_manager, checkpoint_twin):
    assert "slack" in checkpoint_twin
    assert checkpoint_twin["slack"].clone_id == "slack"


@pytest.mark.checkpoint(clones=["github", "slack"])
def test_checkpoint_twin_multi_clone(mock_clone_manager, checkpoint_twin):
    assert set(checkpoint_twin.keys()) == {"github", "slack"}


# ---------------------------------------------------------------------------
# checkpoint init --template
# ---------------------------------------------------------------------------

@pytest.fixture
def runner():
    return CliRunner()


def test_init_raw_template(runner, tmp_path):
    result = runner.invoke(cli_main, ["init", str(tmp_path), "--template", "raw"])
    assert result.exit_code == 0
    harness = tmp_path / "harness.py"
    assert harness.exists()
    src = harness.read_text(encoding="utf-8")
    assert "requests" in src
    assert "CHECKPOINT_TASK" in src


def test_init_anthropic_template(runner, tmp_path):
    result = runner.invoke(cli_main, ["init", str(tmp_path), "--template", "anthropic"])
    assert result.exit_code == 0
    harness = tmp_path / "harness.py"
    assert harness.exists()
    src = harness.read_text(encoding="utf-8")
    assert "anthropic" in src.lower()
    assert "CHECKPOINT_TASK" in src


def test_init_openai_agents_template(runner, tmp_path):
    result = runner.invoke(cli_main, ["init", str(tmp_path), "--template", "openai-agents"])
    assert result.exit_code == 0
    harness = tmp_path / "harness.py"
    assert harness.exists()
    src = harness.read_text(encoding="utf-8")
    assert "openai" in src.lower() or "agents" in src.lower()
    assert "CHECKPOINT_TASK" in src


def test_init_langchain_template(runner, tmp_path):
    result = runner.invoke(cli_main, ["init", str(tmp_path), "--template", "langchain"])
    assert result.exit_code == 0
    harness = tmp_path / "harness.py"
    assert harness.exists()
    src = harness.read_text(encoding="utf-8")
    assert "langchain" in src.lower()
    assert "CHECKPOINT_TASK" in src


def test_init_invalid_template_exits_1(runner, tmp_path):
    result = runner.invoke(cli_main, ["init", str(tmp_path), "--template", "nonexistent"])
    assert result.exit_code != 0


def test_init_default_is_zero_code(runner, tmp_path):
    """v0.3+: the default `init` is zero-code — a declarative harness.json,
    NOT a copied harness.py. The legacy Python template is opt-in via
    `--template raw`."""
    result = runner.invoke(cli_main, ["init", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "harness.json").exists()
    assert not (tmp_path / "harness.py").exists()


def test_init_raw_template_writes_harness_py(runner, tmp_path):
    result = runner.invoke(cli_main, ["init", str(tmp_path), "--template", "raw"])
    assert result.exit_code == 0
    harness = tmp_path / "harness.py"
    assert harness.exists()
    assert "requests" in harness.read_text(encoding="utf-8")


def test_init_idempotent(runner, tmp_path):
    """Running init twice skips existing files without error."""
    runner.invoke(cli_main, ["init", str(tmp_path)])
    result = runner.invoke(cli_main, ["init", str(tmp_path)])
    assert result.exit_code == 0
    assert "already exists" in result.output or "nothing to do" in result.output.lower()


def test_init_scaffold_creates_standard_files(runner, tmp_path):
    runner.invoke(cli_main, ["init", str(tmp_path)])
    assert (tmp_path / "harness.json").exists()
    assert (tmp_path / "scenarios" / "quickstart.md").exists()
    assert (tmp_path / ".checkpoint.json").exists()


def test_init_anthropic_banner_shows_pip(runner, tmp_path):
    result = runner.invoke(cli_main, ["init", str(tmp_path), "--template", "anthropic"])
    assert "pip install anthropic" in result.output


def test_init_langchain_banner_shows_pip(runner, tmp_path):
    result = runner.invoke(cli_main, ["init", str(tmp_path), "--template", "langchain"])
    assert "pip install langchain" in result.output


# ---------------------------------------------------------------------------
# init.py scaffold() unit tests
# ---------------------------------------------------------------------------

def test_scaffold_raw(tmp_path):
    from checkpoint.init import scaffold
    result = scaffold(tmp_path, template="raw")
    assert "harness.py" in result.created
    harness = tmp_path / "harness.py"
    assert "requests" in harness.read_text(encoding="utf-8")


def test_scaffold_anthropic(tmp_path):
    from checkpoint.init import scaffold
    result = scaffold(tmp_path, template="anthropic")
    assert "harness.py" in result.created
    assert "anthropic" in (tmp_path / "harness.py").read_text(encoding="utf-8").lower()


def test_scaffold_openai_agents(tmp_path):
    from checkpoint.init import scaffold
    result = scaffold(tmp_path, template="openai-agents")
    assert "harness.py" in result.created
    src = (tmp_path / "harness.py").read_text(encoding="utf-8")
    assert "agents" in src.lower()


def test_scaffold_langchain(tmp_path):
    from checkpoint.init import scaffold
    result = scaffold(tmp_path, template="langchain")
    assert "harness.py" in result.created
    assert "langchain" in (tmp_path / "harness.py").read_text(encoding="utf-8").lower()


def test_scaffold_invalid_template_raises():
    from checkpoint.init import scaffold
    with pytest.raises(ValueError, match="Unknown template"):
        scaffold("/tmp", template="invalid-xyz")


def test_scaffold_result_has_template_field(tmp_path):
    from checkpoint.init import scaffold
    result = scaffold(tmp_path, template="anthropic")
    assert result.template == "anthropic"


def test_scaffold_banner_mentions_pip_for_frameworks(tmp_path):
    from checkpoint.init import scaffold
    result = scaffold(tmp_path, template="langchain")
    assert "pip install langchain" in result.banner


def test_scaffold_banner_no_pip_for_raw(tmp_path):
    from checkpoint.init import scaffold
    result = scaffold(tmp_path, template="raw")
    assert "pip install" not in result.banner
