---
twins: github
seed: small-project
timeout: 60
tags: archal-verbatim, parity, github
---
# Open a pull request for the login fix

## Setup

The `small-project` seed: `acme/webapp` has a `main` branch, a `fix-login-bug`
branch whose head commit is "fix: persist the session cookie on Safari", the
labels `bug`, `enhancement` and `in-progress`, and the user `reviewer1`. There
are no pull requests yet, and issues #1 and #2 are open — so a new pull request
takes number 3.

## Task

Open a pull request in `acme/webapp` from `fix-login-bug` into `main`, titled
exactly "Fix login bug", with a body that says what the fix does. Request a
review from `reviewer1` and apply the `bug` label to the pull request. Do not
touch the existing issues. End your answer with the pull request number written
as `#N`.

## Criteria

- [D] Exactly 1 pull request was created
- [D] A pull request titled "Fix login bug" exists
- [D] It merges fix-login-bug into main
  => created.github.pulls.head == "fix-login-bug" && created.github.pulls.base == "main"
- [D] reviewer1 is a requested reviewer on it
  => "reviewer1" in created.github.pulls.requested_reviewers
- [D] It carries the bug label  => "bug" in created.github.pulls.labels
- [D] The final answer quotes the pull request number  => answer ~ /#3\b/
- [D!] No issues or pull requests were deleted
  => count(deleted.github.issues) == 0 && count(deleted.github.pulls) == 0
- [D!] The seeded issues were left alone  => count(changed.github.issues) == 0
- [T] The agent made at most 15 calls
- [P] The pull request body explains what the fix changes
