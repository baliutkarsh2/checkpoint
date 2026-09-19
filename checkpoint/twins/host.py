"""Serve several twins from one process: ``python -m checkpoint.twins.host github=8001 slack=8002``.

Twins share most of their import cost (FastAPI, the MCP SDK), so hosting every
twin a run needs in a single interpreter starts them several times faster than
one process per twin. Each twin still gets its own port and its own state.

Prints one JSON line — ``{"ready": {"github": 8001, ...}}`` — once every server
is accepting connections, then serves until terminated.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys

import uvicorn

from checkpoint.twins import registry


def _parse_bindings(pairs: list[str]) -> dict[str, int]:
    bindings: dict[str, int] = {}
    for pair in pairs:
        name, sep, port = pair.partition("=")
        if not sep or not port.isdigit():
            raise SystemExit(f"expected NAME=PORT, got {pair!r}")
        bindings[registry.get(name).name] = int(port)
    return bindings


async def _serve(bindings: dict[str, int], host: str, log_level: str) -> None:
    servers: dict[str, uvicorn.Server] = {}
    for name, port in bindings.items():
        config = uvicorn.Config(
            registry.load_app(registry.get(name)),
            host=host,
            port=port,
            log_level=log_level,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        # One process, many servers: let the process's own default signal
        # handling (and the parent's terminate) stop everything together.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        servers[name] = server

    tasks = [asyncio.create_task(s.serve(), name=f"twin-{n}") for n, s in servers.items()]
    while not all(s.started for s in servers.values()):
        failed = [t for t in tasks if t.done()]
        if failed:
            # A server exited before starting (port taken, import error, ...).
            await asyncio.gather(*failed)
            raise SystemExit(f"twin server {failed[0].get_name()} exited during startup")
        await asyncio.sleep(0.02)
    print(json.dumps({"ready": bindings}), flush=True)
    await asyncio.gather(*tasks)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m checkpoint.twins.host")
    parser.add_argument("bindings", nargs="+", metavar="NAME=PORT")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    # Ctrl-C is the normal way to stop an interactive host; exit quietly.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(_parse_bindings(args.bindings), args.host, args.log_level))


if __name__ == "__main__":
    main()
