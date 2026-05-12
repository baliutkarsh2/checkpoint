# GitHub full-surface acceptance

## Setup

A fresh GitHub workspace. The agent will create a repo, push files on a
feature branch, open a pull request, list workflow runs, search code, and
merge the PR.

## Prompt

In repository "acme/webapp": create the repo, push README.md and src/app.py
on a branch named "feature/launch", open a pull request titled
"Launch initial version" from feature/launch into main, then merge it.

## Success Criteria

- [D] At least one repository named "webapp" exists
- [D] At least one pull request is created
- [D] At least one pull request is merged
- [D] No errors are returned
- [P] The merged pull request title contains the word "Launch"

## Config

clones: github
runs: 1
timeout: 60
