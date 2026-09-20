---
twins: [github, slack]
seed: github=small-project, slack=engineering-team
timeout: 120
tags: [github, slack]
---
# Triage a login incident

## Setup

`acme/webapp` already has two open issues, and the Slack workspace already has
an `#engineering` channel with traffic in it. Both are part of the seed, so a
criterion that only asks whether an issue or a message exists would pass for an
agent that did nothing. Every criterion below is about what *changed*.

## Task

Support has escalated: since this morning's deploy, signing in with Google
fails for every customer. Open an issue on `acme/webapp` titled "Login broken
after deploy" whose body says what broke and when, then tell `#engineering`
about it and quote the issue number you got back.

## Criteria

- [D] Exactly one issue was filed  =>  count(created.github.issues) == 1
- [D] It is titled "Login broken after deploy"
  =>  exists(created.github.issues[title == "Login broken after deploy"])
- [D] Its body says the deploy broke it  =>  exists(created.github.issues[body ~ /deploy/i])
- [D] A message went to #engineering
  =>  count(created.slack.messages[channel_name == "engineering"]) >= 1
- [D] That message quotes the issue number
  =>  exists(created.slack.messages[channel_name == "engineering" && text ~ /#\d+/])
- [D!] No issue was deleted  =>  count(deleted.github.issues) == 0
- [T] It took at most 12 API calls  =>  count(trace) <= 12
- [P] The final answer names the issue it filed and the channel it posted to
