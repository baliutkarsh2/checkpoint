---
twins: github
seed: small-project
timeout: 60
tags: happy-path, github
---
# GitHub happy path — file a bug report

## Setup

The `small-project` seed: the `acme/webapp` repository with two open issues
(#1 "Add dark mode", #2 "Login broken on Safari") and three labels — `bug`,
`enhancement` and `in-progress`. Because the repository already has issues,
every criterion below is written against what the agent *changed*, not against
how many issues exist.

## Task

A user reports that the "Sign in with Google" button stopped working after the
latest deploy. File a bug report on `acme/webapp` titled exactly
"Login broken after deploy", with a body that names the symptom and one step to
reproduce, and apply the existing `bug` label to it. Change nothing else.
End your answer with the new issue number written as `#N`.

## Criteria

- [D] Exactly 1 issue was created
- [D] An issue titled "Login broken after deploy" exists
- [D] The new issue is open and carries the bug label
  => count(created.github.issues[state == "open" && "bug" in labels]) == 1
- [D] The new issue's body names the deploy or the Google button
  => count(created.github.issues[body ~ /deploy|google/i]) == 1
- [D] The final answer quotes the new issue's number  => answer ~ /#3\b/
- [D!] No issues were deleted
- [D!] The two seeded issues were left alone  => count(changed.github.issues) == 0
- [T] The agent made at most 12 calls
- [P] The final answer describes the bug it filed, not just the issue number
