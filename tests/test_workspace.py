"""The workspace: what a file tree looks like once it is queryable state.

A workspace exists so a coding agent can be scored the way an API agent is, and
that only works if the snapshot is trustworthy. The cases here are the ones that
decide a verdict: what is skipped (a stray `.pyc` must never be the file the
agent "created"), what happens to a file too big or too binary to carry, and
whether a path reads the same on Windows as on Linux.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from checkpoint.workspace import MAX_FILE_BYTES, Workspace, WorkspaceError, declared_views


def build(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            # newline="" so the bytes on disk are exactly what the test wrote:
            # text mode would turn every \n into \r\n on Windows and make every
            # `size` assertion below platform-dependent.
            path.write_text(content, encoding="utf-8", newline="")
    return root


def snapshot(root: Path) -> dict[str, dict]:
    with Workspace(root) as workspace:
        workspace.prepare()
        return {record["path"]: record for record in workspace.snapshot()}


# -- the shape of a record ------------------------------------------------------


def test_a_file_becomes_a_record_with_content_and_counts(tmp_path):
    files = snapshot(build(tmp_path, {"src/app.py": "def main():\n    pass\n"}))

    record = files["src/app.py"]
    assert record["content"] == "def main():\n    pass\n"
    assert record["lines"] == 2
    assert record["size"] == len(b"def main():\n    pass\n")
    assert record["binary"] is False and record["truncated"] is False
    assert len(record["digest"]) == 64


def test_paths_are_posix_and_relative_to_the_root(tmp_path):
    """The assertion `workspace.files[path == "src/app.py"]` has to hold on Windows too."""
    files = snapshot(build(tmp_path, {"src/deep/nested/app.py": "x = 1\n"}))

    assert list(files) == ["src/deep/nested/app.py"]
    assert "\\" not in next(iter(files))


def test_crlf_is_normalized_but_the_digest_is_not(tmp_path):
    """A fixture checked out on Windows must assert the same as one checked out on Linux.

    `digest` stays faithful to the bytes, which is what keeps a line-ending-only
    rewrite visible as a change rather than silently identical.
    """
    files = snapshot(build(tmp_path, {"a.txt": b"one\r\ntwo\r\n", "b.txt": b"one\ntwo\n"}))

    assert files["a.txt"]["content"] == files["b.txt"]["content"] == "one\ntwo\n"
    assert files["a.txt"]["lines"] == 2
    assert files["a.txt"]["digest"] != files["b.txt"]["digest"]
    assert files["a.txt"]["size"] == 10, "size is bytes on disk, not characters of content"


def test_an_empty_tree_snapshots_to_nothing(tmp_path):
    (tmp_path / "seed").mkdir()

    assert snapshot(tmp_path / "seed") == {}


def test_an_empty_file_is_a_record_not_an_absence(tmp_path):
    files = snapshot(build(tmp_path, {"empty.txt": ""}))

    assert files["empty.txt"]["size"] == 0
    assert files["empty.txt"]["lines"] == 0
    assert files["empty.txt"]["binary"] is False


# -- limits ----------------------------------------------------------------------


def test_a_binary_file_carries_a_digest_instead_of_its_bytes(tmp_path):
    files = snapshot(build(tmp_path, {"logo.png": b"\x89PNG\r\n\x1a\n\x00\x00rest"}))

    record = files["logo.png"]
    assert record["binary"] is True
    assert record["content"] == "" and record["lines"] == 0
    assert record["size"] == 14 and len(record["digest"]) == 64


def test_invalid_utf8_counts_as_binary(tmp_path):
    """No null byte, still not text: a latin-1 file must not come back as mojibake."""
    files = snapshot(build(tmp_path, {"data.bin": b"caf\xe9 \xff\xfe"}))

    assert files["data.bin"]["binary"] is True
    assert files["data.bin"]["content"] == ""


def test_an_oversized_file_keeps_its_identity_but_not_its_content(tmp_path):
    """It can still be created, deleted or changed — the digest is what says so."""
    big = "x" * (MAX_FILE_BYTES + 1)
    files = snapshot(build(tmp_path, {"big.txt": big, "small.txt": "ok\n"}))

    assert files["big.txt"]["truncated"] is True
    assert files["big.txt"]["content"] == ""
    assert files["big.txt"]["size"] == MAX_FILE_BYTES + 1
    assert len(files["big.txt"]["digest"]) == 64, "hashed in chunks, never loaded"
    assert files["big.txt"]["binary"] is False, "too large is not the same as not text"
    assert files["small.txt"]["truncated"] is False


def test_an_oversized_file_still_shows_up_as_changed(tmp_path):
    """Without the digest, an edit that kept the size would look like no edit."""
    from checkpoint.eval.expr import diff_collection

    before = snapshot(build(tmp_path / "a", {"big.bin": "x" * (MAX_FILE_BYTES + 1)}))
    after = snapshot(build(tmp_path / "b", {"big.bin": "y" * (MAX_FILE_BYTES + 1)}))

    assert before["big.bin"]["size"] == after["big.bin"]["size"]
    assert before["big.bin"]["content"] == after["big.bin"]["content"] == ""
    delta = diff_collection(list(before.values()), list(after.values()), key_field="path")
    assert [f["path"] for f in delta["changed"]] == ["big.bin"]


def test_too_many_files_is_an_error_before_the_copy(tmp_path, monkeypatch):
    """Pointing `workspace:` at a real checkout should fail fast, not copy for a minute."""
    monkeypatch.setattr("checkpoint.workspace.MAX_FILES", 4)
    seed = build(tmp_path / "seed", {f"f{i}.txt": "x" for i in range(5)})

    with Workspace(seed) as workspace:
        with pytest.raises(WorkspaceError) as exc:
            workspace.prepare()

        assert "limit of 4" in str(exc.value)
        assert not any(workspace.root.iterdir()), "nothing was copied"


# -- skip rules -------------------------------------------------------------------


def test_machine_state_is_never_copied_or_snapshotted(tmp_path):
    files = snapshot(build(tmp_path, {
        "app.py": "x = 1\n",
        ".git/HEAD": "ref: refs/heads/main\n",
        "node_modules/left-pad/index.js": "module.exports = 1\n",
        "src/__pycache__/app.cpython-312.pyc": b"\x00cached",
        ".venv/pyvenv.cfg": "home = /usr\n",
    }))

    assert list(files) == ["app.py"]


def test_the_seeds_own_gitignore_is_honoured(tmp_path):
    files = snapshot(build(tmp_path, {
        ".gitignore": "*.log\nbuild/\n!keep.log\n//root-only.txt\n",
        "app.py": "x = 1\n",
        "debug.log": "noise\n",
        "keep.log": "kept\n",
        "nested/other.log": "noise\n",
        "build/out.o": "artifact\n",
        "src/build/also.o": "artifact\n",
    }))

    assert sorted(files) == [".gitignore", "app.py", "keep.log"], (
        "a bare name matches at any depth, a negation wins, and .gitignore is itself a file"
    )


def test_an_anchored_gitignore_rule_only_matches_at_the_root(tmp_path):
    files = snapshot(build(tmp_path, {
        ".gitignore": "/notes.txt\ndocs/*.tmp\n",
        "notes.txt": "root\n",
        "sub/notes.txt": "kept\n",
        "docs/a.tmp": "skipped\n",
        "docs/deep/b.tmp": "kept: * does not cross a slash\n",
    }))

    assert sorted(files) == [".gitignore", "docs/deep/b.tmp", "sub/notes.txt"]


def test_files_the_agent_leaves_behind_obey_the_same_rules(tmp_path):
    """The rules are re-read from the tree, so a new `.pyc` cannot become a "created file"."""
    seed = build(tmp_path / "seed", {".gitignore": "*.log\n", "app.py": "x = 1\n"})
    with Workspace(seed) as workspace:
        workspace.prepare()
        (workspace.root / "run.log").write_text("agent noise\n", encoding="utf-8")
        (workspace.root / "__pycache__").mkdir()
        (workspace.root / "__pycache__" / "app.pyc").write_bytes(b"\x00")
        (workspace.root / "new.py").write_text("y = 2\n", encoding="utf-8")

        assert sorted(r["path"] for r in workspace.snapshot()) == [
            ".gitignore", "app.py", "new.py"]


# -- lifecycle --------------------------------------------------------------------


def test_prepare_wipes_the_tree_before_reseeding(tmp_path):
    """A reused workspace must not show one run the previous run's edits."""
    seed = build(tmp_path / "seed", {"app.py": "original\n", "gone.txt": "here\n"})
    with Workspace(seed) as workspace:
        workspace.prepare()
        (workspace.root / "app.py").write_text("edited\n", encoding="utf-8")
        (workspace.root / "stray.txt").write_text("left over\n", encoding="utf-8")
        (workspace.root / "gone.txt").unlink()
        (workspace.root / "sub").mkdir()
        (workspace.root / "sub" / "deep.txt").write_text("also left over\n", encoding="utf-8")

        workspace.prepare()

        files = {r["path"]: r for r in workspace.snapshot()}
        assert sorted(files) == ["app.py", "gone.txt"]
        assert files["app.py"]["content"] == "original\n"


