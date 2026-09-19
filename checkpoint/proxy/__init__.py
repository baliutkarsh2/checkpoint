"""Checkpoint's intercept proxy: routes an agent's SaaS API calls to local twins.

``server`` (the proxy), ``ca`` (its short-lived CA), ``routes`` (the SaaS
domain registry) and ``__main__`` (``python -m checkpoint.proxy``, the Docker
sidecar's entrypoint).
"""
from .ca import CertificateAuthority
from .routes import auth_header_for, proxy_routes
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
    "proxy_routes",
    "routes_from_json",
    "routes_to_json",
]
