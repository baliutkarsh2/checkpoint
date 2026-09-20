"""The twin catalog: one record per simulated service.

Everything the engine needs to know about a service lives here — the app that
implements it, the production hostnames the sandbox intercepts, and the
credential and URL environment variables an agent receives — so no other module
keeps its own copy of these tables.
"""
from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

from checkpoint.fake_credentials import (
    FAKE_DISCORD_TOKEN,
    FAKE_GITHUB_TOKEN,
    FAKE_GOOGLE_WORKSPACE_TOKEN,
    FAKE_LINEAR_TOKEN,
    FAKE_SLACK_TOKEN,
    FAKE_STRIPE_KEY,
    FAKE_SUPABASE_TOKEN,
)


@dataclass(frozen=True)
class TwinSpec:
    """A simulated service the sandbox can run."""

    name: str
    """Identifier used in scenarios (``twins: github, slack``) and env var names."""
    title: str
    app: str
    """Import path of the ASGI app, ``"package.module:attr"``."""
    domains: tuple[str, ...] = ()
    """Production hostnames routed to this twin. A domain also matches its
    subdomains, so ``supabase.co`` covers ``<project>.supabase.co``."""
    token: str = ""
    """Fake credential exported to the agent. The twin accepts any non-empty
    credential unless strict auth is enabled, in which case only this one."""
    token_env: tuple[str, ...] = ()
    """Environment variables the service's SDKs conventionally read the credential from."""
    auth_scheme: str = "Bearer"
    """How the credential is presented in the Authorization header. GitHub uses
    ``token``, Discord ``Bot``, Linear sends an API key bare (empty scheme)."""
    production_url: str = ""
    """Base URL SDKs use in production; defaults to ``https://<first domain>``."""
    extra_env: dict[str, str] = field(default_factory=dict)
    """Additional variables. ``{url}`` expands to the production URL when the
    sandbox intercepts traffic, and to the twin's direct URL otherwise."""
    docs: str = ""
    builtin: bool = True

    @property
    def env_prefix(self) -> str:
        return self.name.upper().replace("-", "_")

    @property
    def url_env(self) -> str:
        """Variable holding the twin's direct base URL (always exported)."""
        return f"CHECKPOINT_{self.env_prefix}_URL"

    @property
    def seeds_dir(self) -> Path | None:
        module_name = self.app.partition(":")[0]
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError):
            return None
        if spec is None or spec.origin is None:
            return None
        stem = module_name.rsplit(".", 1)[-1]
        d = Path(spec.origin).parent / f"{stem}_seeds"
        return d if d.is_dir() else None

    def seed_names(self) -> list[str]:
        d = self.seeds_dir
        return sorted(p.stem for p in d.glob("*.json")) if d else []

    def agent_env(self, base_url: str, *, intercepted: bool) -> dict[str, str]:
        """Environment an agent needs to reach this twin.

        ``base_url`` is the twin's direct URL. When the sandbox intercepts
        production hostnames, SDK-level URL variables keep their production
        value (the proxy reroutes it); otherwise they point at the twin.
        """
        env = {self.url_env: base_url}
        for var in self.token_env:
            env[var] = self.token
        for var, value in self.extra_env.items():
            env[var] = value.format(url=self.public_url if intercepted else base_url)
        return env

    @property
    def auth_header(self) -> str:
        """The Authorization header value an SDK would send for this service."""
        token = self.token.strip()
        scheme = self.auth_scheme.strip()
        if scheme and token.lower().startswith(scheme.lower() + " "):
            return token  # the fake credential already names its scheme
        return f"{scheme} {token}".strip()

    @property
    def public_url(self) -> str:
        if self.production_url:
            return self.production_url
        return f"https://{self.domains[0]}" if self.domains else ""


