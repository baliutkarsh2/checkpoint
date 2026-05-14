# Linear — backlog triage and sprint assignment

## Setup

A Linear workspace with 5 backlog issues (ENG-10 through ENG-14) across
three labels: Bug, Feature, and Infra. No priorities have been set yet.
Two team members: Alice Chen (admin) and Bob Smith. Use the `backlog-triage` seed.

## Prompt

You are a tech lead triaging the Engineering team's backlog. Your tasks:

1. Set ENG-10 ("Database slow on large queries") to priority High and
   assign it to Alice Chen.
2. Set ENG-13 ("Email notifications not sent on signup") to priority Urgent
   and assign it to Bob Smith.
3. Leave the remaining issues (ENG-11, ENG-12, ENG-14) at their current
   priority but add an estimate of 5 to each.
4. In your final answer, list all issues and their new priority/assignee.

## Success Criteria

- [D] At least 1 linear issue is assigned to user-alice
- [D] At least 1 linear issue is assigned to user-bob
- [P] ENG-10 has been set to High priority (priority=2)
- [P] ENG-13 has been set to Urgent priority (priority=1)
- [P] ENG-11, ENG-12, and ENG-14 each have an estimate of 5
- [P] The final answer lists all five issues with their updated state

## Config

clones: linear
seed: backlog-triage
runs: 1
timeout: 90
tags: triage, linear
