---
twins: linear, github
seed: linear=small-project, github=small-project
timeout: 120
tags: multi-clone, cross-system, linear, github
---
# Linear + GitHub — take an issue into review

## Setup

Linear's `small-project` seed has the Engineering team with ENG-42
("Add OAuth2 login support", priority 2, unassigned) in the Todo state, and a
workflow state called "In Review".

GitHub's `small-project` seed has `acme/webapp` with a `main` branch and a
`fix-login-bug` branch. There is **no** `feature/oauth2-login` branch — the
agent has to create it, and give it a commit of its own, because GitHub refuses
a pull request between two branches that point at the same commit. There are no
pull requests yet and issues #1 and #2 exist, so the first pull request takes
number 3.

## Task

Connect the two systems for ENG-42:

1. In `acme/webapp`, create a branch `feature/oauth2-login` from the head of
   `main`.
2. On that branch, add a file `docs/oauth2.md` sketching the work, with the
   commit message "docs: scope OAuth2 login". Without a commit of its own the
   branch cannot be the head of a pull request.
3. Open a pull request from `feature/oauth2-login` into `main` titled exactly
   "Add OAuth2 login support", with a body that references the Linear issue as
   `Closes ENG-42`.
4. Move ENG-42 to the "In Review" state.
5. Add a comment to ENG-42 containing the pull request URL
   (`https://github.com/acme/webapp/pull/<number>`).

Change nothing else in either system. Report the pull request number and
ENG-42's new state in your answer.

## Criteria

- [D] The feature/oauth2-login branch exists
  => exists(github.branches[key == "acme/webapp:feature/oauth2-login"])
- [D] The branch has a commit of its own that adds docs/oauth2.md
  => exists(created.github.commits[repo == "acme/webapp" && "docs/oauth2.md" in files])
- [D] Exactly 1 pull request was created
- [D] A pull request titled "Add OAuth2 login support" exists
- [D] It merges feature/oauth2-login into main
  => count(created.github.pulls[head == "feature/oauth2-login" && base == "main"]) == 1
- [D] Its body references ENG-42  => count(created.github.pulls[body ~ /ENG-42/]) == 1
- [D] ENG-42 is now In Review
  => count(linear.issues[identifier == "ENG-42" && stateId == "state-in-review"]) == 1
- [D] A comment on ENG-42 links the pull request
  => exists(created.linear.comments[issueIdentifier == "ENG-42" && body ~ /acme\/webapp\/pull\/3/])
- [D] ENG-42 is the only Linear issue that changed
  => count(changed.linear.issues) == 1 && count(changed.linear.issues[identifier == "ENG-42"]) == 1
- [D!] Nothing was deleted in either system
  => count(deleted.linear.issues) == 0 && count(deleted.github.issues) == 0
- [P] The final answer reports the pull request number and ENG-42's new state
