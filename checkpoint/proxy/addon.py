"""mitmproxy addon: TLS-intercept route mode.

For every TLS request whose SNI matches a registered route:
  1. Look up Route(twin_url, bootstrap_token) by SNI / pretty_host.
  2. Rewrite scheme/host/port to point at the twin (which is plain HTTP).
  3. Preserve the original Host header (for parity with real API behaviour).
  4. Swap Authorization to "token <bootstrap_token>" so the twin's
     bootstrap-token auth (Phase 2 GH-02) accepts the request — even if
     the caller used the wrong/missing token. This is what makes the
     "agent runs unmodified" claim true.

The runner (checkpoint.docker.runner) seeds the in-container routes registry
via the CHECKPOINT_ROUTES env var (JSON: {"api.github.com": "http://host.docker.internal:54123"}).
This is necessary because routes.py state in the orchestrator's Python process
does NOT propagate into the sidecar container — they're separate runtimes.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

from mitmproxy import http

# mitmproxy loads this file as a top-level script (not as a package member),
# so relative imports fail with ModuleNotFoundError. Use the absolute import.
from checkpoint.proxy.routes import lookup, register

log = logging.getLogger("checkpoint.proxy")


def _seed_routes_from_env() -> None:
    """Read CHECKPOINT_ROUTES env (JSON dict of domain -> twin_url) and register each."""
    raw = os.environ.get("CHECKPOINT_ROUTES", "").strip()
    if not raw:
        log.warning("CHECKPOINT_ROUTES not set; addon will passthrough every request")
        return
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("CHECKPOINT_ROUTES is not valid JSON: %s", e)
        return
    for domain, twin_url in mapping.items():
        try:
            # Inherit bootstrap token from parent domain if this exact domain is unknown.
            token = None
            parts = domain.split(".")
            for i in range(len(parts)):
                r = lookup(".".join(parts[i:]))
                if r is not None:
                    token = r.bootstrap_token
                    break
            register(domain, twin_url, bootstrap_token=token)
            log.info("seeded route: %s -> %s", domain, twin_url)
        except Exception as e:
            # Downgrade to warning: parent-domain matching in _lookup_with_parent
            # still routes traffic correctly even if this registration fails.
            log.warning("could not pre-register %s (will use parent-domain match): %s", domain, e)


def _lookup_with_parent(host: str):
    """Look up a route by exact match first, then by parent domain.

    This allows 'supabase.co' in the routes registry to match any request
    to '<project-id>.supabase.co' from a real SDK without knowing the project ID.
    """
    route = lookup(host)
    if route is not None:
        return route
    # Try progressively shorter suffixes: a.b.c -> b.c -> c
    parts = host.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[i:])
        route = lookup(parent)
        if route is not None:
            log.debug("parent-domain match: %s -> %s", host, parent)
            return route
    return None


_seed_routes_from_env()


class RouteMode:
    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        route = _lookup_with_parent(host)
        if route is None or not route.twin_url:
            log.debug("passthrough: %s", host)
            return

        parsed = urlparse(route.twin_url)
        twin_host = parsed.hostname or "127.0.0.1"
        twin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        twin_scheme = parsed.scheme or "http"

        # Retarget the connection FIRST. mitmproxy auto-rewrites Host header to
        # match the new request.host:port when these are mutated.
        flow.request.host = twin_host
        flow.request.port = twin_port
        flow.request.scheme = twin_scheme

        # NOW restore the original Host header for parity (real api.github.com behaviour).
        # The twin's FastAPI routing only uses path; Host is preserved purely for fidelity.
        flow.request.headers["Host"] = host

        # Header swap: caller's Authorization (whatever it is) -> twin bootstrap token.
        # GitHub uses `token <…>`; Slack/Stripe use `Bearer <…>`.
        if host == "api.github.com":
            flow.request.headers["Authorization"] = f"token {route.bootstrap_token}"
        else:
            flow.request.headers["Authorization"] = f"Bearer {route.bootstrap_token}"

        log.info(
            "route: %s -> %s://%s:%d%s",
            host, twin_scheme, twin_host, twin_port, flow.request.path,
        )

    def response(self, flow: http.HTTPFlow) -> None:
        # No response rewriting needed in Phase 1; twin already returns GitHub-shaped JSON.
        pass


addons = [RouteMode()]
