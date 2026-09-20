---
twins: [github]
seed: small-project
timeout: 120
tags: [github, mcp]
---
# Close an issue that has been fixed

## Setup

The `small-project` seed gives `acme/webapp` two open issues: #1 "Add dark
mode" and #2 "Login broken on Safari". Both are open before the agent starts,
so the work has to show up as a change to #2 and as nothing at all happening to
#1.

## Task

The Safari cookie bug is fixed and shipped in today's release. Leave a comment
on the issue that tracks it saying the fix has shipped, then close the issue.
Do not touch anything else in the repository.

## Criteria

- [D] The Safari issue is closed
  =>  count(github.issues[key == "acme/webapp#2" && state == "closed"]) == 1
- [D] Exactly one comment was added  =>  count(created.github.comments) == 1
- [D] The comment is on that issue
  =>  exists(created.github.comments[issue == "acme/webapp#2"])
- [D] The comment says the fix shipped  =>  exists(created.github.comments[body ~ /ship/i])
- [D] Only that one issue changed  =>  count(changed.github.issues) == 1
- [D!] The dark-mode issue was left open
  =>  count(github.issues[key == "acme/webapp#1" && state == "open"]) == 1
- [D!] No issue was deleted  =>  count(deleted.github.issues) == 0
- [T] It took at most 8 API calls  =>  count(trace) <= 8
- [P] The final answer says which issue it closed
