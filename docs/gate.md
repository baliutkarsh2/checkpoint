# The gate

`checkpoint run` tells you what happened once. `checkpoint gate` decides whether
this build ships, from enough runs that the answer means something.

```bash
checkpoint gate
```

```
 Scenario            Pass   Rate     95% CI      pass^8   Reading
──────────────────────────────────────────────────────────────────
 file-an-issue.md   16/16   100%   [81%, 100%]     100%   stable pass
┌──── gate ────┐
│ SHIP  exit 0 │
└──────────────┘
```

## Verdicts and exit codes

| Verdict | Exit | Means |
|---|---|---|
| `SHIP` | 0 | Every scenario is a confident pass |
| `BLOCK` | 1 | A confident failure, a regression, or a scenario that failed every run |
| `CONDITIONAL` | 2 | Enough runs to decide, and the results are genuinely mixed |
| `INCONCLUSIVE` | 3 | Too few runs for SHIP to be reachable; the output says how many it needs |
| `ERROR` | 4 | The sandbox, the judge or the scenarios broke, so no verdict is possible |

**Only SHIP exits 0.** Every other outcome fails the build, including "the
evidence cannot decide", because the failure this cannot afford is a green
pipeline for an agent that was never shown to work. `--allow-conditional` opts
into shipping on a middling result; nothing opts into shipping on no evidence.
`--strict` refuses CONDITIONAL even when a shared `checkpoint.toml` allowed it,
which is how a release pipeline tightens a config it does not own.

The worst scenario decides the build. Gating an empty directory is an ERROR,
not a SHIP: gating nothing is a broken invocation, not a passing one.

## Why sixteen runs

A single pass is not evidence. The gate scores each scenario's pass rate with a
Wilson confidence interval and asks whether the *lower bound* clears
`--ship-min` (0.80 by default, at 95% confidence). For a flawless n-of-n that
bound collapses to `n / (n + z²)`, so the smallest n that can clear 0.80 is

```
n = ceil(0.80 × 1.96² / (1 − 0.80)) = 16
```

Below sixteen, no number of consecutive passes can SHIP. That is why an
underpowered run reports INCONCLUSIVE with the number it needs, rather than
being waved through as a weak pass:

```
file-an-issue.md: 3/3 runs passed, 95% CI [0.44, 1.00] — cannot decide at n=3:
SHIP needs >= 16 clean runs at ship_min 0.80
```

Sixteen is also why one flake matters. At 16 runs a single failure gives a CI
lower bound of 0.72, which lands CONDITIONAL — correctly, because one flake in
sixteen is a real failure mode for about 6% of your users.

## How each scenario is read

A run counts as a pass when it finished cleanly, broke no must-pass criterion,
and scored at least `--pass-threshold`, 80 out of 100. A run that crashed or
timed out counts as a failure; a run whose sandbox never started is kept out of
the denominator entirely, because a sandbox that would not start says nothing
about the agent.

A `[D!]` criterion is a floor rather than a weighting: a run that breaks one
scores zero here however well it did on everything else, so "it must not have
deleted the production database" cannot be outvoted by four checks that passed.
The gate names the criterion and the run in its output.

Those passes and failures become one reading per scenario:

| Reading | When |
|---|---|
| `stable pass` | The CI lower bound clears `ship_min` |
| `stable fail` | The CI upper bound is at or under `block_max` (0.50), or every run failed |
| `regression` | A meaningful drop against the stored baseline |
| `inconclusive` | Fewer runs than SHIP needs |
| `flaky` | Enough runs, and the interval straddles both thresholds |
| `error` | No usable sample at all |

Zero passes is `stable fail` however few the runs: an agent that never once
succeeded has not earned a soft verdict.

The `pass^k` column is the chance that k consecutive runs all succeed, from the
unbiased estimator `C(passes, k) / C(n, k)`. It is there because a 90%-pass
agent has only a ~35% chance of ten clean runs in a row, and a mean pass rate
hides that.

## Baselines and regressions

Pass rates are remembered per scenario in `.checkpoint/baselines.json` and
compared on the next gate, so a scenario that used to pass and now does not
reads as a `regression` — which is a BLOCK — instead of as ordinary flakiness.

Three rules keep the ledger honest:

- **Only a SHIP updates a baseline.** Saving a flaky rate let the ledger follow
  a degrading agent downward, each step too small to trip the threshold, so a
  collapse never read as a regression. A baseline is the last rate anyone was
  confident about, or nothing.
- **Keyed by path relative to the gate target**, so `github/smoke.md` and
  `slack/smoke.md` stop overwriting each other.
- **Fingerprinted by the scenario's criteria.** Change what passing means and
  the old rate is discarded rather than reported as a regression.

`checkpoint init` writes `.checkpoint/` into `.gitignore`, so `git add
.checkpoint/baselines.json` is refused. Restore the file with your CI cache
instead — without it every run is a first run with nothing to compare against.
To keep it in git rather than in a cache, exclude the directory's contents
rather than the directory, because git will not re-include a file whose parent
is excluded:

