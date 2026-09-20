"""Checkpoint's intercept proxy: routes an agent's SaaS API calls to local twins.

``server`` is the proxy itself, ``ca`` its short-lived certificate authority,
``routes`` the adapter from the twin registry to the proxy's routing table, and
``__main__`` a standalone entry point (``python -m checkpoint.proxy``) for
running it outside a sandbox.
"""
from .ca import CertificateAuthority
from .routes import auth_header_for, intercepted_domains, proxy_routes
from .server import (
    LLM_PROVIDER_HOSTS,
    EgressPolicy,
    InterceptProxy,
    ProxyEvent,
    Route,
    routes_from_json,
    routes_to_json,
)

__all__ = [
    "LLM_PROVIDER_HOSTS",
    "CertificateAuthority",
    "EgressPolicy",
    "InterceptProxy",
    "ProxyEvent",
    "Route",
    "auth_header_for",
    "intercepted_domains",
    "proxy_routes",
    "routes_from_json",
    "routes_to_json",
]
