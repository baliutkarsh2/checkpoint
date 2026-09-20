"""The repo has to obey the policies it publishes.

Three of the files added for the first public release make claims that nothing
executed: `.pre-commit-config.yaml` states a size ceiling for committed files,
`.github/CODEOWNERS` names paths that must never merge unreviewed, and
`.editorconfig` promises to agree with `.gitattributes`. Each is the kind of
claim that stays written long after it stopped being true, and none of them
fails a build when it rots:

  * `check-added-large-files` only inspects files being ADDED, so a tree can
    contradict its own --maxkb indefinitely and the hook still passes. That is
    exactly what happened: the 1292 KB vendored Linear schema sat under a
    1024 KB limit, green.
  * A CODEOWNERS rule for a path that has since moved protects nothing, and
    GitHub reports no error for it -- the rule is simply never matched.
  * `.editorconfig` and `.gitattributes` are two halves of one decision about
    line endings. Nothing but a test notices when one half changes.
"""
from __future__ import annotations

import configparser
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _text(path: Path) -> str:
    # Several files in this tree are CRLF, and some carry em-dashes. An explicit
    # encoding handles both; letting the platform choose gets cp1252 here.
    return path.read_text(encoding="utf-8")


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - not a git checkout
        # An sdist or an exported tree has no index to ask; that is not a
        # failing repository, and reporting it as one sends the reader hunting.
        pytest.skip("not a git checkout; hygiene sweep skipped")
        out = b""  # unreachable: pytest.skip raises. Bound anyway so that the
                   # function has one exit and `out` is assigned on every path,
                   # which is what a reader and a static analyser both need.
    # Decoded here rather than via `text=True`, which decodes as cp1252 on
    # Windows and would mangle any non-ASCII path git prints.
    return [line for line in out.decode("utf-8").splitlines() if line.strip()]


# --- the large-file policy --------------------------------------------------


def _large_file_hook() -> dict:
    config = yaml.safe_load(_text(ROOT / ".pre-commit-config.yaml"))
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    found = [hook for hook in hooks if hook["id"] == "check-added-large-files"]
    assert len(found) == 1, "expected exactly one check-added-large-files hook"
    return found[0]


def _max_bytes(hook: dict) -> int:
    maxkb = next(int(a.split("=", 1)[1]) for a in hook["args"] if a.startswith("--maxkb="))
    return maxkb * 1024


def test_no_tracked_file_exceeds_the_size_limit_without_a_named_exclusion() -> None:
    """A tree that breaks its own --maxkb teaches everyone to ignore the number.

    The hook cannot catch this itself: it looks at files being added, not at
    files already committed, so the contradiction is invisible to it forever.
    """
    hook = _large_file_hook()
    limit = _max_bytes(hook)
    # `(?!)` never matches, so a hook with no exclusion excuses nothing.
    excluded = re.compile(hook.get("exclude", r"(?!)"))
    offenders = [
        f"{rel} ({(ROOT / rel).stat().st_size // 1024} KB)"
        for rel in _tracked_files()
        if (ROOT / rel).is_file()
        and (ROOT / rel).stat().st_size > limit
        and not excluded.search(rel)
    ]
    assert not offenders, (
        f"tracked files exceed the {limit // 1024} KB ceiling in "
        f".pre-commit-config.yaml and are not excluded there: {sorted(offenders)}. "
        "Either the file does not belong in git, or the exclusion needs to name "
        "it and say why."
    )


def test_every_large_file_exclusion_still_names_an_oversized_file() -> None:
    """The other direction: an exemption nobody needs is a hole nobody notices.

    The exclusion is a regex, so it cannot be read off as a list of paths.
    Instead: some tracked file must both match it and actually exceed the limit.
    If the vendored schema is ever dropped or renamed, this fails and the
    exemption goes with it, rather than silently widening to cover whatever
    large file lands at that path next.
    """
    hook = _large_file_hook()
    exclude = hook.get("exclude")
    assert exclude, (
        "check-added-large-files has no `exclude`; if the ceiling now fits every "
        "tracked file, delete this test rather than leaving its premise here"
    )
    limit = _max_bytes(hook)
    matched = [
        rel
        for rel in _tracked_files()
        if re.search(exclude, rel)
        and (ROOT / rel).is_file()
        and (ROOT / rel).stat().st_size > limit
    ]
    assert matched, (
        f"the exclusion {exclude!r} exempts nothing that is actually over "
        f"{limit // 1024} KB, so it is dead text that only weakens the hook"
    )


# --- CODEOWNERS -------------------------------------------------------------

