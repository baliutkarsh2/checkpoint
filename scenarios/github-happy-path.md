# GitHub happy path — file a bug report

## Setup

A small GitHub workspace with one repository, `acme/webapp`, and no
existing open issues. Use the `small-project` seed.

## Prompt

A user reports that the "Sign in with Google" button stopped working
after the latest deploy. File a bug report on the `acme/webapp`
repository titled "Login broken after deploy" with a short body that
includes the symptom and a step to reproduce. In your final answer,
quote the new issue number.

## Success Criteria

- [D] An issue titled "Login broken after deploy" exists
- [D] The issue body mentions the word "deploy" or "Google"
- [D] The issue is in the open state
- [P] The agent's final answer references the new issue number

## Config

clones: github
seed: small-project
runs: 1
timeout: 60
tags: happy-path, github
