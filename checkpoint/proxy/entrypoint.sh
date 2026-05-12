#!/usr/bin/env bash
# Mint a fresh CA on every container start; write to the shared /archal-out
# bind-mount so the harness container picks it up via the four CA env vars.
set -euo pipefail

OUT_DIR="${ARCHAL_OUT_DIR:-/archal-out}"
mkdir -p "$OUT_DIR"

echo "[sidecar] minting fresh CA -> $OUT_DIR/ca.crt"
python -c "from pathlib import Path; from checkpoint.proxy.ca import mint_ca; print(mint_ca(Path('$OUT_DIR')))"

# mitmproxy expects a single PEM with cert+key concatenated for --certs.
cat "$OUT_DIR/ca.crt" "$OUT_DIR/ca.key" > "$OUT_DIR/ca-combined.pem"

echo "[sidecar] starting mitmdump on :${SIDECAR_PORT:-443}"
exec mitmdump \
    --mode regular \
    --listen-host 0.0.0.0 \
    --listen-port "${SIDECAR_PORT:-443}" \
    --set confdir="$OUT_DIR/mitmproxy" \
    --certs "*=$OUT_DIR/ca-combined.pem" \
    -s /opt/checkpoint/checkpoint/proxy/addon.py
