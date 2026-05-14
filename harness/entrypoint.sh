#!/bin/sh
# Combine system CA bundle with the sidecar's self-signed CA so that:
#   - Real external services (OpenAI) are trusted via system CAs
#   - Sidecar-intercepted traffic (GitHub/Supabase twins) is trusted via sidecar CA
set -e

SIDECAR_CA="${SSL_CERT_FILE:-/archal-out/ca.crt}"
SYSTEM_CA="/etc/ssl/certs/ca-certificates.crt"
COMBINED="/tmp/combined-ca.crt"

cat "$SYSTEM_CA" "$SIDECAR_CA" > "$COMBINED"

export REQUESTS_CA_BUNDLE="$COMBINED"
export SSL_CERT_FILE="$COMBINED"
export NODE_EXTRA_CA_CERTS="$COMBINED"

exec python harness.py
