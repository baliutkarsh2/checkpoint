# Twins

A twin is a stateful in-memory copy of a service your agent calls. It speaks
the real API — the paths, the payload shapes, the error envelopes and the
headers the official SDKs depend on — and it remembers what was done to it, so a
criterion can check the state the agent left rather than the sentence the agent
wrote about it.

Twins are not mocks. There is no recorded fixture and no pre-arranged response:
the agent's calls change the twin's state, and the next call sees the change.

## The seven

```bash
checkpoint twins list
```

| Twin | Intercepts | Credential the SDKs read | Seeds |
|---|---|---|---|
| `github` | `api.github.com`, `uploads.github.com` | `GITHUB_TOKEN`, `GH_TOKEN` | `empty`, `small-project`, `large-backlog`, `stale-issues`, `enterprise-repo`, `ci-cd-pipeline`, `merge-conflict`, `rate-limited`, `permissions-denied` |
| `slack` | `slack.com` | `SLACK_BOT_TOKEN`, `SLACK_TOKEN` | `empty`, `engineering-team`, `busy-workspace`, `incident-active` |
| `stripe` | `api.stripe.com` | `STRIPE_API_KEY`, `STRIPE_SECRET_KEY` | `empty`, `small-business`, `checkout-flow`, `subscription-heavy`, `subscription-lifecycle` |
| `linear` | `api.linear.app` | `LINEAR_API_KEY` | `empty`, `small-project`, `backlog-triage`, `sprint-planning` |
| `supabase` | `supabase.co` | `SUPABASE_KEY`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | `empty`, `small-app`, `ecommerce` |
| `discord` | `discord.com`, `discordapp.com` | `DISCORD_TOKEN`, `DISCORD_BOT_TOKEN` | `empty`, `small-server`, `incident-response` |
| `google-workspace` | `gmail.googleapis.com`, `www.googleapis.com`, `oauth2.googleapis.com` | `GOOGLE_OAUTH_ACCESS_TOKEN` | `empty`, `small-team` |

A domain covers its subdomains, so `supabase.co` catches
`<project>.supabase.co`. `gmail` and `google` are accepted as names for
`google-workspace`.

What each one models:

- **github** — repos, refs, file contents, commits, issues, comments, labels,
  assignees, pull requests with reviews and merges, releases, workflow runs and
  search. Records carry absolute hypermedia URLs because clients build their
  next request out of them.
- **slack** — the Web API as RPC at `/api/<method>`, including its habit of
  reporting application errors with HTTP 200 and `{"ok": false}`. The credential
  belongs to a bot that can read and post anywhere without joining first.
- **stripe** — customers, products and prices, payment intents with their
  charges and refunds, invoices, subscriptions, checkout sessions, payment
  methods, coupons, disputes, events and the balance. Accepts Stripe's
  form encoding as well as JSON, and honours `Idempotency-Key`.
- **linear** — GraphQL at `/graphql`, served from Linear's published schema, so
  `@linear/sdk` and hand-written queries both work.
- **supabase** — one project as its four real services behind one origin:
  PostgREST rows and filters, auth, storage, and edge-function stubs, each with
  its own error shape. The database has exactly the tables the seed declares, so
  querying a table that does not exist fails as it would in production.
- **discord** — the v10 REST surface: applications, guilds, members, roles,
  channels, threads, messages with uploads and reactions, and webhooks. Every
  id is a snowflake integer, because the official clients decode them as such.
- **google-workspace** — Gmail, Drive and Calendar on their real paths, plus
  media and resumable uploads, batch fan-out, and the OAuth token exchange —
  without which a service account would refresh its token against real Google.

Every twin also serves its operations over MCP, at `/mcp/` under its base URL.
`checkpoint twins tools github` lists them.

## Seeds

A seed is the world the twin starts from. Pick one per twin in the scenario:

```yaml
twins: [github, slack]
seed: github=small-project, slack=engineering-team
```

A seed with no `twin=` prefix applies to the first twin only. `empty` is what
you get with no seed at all.