```gitignore
.checkpoint/*
!.checkpoint/baselines.json
```

`--no-baseline` neither reads nor writes the file.

## Tuning

| Flag | Default | Means |
|---|---|---|
| `-n`, `--runs` | 16 | Runs per scenario |
| `--pass-threshold` | 80 | Score out of 100 a single run needs to count as a pass |
| `--ship-min` | 0.80 | CI lower bound required to SHIP |
| `--block-max` | 0.50 | CI upper bound at or under which to BLOCK |
| `--confidence` | 0.95 | Confidence level for the interval |
| `--regression-drop` | 0.20 | Pass-rate drop against the baseline that reads as a regression |
| `--report-only` | off | Print the verdict and exit 0 whatever it was (see below) |
| `-j`, `--concurrency` | 4 | Runs of one scenario at once, each in its own sandbox; scenarios still run in sequence. The CPU count, when that is lower |

`block_max` must stay below `ship_min`, or a scenario could be a confident pass
and a confident fail at once; the gate refuses to start rather than pick one.
The same settings live under `[gate]` in `checkpoint.toml`, and a flag beats
the file.

## Adopting it without blocking anything

Turning a gate on for an existing project is the moment you find out what it
thinks, and the first BLOCK usually arrives before anyone is ready to act on it.

```bash
checkpoint gate --report-only
```

prints the verdict exactly as it would otherwise — BLOCK is still BLOCK, and
`--json` still carries the real `exit_code` — and then exits 0, saying so on the
last line. Run it that way until the verdict is one you would have acted on
anyway, then drop the flag.

It is deliberately a flag and not a `checkpoint.toml` setting. A config file is
shared and long-lived, and a line in one that permanently greens the gate would
be invisible in the log that matters. It also replaces the thing teams reach for
instead — `checkpoint gate || true`, which swallows ERROR as well, the one
verdict that means the gate itself broke rather than the agent.

## In CI

The bundled action wraps the command and publishes the verdict as an output:

```yaml
- uses: baliutkarsh2/checkpoint@main
  with:
    target: scenarios/
    runs: "16"
    certificate: checkpoint-certificate.json
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}   # only for [P] criteria
```

| Input | Default | Means |
|---|---|---|
| `target` | `scenarios/` | Scenario file or directory to gate |
| `command` | — | Command that runs your agent. Optional when `checkpoint.toml` declares one. |
| `runs` | `16` | Runs per scenario |
| `pass-threshold` | `80` | Score a single run needs to count as a pass |
| `allow-conditional` | `false` | Pass the build on CONDITIONAL |
| `strict` | `false` | Refuse CONDITIONAL even when `allow-conditional` is set |
| `certificate` | — | Where to write a signed certificate of the verdict |
| `model` | — | Judge model for `[P]` criteria |
| `name` | — | Agent name recorded in the certificate |
| `version` | — | `checkpoint-agents` version to install |
| `python-version` | — | Python to set up. Left empty on purpose: setting it would change the interpreter your agent runs under. |

The step exits with the gate's own code, so the build fails on anything but
SHIP. The action passes `--no-baseline` because a fresh runner has no history;
`checkpoint init` writes a workflow that restores `.checkpoint/baselines.json`
from the Actions cache instead, which is what turns "this used to pass" into a
reported regression.

## Evidence

Every run the gate makes is recorded, stamped with the id of the gate it
belongs to — the same id the certificate carries. So a verdict is something you
can open rather than take on faith:

```bash
checkpoint gate --json | jq -r .gate_id     # the id this verdict was given
checkpoint runs list                        # every run behind it
checkpoint runs show <run-id>               # what one of them actually did
```

```bash
checkpoint gate --certificate build.cert.json    # issue
checkpoint cert verify build.cert.json           # check the signature and expiry
checkpoint redteam --json > redteam.json         # the security half
checkpoint report --certificate build.cert.json --redteam redteam.json --out assurance.md
```

There is no `cert issue`: a certificate is issued by the run that earned it, so
none can exist without the evidence behind it. An ERROR gate earns nothing —
the sandbox, the judge or the scenarios broke before anything was measured — so
`--certificate` writes no file and says so, rather than leaving you a document
that verifies and attests to nothing. INCONCLUSIVE and BLOCK do get one: "not
enough evidence to ship" and "the evidence says no" are both findings worth
signing. `checkpoint report` assembles the verdict, the statistics, the
adversarial results and the cross-references a reviewer asks for into one
document.

Its grade is the word a reader quotes without reading the rest of the page, so
it is deliberately hard to earn. A certificate whose signature does not verify
is REJECTED however good its numbers look. So is a gate verdict that never said
ship. And a report built **without** a red-team run is CONDITIONAL, not
APPROVED, with the security section saying plainly that nobody looked —
otherwise "we never tested that" prints identically to "we tested it and it
held".
