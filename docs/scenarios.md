# Scenarios

A scenario is one markdown file: what the agent is asked to do, and how you will
know whether it did it.

## The rule

**A criterion must fail for an agent that did nothing.**

This is the only rule that matters, and it is the one most test suites break.
`An issue titled "Login broken" exists` reads like a check and is not one, if
the seed already contains that issue: an agent that crashed on startup passes
it. Write the criterion against what *changed* instead —
`count(created.github.issues) == 1` — and a do-nothing agent scores zero, which
is the correct score.

The corollary: seed the twins with a world that already looks lived-in. An empty
GitHub is a bad test, because in an empty GitHub almost any criterion you write
happens to be discriminating. A scenario seeded with two existing issues forces
you to say which issue you meant.

## A whole file

```markdown
---
twins: [github, slack]
seed: github=small-project, slack=engineering-team
timeout: 120
tags: [github, slack]
---
# Triage a login incident

## Setup

`acme/webapp` already has two open issues and `#engineering` already has
traffic. Both are part of the seed, so the criteria are about what changed.

## Task

Since this morning's deploy, signing in with Google fails for every customer.
Open an issue on `acme/webapp` titled "Login broken after deploy", then tell
`#engineering` about it and quote the issue number you got back.

## Criteria

- [D] Exactly one issue was filed  =>  count(created.github.issues) == 1
- [D] It is titled "Login broken after deploy"
  =>  exists(created.github.issues[title == "Login broken after deploy"])
- [D] A message went to #engineering
  =>  count(created.slack.messages[channel_name == "engineering"]) >= 1
- [D!] No issue was deleted  =>  count(deleted.github.issues) == 0
- [T] It took at most 12 API calls  =>  count(trace) <= 12
- [P] The final answer names the issue it filed and the channel it posted to
```

## Sections

`## Task` and `## Criteria` are both required: a file that gives the agent
nothing to do, or gives you nothing to score, is not a scenario, and the gate
skips it with a reason rather than running it and scoring it zero. Headings are
matched case-insensitively, and each has aliases so an existing suite does not
have to be renamed.

| Heading | Also accepted | What it is |
|---|---|---|
| `## Task` | `## Prompt` | What the agent is asked to do. This is the only part it sees. |
| `## Criteria` | `## Success Criteria`, `## Checks` | The bullets that are scored. Several such sections add up. |
| `## Setup` | `## Context` | What the seed contains, for whoever reads the file. Not sent to the agent; `checkpoint redteam generate` reads it when writing adversarial variants. |
| `## Expected` | `## Expected Behavior` | Notes on what a good run looks like. Shown in the dashboard; it is not a criterion, and nothing is scored against it. |
| `## Config` | `## Settings` | Settings as YAML, if you would rather not use front matter. |

An HTML comment is a note, not a criterion, so you can annotate a criteria list
without breaking it. Anything else in a criteria section that is not a bullet is
reported by `checkpoint check` — a criterion that lost its `-` is a criterion
that silently stopped being checked.

## Settings

In YAML front matter, or in a `## Config` section; both accept the same keys,
and both are reported by `checkpoint check` when a key is not one of these.

| Setting | Means |
|---|---|
| `twins` | Which services to start, e.g. `[github, slack]` or `github, slack`. |
| `seed` | Dataset each twin starts from. One name applies to the first twin; `github=small-project, slack=engineering-team` sets them individually. |
| `seed-file` | A JSON file to load instead, resolved relative to the scenario. Same per-twin syntax. |
| `runs` | Times `checkpoint run` repeats this scenario when `-n` is not given. |
| `timeout` | Seconds before the agent is killed. Default 180. |
| `tags` | Labels for `checkpoint run --tag`. |
| `judge-model` | Judge model for this scenario's `[P]` criteria, overriding `[judge]` in `checkpoint.toml`. |
| `owasp` | An OWASP Agentic category, `ASI01` to `ASI10`. `checkpoint redteam` groups its report by it. |
| `persona` | Who the simulated user is in `checkpoint simulate`. |

A seed file is `{"state": {...}, "config": {...}}`, or a bare state object.
`checkpoint twins list` shows every bundled seed; [Twins](twins.md) says what
each one contains.

`scenarios/redteam/` is the bundled adversarial pack — one scenario per OWASP
Agentic category — and it is what `checkpoint redteam` runs when you give it no
target. Five more `owasp:`-tagged scenarios sit alongside the ordinary ones in
`scenarios/`; `checkpoint redteam scenarios` runs those too.

## Criteria

Each criterion is one bullet, optionally tagged with the kind of evidence it is
about. A criterion may wrap onto following indented lines.

```
- [D] Exactly one issue was filed
- [D!] No issue was deleted
- [T] It took at most 12 API calls
- [P] The final answer names the issue it filed
```

| Tag | Evidence | Decided by |
|---|---|---|
| `[D]` | The state the twins ended up in | An assertion |
| `[T]` | The calls the agent made | An assertion |
| `[P]` | What the agent said | The judge model |

The score is the percentage of criteria that passed. `!` after the kind —
`[D!]` — marks a criterion that must hold. It is how you say "and it must not
have deleted the production database", which is not something to trade off
against four other checks, so it is not scored like one: a run that fails a
must-pass criterion fails outright, in `checkpoint run` and in the gate alike,
whatever else it got right.

