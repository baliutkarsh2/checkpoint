"""``python -m checkpoint.proxy`` — run the intercept proxy as its own process.

A run does not need this — the sandbox starts the proxy in-process. It exists
for the cases that are outside a run: pointing a container, a VM or another
machine's agent at the twins, and debugging interception by hand.

It mints a fresh CA into ``--ca-dir``, binds the forward-proxy listener (and the
transparent TLS listener with ``--transparent-port``), then prints exactly one
``ready`` line on stdout so a supervising process knows when to proceed.
Diagnostics go to stderr. SIGTERM and SIGINT shut it down cleanly, which matters
when it is PID 1 in a container: without a handler, a stop request waits out its
full timeout.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from .ca import CertificateAuthority
from .server import EgressPolicy, InterceptProxy, Route, routes_from_json


def _listen_address(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host.strip("[]") or "0.0.0.0", int(port)


def _routes(value: str) -> list[Route]:
    try:
        return routes_from_json(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --routes: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m checkpoint.proxy",
        description="Checkpoint's intercepting proxy: routes SaaS API hosts to local twins.",
    )
    parser.add_argument("--listen", type=_listen_address, default="127.0.0.1:8080",
                        metavar="HOST:PORT", help="forward-proxy address (default: %(default)s)")
    parser.add_argument("--transparent-port", type=int, metavar="PORT",
                        help="also accept direct TLS on this port, routed by SNI")
    parser.add_argument("--routes", type=_routes, default="{}", metavar="JSON",
                        help='{"domain": "http://upstream"} or {"domain": {"upstream_url": ..., '
                             '"auth_header": ..., "extra_headers": {...}}}')
    parser.add_argument("--ca-dir", type=Path, required=True,
                        help="where to write ca.crt, ca.key and bundle.pem")
    parser.add_argument("--allow", action="append", metavar="PATTERN",
                        help="egress allowlist entry for non-routed hosts (host, *.suffix, "
                             "host:port); repeatable. Without any, egress is open.")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every proxied request")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, format="[proxy] %(levelname)s %(message)s",
                        level=logging.DEBUG if args.verbose else logging.WARNING)
    policy = EgressPolicy.allowlist(args.allow) if args.allow else EgressPolicy.open()
    ca = CertificateAuthority.create(args.ca_dir)
    host, port = args.listen
    proxy = InterceptProxy(args.routes, policy, ca, host=host, port=port,
                           transparent_port=args.transparent_port)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    proxy.start()
    try:
        transparent = f", transparent TLS on :{proxy.transparent_port}" if proxy.transparent_port else ""
        print(f"[proxy] listening on {host}:{proxy.port}{transparent}; CA at {ca.cert_path}; "
              f"routes: {', '.join(r.domain for r in args.routes) or 'none'}", file=sys.stderr)
        print("ready", flush=True)
        # Wake periodically: on Windows a bare Event.wait() would hold off Ctrl+C.
        while not stop.wait(0.5):
            pass
    finally:
        proxy.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
