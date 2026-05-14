#!/usr/bin/env bash
# Mint a fresh CA on every container start; write to the shared /archal-out
# bind-mount so the harness container picks it up via the four CA env vars.
set -euo pipefail

OUT_DIR="${ARCHAL_OUT_DIR:-/archal-out}"
mkdir -p "$OUT_DIR"

echo "[sidecar] minting fresh CA -> $OUT_DIR/ca.crt"
python -c "from pathlib import Path; from checkpoint.proxy.ca import mint_ca; print(mint_ca(Path('$OUT_DIR')))"

# mitmproxy uses its conf-dir's mitmproxy-ca.pem as the signing CA for per-SNI
# leaf certs. Place our minted CA cert+key there as the combined PEM.
MITM_CONF="$OUT_DIR/mitmproxy"
mkdir -p "$MITM_CONF"
cat "$OUT_DIR/ca.key" "$OUT_DIR/ca.crt" > "$MITM_CONF/mitmproxy-ca.pem"
chmod 600 "$MITM_CONF/mitmproxy-ca.pem"

echo "[sidecar] starting mitmdump on :${SIDECAR_PORT:-443} (reverse-to-twin mode)"
TWIN_UPSTREAM="${TWIN_UPSTREAM:-http://127.0.0.1:18080}"
exec mitmdump \
    --mode "reverse:${TWIN_UPSTREAM}@${SIDECAR_PORT:-443}" \
    --listen-host 0.0.0.0 \
    --set confdir="$MITM_CONF" \
    --set keep_host_header=true \
    --showhost \
    -s /opt/checkpoint/checkpoint/proxy/addon.py
