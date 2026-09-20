"""Turning "which twins are running" into routes the intercept proxy can use.

Every fact here comes from the twin registry. This module used to keep its own
copy of the domain-to-credential table, which drifted: it had ``api.github.com``
but not ``uploads.github.com``, ``discord.com`` but not ``discordapp.com``, and
``gmail.googleapis.com`` but not ``oauth2.googleapis.com`` — so an intercepted
request to any of those three reached its twin carrying no credential at all,
and the twin refused it. A second table of the same facts is a bug waiting for
the first table to change.
"""
from __future__ import annotations

from collections.abc import Mapping

from checkpoint.twins import registry

from .server import Route


def auth_header_for(domain: str) -> str | None:
    """The ``Authorization`` value the twin behind ``domain`` accepts.

    Matches a parent domain the way the registry does, so a Supabase project at
    ``abcdefgh.supabase.co`` gets the Supabase twin's credential. None when no
    twin claims the domain.
    """
    spec = registry.for_domain(domain.lower().rstrip("."))
    return spec.auth_header if spec else None


def proxy_routes(upstreams: Mapping[str, str]) -> list[Route]:
    """Intercept-proxy routes for ``{domain: twin_url}``.

    Each route carries the credential its twin accepts, so an agent
    authenticating with a token of its own — the normal case, since it has no
    reason to know Checkpoint's — reaches the twin instead of a 401.
    """
    return [
        Route(domain, twin_url, auth_header=auth_header_for(domain))
        for domain, twin_url in upstreams.items()
    ]


def intercepted_domains() -> list[str]:
    """Every production hostname a run can route into a twin."""
    return sorted(registry.domains())
