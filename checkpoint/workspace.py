"""The workspace: a file tree an agent edits, presented as queryable state.

The largest category of agent Checkpoint could not test is the one that edits a
repository — a coding agent, a migration tool, a docs generator. It never calls
an API, so there was nothing to trace and nothing to diff.

A workspace fixes that without inventing a second assertion language. It is
modelled as one more *namespace* alongside the twins, called ``workspace``,
holding a single collection, ``files``, keyed by ``path``. Because the
evaluator resolves the head of a path by looking it up among the namespaces the
run produced, every root the language already has works on it unchanged::

    workspace.files                          the tree the agent left
    seed.workspace.files                     the tree it started from
    created.workspace.files                  files it added
    deleted.workspace.files                  files it removed
    changed.workspace.files                  files it edited

    count(created.workspace.files) == 1
    exists(workspace.files[path == "README.md"])
    count(workspace.files[path == "src/app.py" && content ~ /def main/]) == 1

One record per file::

    path       POSIX, relative to the workspace root ("src/app.py")
    content    the decoded text, or "" when the file is binary or oversized
    size       bytes on disk
    lines      lines of text (0 when there is no text)
    binary     the bytes are not text; only decided for files within MAX_FILE_BYTES,
               since an oversized file is never loaded to find out
    truncated  the file is larger than MAX_FILE_BYTES, so content was not carried
    digest     sha256 of the bytes on disk

``digest`` is what makes ``changed`` honest. Two records are compared field by
field, so without it an edit to a binary or oversized file — whose ``content``
is empty in both snapshots — would look like no edit at all whenever the size
happened to stay the same.

**Limits.** A tree is a fixture, not a production checkout, and every byte here
ends up in the run record that a gate certificate is built from.

* ``MAX_FILE_BYTES`` (1 MiB): a larger file keeps its record, its ``size`` and
  its ``digest`` — so it can still be created, deleted or changed — but its
  content is left on disk. Source files are orders of magnitude smaller; a file
  this large is a build artifact or a dataset.
* ``MAX_FILES`` (2000): a bigger tree raises :class:`WorkspaceError` rather than
  spending minutes copying what is almost certainly the wrong directory. It is
  checked against the seed *before* the copy, so the error arrives first.
* Binary files carry ``digest`` instead of bytes. Base64 in a world would be
  unreadable in a report and unusable in an assertion.

**Skip rules.** ``SKIP_NAMES`` (``.git``, ``node_modules``, ``__pycache__``,
virtualenvs, tool caches, ``.DS_Store``) is never copied and never snapshotted:
these are machine state, and a criterion counting "files the agent created"
must not be decided by a stray ``.pyc``. On top of that the seed's own
**root ``.gitignore``** is honoured, with the subset of the format fixtures
actually use: comments, blank lines, ``!`` negation, ``/`` anchoring,
trailing-``/`` directory rules, ``*``/``?``/``**`` globs, and last-match-wins.
Nested ``.gitignore`` files, ``.git/info/exclude`` and the user's global excludes
are **not** read — honouring them would mean either shelling out to ``git`` or
carrying a full implementation, and a fixture tree deep enough to need them is a
fixture that should be smaller. ``.gitignore`` itself is an ordinary file and is
copied and snapshotted like any other.

Text is decoded as UTF-8 and CRLF is normalized to LF, so a fixture checked out
on Windows and one checked out on Linux produce the same ``content`` and the
same assertions pass. ``size`` and ``digest`` stay faithful to the bytes on
disk, which is how a line-ending-only rewrite still shows up as ``changed``.
Symbolic links are followed; dangling ones are skipped.

**This is a convention, not containment.** The agent is handed a temporary
directory and started in it. Nothing here stops the process from writing
anywhere else on the machine it can reach — there is no chroot, no mount
namespace, no filesystem policy. What a workspace provides is a disposable tree
to seed and a diff to score, which is what testing an agent you are developing
needs. Running an agent you do not trust needs a container around the whole
process, and this module should not be read as providing one.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from checkpoint.twins.kit import View

NAMESPACE = "workspace"
"""The name a workspace answers to in an assertion: ``workspace.files``."""

COLLECTION = "files"

FIELDS = ("path", "content", "size", "lines", "binary", "truncated", "digest")
"""Declared so file criteria compile against an empty tree, where the items
themselves reveal no field names at all."""

NOUNS = ("file", "files")

MAX_FILE_BYTES = 1_048_576
MAX_FILES = 2_000

SKIP_NAMES = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".venv", "venv", ".eggs", ".DS_Store",
})
"""Names never copied and never snapshotted, wherever they appear in the tree."""

_ENV_VAR = "CHECKPOINT_WORKSPACE"


class WorkspaceError(RuntimeError):
    """The workspace could not be built or seeded (never an agent failure)."""


@dataclass
class Workspace:
    """A temporary file tree, seeded from a directory and diffed after the run.

    Lives exactly as long as the sandbox around it: ``start()`` makes the
    directory, ``prepare()`` refills it from the seed, ``views()`` reads it back
    as records, ``stop()`` removes it. Reusing one across runs is the point —
    ``prepare()`` wipes the tree first, so run two cannot see run one's edits.
    """

    seed: Path | None = None
    """Directory copied in by ``prepare()``. None means "start empty"."""

    _root: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.seed is not None:
            self.seed = Path(self.seed)

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> Workspace:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def started(self) -> bool:
        return self._root is not None

    @property
    def root(self) -> Path:
        if self._root is None:
            raise WorkspaceError("workspace is not started")
        return self._root

    def start(self) -> None:
        if self._root is None:
            # Resolved because the agent runs here: a subprocess reports its cwd
            # with symlinks already collapsed (/var -> /private/var on macOS),
            # and a test comparing the two should not fail over that.
            self._root = Path(tempfile.mkdtemp(prefix="checkpoint-workspace-")).resolve()

    def stop(self) -> None:
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None

    def prepare(self, seed: str | Path | None = None) -> None:
        """Empty the tree, then copy the seed into it.

        ``seed`` overrides the one this workspace was built with, so a reused
        sandbox can serve scenarios that seed from different fixtures.
        """
        if seed is not None:
            self.seed = Path(seed)
        root = self.root
        _clear(root)
        if self.seed is None:
            return
        source = self.seed
        if not source.is_dir():
            raise WorkspaceError(
                f"workspace seed not found: {source} "
                f"({'not a directory' if source.exists() else 'no such directory'})"
            )
        skip = _Skips.for_tree(source)
        count = sum(1 for _ in _walk(source, skip))
        if count > MAX_FILES:
            raise WorkspaceError(
                f"workspace seed {source} holds {count} files, more than the limit of "
                f"{MAX_FILES}; point `workspace:` at a fixture rather than a real checkout"
            )
        shutil.copytree(source, root, ignore=skip.copytree_ignore, dirs_exist_ok=True,
                        ignore_dangling_symlinks=True)

    # -- inspection ----------------------------------------------------------

    def snapshot(self) -> list[dict]:
        """Every file in the tree, as records, ordered by path."""
        root = self.root
        skip = _Skips.for_tree(root)
        files = sorted(_walk(root, skip), key=lambda p: p.as_posix())
        if len(files) > MAX_FILES:
            raise WorkspaceError(
                f"the agent left {len(files)} files in the workspace, more than the "
                f"limit of {MAX_FILES}"
            )
        return [record for record in (_read(root, rel) for rel in files) if record is not None]

    def views(self) -> dict[str, View]:
        """The workspace as normalized collections, exactly as a twin describes itself."""
        # Imported here: kit pulls in the whole web stack, and nothing about a
        # directory of files should make `checkpoint check` start FastAPI.
        from checkpoint.twins.kit import View

        return {COLLECTION: View(items=self.snapshot(), key="path",
                                 nouns=NOUNS, fields=FIELDS)}

    def state(self) -> dict:
        """A compact listing for reports and ``checkpoint runs show``.

        Content is deliberately left out: ``views()`` already carries it, and a
        run record should not hold the tree twice.
        """
        return {
            "root": str(self.root),
            "files": [{k: rec[k] for k in ("path", "size", "lines", "binary")}
                      for rec in self.snapshot()],
        }

    def agent_env(self) -> dict[str, str]:
        """What an agent needs to find the tree it is supposed to edit."""
        return {_ENV_VAR: str(self.root)}


def declared_views() -> dict[str, dict]:
    """The ``files`` collection as a schema, with no tree to read.

    ``checkpoint check`` has to tell an author that ``workspace.files[path ==
    ...]`` compiles before anything has been copied anywhere, and the field
    names are declared rather than discovered, so nothing needs to exist on disk.
    """
    from checkpoint.twins.kit import View

    return {COLLECTION: View(items=[], key="path", nouns=NOUNS, fields=FIELDS).to_json()}


# -- emptying the tree ----------------------------------------------------------


def _clear(root: Path) -> None:
    """Remove everything in ``root``, or say plainly that it could not be done.

    The gate reuses one sandbox for sixteen runs, so "each run starts from the
    seed" rests entirely on this. A tree that cannot be emptied is raised rather
    than ignored: serving the next run a leftover file would show up as an agent
    that created something it never touched.
    """
    for child in root.iterdir():
        _remove(child)
    leftover = sorted(p.name for p in root.iterdir())
    if leftover:
        raise WorkspaceError(
            f"could not empty the workspace at {root}; left behind: {', '.join(leftover[:10])}"
        )


def _remove(path: Path) -> None:
    try:
        _delete(path)
    except OSError:
        # An agent may leave a file read-only, which on Windows blocks deletion
        # outright rather than falling back to the directory's permissions.
        _make_writable(path)
        try:
            _delete(path)
        except OSError:
            pass  # reported by the caller, with the name


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _make_writable(path: Path) -> None:
    targets = [path, *path.rglob("*")] if path.is_dir() else [path]
    for target in targets:
        try:
            target.chmod(target.stat().st_mode | stat.S_IWRITE)
        except OSError:
            continue


# -- reading the tree ---------------------------------------------------------


def _walk(root: Path, skip: _Skips) -> list[Path]:
    """Every file under ``root`` that survives the skip rules, relative to it."""
    out: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            rel = entry.relative_to(root)
            try:
                is_dir = entry.is_dir()
            except OSError:  # a dangling symlink
                continue
            if skip.ignores(rel.as_posix(), is_dir):
                continue
            if is_dir:
                stack.append(entry)
            elif entry.is_file():
                out.append(rel)
    return out


def _read(root: Path, rel: Path) -> dict | None:
    """One file as a record, or None if it vanished while we were reading."""
    path = root / rel
    record: dict[str, Any] = {
        "path": rel.as_posix(), "content": "", "size": 0, "lines": 0,
        "binary": False, "truncated": False, "digest": "",
    }
    try:
        size = path.stat().st_size
        record["size"] = size
        # An oversized file is hashed in chunks and never loaded: whatever an
        # agent dropped in the tree, Checkpoint must not try to hold it in
        # memory to find out it was too big to hold.
        if size > MAX_FILE_BYTES:
            record["truncated"] = True
            record["digest"] = _digest_of(path)
            return record
        raw = path.read_bytes()
    except OSError:
        return None
    record["size"] = len(raw)
    record["digest"] = hashlib.sha256(raw).hexdigest()
    if b"\x00" in raw[:8192]:
        record["binary"] = True
        return record
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        record["binary"] = True
        return record
    # Normalized so the same fixture asserts the same way on Windows and Linux.
    # `size` and `digest` stay faithful to the bytes, so an agent that rewrote
    # only the line endings still lands in `changed.workspace.files`.
    text = text.replace("\r\n", "\n")
    record["content"] = text
    record["lines"] = len(text.splitlines())
    return record


def _digest_of(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


# -- skip rules ----------------------------------------------------------------


@dataclass
class _Skips:
    """The names and ``.gitignore`` rules that keep machine state out of a snapshot."""

    root: Path
    """The tree the rules are anchored to."""
    rules: tuple[_Rule, ...] = ()
    """Compiled root ``.gitignore`` rules, in file order."""

    @classmethod
    def for_tree(cls, root: Path) -> _Skips:
        try:
            text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return cls(root)
        return cls(root, tuple(_gitignore_rules(text)))

    def ignores(self, rel: str, is_dir: bool) -> bool:
        """Whether ``rel`` (POSIX, relative to the root) is skipped."""
        if Path(rel).name in SKIP_NAMES:
            return True
        ignored = False
        for rule in self.rules:  # last match wins, as git does
            if rule.dir_only and not is_dir:
                continue
            if rule.pattern.match(rel):
                ignored = not rule.negated
        return ignored

    def copytree_ignore(self, directory: str, names: list[str]) -> set[str]:
        """The ``ignore`` callable :func:`shutil.copytree` asks for each directory.

        Ignoring a directory here also prunes it, which is what gives the
        "everything under an ignored directory stays ignored" rule for free.
        """
        here = Path(directory)
        skipped = set()
        for name in names:
            entry = here / name
            try:
                rel = entry.relative_to(self.root).as_posix()
            except ValueError:  # not under the seed root: leave it to copytree
                continue
            if self.ignores(rel, entry.is_dir()):
                skipped.add(name)
        return skipped


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    negated: bool
    dir_only: bool


def _gitignore_rules(text: str) -> list[_Rule]:
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        if line.startswith("\\"):  # an escaped leading "!" or "#"
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        # git anchors a pattern that contains a slash; a bare name matches at any depth.
        anchored = line.startswith("/") or "/" in line
        body = _translate(line.lstrip("/"))
        prefix = "" if anchored else "(?:.*/)?"
        rules.append(_Rule(re.compile(f"^{prefix}{body}(?:/.*)?$"), negated, dir_only))
    return rules


def _translate(pattern: str) -> str:
    """One gitignore pattern as a regex body: ``*`` stops at ``/``, ``**`` does not."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
            elif pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)
