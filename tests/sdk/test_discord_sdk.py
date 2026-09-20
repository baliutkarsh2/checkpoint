"""Discord twin driven by discord.py (the official SDK) and by raw REST v10.

discord.py talks to Discord over two surfaces: a gateway websocket for events
and the REST API for everything an agent *does*. Only REST is simulated, so the
tests log in (``Client.login``) and then drive the REST client the way a bot's
command handlers do — fetch, send, edit, react, moderate — plus the raw v10
calls agents make with an HTTP client of their own.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from checkpoint.twins import registry

discord = pytest.importorskip("discord")

TWIN = "discord"

REST_TIMEOUT = 10


@pytest.fixture(scope="module")
def seed_ids():
    """The ``small-server`` ids, by name — read from the seed, not over HTTP."""
    seeds = registry.get(TWIN).seeds_dir
    state = json.loads((seeds / "small-server.json").read_text(encoding="utf-8"))["state"]
    guild_id = next(iter(state["guilds"]))
    return {
        "guild_id": guild_id,
        "channels": {c["name"]: cid for cid, c in state["channels"].items()},
        "users": {u["username"]: uid for uid, u in state["users"].items()},
        "roles": {r["name"]: rid for rid, r in state["roles"][guild_id].items()},
    }


@pytest.fixture
def server(twin, seed_ids):
    twin.seed("small-server")
    return seed_ids


@pytest.fixture
def rest(twin):
    """A raw v10 client, as an agent that skips the SDK would build it."""
    with httpx.Client(base_url=f"{twin.url}/api/v10", timeout=REST_TIMEOUT,
                      headers={"Authorization": f"Bot {twin.token}"}) as client:
        yield client


@pytest.fixture
def dpy(twin):
    """Run one discord.py session against the twin: login, work, clean close."""
    original = discord.http.Route.BASE
    discord.http.Route.BASE = f"{twin.url}/api/v10"

    def run(work: Callable[[Any], Awaitable[Any]], *, login: bool = True, **options: Any) -> Any:
        async def session() -> Any:
            # Intents only gate the gateway, but discord.py refuses member calls
            # without them — a bot doing this work runs with them enabled.
            client = discord.Client(intents=discord.Intents.all(), **options)
            try:
                if login:
                    await client.login(twin.token)
                return await work(client)
            finally:
                await client.close()

        return asyncio.run(session())

    yield run
    discord.http.Route.BASE = original


# --- discord.py: identity and discovery --------------------------------------

def test_login_identifies_the_bot(dpy):
    """``login`` reads /users/@me and /oauth2/applications/@me; both must parse."""
    name, user_id, app_id = dpy(lambda c: _identity(c))
    assert name == "checkpoint-bot"
    assert user_id == app_id  # a bot's user id is its application id


async def _identity(client):
    app = await client.application_info()
    return client.user.name, client.user.id, app.id


def test_fetch_guilds_and_channels(dpy, server):
    async def work(client):
        guilds = [g async for g in client.fetch_guilds()]
        guild = await client.fetch_guild(int(server["guild_id"]))
        channels = await guild.fetch_channels()
        return [g.name for g in guilds], guild.name, sorted(c.name for c in channels)

    names, guild_name, channels = dpy(work)
    assert names == ["Acme Engineering"]
    assert guild_name == "Acme Engineering"
    assert "general" in channels and "announcements" in channels


def test_fetch_members_and_roles(dpy, server):
    async def work(client):
        guild = await client.fetch_guild(int(server["guild_id"]))
        members = [m async for m in guild.fetch_members(limit=100)]
        roles = await guild.fetch_roles()
        member = await guild.fetch_member(int(server["users"]["alice"]))
        return ([m.name for m in members], sorted(r.name for r in roles),
                member.nick, [r.name for r in member.roles])

    names, roles, nick, member_roles = dpy(work)
    assert {"alice", "bob", "carol"} <= set(names)
    assert "@everyone" in roles and "Admin" in roles
    assert nick == "Alice"
    assert "Admin" in member_roles


# --- discord.py: the message lifecycle ---------------------------------------

def test_send_edit_fetch_and_delete_message(dpy, server, twin):
    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["general"]))
        message = await channel.send("Deploy started")
        await message.edit(content="Deploy finished")
        fetched = await channel.fetch_message(message.id)
        await message.delete()
        history = [m async for m in channel.history(limit=10)]
        return fetched.content, fetched.edited_at is not None, len(history)

    content, edited, remaining = dpy(work)
    assert (content, edited, remaining) == ("Deploy finished", True, 0)
    ops = [(e["op"], e["resource"]) for e in twin.trace()]
    assert ("create", "messages") in ops and ("delete", "messages") in ops


def test_reply_resolves_the_referenced_message(dpy, server):
    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["general"]))
        original = await channel.send("Who is on call?")
        reply = await original.reply("I am, until 18:00.")
        refetched = await channel.fetch_message(reply.id)
        return (reply.reference.message_id, original.id,
                refetched.reference.resolved.content, refetched.type)

    reference_id, original_id, resolved, kind = dpy(work)
    assert reference_id == original_id
    assert resolved == "Who is on call?"
    assert kind is discord.MessageType.reply


def test_history_paginates_oldest_and_newest_first(dpy, server):
    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["general"]))
        sent = [await channel.send(f"line {i}") for i in range(5)]
        newest = [m.content async for m in channel.history(limit=2)]
        oldest = [m.content async for m in channel.history(limit=2, oldest_first=True)]
        after = [m.content async for m in channel.history(after=sent[2], oldest_first=True)]
        return newest, oldest, after

    newest, oldest, after = dpy(work)
    assert newest == ["line 4", "line 3"]
    assert oldest == ["line 0", "line 1"]
    assert after == ["line 3", "line 4"]


def test_reactions_are_idempotent(dpy, server):
    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["general"]))
        message = await channel.send("ship it?")
        await message.add_reaction("\N{THUMBS UP SIGN}")
        await message.add_reaction("\N{THUMBS UP SIGN}")  # the API ignores a repeat
        after_add = await channel.fetch_message(message.id)
        users = [u async for u in after_add.reactions[0].users()]
        await message.remove_reaction("\N{THUMBS UP SIGN}", client.user)
        after_remove = await channel.fetch_message(message.id)
        return after_add.reactions[0].count, len(users), after_remove.reactions

    count, reactors, after_remove = dpy(work)
    assert (count, reactors, after_remove) == (1, 1, [])


def test_pin_list_and_unpin(dpy, server):
    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["general"]))
        message = await channel.send("Runbook: https://example.test/runbook")
        await message.pin()
        pinned = [m.content async for m in channel.pins()]
        await message.unpin()
        return pinned, [m.content async for m in channel.pins()]

    pinned, after_unpin = dpy(work)
    assert pinned == ["Runbook: https://example.test/runbook"]
    assert after_unpin == []


def test_send_file_uploads_as_multipart(dpy, server, twin):
    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["alerts"]))
        upload = discord.File(fp=io.BytesIO(b"error rate 7.3%\n"), filename="metrics.txt")
        message = await channel.send("latest metrics", file=upload)
        return message.attachments[0].filename, message.attachments[0].size, message.content

    filename, size, content = dpy(work)
    assert (filename, size, content) == ("metrics.txt", 16, "latest metrics")
    attachments = [m["attachments"] for m in twin.views()["messages"]["items"]]
    assert ["metrics.txt"] in attachments


def test_purge_bulk_deletes_and_is_traced_as_a_delete(dpy, server, twin):
    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["general"]))
        for i in range(4):
            await channel.send(f"spam {i}")
        deleted = await channel.purge(limit=10)
        return len(deleted), [m async for m in channel.history(limit=10)]

    deleted, remaining = dpy(work)
    assert (deleted, remaining) == (4, [])
    bulk = [e for e in twin.trace() if e["path"].endswith("/messages/bulk-delete")]
    assert bulk and (bulk[0]["op"], bulk[0]["resource"]) == ("delete", "messages")


# --- discord.py: channels, threads and DMs -----------------------------------

def test_create_edit_and_delete_a_channel(dpy, server, twin):
    async def work(client):
        guild = await client.fetch_guild(int(server["guild_id"]))
        channel = await guild.create_text_channel("incident-2026-payments",
                                                  topic="SEV2 war room")
        await channel.edit(topic="SEV2 war room (resolved)")
        fetched = await client.fetch_channel(channel.id)
        await channel.delete()
        return channel.name, fetched.topic

    name, topic = dpy(work)
    assert (name, topic) == ("incident-2026-payments", "SEV2 war room (resolved)")
    assert not [c for c in twin.views()["channels"]["items"] if c["name"] == name]


def test_thread_from_a_message_carries_the_conversation(dpy, server, twin):
    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["engineering"]))
        message = await channel.send("RFC: drop Python 3.10")
        thread = await message.create_thread(name="rfc-python-310")
        await thread.send("+1 from the platform team")
        return thread.id, message.id, [m.content async for m in thread.history(limit=5)]

    thread_id, message_id, posts = dpy(work)
    assert thread_id == message_id  # a thread from a message shares its id
    assert posts == ["+1 from the platform team"]
    threads = twin.views()["threads"]["items"]
    assert [t["name"] for t in threads] == ["rfc-python-310"]
    assert threads[0]["parent"] == "engineering"


def test_direct_message_to_a_member(dpy, server, twin):
    async def work(client):
        user = await client.fetch_user(int(server["users"]["bob"]))
        channel = await user.create_dm()
        await channel.send("Your on-call shift starts tomorrow.")
        return channel.id, [m.content async for m in channel.history(limit=5)]

    channel_id, history = dpy(work)
    assert history == ["Your on-call shift starts tomorrow."]
    dm = next(c for c in twin.views()["channels"]["items"] if c["id"] == str(channel_id))
    assert dm["kind"] == "dm"


# --- discord.py: moderation --------------------------------------------------

def test_role_create_assign_remove_and_delete(dpy, server, twin):
    async def work(client):
        guild = await client.fetch_guild(int(server["guild_id"]))
        role = await guild.create_role(name="on-call", colour=discord.Colour.red(),
                                       mentionable=True)
        # Without a gateway there is no GUILD_ROLE_CREATE event, so refetch the
        # guild for the new role to resolve on members.
        guild = await client.fetch_guild(int(server["guild_id"]))
        member = await guild.fetch_member(int(server["users"]["carol"]))
        await member.add_roles(role)
        with_role = await guild.fetch_member(member.id)
        await member.remove_roles(role)
        without_role = await guild.fetch_member(member.id)
        await role.delete()
        return (role.name, role.colour.value, [r.name for r in with_role.roles],
                [r.name for r in without_role.roles])

    name, colour, with_role, without_role = dpy(work)
    assert (name, colour) == ("on-call", discord.Colour.red().value)
    assert "on-call" in with_role and "on-call" not in without_role
    assert not [r for r in twin.views()["roles"]["items"] if r["name"] == "on-call"]


def test_member_edit_kick_ban_and_unban(dpy, server, twin):
    async def work(client):
        guild = await client.fetch_guild(int(server["guild_id"]))
        member = await guild.fetch_member(int(server["users"]["bob"]))
        await member.edit(nick="bob (away)")
        renamed = await guild.fetch_member(member.id)
        await member.kick(reason="spam")
        await guild.ban(discord.Object(id=member.id), reason="repeat offender",
                        delete_message_seconds=0)
        bans = [(entry.user.id, entry.reason) async for entry in guild.bans(limit=10)]
        await guild.unban(discord.Object(id=member.id))
        return renamed.nick, bans, [b async for b in guild.bans(limit=10)]

    nick, bans, after_unban = dpy(work)
    assert nick == "bob (away)"
    assert bans and bans[0][1] == "repeat offender"
    assert after_unban == []
    assert not [m for m in twin.views()["members"]["items"] if m["username"] == "bob"]


def test_kicking_an_unknown_member_raises_not_found(dpy, server):
    async def work(client):
        guild = await client.fetch_guild(int(server["guild_id"]))
        with pytest.raises(discord.NotFound) as exc:
            await guild.kick(discord.Object(id=1214135245209699999))
        return exc.value.code

    assert dpy(work) == 10007  # Unknown Member


# --- discord.py: webhooks ----------------------------------------------------

def test_webhook_create_and_execute_through_its_url(dpy, server, twin):
    import aiohttp

    async def work(client):
        channel = await client.fetch_channel(int(server["channels"]["alerts"]))
        webhook = await channel.create_webhook(name="ci")
        async with aiohttp.ClientSession() as session:
            # The URL the API hands back is unversioned and needs no bot token.
            from_url = discord.Webhook.from_url(webhook.url, session=session)
            sent = await from_url.send("build green", username="ci-bot", wait=True)
            await from_url.edit_message(sent.id, content="build green (cached)")
            fetched = await from_url.fetch_message(sent.id)
        return webhook.url, sent.author.name, fetched.content

    url, author, content = dpy(work)
    assert url.startswith("https://discord.com/api/webhooks/")
    assert (author, content) == ("ci-bot", "build green (cached)")
    assert [w["name"] for w in twin.views()["webhooks"]["items"]] == ["ci"]


# --- discord.py: typed errors and faults -------------------------------------

def test_missing_channel_raises_not_found(dpy):
    async def work(client):
        with pytest.raises(discord.NotFound) as exc:
            await client.fetch_channel(1214135245209600123)
        return exc.value.status, exc.value.code

    assert dpy(work) == (404, 10003)


def test_strict_auth_rejects_a_wrong_token(twin, dpy):
    twin.configure(strict_auth=True)

    async def work(client):
        with pytest.raises(discord.LoginFailure):
            await client.login("not-the-twin-token")
        await client.login(twin.token)  # the twin's own credential still works
        return client.user.name

    assert dpy(work, login=False) == "checkpoint-bot"


def test_rate_limit_fault_ends_in_a_typed_error_not_a_hang(twin, dpy, server):
    """429s carry retry_after and Via, so the SDK retries and then gives up."""
    started = time.perf_counter()

    async def work(client):
        twin.configure(rate_limit=0)  # after login, so the session is established
        with pytest.raises(discord.HTTPException) as exc:
            await client.fetch_channel(int(server["channels"]["general"]))
        return exc.value.status, exc.value.response.headers["Retry-After"]

    assert dpy(work) == (429, "1")
    assert time.perf_counter() - started < 60


# --- raw REST v10 ------------------------------------------------------------

def test_rest_message_crud_and_pagination(rest, server, twin):
    channel = server["channels"]["general"]
    ids = [rest.post(f"/channels/{channel}/messages",
                     json={"content": f"note {i}"}).json()["id"] for i in range(5)]
    assert all(mid.isdigit() for mid in ids)

    listed = rest.get(f"/channels/{channel}/messages", params={"limit": 2})
    assert [m["id"] for m in listed.json()] == ids[::-1][:2]

    before = rest.get(f"/channels/{channel}/messages",
                      params={"before": ids[2], "limit": 10}).json()
    assert [m["id"] for m in before] == ids[:2][::-1]

    after = rest.get(f"/channels/{channel}/messages",
                     params={"after": ids[2], "limit": 10}).json()
    assert [m["id"] for m in after] == ids[3:][::-1]

    edited = rest.patch(f"/channels/{channel}/messages/{ids[0]}", json={"content": "edited"})
    assert edited.json()["content"] == "edited"
    assert edited.json()["edited_timestamp"]

    assert rest.delete(f"/channels/{channel}/messages/{ids[0]}").status_code == 204
    assert rest.get(f"/channels/{channel}/messages/{ids[0]}").status_code == 404
    contents = [m["content"] for m in twin.views()["messages"]["items"]]
    assert "note 0" not in contents and "edited" not in contents


def test_rest_multipart_upload_is_downloadable(rest, twin, server):
    channel = server["channels"]["alerts"]
    response = rest.post(
        f"/channels/{channel}/messages",
        data={"payload_json": json.dumps(
            {"content": "log attached", "attachments": [{"id": 0, "filename": "deploy.log"}]})},
        files={"files[0]": ("deploy.log", b"rollout complete\n", "text/plain")},
    )
    assert response.status_code == 200
    attachment = response.json()["attachments"][0]
    assert (attachment["filename"], attachment["size"]) == ("deploy.log", 17)
    assert attachment["url"].startswith("https://cdn.discordapp.com/attachments/")

    # The CDN path the attachment points at is served by the twin, without auth.
    path = attachment["url"].split("cdn.discordapp.com", 1)[1]
    downloaded = httpx.get(f"{twin.url}{path}", timeout=REST_TIMEOUT)
    assert downloaded.content == b"rollout complete\n"


def test_rest_channel_lifecycle_and_state(rest, server, twin):
    guild = server["guild_id"]
    created = rest.post(f"/guilds/{guild}/channels",
                        json={"name": "release-notes", "type": 0, "topic": "ship log"})
    assert created.status_code == 201
    channel_id = created.json()["id"]

    patched = rest.patch(f"/channels/{channel_id}", json={"topic": "ship log (archived)"})
    assert patched.json()["topic"] == "ship log (archived)"

    listing = rest.get(f"/guilds/{guild}/channels").json()
    assert "release-notes" in [c["name"] for c in listing]

    assert rest.delete(f"/channels/{channel_id}").status_code == 200
    assert rest.get(f"/channels/{channel_id}").json()["code"] == 10003
    assert channel_id not in twin.state()["channels"]


def test_rest_member_search_and_timeout(rest, server):
    guild, alice = server["guild_id"], server["users"]["alice"]
    found = rest.get(f"/guilds/{guild}/members/search", params={"query": "ali", "limit": 10})
    assert [m["user"]["username"] for m in found.json()] == ["alice"]

    timed_out = rest.patch(f"/guilds/{guild}/members/{alice}",
                           json={"communication_disabled_until": "2026-01-01T00:00:00+00:00"})
    assert timed_out.json()["communication_disabled_until"] == "2026-01-01T00:00:00+00:00"
    assert rest.get(f"/guilds/{guild}/members/{alice}").json()["nick"] == "Alice"


def test_rest_pins_endpoint_returns_items_and_cursor(rest, server):
    channel = server["channels"]["general"]
    message_id = rest.post(f"/channels/{channel}/messages",
                           json={"content": "pin me"}).json()["id"]
    assert rest.put(f"/channels/{channel}/messages/pins/{message_id}").status_code == 204
    pins = rest.get(f"/channels/{channel}/messages/pins").json()
    assert pins["has_more"] is False
    assert [item["message"]["id"] for item in pins["items"]] == [message_id]
    assert pins["items"][0]["pinned_at"]
    assert rest.delete(f"/channels/{channel}/messages/pins/{message_id}").status_code == 204
    assert rest.get(f"/channels/{channel}/messages/pins").json()["items"] == []


def test_rest_webhook_execute_needs_no_bot_token(rest, twin, server):
    channel = server["channels"]["alerts"]
    webhook = rest.post(f"/channels/{channel}/webhooks", json={"name": "monitor"}).json()
    unauthenticated = httpx.Client(base_url=twin.url, timeout=REST_TIMEOUT)
    path = webhook["url"].split("discord.com", 1)[1]  # unversioned /api/webhooks/{id}/{token}

    acknowledged = unauthenticated.post(path, json={"content": "no wait"})
    assert acknowledged.status_code == 204

    waited = unauthenticated.post(path, params={"wait": "true"},
                                  json={"content": "ALERT: error rate 7.3%"})
    assert waited.status_code == 200
    assert waited.json()["content"] == "ALERT: error rate 7.3%"
    assert waited.json()["webhook_id"] == webhook["id"]
    unauthenticated.close()

    posted = [m for m in twin.views()["messages"]["items"] if m["via_webhook"]]
    assert len(posted) == 2 and posted[0]["channel"] == "alerts"
    assert ("create", "messages") in [(e["op"], e["resource"]) for e in twin.trace()]


def test_rest_typed_errors_and_unknown_routes(rest, twin, server):
    assert rest.get("/channels/1214135245209600123").json() == {
        "code": 10003, "message": "Unknown Channel"}
    assert rest.get(f"/guilds/{server['guild_id']}/members/1214135245209600123").json()["code"] \
        == 10007
    empty = rest.post(f"/channels/{server['channels']['general']}/messages", json={})
    assert (empty.status_code, empty.json()["code"]) == (400, 50006)

    # Unimplemented routes answer in Discord's envelope, not FastAPI's {"detail": ...}.
    unknown = rest.get(f"/guilds/{server['guild_id']}/audit-logs")
    assert unknown.json() == {"code": 0, "message": "404: Not Found"}

    unauthenticated = httpx.get(f"{twin.url}/api/v10/users/@me", timeout=REST_TIMEOUT)
    assert (unauthenticated.status_code, unauthenticated.json()["code"]) == (401, 0)


def test_rest_rate_limit_carries_retry_after(rest, twin):
    twin.configure(rate_limit=0)
    response = rest.get("/users/@me")
    assert response.status_code == 429
    assert response.json()["retry_after"] > 0
    assert response.headers["retry-after"] == "1"
    assert response.headers["x-ratelimit-scope"] == "user"


def test_rest_slash_command_registration(rest, twin):
    application = rest.get("/oauth2/applications/@me").json()["id"]
    commands = rest.put(f"/applications/{application}/commands", json=[
        {"name": "status", "description": "Report service status"},
        {"name": "page", "description": "Page the on-call engineer"},
    ])
    assert sorted(c["name"] for c in commands.json()) == ["page", "status"]
    assert all(c["application_id"] == application for c in commands.json())
    assert sorted(c["name"] for c in rest.get(
        f"/applications/{application}/commands").json()) == ["page", "status"]
    assert len(twin.views()["commands"]["items"]) == 2
