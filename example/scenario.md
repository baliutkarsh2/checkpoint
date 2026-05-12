# Create an Issue (route-mode smoke test)

## Setup
A fresh, empty GitHub workspace. The agent talks to `https://api.github.com`
directly via stock `requests` — the TLS sidecar intercepts and forwards to
the local GitHub twin.

## Prompt
Create a GitHub issue in repository "acme/webapp" titled "hello world" with a
brief, friendly body describing a greeting.

## Success Criteria
- [D] An issue titled "hello world" exists
- [D] At least one issue is created
- [P] The issue body is a non-empty, friendly greeting
- [P] The agent did not create any unrelated issues

## Config
clones: github
runs: 1
timeout: 120
