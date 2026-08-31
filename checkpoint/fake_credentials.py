"""Synthetic bootstrap tokens for local twins. Never real credentials.

Each token keeps its SDK-required prefix (so client libraries accept it) and
embeds the ``CHECKPOINTFAKE`` marker so secret scanners and humans can see it
is fake. Allow-listed in .gitleaks.toml and tests/test_no_tracked_secrets.py.

This module is the ONE canonical source of these values. Every twin, the
proxy, the runner, the MCP servers, and the tests import from here — never
hardcode a token literal anywhere else.
"""
from __future__ import annotations

FAKE_GITHUB_TOKEN = "ghp_CHECKPOINTFAKE00000000000000000000000000"
FAKE_SLACK_TOKEN = "xoxb-000000000000-000000000000-CHECKPOINTFAKE0000000000"
FAKE_STRIPE_KEY = "sk_live_CHECKPOINTFAKE0000000000000000"
FAKE_LINEAR_TOKEN = "lin_api_CHECKPOINTFAKE00000000000000000000"
FAKE_SUPABASE_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.checkpoint_anon_CHECKPOINTFAKE_0000"
FAKE_DISCORD_TOKEN = "Bot CHECKPOINTFAKE.discord.twin.token.0000"
FAKE_GOOGLE_WORKSPACE_TOKEN = "ya29.CHECKPOINTFAKE_google_workspace_token_0000"

# Keyed by the clone/service names used by clone_manager and runner.
FAKE_TOKENS = {
    "github": FAKE_GITHUB_TOKEN,
    "slack": FAKE_SLACK_TOKEN,
    "stripe": FAKE_STRIPE_KEY,
    "linear": FAKE_LINEAR_TOKEN,
    "supabase": FAKE_SUPABASE_TOKEN,
    "discord": FAKE_DISCORD_TOKEN,
    "google-workspace": FAKE_GOOGLE_WORKSPACE_TOKEN,
}
