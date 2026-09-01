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

# This image does two jobs: it runs mitmdump with the route addon, AND it hosts
# the twin FastAPI apps under uvicorn (see _DOCKER_TWIN_APPS in
# checkpoint/docker/runner.py), which is why fastapi/uvicorn/mcp are here.
#
# It deliberately does NOT `pip install` the checkpoint package with its full
# dependency set. Doing so pulls the OpenAI SDK — which only the host-side judge
# uses, never the sidecar — and the resulting graph is large enough that pip
# fails to resolve it at all. Installing just what runs here keeps the image
# small and the build reliable.
#
# Keep this list in sync with pyproject; tests/docker/test_sidecar_image.py
# fails if it drifts or stops covering what the twins import.
RUN pip install --no-cache-dir \
      "mitmproxy>=11.0" \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.31.1" \
      "mcp>=1.27" \
      "httpx>=0.28" \
      "sse-starlette>=2.1" \
      "pydantic>=2.9" \
      "python-dotenv>=1.0"

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
