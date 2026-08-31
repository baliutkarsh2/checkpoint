"""Acceptance tests for the Supabase twin REST API.

Uses httpx.AsyncClient + ASGITransport to test the twin in-process
(no subprocess, no ports). Covers PostgREST, Auth, Storage, and all
MCP-facing routes that were previously missing or duplicated.
"""
from __future__ import annotations

import httpx
import pytest

from checkpoint.fake_credentials import FAKE_SUPABASE_TOKEN
from checkpoint.twins.supabase import STATE, TRACE, _fresh_state, app

TOKEN = FAKE_SUPABASE_TOKEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JAUTH = {**AUTH, "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def reset_state():
    STATE.clear()
    STATE.update(_fresh_state())
    TRACE.clear()
    yield
    STATE.clear()
    STATE.update(_fresh_state())
    TRACE.clear()


@pytest.fixture
def transport():
    return httpx.ASGITransport(app=app)


# --- Introspection -----------------------------------------------------------

@pytest.mark.asyncio
async def test_health(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/_health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_reset(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/things", headers=JAUTH, json={"name": "x"})
        r = await c.post("/_reset")
    assert r.status_code == 200
    assert STATE["tables"] == {}


# --- PostgREST ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_tables_empty(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/rest/v1/", headers=AUTH)
    assert r.status_code == 200
    assert "definitions" in r.json()


@pytest.mark.asyncio
async def test_list_tables_after_insert(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/products", headers=JAUTH, json={"name": "Widget"})
        r = await c.get("/rest/v1/", headers=AUTH)
    defs = r.json()["definitions"]
    assert "products" in defs
    assert defs["products"]["row_count"] == 1


@pytest.mark.asyncio
async def test_insert_and_query(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/rest/v1/products", headers=JAUTH,
                         json={"name": "Widget", "price": 9.99})
        assert r.status_code == 201
        r = await c.get("/rest/v1/products", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "Widget"


@pytest.mark.asyncio
async def test_insert_auto_id(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/items", headers=JAUTH, json={"val": 1})
    assert STATE["tables"]["items"]["rows"][0].get("id") is not None


@pytest.mark.asyncio
async def test_insert_bulk(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/rest/v1/logs", headers=JAUTH,
                         json=[{"msg": "a"}, {"msg": "b"}, {"msg": "c"}])
    assert r.status_code == 201
    assert len(STATE["tables"]["logs"]["rows"]) == 3


@pytest.mark.asyncio
async def test_query_with_eq_filter(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/orders", headers=JAUTH,
                     json=[{"status": "pending"}, {"status": "shipped"}, {"status": "pending"}])
        r = await c.get("/rest/v1/orders", headers=AUTH, params={"status": "eq.pending"})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(row["status"] == "pending" for row in rows)


@pytest.mark.asyncio
async def test_query_limit_offset(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/items", headers=JAUTH,
                     json=[{"n": i} for i in range(10)])
        r = await c.get("/rest/v1/items", headers=AUTH, params={"limit": 3, "offset": 2})
    assert r.status_code == 200
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_update_rows(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/tasks", headers=JAUTH,
                     json={"title": "Buy milk", "done": False})
        r = await c.patch("/rest/v1/tasks", headers=JAUTH,
                          params={"title": "eq.Buy milk"}, json={"done": True})
    assert r.status_code == 204
    assert STATE["tables"]["tasks"]["rows"][0]["done"] is True


@pytest.mark.asyncio
async def test_delete_rows(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/events", headers=JAUTH,
                     json=[{"type": "click"}, {"type": "view"}, {"type": "click"}])
        r = await c.delete("/rest/v1/events", headers=AUTH, params={"type": "eq.click"})
    assert r.status_code == 204
    rows = STATE["tables"]["events"]["rows"]
    assert len(rows) == 1
    assert rows[0]["type"] == "view"


@pytest.mark.asyncio
async def test_upsert_on_conflict(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/settings", headers=JAUTH,
                     json={"key": "theme", "value": "dark"}, params={"on_conflict": "key"})
        await c.post("/rest/v1/settings", headers=JAUTH,
                     json={"key": "theme", "value": "light"}, params={"on_conflict": "key"})
    rows = STATE["tables"]["settings"]["rows"]
    assert len(rows) == 1
    assert rows[0]["value"] == "light"


@pytest.mark.asyncio
async def test_rpc_stub(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/rest/v1/rpc/get_user_count", headers=JAUTH, json={})
    assert r.status_code == 200
    body = r.json()
    assert "_rpc" in body or "_stub" in body


@pytest.mark.asyncio
async def test_auth_requires_token(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/rest/v1/secrets")
    assert r.status_code == 401


# --- Auth API ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_auth_user(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/auth/v1/admin/users", headers=JAUTH,
                         json={"email": "alice@example.com", "password": "s3cr3t"})
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "alice@example.com"
    assert "id" in body


@pytest.mark.asyncio
async def test_list_auth_users(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/auth/v1/admin/users", headers=JAUTH, json={"email": "bob@example.com"})
        r = await c.get("/auth/v1/admin/users", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["users"][0]["email"] == "bob@example.com"


@pytest.mark.asyncio
async def test_get_auth_user(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        uid = (await c.post("/auth/v1/admin/users", headers=JAUTH,
                            json={"email": "carol@example.com"})).json()["id"]
        r = await c.get(f"/auth/v1/admin/users/{uid}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["email"] == "carol@example.com"


@pytest.mark.asyncio
async def test_update_auth_user_put(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        uid = (await c.post("/auth/v1/admin/users", headers=JAUTH,
                            json={"email": "dan@example.com"})).json()["id"]
        r = await c.put(f"/auth/v1/admin/users/{uid}", headers=JAUTH,
                        json={"email": "dan-new@example.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "dan-new@example.com"


@pytest.mark.asyncio
async def test_update_auth_user_patch(transport):
    """PATCH is the method the MCP server uses — must be supported."""
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        uid = (await c.post("/auth/v1/admin/users", headers=JAUTH,
                            json={"email": "eve@example.com"})).json()["id"]
        r = await c.patch(f"/auth/v1/admin/users/{uid}", headers=JAUTH,
                          json={"email": "eve-updated@example.com", "ban_duration": "24h"})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "eve-updated@example.com"
    assert body.get("ban_duration") == "24h"


@pytest.mark.asyncio
async def test_delete_auth_user(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        uid = (await c.post("/auth/v1/admin/users", headers=JAUTH,
                            json={"email": "frank@example.com"})).json()["id"]
        r = await c.delete(f"/auth/v1/admin/users/{uid}", headers=AUTH)
    assert r.status_code == 200
    assert uid not in STATE["auth_users"]


# --- Storage: Buckets --------------------------------------------------------

@pytest.mark.asyncio
async def test_create_bucket(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/storage/v1/bucket", headers=JAUTH,
                         json={"name": "avatars", "public": True})
    assert r.status_code == 200
    assert "avatars" in STATE["storage"]["buckets"]
    assert STATE["storage"]["buckets"]["avatars"]["public"] is True


@pytest.mark.asyncio
async def test_create_bucket_conflict(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/storage/v1/bucket", headers=JAUTH, json={"name": "docs"})
        r = await c.post("/storage/v1/bucket", headers=JAUTH, json={"name": "docs"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_list_and_get_buckets(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/storage/v1/bucket", headers=JAUTH, json={"name": "a"})
        await c.post("/storage/v1/bucket", headers=JAUTH, json={"name": "b"})
        r_list = await c.get("/storage/v1/bucket", headers=AUTH)
        r_get = await c.get("/storage/v1/bucket/a", headers=AUTH)
    assert {b["name"] for b in r_list.json()} == {"a", "b"}
    assert r_get.json()["name"] == "a"


@pytest.mark.asyncio
async def test_update_bucket(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/storage/v1/bucket", headers=JAUTH, json={"name": "files", "public": False})
        r = await c.put("/storage/v1/bucket/files", headers=JAUTH, json={"public": True})
    assert r.status_code == 200
    assert STATE["storage"]["buckets"]["files"]["public"] is True


@pytest.mark.asyncio
async def test_empty_bucket(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/storage/v1/bucket", headers=JAUTH, json={"name": "tmp"})
        await c.post("/storage/v1/object/tmp/file.txt",
                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "text/plain"},
                     content=b"hello")
        r = await c.post("/storage/v1/bucket/tmp/empty", headers=JAUTH)
    assert r.status_code == 200
    assert STATE["storage"]["buckets"]["tmp"]["object_count"] == 0
    assert not [k for k in STATE["storage"]["objects"] if k.startswith("tmp/")]


@pytest.mark.asyncio
async def test_delete_bucket(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/storage/v1/bucket", headers=JAUTH, json={"name": "trash"})
        r = await c.delete("/storage/v1/bucket/trash", headers=AUTH)
    assert r.status_code == 200
    assert "trash" not in STATE["storage"]["buckets"]


# --- Storage: Objects --------------------------------------------------------

async def _make_bucket(c: httpx.AsyncClient, name: str) -> None:
    await c.post("/storage/v1/bucket", headers=JAUTH, json={"name": name})


@pytest.mark.asyncio
async def test_upload_and_download_object(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "pics")
        r_up = await c.post("/storage/v1/object/pics/photo.jpg",
                            headers={"Authorization": f"Bearer {TOKEN}",
                                     "Content-Type": "image/jpeg"},
                            content=b"\xff\xd8\xff")
        assert r_up.status_code == 200
        assert r_up.json()["Key"] == "pics/photo.jpg"
        r_dl = await c.get("/storage/v1/object/pics/photo.jpg", headers=AUTH)
    assert r_dl.status_code == 200


@pytest.mark.asyncio
async def test_list_objects_get(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "docs")
        for name in ["a.txt", "b.txt", "sub/c.txt"]:
            await c.post(f"/storage/v1/object/docs/{name}",
                         headers={"Authorization": f"Bearer {TOKEN}",
                                  "Content-Type": "text/plain"},
                         content=b"x")
        r = await c.get("/storage/v1/object/list/docs", headers=AUTH)
    assert r.status_code == 200
    assert {o["name"] for o in r.json()} == {"a.txt", "b.txt", "sub/c.txt"}


@pytest.mark.asyncio
async def test_list_objects_post(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "data")
        await c.post("/storage/v1/object/data/file.csv",
                     headers={"Authorization": f"Bearer {TOKEN}",
                              "Content-Type": "text/csv"},
                     content=b"a,b")
        r = await c.post("/storage/v1/object/list/data", headers=JAUTH, json={"prefix": ""})
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_get_object_info(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "assets")
        await c.post("/storage/v1/object/assets/logo.png",
                     headers={"Authorization": f"Bearer {TOKEN}",
                              "Content-Type": "image/png"},
                     content=b"PNG")
        r = await c.get("/storage/v1/object/info/assets/logo.png", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "logo.png"
    assert "metadata" in body
    assert "_content" not in body


@pytest.mark.asyncio
async def test_bulk_delete_objects(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "logs")
        for name in ["2024-01.log", "2024-02.log", "2024-03.log"]:
            await c.post(f"/storage/v1/object/logs/{name}",
                         headers={"Authorization": f"Bearer {TOKEN}",
                                  "Content-Type": "text/plain"},
                         content=b"log")
        r = await c.request("DELETE", "/storage/v1/object/logs", headers=JAUTH,
                             json={"prefixes": ["2024-01.log", "2024-02.log"]})
    assert r.status_code == 200
    removed = {o["name"] for o in r.json()}
    assert removed == {"2024-01.log", "2024-02.log"}
    remaining = [k for k in STATE["storage"]["objects"] if k.startswith("logs/")]
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_move_object(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "vault")
        await c.post("/storage/v1/object/vault/old.txt",
                     headers={"Authorization": f"Bearer {TOKEN}",
                              "Content-Type": "text/plain"},
                     content=b"secret")
        r = await c.post("/storage/v1/object/move", headers=JAUTH,
                         json={"bucketId": "vault", "sourceKey": "old.txt",
                               "destinationKey": "new.txt"})
    assert r.status_code == 200
    assert "vault/old.txt" not in STATE["storage"]["objects"]
    assert "vault/new.txt" in STATE["storage"]["objects"]


@pytest.mark.asyncio
async def test_copy_object(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "src")
        await _make_bucket(c, "dst")
        await c.post("/storage/v1/object/src/original.txt",
                     headers={"Authorization": f"Bearer {TOKEN}",
                              "Content-Type": "text/plain"},
                     content=b"data")
        r = await c.post("/storage/v1/object/copy", headers=JAUTH,
                         json={"bucketId": "src", "sourceKey": "original.txt",
                               "destinationKey": "copy.txt",
                               "destinationBucket": "dst"})
    assert r.status_code == 200
    assert "src/original.txt" in STATE["storage"]["objects"]
    assert "dst/copy.txt" in STATE["storage"]["objects"]


@pytest.mark.asyncio
async def test_create_signed_url(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "private")
        await c.post("/storage/v1/object/private/secret.pdf",
                     headers={"Authorization": f"Bearer {TOKEN}",
                              "Content-Type": "application/pdf"},
                     content=b"%PDF")
        r = await c.post("/storage/v1/object/sign/private/secret.pdf",
                         headers=JAUTH, json={"expiresIn": 3600})
    assert r.status_code == 200
    body = r.json()
    assert "signedURL" in body
    assert "token" in body


@pytest.mark.asyncio
async def test_mcp_json_envelope_upload(transport):
    """The MCP shim sends JSON envelopes for text object upload."""
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await _make_bucket(c, "notes")
        r = await c.post("/storage/v1/object/notes/readme.md",
                         headers=JAUTH,
                         json={"_mcp_content": "# Hello",
                               "_mcp_content_type": "text/markdown"})
    assert r.status_code == 200
    obj = STATE["storage"]["objects"]["notes/readme.md"]
    assert obj.get("_content") == "# Hello"


# --- Trace -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trace_captures_calls(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/rest/v1/things", headers=JAUTH, json={"x": 1})
        await c.get("/rest/v1/things", headers=AUTH)
    assert len(TRACE) == 2
    assert TRACE[0]["method"] == "POST"
    assert TRACE[1]["method"] == "GET"
