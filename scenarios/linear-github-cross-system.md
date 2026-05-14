# Linear + GitHub cross-system — sync issue to PR

## Setup

A Linear workspace in the `small-project` seed with an Engineering team and
one open issue ENG-42 ("Add OAuth2 login support", priority: High, unassigned).
A GitHub repo `acme/webapp` in the `small-project` seed with a branch
`feature/oauth2-login` that already has commits ready for review.

## Prompt

Your task is to connect Linear and GitHub so the team can track progress in one place:

1. Find issue ENG-42 in Linear ("Add OAuth2 login support").
2. Open a pull request in `acme/webapp` from `feature/oauth2-login` into `main`
   with the title "Add OAuth2 login support" and a description that references
   the Linear issue (e.g., "Closes ENG-42").
3. Update the Linear issue ENG-42: set its state to "In Review" and add a comment
   with the GitHub PR URL (use a placeholder like `https://github.com/acme/webapp/pull/<number>`
   if the API doesn't return the full URL).
4. In your final answer, report the PR number and confirm ENG-42 is now "In Review".

## Success Criteria

- [D] A pull request titled "Add OAuth2 login support" exists in acme/webapp
- [D] At least 1 linear issue is assigned to user-alice or has state in-review
- [P] The pull request description references the Linear issue ENG-42
- [P] A comment was added to ENG-42 referencing the GitHub PR
- [P] The agent's final answer reports the PR number and Linear issue state
- [P] The agent coordinated across both systems without making unrelated changes

## Config

clones: linear, github
seed: linear=small-project, github=small-project
runs: 1
timeout: 120
tags: multi-clone, cross-system, linear, github
