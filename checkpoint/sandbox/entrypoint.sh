#!/usr/bin/env bash
# Sandbox supervisor entrypoint.
#
# Order:
#   1. Mint CA into $ARCHAL_OUT_DIR/ca.crt.
#   2. Start github twin on $CHECKPOINT_GITHUB_PORT (background).
#   3. Start slack twin  on $CHECKPOINT_SLACK_PORT  (background).
#   4. Start stripe twin on $CHECKPOINT_STRIPE_PORT (background).
#   5. Wait for /_health on all three.
#   6. Start mitmdump on :$SIDECAR_PORT with the route-mode addon
#      (background).
#   7. Export the four CA env vars + CHECKPOINT_*_URL + bootstrap tokens.
#   8. Trap SIGTERM/EXIT so we kill children on shutdown.
#   9. exec "$@" — the user-supplied command (defaults to /bin/bash).
set -euo pipefail

OUT_DIR="${ARCHAL_OUT_DIR:-/archal-out}"
GH_PORT="${CHECKPOINT_GITHUB_PORT:-8000}"
SL_PORT="${CHECKPOINT_SLACK_PORT:-8001}"
ST_PORT="${CHECKPOINT_STRIPE_PORT:-8002}"
SIDECAR_PORT="${SIDECAR_PORT:-443}"
HOST="127.0.0.1"

mkdir -p "$OUT_DIR"

echo "[sandbox] minting fresh CA -> $OUT_DIR/ca.crt"
python -c "from pathlib import Path; from checkpoint.proxy.ca import mint_ca; mint_ca(Path('$OUT_DIR'))"

MITM_CONF="$OUT_DIR/mitmproxy"
mkdir -p "$MITM_CONF"
cat "$OUT_DIR/ca.key" "$OUT_DIR/ca.crt" > "$MITM_CONF/mitmproxy-ca.pem"
chmod 600 "$MITM_CONF/mitmproxy-ca.pem"

# Per-twin log files for debug.
mkdir -p "$OUT_DIR/logs"

PIDS=()

start_twin() {
    local name="$1" app="$2" port="$3"
    echo "[sandbox] starting $name twin on :$port"
    python -m uvicorn "$app" --host "$HOST" --port "$port" --log-level warning \
        > "$OUT_DIR/logs/$name.log" 2>&1 &
    PIDS+=($!)
}

wait_healthy() {
    local name="$1" port="$2"
    for _ in $(seq 1 60); do
        if python -c "import sys, urllib.request; \
                       urllib.request.urlopen('http://$HOST:$port/_health', timeout=1).read(); \
                       sys.exit(0)" 2>/dev/null; then
            echo "[sandbox] $name OK on :$port"
            return 0
        fi
        sleep 0.25
    done
    echo "[sandbox] $name failed health on :$port" >&2
    return 1
}

start_twin github checkpoint.twins.github:app "$GH_PORT"
start_twin slack  checkpoint.twins.slack:app  "$SL_PORT"
start_twin stripe checkpoint.twins.stripe:app "$ST_PORT"

wait_healthy github "$GH_PORT"
wait_healthy slack  "$SL_PORT"
wait_healthy stripe "$ST_PORT"

# mitmdump route table: agent SDKs hit the real-API hostname; sidecar
# rewrites to the twin URL based on Host header.
CHECKPOINT_ROUTES_JSON=$(cat <<EOF
{
  "api.github.com": "http://$HOST:$GH_PORT",
  "slack.com": "http://$HOST:$SL_PORT",
  "api.stripe.com": "http://$HOST:$ST_PORT"
}
EOF
)
export CHECKPOINT_ROUTES="$CHECKPOINT_ROUTES_JSON"

echo "[sandbox] starting mitmdump on :$SIDECAR_PORT (route mode)"
mitmdump \
    --mode "regular@$SIDECAR_PORT" \
    --listen-host 0.0.0.0 \
    --set confdir="$MITM_CONF" \
    --set keep_host_header=true \
    --showhost \
    -s /opt/checkpoint/checkpoint/proxy/addon.py \
    > "$OUT_DIR/logs/mitmdump.log" 2>&1 &
PIDS+=($!)

# Wire env for the user command.
export CHECKPOINT_GITHUB_URL="http://$HOST:$GH_PORT"
export CHECKPOINT_SLACK_URL="http://$HOST:$SL_PORT"
export CHECKPOINT_STRIPE_URL="http://$HOST:$ST_PORT"
export CHECKPOINT_BASE_URL="$CHECKPOINT_GITHUB_URL"

# Twin bootstrap tokens — the gh-bridge needs GITHUB_TOKEN; agents using
# the TLS sidecar route will have these stamped by the addon, but if
# they hit the twin directly via CHECKPOINT_*_URL they need the real
# token.
export GITHUB_TOKEN="ghp_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt"
export SLACK_TOKEN="xoxb-123456789012-234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
export STRIPE_API_KEY="sk_live_51Abc123DefGhiJklMnoPqrStUvWxYz0123456789"

# CA env vars for stock HTTPS clients inside the sandbox.
export NODE_EXTRA_CA_CERTS="$OUT_DIR/ca.crt"
export SSL_CERT_FILE="$OUT_DIR/ca.crt"
export REQUESTS_CA_BUNDLE="$OUT_DIR/ca.crt"
export CURL_CA_BUNDLE="$OUT_DIR/ca.crt"

cleanup() {
    echo "[sandbox] cleaning up child PIDs: ${PIDS[*]:-}"
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT TERM INT

echo "[sandbox] ready. Executing: $*"
exec "$@"
