"""The committed dashboard bundle has to be the one this source builds.

`checkpoint view` serves `checkpoint/dashboard/static/`, and that bundle is
committed so a `pip install` from a git checkout gives a working dashboard with
no Node involved. Nothing rebuilds it on the way in, so the failure to catch is
an edit to `web/src` that never reached the bundle — a dashboard that quietly
serves last month's UI while the tests all pass.

Diffing a rebuild against the committed bytes does not work: the bundler is not
byte-reproducible across operating systems and Node majors, so a correct bundle
built on a laptop looks stale on a Linux runner. What has to be true is
narrower, so `npm run build` stamps a hash of its *inputs* into
`static/.source-hash` and this recomputes it.

This lives in pytest rather than in a CI step on purpose: it is the same check
a contributor gets locally, it runs on every Python version leg, and it needs
no Node. It mirrors `web/scripts/stamp-source-hash.mjs`; if the two ever drift
this fails loudly, which is the safe direction for a guard to break.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "checkpoint" / "dashboard" / "web"
STAMP = REPO_ROOT / "checkpoint" / "dashboard" / "static" / ".source-hash"

# Everything the bundle's contents depend on, in the order the stamping script
# walks them. package-lock.json is in here because a dependency bump changes
# the output without touching src/.
INPUTS = ["src", "index.html", "package.json", "package-lock.json",
          "vite.config.ts", "tsconfig.json", "tsconfig.node.json",
          "postcss.config.js"]

REBUILD = "cd checkpoint/dashboard/web && npm ci && npm run build"


def _files(path: Path) -> list[Path]:
    if not path.exists():
        return []  # an optional config this project does not use
    if not path.is_dir():
        return [path]
    return [f for name in sorted(p.name for p in path.iterdir())
            for f in _files(path / name)]


def _contents(path: Path) -> bytes:
    """What the file says, not how the checkout wrote it to disk.

    `.gitattributes` keeps these LF in git, but the conversion happens on
    commit, not on save: a Windows working tree can hold CRLF and still be
    clean. Hashing raw bytes there yields a stamp no Linux runner reproduces.
    """
    raw = path.read_bytes()
    return raw if b"\0" in raw else raw.replace(b"\r\n", b"\n")


def source_hash() -> str:
    digest = hashlib.sha256()
    for name in INPUTS:
        for path in _files(WEB / name):
            # The path goes in too, so renaming a file counts as a change.
            digest.update(path.relative_to(WEB).as_posix().encode())
            digest.update(_contents(path))
    return digest.hexdigest()


@pytest.mark.skipif(not WEB.is_dir(), reason="SPA sources are not in this tree")
def test_the_committed_bundle_was_built_from_this_source():
    assert STAMP.is_file(), (
        f"{STAMP.relative_to(REPO_ROOT).as_posix()} is missing — the committed "
        f"bundle cannot be checked against its source. Rebuild it:\n  {REBUILD}")
    assert STAMP.read_text(encoding="utf-8").strip() == source_hash(), (
        "The dashboard SPA source changed but checkpoint/dashboard/static/ was "
        f"not rebuilt, so `checkpoint view` serves a stale UI. Rebuild and "
        f"commit it:\n  {REBUILD}")


@pytest.mark.skipif(not WEB.is_dir(), reason="SPA sources are not in this tree")
def test_the_hash_ignores_line_endings(tmp_path):
    """The guard is worth nothing if it fires on a checkout difference."""
    crlf = tmp_path / "a.ts"
    crlf.write_bytes(b"const a = 1;\r\nconst b = 2;\r\n")
    lf = tmp_path / "b.ts"
    lf.write_bytes(b"const a = 1;\nconst b = 2;\n")
    assert _contents(crlf) == _contents(lf)


@pytest.mark.skipif(not WEB.is_dir(), reason="SPA sources are not in this tree")
def test_every_input_the_stamping_script_reads_is_listed_here():
    """Two implementations, one list — drift in it is the way this goes wrong."""
    script = (WEB / "scripts" / "stamp-source-hash.mjs").read_text(encoding="utf-8")
    listed = script.split("const INPUTS = [", 1)[1].split("]", 1)[0]
    assert [name.strip().strip('",') for name in listed.replace("\n", " ").split()
            if name.strip().strip('",')] == INPUTS