def test_a_read_only_file_the_agent_left_does_not_survive_into_the_next_run(tmp_path):
    """On Windows a read-only file refuses deletion outright, so the wipe forces it."""
    seed = build(tmp_path / "seed", {"app.py": "original\n"})
    with Workspace(seed) as workspace:
        workspace.prepare()
        locked = workspace.root / "locked.txt"
        locked.write_text("agent output\n", encoding="utf-8")
        locked.chmod(0o444)

        workspace.prepare()

        assert [r["path"] for r in workspace.snapshot()] == ["app.py"]


def test_a_seed_that_does_not_exist_is_an_error(tmp_path):
    with Workspace(tmp_path / "nope") as workspace, pytest.raises(WorkspaceError) as exc:
        workspace.prepare()

    assert "not found" in str(exc.value)


def test_a_workspace_with_no_seed_starts_empty(tmp_path):
    with Workspace() as workspace:
        workspace.prepare()

        assert workspace.snapshot() == []
        assert workspace.root.is_dir()


def test_stop_removes_the_tree(tmp_path):
    workspace = Workspace(build(tmp_path / "seed", {"a.txt": "x"}))
    workspace.start()
    workspace.prepare()
    root = workspace.root
    workspace.stop()

    assert not root.exists()
    assert not workspace.started
    with pytest.raises(WorkspaceError):
        _ = workspace.root


