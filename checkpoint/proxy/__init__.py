"""Checkpoint's intercept proxy: routes an agent's SaaS API calls to local twins.

``server`` (the proxy), ``ca`` (its short-lived CA), ``routes`` (the SaaS
domain registry) and ``__main__`` (``python -m checkpoint.proxy``, the Docker
sidecar's entrypoint).
"""
