---
twins: github
seed: small-project
timeout: 60
tags: pull-request, github
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
  => count(created.github.pulls[head == "fix-login-bug" && base == "main"]) == 1
- [D] reviewer1 is a requested reviewer on it
  => count(created.github.pulls["reviewer1" in requested_reviewers]) == 1
- [D] It carries the bug label  => count(created.github.pulls["bug" in labels]) == 1
- [D] The final answer quotes the pull request number  => answer ~ /#3\b/
- [D!] No issues or pull requests were deleted
  => count(deleted.github.issues) == 0 && count(deleted.github.pulls) == 0
- [D!] The seeded issues were left alone  => count(changed.github.issues) == 0
- [T] The agent made at most 15 calls
- [P] The pull request body explains what the fix changes
