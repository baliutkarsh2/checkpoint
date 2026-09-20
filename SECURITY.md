# Security Policy

## Reporting a vulnerability

Email **security@usecheckpoint.dev** with the details and, if you have one, a
proof of concept. Please do not open a public issue for a security report. We
aim to acknowledge within 48 hours.

Checkpoint intercepts TLS and runs agent code, so the boundaries below are the
product. If you find a way for a scenario, an agent under test, or a dashboard
request to cross one of them, that is in scope and we want to hear about it.

## Supported versions

Checkpoint is pre-1.0. Fixes land on `main` and in the next release; there are
no backports to earlier versions yet.

---

## The security model

Checkpoint runs a program you supply and lies to it about the network. It is
worth knowing exactly how far that goes.

### The certificate authority

To route an agent's real HTTPS calls into a local twin, the proxy terminates TLS
for the hosts it routes and signs leaf certificates with its own CA.

* **A fresh CA is minted per run** and expires after a day.
* **It is never installed in your system or browser trust store.** It is written
  to the run's own directory and handed to the agent through environment
  variables (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`,
  `HTTPLIB2_CA_CERTS`, `NODE_EXTRA_CA_CERTS`). Only the process under test
  trusts it, and only while it runs. Nothing outside the run is affected, and
  there is nothing to uninstall afterwards.
* The private key is written mode 0600 where the OS supports it.
* The trust stores are pointed at a **bundle** — the public roots plus this CA —
  not at the CA alone. Calls that are *not* routed to a twin, such as the agent's
  own traffic to its model provider, keep verifying against the real roots.

### The agent's environment

The agent is your program, started as an ordinary subprocess. **It inherits your
environment**, including every API key in it. Checkpoint overrides the
credentials belonging to the twins in *this* run, so an SDK picks up the twin
instead of the real service — but a credential for a service this run does not
twin is passed through untouched.

What stops that credential reaching the outside world is the **egress policy**,
not the environment:

| `egress` | The agent can reach |
|---|---|
| `none` | the twins, and nothing else |
| `llm` (default) | the twins, plus known model-provider hosts |
| `open` | anything |

`--allow-host` / `allow_hosts` adds to the allowlist under `none` and `llm`;
under `open` there is no allowlist to add to. Blocked connections are recorded
and reported rather than silently dropped. `egress = "open"` removes the only
network boundary Checkpoint provides — use it knowing that.

### The workspace is not a jail

A scenario with `workspace:` copies a fixture directory to a throwaway location
and runs the agent with that copy as its working directory. The fixture itself
is never written to, and every run starts from a fresh copy.

That is a **disposable tree and a diff, not containment**. The agent is a normal
process with your permissions: nothing stops it writing outside the workspace,
reading your home directory, or spawning what it likes. Run agents you trust, or
run Checkpoint inside a container or VM — the bundled `Dockerfile` and
`docker-compose.yml` exist for that.

`--read-only` refuses writes *at the twin* and fails the run if one was
attempted. It constrains what the agent can do to the twins. It does not
constrain the process.

### The dashboard

`checkpoint view` binds to `127.0.0.1` and needs no authentication there,
because `POST /api/jobs` starts the agent under test — serving it is equivalent
to handing out command execution.

Binding anywhere else **requires** `CHECKPOINT_DASHBOARD_API_KEY`; the server
refuses to start on a non-loopback bind without one. Set
`CHECKPOINT_DASHBOARD_READ_ONLY=1` for an instance that should only ever be
looked at. Only point a dashboard at runs and scenarios you trust.

---

## Synthetic credentials

The twins accept synthetic bootstrap tokens shaped like the real thing
(`ghp_…`, `xoxb-…`, `sk_test_…`), so vendor SDKs accept them without complaint.
**None of them is ever a real credential**: each keeps the prefix its SDK
requires and carries the literal marker `CHECKPOINTFAKE` in its body.

They live in one place, `checkpoint/fake_credentials.py`, and are allow-listed
in `.gitleaks.toml`. Do not add a hardcoded token anywhere else — import the
constant instead. Two guards enforce this:

* `tests/test_no_tracked_secrets.py` fails the ordinary test suite if a tracked
  file contains a real-shaped provider key, or a synthetic one without the
  marker. It covers OpenAI, Anthropic, Google, AWS, GitHub, Slack, Stripe,
  GitLab, Hugging Face and PEM private keys.
* [gitleaks](https://github.com/gitleaks/gitleaks) runs over the full history on
  every push and pull request, as a required check.

## Reporting practice for this repository

Secret scanning and push protection are enabled. If you are forking or
self-hosting, turn both on: *Settings → Code security and analysis*.