_BUILTINS: tuple[TwinSpec, ...] = (
    TwinSpec(
        name="github",
        title="GitHub",
        app="checkpoint.twins.github:app",
        domains=("api.github.com", "uploads.github.com"),
        token=FAKE_GITHUB_TOKEN,
        token_env=("GITHUB_TOKEN", "GH_TOKEN"),
        auth_scheme="token",
        extra_env={"GITHUB_API_URL": "{url}"},
        docs="https://docs.github.com/rest",
    ),
    TwinSpec(
        name="slack",
        title="Slack",
        app="checkpoint.twins.slack:app",
        domains=("slack.com",),
        token=FAKE_SLACK_TOKEN,
        token_env=("SLACK_BOT_TOKEN", "SLACK_TOKEN"),
        docs="https://api.slack.com/methods",
    ),
    TwinSpec(
        name="stripe",
        title="Stripe",
        app="checkpoint.twins.stripe:app",
        domains=("api.stripe.com",),
        token=FAKE_STRIPE_KEY,
        token_env=("STRIPE_API_KEY", "STRIPE_SECRET_KEY"),
        docs="https://docs.stripe.com/api",
    ),
    TwinSpec(
        name="linear",
        title="Linear",
        app="checkpoint.twins.linear:app",
        domains=("api.linear.app",),
        token=FAKE_LINEAR_TOKEN,
        # Linear personal API keys are sent bare, with no scheme.
        token_env=("LINEAR_API_KEY",),
        auth_scheme="",
        docs="https://linear.app/developers",
    ),
    TwinSpec(
        name="supabase",
        title="Supabase",
        app="checkpoint.twins.supabase:app",
        domains=("supabase.co",),
        token=FAKE_SUPABASE_TOKEN,
        token_env=("SUPABASE_KEY", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"),
        # supabase.co is the parent domain; SDKs need a project URL under it.
        production_url="https://checkpoint.supabase.co",
        extra_env={"SUPABASE_URL": "{url}"},
        docs="https://supabase.com/docs/reference",
    ),
    TwinSpec(
        name="discord",
        title="Discord",
        app="checkpoint.twins.discord:app",
        domains=("discord.com", "discordapp.com"),
        # Bot tokens are stored bare: every SDK adds the "Bot " scheme itself,
        # so a prefixed one reaches the API as "Bot Bot <token>".
        token=FAKE_DISCORD_TOKEN.removeprefix("Bot "),
        token_env=("DISCORD_TOKEN", "DISCORD_BOT_TOKEN"),
        auth_scheme="Bot",
        docs="https://discord.com/developers/docs/reference",
    ),
    TwinSpec(
        name="google-workspace",
        title="Google Workspace",
        app="checkpoint.twins.google_workspace:app",
        # oauth2.googleapis.com is where every google-auth credential refreshes
        # its access token; without it a service account reaches real Google.
        domains=("gmail.googleapis.com", "www.googleapis.com", "oauth2.googleapis.com"),
        token=FAKE_GOOGLE_WORKSPACE_TOKEN,
        token_env=("GOOGLE_OAUTH_ACCESS_TOKEN",),
        docs="https://developers.google.com/workspace",
    ),
)

_REGISTRY: dict[str, TwinSpec] = {spec.name: spec for spec in _BUILTINS}

# Names users reach for that mean a built-in twin.
_ALIASES = {"gmail": "google-workspace", "google": "google-workspace", "gh": "github"}


class UnknownTwinError(KeyError):
    def __str__(self) -> str:  # KeyError quotes its message; keep it readable
        return str(self.args[0])


def get(name: str) -> TwinSpec:
    key = _ALIASES.get(name.strip().lower(), name.strip().lower())
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownTwinError(
            f"unknown twin {name!r}; available: {', '.join(names())}"
        ) from None


def domains() -> dict[str, TwinSpec]:
    """Every hostname Checkpoint intercepts, and the twin that answers it."""
    return {domain: spec for spec in all_specs() for domain in spec.domains}


def for_domain(host: str) -> TwinSpec | None:
    """The twin that serves ``host``, matching parent domains as the proxy does."""
    table = domains()
    parts = host.split(".")
    for i in range(len(parts) - 1):
        spec = table.get(".".join(parts[i:]))
        if spec is not None:
            return spec
    return None


def names() -> list[str]:
    return sorted(_REGISTRY)


def all_specs() -> list[TwinSpec]:
    return [_REGISTRY[n] for n in names()]


def register(spec: TwinSpec) -> TwinSpec:
    """Add a user-defined twin (``[twins.<name>]`` in checkpoint.toml)."""
    if spec.name in _REGISTRY and _REGISTRY[spec.name].builtin:
        raise ValueError(f"cannot replace the built-in {spec.name!r} twin; choose another name")
    _REGISTRY[spec.name] = spec
    return spec


def load_app(spec: TwinSpec):  # type: ignore[no-untyped-def]
    """Import and return the ASGI app for ``spec``."""
    module_name, _, attr = spec.app.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr or "app")
