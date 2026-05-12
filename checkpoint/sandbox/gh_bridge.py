"""SBX-02: gh CLI -> GitHub-twin REST bridge.

Filled in by Task 4. This stub gets the sandbox image to build with a
working `gh` shim before the full bridge ships.
"""
from __future__ import annotations

import json
import os
import sys


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("--help", "-h", "help"):
        print("gh (checkpoint sandbox bridge) — Task 4 fills in the body.")
        return 0
    if argv[0:2] == ["auth", "status"]:
        host = os.environ.get("CHECKPOINT_GITHUB_URL", "http://127.0.0.1:8000")
        print(f"Logged in to api.github.com (twin) as agent at {host}")
        return 0
    sys.stderr.write(f"gh: '{argv[0]}' not supported in sandbox bridge stub yet\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
