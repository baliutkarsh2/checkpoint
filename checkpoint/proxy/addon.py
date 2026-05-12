"""mitmproxy addon — TLS-intercept route mode.

Phase 1 shell: imports cleanly and is loadable via `mitmdump --scripts addon.py`.
PLAN-02 implements the full request rewrite + header swap + forward logic.
"""
from __future__ import annotations

import logging

from mitmproxy import http

from .routes import lookup

log = logging.getLogger("checkpoint.proxy")


class RouteMode:
    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        route = lookup(host)
        if route is None:
            log.debug("passthrough: %s", host)
            return
        # PLAN-02: rewrite scheme/host/port to route.twin_url and swap Authorization header.
        log.info("would route: %s -> %s", host, route.twin_url or "<unset>")

    def response(self, flow: http.HTTPFlow) -> None:
        # PLAN-02: optionally rewrite response Location headers etc.
        pass


addons = [RouteMode()]
