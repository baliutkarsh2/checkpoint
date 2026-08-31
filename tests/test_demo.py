"""`checkpoint demo` — the zero-setup golden path must score 100 offline, no key."""
from __future__ import annotations

from click.testing import CliRunner

from checkpoint.cli import main


def test_demo_runs_offline_with_no_api_key(monkeypatch, tmp_path):
    # The whole point of `demo`: it must work with no OPENAI_API_KEY and no
    # Docker. Run from an empty cwd so no stray .checkpoint.json interferes.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["demo"])

    assert result.exit_code == 0, result.output
    assert "100/100" in result.output


def test_demo_assets_are_importable_package_data():
    """The demo scenario + harness must live inside the package (ship in wheel)."""
    from pathlib import Path

    import checkpoint

    demo_dir = Path(checkpoint.__file__).parent / "demo"
    assert (demo_dir / "smoke-scenario.md").is_file()
    assert (demo_dir / "harness_fake.py").is_file()
    # The demo harness must not import third-party deps (stdlib only).
    src = (demo_dir / "harness_fake.py").read_text(encoding="utf-8")
    assert "import requests" not in src
