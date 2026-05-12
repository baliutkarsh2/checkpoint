# Checkpoint sandbox — harness runtime + TLS sidecar + all three twins
# preinstalled, plus a minimal `gh` CLI bridge to the GitHub twin.
#
# Build from the checkpoint/ directory:
#   sg docker -c "docker build -f checkpoint/sandbox/Dockerfile \
#                  -t checkpoint-sandbox ."
#
# Run (mount your workspace, then exec your harness):
#   sg docker -c "docker run --rm -v $PWD:/workspace checkpoint-sandbox \
#                  python /workspace/harness.py"
FROM python:3.12-slim

# python:3.12-slim already ships /usr/bin/bash + ca-certificates + libssl.
# We deliberately skip apt-get: the build env can't reach debian.org mirrors
# (same constraint as the proxy Dockerfile). The entrypoint uses `python` for
# healthchecks instead of curl, and the lack of tini is fine because uvicorn
# children get killed via the trap in entrypoint.sh on EXIT.

WORKDIR /opt/checkpoint

# Install the checkpoint package (which brings mitmproxy, docker SDK,
# httpx, fastapi, uvicorn, mcp).
COPY pyproject.toml ./
COPY checkpoint ./checkpoint
RUN pip install --no-cache-dir .

# Entrypoint mints the CA, starts twins + sidecar, then execs the user
# command. /usr/local/bin/gh is a tiny shim that calls the bridge.
COPY checkpoint/sandbox/entrypoint.sh /opt/checkpoint/sandbox-entrypoint.sh
COPY checkpoint/sandbox/gh /usr/local/bin/gh
RUN chmod +x /opt/checkpoint/sandbox-entrypoint.sh /usr/local/bin/gh

# The sandbox writes its CA + state to /archal-out, identical contract
# to the standalone sidecar so an existing --docker harness sees the
# same env.
VOLUME ["/archal-out", "/workspace"]

# Twins on 8000/8001/8002, sidecar on 443.
EXPOSE 8000 8001 8002 443

ENV PYTHONUNBUFFERED=1 \
    ARCHAL_OUT_DIR=/archal-out \
    CHECKPOINT_GITHUB_PORT=8000 \
    CHECKPOINT_SLACK_PORT=8001 \
    CHECKPOINT_STRIPE_PORT=8002 \
    SIDECAR_PORT=443

# Entrypoint sets up CA + twins + sidecar, then execs the CMD.
# Child PIDs are killed by the entrypoint's EXIT trap.
ENTRYPOINT ["/opt/checkpoint/sandbox-entrypoint.sh"]
# Default: drop into bash for interactive exploration.
CMD ["/bin/bash"]
