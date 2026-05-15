#!/bin/sh
set -e
SIDECAR_CA="${SSL_CERT_FILE:-/archal-out/ca.crt}"
cat /etc/ssl/certs/ca-certificates.crt "$SIDECAR_CA" > /tmp/combined-ca.crt
export REQUESTS_CA_BUNDLE=/tmp/combined-ca.crt
export SSL_CERT_FILE=/tmp/combined-ca.crt
export NODE_EXTRA_CA_CERTS=/tmp/combined-ca.crt
exec python harness.py