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

# The sidecar runs exactly one thing: `mitmdump -s checkpoint/proxy/addon.py`.
# That addon imports mitmproxy, the standard library, and two dependency-free
# checkpoint modules (proxy.routes -> fake_credentials). It does NOT need
# fastapi, uvicorn, openai, mcp or the docker SDK.
#
# So install mitmproxy alone and put the package on PYTHONPATH rather than
# `pip install`-ing checkpoint with its full dependency set. Installing the whole
# stack here made the image far larger than necessary and, combined with
# mitmproxy's own tightly-constrained graph, could not be resolved by pip at all.
RUN pip install --no-cache-dir "mitmproxy>=11.0"

COPY checkpoint ./checkpoint
ENV PYTHONPATH=/opt/checkpoint

# Entrypoint mints CA then starts mitmdump.
COPY checkpoint/proxy/entrypoint.sh /opt/checkpoint/entrypoint.sh
RUN chmod +x /opt/checkpoint/entrypoint.sh

VOLUME ["/archal-out"]
EXPOSE 443

ENV SIDECAR_PORT=443 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/opt/checkpoint/entrypoint.sh"]
