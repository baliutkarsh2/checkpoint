# Self-hosting the dashboard

`checkpoint view` serves every run recorded on this machine: the calls the agent
made, the state it left, the criteria that failed with the reasoning behind
each, and the pass rate over time — which is the part a single run cannot show
you.

```bash
checkpoint view --open        # http://127.0.0.1:4001
```

That is all most people need. The rest of this page is about running one
somewhere your team can reach, which is a different thing with different risks.

## Before you expose it

The dashboard can start agent processes on request (`POST /api/jobs`). On
loopback that is a local tool; on any other interface it is remote code
execution for anyone who can reach the port. `checkpoint view` refuses to bind
off loopback without a key:

```bash
export CHECKPOINT_DASHBOARD_API_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
checkpoint view --host 0.0.0.0
```

| Variable | Effect |
|---|---|
| `CHECKPOINT_DASHBOARD_API_KEY` | Every `/api/*` write needs `Authorization: Bearer <key>` |
| `CHECKPOINT_DASHBOARD_AUTH_READS=1` | Reads need it too. Set this when the dashboard is reachable from the internet. |
| `CHECKPOINT_DASHBOARD_READ_ONLY=1` | Job creation is refused entirely. This is the right setting for a team instance that only browses history. |
| `CHECKPOINT_HOME` | Where config, baselines and signing keys live |
| `CHECKPOINT_LOG_LEVEL` | Log level for the access log and the watcher |

Also: terminate TLS upstream, because a bearer token over plain HTTP is a token
you have given away, and restrict ingress to your VPN or office range — auth
here is one shared secret, not per-user accounts.

## Container

The repository's `Dockerfile` builds the SPA from source and runs
`checkpoint view` as a non-root user with a healthcheck at `/healthz`.

```bash
docker build -t checkpoint:latest .
docker run -p 4001:4001 -v ck-data:/data \
  -e CHECKPOINT_DASHBOARD_API_KEY=... checkpoint:latest
```

`/data` holds runs, config and scenarios; mount a volume there or the history
is gone with the container.

## Compose

```bash
cp .env.example .env     # set CHECKPOINT_DASHBOARD_API_KEY, and OPENAI_API_KEY if you judge here
docker compose up -d
```

Same image, plus a named volume and the healthcheck wired up. Put it behind
your own nginx, Caddy or Traefik and proxy to `127.0.0.1:4001`.

## Fly.io

```bash
flyctl launch --copy-config --no-deploy --name <your-app>
flyctl secrets set CHECKPOINT_DASHBOARD_API_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
flyctl volumes create checkpoint_data --size 1 --region <your-region>
flyctl deploy
```

`fly.toml` sets `auto_stop_machines = "stop"`, so the machine sleeps when
nobody is looking and wakes on the next request.

## Render

`render.yaml` is a blueprint: point Render at the repository and it provisions
the web service from the same `Dockerfile`, a 1 GB disk at `/data`, and a
generated `CHECKPOINT_DASHBOARD_API_KEY`. Set any judge key yourself in the
Environment tab.

## Letting CI write, and the dashboard read

The least surprising arrangement is one-way: CI produces run records, the
dashboard displays them, and neither needs credentials for the other.

1. CI runs `checkpoint gate --json` and fails the build on anything but SHIP.
2. It uploads `.checkpoint/cache/runs/*.json` as artifacts.
3. A post-CI step copies those files into the dashboard's `/data/runs/`. The
   watcher picks them up and pushes a `run.created` event to anyone watching.
4. The instance runs with `CHECKPOINT_DASHBOARD_READ_ONLY=1`, so nothing can be
   launched from the browser.

## Operating it

`/healthz` is the liveness endpoint every path above already uses.
`/metrics` is Prometheus exposition format — uptime, request counts and
durations by route, job counts by status, and the number of live event
subscribers. Requests are logged one structured line each, with a request id.

Upgrades are `docker compose pull && docker compose up -d --force-recreate`.
Run records are version-stable, so the same volume works against a later image.

| Symptom | First thing to check |
|---|---|
| 503 at `/` | The SPA bundle is missing — the build stage failed. Check the `npm run build` step in the image build. |
| 401 on every call | A key is set and the client is not sending `Authorization: Bearer`. |
| 403 on `POST /api/jobs` | `CHECKPOINT_DASHBOARD_READ_ONLY=1` is on. |
| New runs never appear | The files did not land in `/data/runs/`, or they are not valid JSON. |
| Live updates stop after a few minutes | A reverse proxy is idle-closing the event stream. Raise `proxy_read_timeout`. |
