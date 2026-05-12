"""SBX-01: Pre-baked sandbox image bundling harness runtime + sidecar + twins.

Contents:
  - ``Dockerfile`` builds a single image based on ``python:3.12-slim`` with
    mitmproxy, the checkpoint package, and the gh-bridge script preinstalled.
  - ``entrypoint.sh`` mints a fresh CA, starts the three twins on
    ``:8000``/``:8001``/``:8002``, starts mitmdump on ``:443``, exports
    ``CHECKPOINT_*_URL`` env vars, then ``exec``s the user-supplied command.
  - ``gh_bridge.py`` (SBX-02) translates a small subset of ``gh`` CLI
    invocations to GitHub-twin REST calls.

The image is built from the ``checkpoint/`` directory:

    sg docker -c "docker build -f checkpoint/sandbox/Dockerfile \\
                    -t checkpoint-sandbox checkpoint/"

and run with a user workspace mounted in:

    docker run --rm -v $PWD:/workspace checkpoint-sandbox /workspace/run.sh
"""