CODEOWNERS = ROOT / ".github" / "CODEOWNERS"

OWNER_RULES = [
    line.split()
    for line in _text(CODEOWNERS).splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]


def test_codeowners_is_syntactically_valid() -> None:
    """GitHub ignores a malformed rule silently; nothing tells you review stopped."""
    assert OWNER_RULES, "CODEOWNERS has no rules"
    for rule in OWNER_RULES:
        pattern, owners = rule[0], rule[1:]
        assert owners, f"CODEOWNERS rule {pattern!r} names no owner"
        for owner in owners:
            assert owner.startswith("@") or "@" in owner, (
                f"CODEOWNERS owner {owner!r} for {pattern!r} is neither a "
                "@user/@org/team handle nor an email address"
            )


def test_there_is_a_catch_all_rule() -> None:
    """Without `*`, only the listed paths are owned; everything else merges unreviewed."""
    assert any(rule[0] == "*" for rule in OWNER_RULES), (
        "CODEOWNERS has no `*` rule, so any path it does not name has no owner"
    )


@pytest.mark.parametrize(
    "pattern",
    sorted({rule[0] for rule in OWNER_RULES if "*" not in rule[0]}),
)
def test_every_literal_codeowners_path_exists(pattern: str) -> None:
    """A rule for a path that moved protects nothing, and GitHub reports no error."""
    assert (ROOT / pattern.strip("/")).exists(), (
        f"CODEOWNERS assigns an owner to {pattern}, which is not in the tree -- "
        "the rule never matches, so that path is covered only by the catch-all"
    )


# --- .editorconfig agrees with .gitattributes -------------------------------

EDITORCONFIG = ROOT / ".editorconfig"
GITATTRIBUTES = ROOT / ".gitattributes"


def _editorconfig() -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser()
    # `root = true` sits outside any section, which the ini parser will not take.
    parser.read_string("[__preamble__]\n" + _text(EDITORCONFIG))
    return {name: dict(parser[name]) for name in parser.sections()}


def _expand(pattern: str) -> set[str]:
    """`*.{ts,tsx}` -> {`*.ts`, `*.tsx`}. One brace group is all this file uses."""
    match = re.search(r"\{([^}]*)\}", pattern)
    if not match:
        return {pattern}
    head, tail = pattern[: match.start()], pattern[match.end() :]
    return {head + part + tail for part in match.group(1).split(",")}


def test_lf_is_the_default_and_nothing_quietly_overrides_it() -> None:
    r"""A shell script or Dockerfile with CRLF fails in the Linux image with `\r: not found`.

    `.gitattributes` pins `eol=lf` so git writes LF; `.editorconfig` is what
    stops the editor putting CRLF back on the next save. Only `unset` may
    override it, and only for the files git itself excludes from text handling.
    """
    sections = _editorconfig()
    assert sections["__preamble__"].get("root") == "true", (
        ".editorconfig must set `root = true`, or an unrelated file further up "
        "the filesystem silently supplies settings for this repo"
    )
    assert sections["*"].get("end_of_line") == "lf"
    assert sections["*"].get("charset") == "utf-8"
    assert sections["*"].get("insert_final_newline") == "true"
    overrides = {
        name: values["end_of_line"]
        for name, values in sections.items()
        if name not in {"__preamble__", "*"} and "end_of_line" in values
    }
    assert all(value == "unset" for value in overrides.values()), (
        f".editorconfig overrides end_of_line away from lf: {overrides}"
    )


@pytest.mark.parametrize(
    "pattern",
    sorted(
        line.split()[0]
        for line in _text(GITATTRIBUTES).splitlines()
        if line.strip()
        and not line.lstrip().startswith("#")
        and re.search(r"(^|\s)(binary|-text)(\s|$)", line)
    ),
)
def test_every_file_git_treats_as_binary_is_exempt_in_editorconfig(pattern: str) -> None:
    """An editor that "fixes" a binary or a generated bundle corrupts it.

    `.gitattributes` marks these `binary` / `-text` so git never rewrites their
    bytes. The committed SPA bundle is the one that bites: it is checked against
    a hash of its sources (tests/test_spa_bundle.py), so a stray newline or a
    trimmed line invalidates that check while looking like a harmless save.
    """
    exempt = {
        expanded
        for name, values in _editorconfig().items()
        for expanded in _expand(name)
        if values.get("end_of_line") == "unset"
    }
    assert pattern in exempt, (
        f".gitattributes excludes {pattern} from text handling, but "
        f".editorconfig has no section for it setting `end_of_line = unset`. "
        f"Exempt today: {sorted(exempt)}"
    )
