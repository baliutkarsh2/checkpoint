# Checkpoint TLS sidecar — mitmproxy with route-mode addon.
#
# Build from the checkpoint/ directory:
#   docker build -f checkpoint/proxy/Dockerfile -t checkpoint-sidecar .
#
# CA is minted at container startup, NOT at build time.
FROM python:3.11-slim

# NOTE: We skip apt-get because the base image already ships ca-certificates
# and mitmproxy/cryptography distribute manylinux wheels — no compiler needed.
# (Phase 1 deviation Rule 3: build environment cannot reach debian.org mirrors.)

WORKDIR /opt/checkpoint

# Install checkpoint package (which has mitmproxy + docker + cryptography as deps).
COPY pyproject.toml ./
COPY checkpoint ./checkpoint
RUN pip install --no-cache-dir .

# Entrypoint mints CA then starts mitmdump.
COPY checkpoint/proxy/entrypoint.sh /opt/checkpoint/entrypoint.sh
RUN chmod +x /opt/checkpoint/entrypoint.sh

VOLUME ["/archal-out"]
EXPOSE 443

ENV SIDECAR_PORT=443 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/opt/checkpoint/entrypoint.sh"]
