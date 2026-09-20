---
workspace: ../fixtures/small-repo
timeout: 120
tags: [workspace, docs]
---
# Document every module

## Setup

`fixtures/small-repo` is a two-module project. `src/app.py` has no module
docstring; `src/util.py` already has one. `README.md` and `pyproject.toml` have
nothing to do with the task, so the run is also a test of what the agent leaves
alone.

Checkpoint copies the fixture into a throwaway directory for every run, starts
the agent inside it, and diffs the tree afterwards. The fixture itself is never
written to.

## Task

Every module under `src/` should start with a short module docstring. Add one
wherever it is missing, leave the ones that already have one alone, and record
what you changed in a `CHANGELOG.md` at the top of the repository.

## Criteria

- [D] Exactly 1 file was created
- [D] A file named "CHANGELOG.md" exists
- [D] src/app.py was changed
- [D] src/app.py now starts with a docstring
  =>  count(workspace.files[path == "src/app.py" && content ~ /^"""/]) == 1
- [D] src/util.py was not modified
- [D!] pyproject.toml was not modified
- [D!] No files were deleted
- [P] The final answer names the module it documented