def test_agent_env_points_at_the_tree(tmp_path):
    with Workspace() as workspace:
        assert workspace.agent_env() == {"CHECKPOINT_WORKSPACE": str(workspace.root)}


# -- the view the assertion language sees ------------------------------------------


def test_views_describe_one_collection_keyed_by_path(tmp_path):
    with Workspace(build(tmp_path / "seed", {"a.txt": "x"})) as workspace:
        workspace.prepare()
        views = workspace.views()

    assert list(views) == ["files"]
    described = views["files"].to_json()
    assert described["key"] == "path"
    assert described["nouns"] == ["file", "files"]
    assert described["tombstone"] is None, "a deleted file is gone, not archived"
    assert {"path", "content", "size", "lines", "binary"} <= set(described["fields"])


def test_the_schema_is_declared_so_criteria_compile_with_no_tree_on_disk(tmp_path):
    """`checkpoint check` runs before anything has been copied anywhere."""
    described = declared_views()["files"]

    assert described["items"] == []
    assert described["key"] == "path"
    assert {"path", "content", "size", "lines", "binary"} <= set(described["fields"])


def test_state_lists_the_tree_without_carrying_it_twice(tmp_path):
    with Workspace(build(tmp_path / "seed", {"a.txt": "x" * 100})) as workspace:
        workspace.prepare()
        state = workspace.state()

    assert state["root"] == str(workspace_root_of(state))
    assert state["files"] == [{"path": "a.txt", "size": 100, "lines": 1, "binary": False}]
    assert "content" not in state["files"][0], "views() already carries the content"


def workspace_root_of(state: dict) -> Path:
    return Path(state["root"])