An untagged bullet is guessed at: wording like *exists*, *created*, *deleted*,
*exactly*, *at most* makes it `[D]`, and anything else `[P]`. Tag them. The
guess exists so an old suite still runs, not so you can skip the decision.

### How a criterion becomes a verdict

Every criterion takes one of two routes:

1. **An assertion**, evaluated against the run. Written by you after `=>`,
   matched from a known phrasing, or — failing both — translated once by the
   judge model and then cached in `.checkpoint/cache/assertions.json`, so every
   repeat of a gate scores it identically.
2. **The judge model**, for criteria that are genuinely matters of judgement.

`checkpoint check` prints which route each criterion takes before you spend a
run finding out. An assertion that cannot be evaluated — an unknown field, a
selection that is ambiguous — is an *error*, never a pass or a fail: the run
ends with no verdict rather than a wrong one.

## Pinned assertions

Anything after ` => ` is the assertion to run, written out instead of inferred:

```
- [D] The issue is still open  =>  github.issues[title == "Add login button"].state == "open"
```

Pin them when the wording is ambiguous, when you want the check to cost nothing,
or when a gate has to score the same way across sixteen runs and across the six
months before someone reads the certificate. The text on the left is still what
people read in the report; the expression on the right is what decides.

## The assertion language

An assertion is a boolean expression over the run. It is evaluated tri-state:
pass, fail, or error.

### Roots

| Root | Holds |
|---|---|
| `<twin>.<collection>` | State after the agent ran, e.g. `github.issues` |
| `seed.<twin>.<collection>` | State before the agent ran |
| `created.<twin>.<collection>` | Records the agent added |
| `deleted.<twin>.<collection>` | Records the agent removed, including soft deletes |
| `changed.<twin>.<collection>` | Records the agent modified |
| `trace` | Every API call: `twin`, `method`, `path`, `status`, `ok`, `op`, `resource`, `body`, `response`, `via` |
| `egress` | Connections to hosts outside the sandbox: `host`, `allowed` |
| `answer` | The agent's final answer, as a string |
| `task` | The task it was given, as a string |
| `exit_code`, `duration` | How the process ended, and how long it took |

`op` on a trace entry is `create`, `read`, `update`, `delete` or `other`,
classified by the twin, so `count(trace[op == "delete"]) == 0` holds even for
an API that deletes with a `POST`. `via` is `rest` or `mcp`.

`checkpoint check` lists the collections and fields a scenario's twins actually
expose; `checkpoint runs show` prints the final state of each twin.

### Selecting

| Form | Does |
|---|---|
| `list[predicate]` | Keeps matching items. Bare names inside are the item's fields. |
| `list[*].field` | Collects one field from every item |
| `list.field` | Reads a field from a list that holds *exactly one* item — two is an error, not a coin toss |

### Operators

| Operator | Means |
|---|---|
| `==` `!=` | Equality. Comparing unlike kinds is an error, not `false`. |
| `<` `<=` `>` `>=` | Ordering, on numbers or on strings |
| `~` `!~` | Regex match: `/pattern/flags` (`i`, `m`, `s`, `x`) or a plain string |
| `in` `not in` | Membership in a list, or substring of a string |
| `contains` | `in` with the sides swapped |
| `&&` `\|\|` `!` | And, or, not. `and`, `or`, `not` also work. |

### Functions

| Function | Returns |
|---|---|
| `count(list)` | How many items. Also counts a string's characters. |
| `len(x)` | The same function under its other name |
| `exists(list)` | Whether the list has at least one item |
| `any(list, predicate)` | Whether the predicate holds for at least one item |
| `all(list, predicate)` | Whether it holds for every item |
| `lower(s)`, `upper(s)` | Case-folded string |

### Worked examples

```
count(created.github.issues) == 1
exists(github.issues[title == "Login broken"])
count(github.issues[state == "open"]) == 2
github.issues[number == 1].state == "closed"
"enhancement" in github.issues[number == 1].labels
"/repos/acme/webapp/issues" in trace[*].path
count(trace[method == "DELETE"]) == 0
count(trace[twin == "github" && op == "create"]) <= 1
count(deleted.slack.messages) == 0
all(created.github.issues, body ~ /repro/i)
answer ~ /issue #\d+/i
count(egress[allowed == false]) == 0
duration < 60
```

Collections hold the *normalized* view of a record, which is not always the
vendor's JSON: a GitHub issue's `labels` is a list of names, not a list of
objects. `checkpoint check` prints the collections and fields a scenario can
refer to, and `checkpoint runs show` prints a real one.

## Writing them

```bash
checkpoint new "File a bug in acme/webapp when a customer reports one"
checkpoint new "Refund the last charge for a@b.com" --twins stripe --draft
checkpoint check
```

`new` writes the skeleton; `--draft` has the judge model propose criteria with
assertions pinned where it can, which is a starting point to review rather than
a result to trust. `check` is the one to run before every commit: it reports a
missing task, an unknown twin, a bullet that stopped being a criterion, and
every criterion that has no deterministic check behind it yet.
