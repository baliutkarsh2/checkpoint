#!/usr/bin/env bash
# Run Checkpoint's intercept proxy as the TLS sidecar.
#
# It mints a fresh CA into the shared /checkpoint-out bind-mount (the harness
# trusts it through the CA env vars), listens for direct TLS on :443 — the
# harness resolves every intercepted SaaS domain to this container — and also
# as a regular forward proxy on :8080. Once both sockets are bound it prints a
# single "ready" line, which checkpoint/docker/runner.py waits for.
set -euo pipefail

OUT_DIR="${CHECKPOINT_OUT_DIR:-/checkpoint-out}"
ROUTES="${CHECKPOINT_ROUTES:-}"
if [ -z "$ROUTES" ]; then
    echo "[sidecar] CHECKPOINT_ROUTES is not set; nothing will be routed to a twin" >&2
    ROUTES='{}'
fi

exec python -m checkpoint.proxy \
    --listen "0.0.0.0:${PROXY_PORT:-8080}" \
    --transparent-port "${SIDECAR_PORT:-443}" \
    --ca-dir "$OUT_DIR" \
    --routes "$ROUTES"