Seed with something that already looks lived-in. A criterion written against an
empty service is almost always accidentally discriminating — see
[Scenarios](scenarios.md#the-rule).

Your own seed is a JSON file, named with `seed-file` and resolved relative to
the scenario:

```json
{
  "state": {"repos": {"acme/webapp": {"name": "webapp", "full_name": "acme/webapp"}}},
  "config": {"rate_limit": 5}
}
```

A bare state object works too. `checkpoint runs show` prints the state a run
ended with, which is the quickest way to learn a twin's shape.

## Breaking them on purpose

The `config` block of a seed sets knobs and faults. They are the same eight for
every twin, and each twin shapes the resulting error the way the real service
does — an SDK has to parse it into the exception and the retry it would take in
production, or the test is not testing the error path you ship.

| Knob | Effect |
|---|---|
| `rate_limit` | Requests allowed before every call returns 429 |
| `read_only` | Writes are refused |
| `permissions_denied` | Writes are refused as a permissions error |
| `latency_ms` | Delay added to every call |
| `error_rate` | Fraction of calls that fail with 500, seeded so it repeats |
| `fault_seed` | The seed behind `error_rate` |
| `fail` | Targeted rules: `[{"method": "POST", "path": "/issues", "status": 503, "times": 1}]` |
| `strict_auth` | Accept only the twin's own fake credential; by default any non-empty one works, so the agent's usual token handling runs unchanged |

Two bundled GitHub seeds are nothing but a fault: `rate-limited` (429 after
five calls) and `permissions-denied`. Two knobs also have flags, because they
are the ones you reach for against a scenario you already have:

```bash
checkpoint run --rate-limit 5     # refuse each twin's calls after five
checkpoint run --read-only        # refuse every write, and fail the run if one was attempted
```

`--read-only` is the one to reach for on an agent you do not trust yet: the
attempt is the violation, and it is recorded even if the refusal then made the
agent crash.

A twin you started by hand takes the same settings over its control plane.
`checkpoint twins status github` prints its URL; `POST <url>/_config` with the
knobs you want, and `GET` the same path to see what is in force. Unknown keys
are rejected rather than ignored.

## Running one by hand

`checkpoint run` starts the twins a scenario names and throws them away when
the run ends. To build against one interactively, keep it alive:

```bash
checkpoint twins start github --seed large-backlog
checkpoint twins status              # what is running, and where
checkpoint twins seed github stale-issues
checkpoint twins reset github        # back to factory state, same URL
checkpoint twins stop github
```

Nothing a twin stores is real, and nothing it does leaves the machine.

## Adding your own

The seven cover the services most agents touch, and not yours. Point Checkpoint
at an ASGI app in your own repository and it becomes a twin like any other:
scenarios name it, the sandbox starts it, the proxy routes its production
hostnames into it, and assertions read its collections.

```toml
[twins.billing]
app = "mycompany.testing.billing_twin:app"
domains = ["api.billing.internal"]
token_env = ["BILLING_API_KEY"]
```

| Key | Means |
|---|---|
| `app` | `package.module:attribute` naming the ASGI app. Required. |
| `domains` | Production hostnames routed into it. A domain covers its subdomains. |
| `title` | Display name. Defaults to the twin's name. |
| `token` | The fake credential handed to the agent. Defaults to `cptk_CHECKPOINTFAKE_<NAME>`. |
| `token_env` | Variables your SDK reads that credential from. |
| `auth_scheme` | How it is presented in `Authorization`. `Bearer` by default; GitHub uses `token`, Linear sends the key bare. |
| `production_url` | Base URL the SDK uses in production. Defaults to `https://<first domain>`. |
| `extra_env` | More variables for the agent. `{url}` expands to the production URL when interception is on, and to the twin's own URL otherwise. |
| `docs` | A link, for `checkpoint twins list`. |

The app is imported from the project root, so it can live anywhere in your
repository. Building it on `checkpoint.twins.kit` gets the control plane, the
trace, the collection views assertions read, and all eight fault knobs for
free — which is what makes a twin of your own behave like the bundled seven
rather than like a mock with a different name.
