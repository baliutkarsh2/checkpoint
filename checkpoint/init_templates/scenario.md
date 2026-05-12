# Starter scenario — create an issue

## Setup

A small GitHub workspace with one repository called `acme/webapp` and no
existing issues. Use the `small-project` seed.

## Prompt

Create an issue in the `acme/webapp` repository titled "Add login button"
with a body that briefly describes the request. Confirm in your final
answer that the issue was created and quote the issue number.

## Success Criteria

- [D] At least one issue exists in `acme/webapp` after the run
- [D] An issue titled "Add login button" exists
- [P] The agent's final answer references the issue number it created

## Config

clones: github
seed: small-project
runs: 1
timeout: 60
tags: starter, github
