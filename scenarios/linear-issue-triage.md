---
twins: linear
seed: backlog-triage
timeout: 90
tags: triage, linear
---
# Linear — backlog triage and sprint assignment

## Setup

The `backlog-triage` seed: the Engineering team (`ENG`) with five issues,
ENG-10 through ENG-14, all in the Backlog state, all unassigned, all at
priority 0 ("No priority") and none with an estimate. Labels are Bug, Feature
and Infra; the members are Alice Chen (`user-alice`) and Bob Smith
(`user-bob`).

## Task

You are triaging the Engineering backlog. Do exactly this:

1. Set ENG-10 ("Database slow on large queries") to priority 2 (High) and
   assign it to Alice Chen.
2. Set ENG-13 ("Email notifications not sent on signup") to priority 1 (Urgent)
   and assign it to Bob Smith.
3. Leave ENG-11, ENG-12 and ENG-14 at their current priority, but give each an
   estimate of 5.

Create nothing and delete nothing. In your answer, list all five issues with
their priority, assignee and estimate.

## Criteria

- [D] ENG-10 is High priority and assigned to Alice Chen
  => linear.issues[identifier == "ENG-10"].priority == 2 && linear.issues[identifier == "ENG-10"].assigneeId == "user-alice"
- [D] ENG-13 is Urgent priority and assigned to Bob Smith
  => linear.issues[identifier == "ENG-13"].priority == 1 && linear.issues[identifier == "ENG-13"].assigneeId == "user-bob"
- [D] ENG-11, ENG-12 and ENG-14 each have an estimate of 5
  => all(linear.issues[identifier in ["ENG-11", "ENG-12", "ENG-14"]], estimate == 5)
- [D] ENG-11, ENG-12 and ENG-14 kept their priority
  => all(linear.issues[identifier in ["ENG-11", "ENG-12", "ENG-14"]], priority == 0)
- [D] Exactly 5 issues were changed
- [D!] No issues were deleted
- [D!] No issues were created
- [T] The agent made at most 30 calls
- [P] The final answer lists all five issues with their new priority, assignee and estimate
