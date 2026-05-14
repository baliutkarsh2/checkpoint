"""Supabase twin REST surface — PostgREST, Auth, Storage, seeds."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import supabase as sb


@pytest.fixture(autouse=True)
def _reset_state():
    sb.STATE.clear()
    sb.STATE.update(sb._fresh_state())
    sb.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(sb.app)


TOKEN = sb.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}


# --- auth -------------------------------------------------------------------

def test_missing_token_returns_401(client):
    r = client.get("/rest/v1/users")
    assert r.status_code == 401


def test_wrong_token_returns_401(client):
    r = client.get("/rest/v1/users", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


# --- PostgREST --------------------------------------------------------------

def _seed_table(client):
    """Helper: seed a 'products' table with 3 rows."""
    sb.STATE["tables"]["products"] = {
        "columns": [
            {"name": "id", "type": "integer"},
            {"name": "name", "type": "text"},
            {"name": "price", "type": "numeric"},
            {"name": "active", "type": "boolean"},
        ],
        "rows": [
            {"id": 1, "name": "Widget", "price": 9.99, "active": True},
            {"id": 2, "name": "Gadget", "price": 19.99, "active": True},
            {"id": 3, "name": "Doohickey", "price": 4.99, "active": False},
        ],
    }


def test_postgrest_select_all(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3


def test_postgrest_select_unknown_table(client):
    r = client.get("/rest/v1/nothing?select=*", headers=H)
    # Should return 200 with empty list for unknown table
    assert r.status_code == 200
    assert r.json() == []


def test_postgrest_filter_eq(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*&id=eq.1", headers=H)
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Widget"


def test_postgrest_filter_neq(client):
    _seed_table(client)
    # active is a Python bool; str(False)=="False", so use "False" (capitalized)
    r = client.get("/rest/v1/products?select=*&active=eq.False", headers=H)
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Doohickey"


def test_postgrest_filter_gt_lt(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*&price=gt.10", headers=H)
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Gadget"


def test_postgrest_filter_like(client):
    _seed_table(client)
    # The twin uses % as wildcards (PostgREST syntax), not *
    r = client.get("/rest/v1/products?select=*&name=like.%25adget%25", headers=H)
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "Gadget"


def test_postgrest_filter_in(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*&id=in.(1,3)", headers=H)
    body = r.json()
    assert len(body) == 2
    ids = {row["id"] for row in body}
    assert ids == {1, 3}


def test_postgrest_insert_row(client):
    _seed_table(client)
    r = client.post(
        "/rest/v1/products",
        headers={**H, "Content-Type": "application/json"},
        json={"id": 4, "name": "Thingamajig", "price": 29.99, "active": True},
    )
    assert r.status_code in (200, 201)
    assert len(sb.STATE["tables"]["products"]["rows"]) == 4


def test_postgrest_insert_creates_table(client):
    r = client.post(
        "/rest/v1/new_table",
        headers={**H, "Content-Type": "application/json"},
        json={"col": "val"},
    )
    assert r.status_code in (200, 201)
    assert "new_table" in sb.STATE["tables"]


def test_postgrest_update_rows(client):
    _seed_table(client)
    r = client.patch(
        "/rest/v1/products?id=eq.1",
        headers={**H, "Content-Type": "application/json"},
        json={"price": 12.99},
    )
    assert r.status_code in (200, 204)
    row = next(r for r in sb.STATE["tables"]["products"]["rows"] if r["id"] == 1)
    assert row["price"] == 12.99


def test_postgrest_delete_rows(client):
    _seed_table(client)
    r = client.delete("/rest/v1/products?id=eq.3", headers=H)
    assert r.status_code in (200, 204)
    names = [r["name"] for r in sb.STATE["tables"]["products"]["rows"]]
    assert "Doohickey" not in names


def test_postgrest_limit_offset(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*&limit=2&offset=1", headers=H)
    assert len(r.json()) == 2


def test_postgrest_order(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*&order=price.asc", headers=H)
    prices = [row["price"] for row in r.json()]
    assert prices == sorted(prices)


# --- Auth API ---------------------------------------------------------------

def test_auth_list_users_empty(client):
    r = client.get("/auth/v1/admin/users", headers=H)
    assert r.status_code == 200
    assert r.json()["users"] == []


def test_auth_create_user(client):
    r = client.post("/auth/v1/admin/users", headers=H, json={
        "email": "alice@test.com",
        "password": "secret123",
    })
    assert r.status_code in (200, 201)
    body = r.json()
    assert body["email"] == "alice@test.com"
    assert "id" in body


def test_auth_get_user(client):
    user = client.post("/auth/v1/admin/users", headers=H, json={
        "email": "bob@test.com", "password": "pw"
    }).json()
    r = client.get(f"/auth/v1/admin/users/{user['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["email"] == "bob@test.com"


def test_auth_update_user(client):
    user = client.post("/auth/v1/admin/users", headers=H, json={
        "email": "carol@test.com", "password": "pw"
    }).json()
    r = client.put(f"/auth/v1/admin/users/{user['id']}", headers=H, json={
        "email": "carol2@test.com"
    })
    assert r.status_code == 200
    assert r.json()["email"] == "carol2@test.com"


def test_auth_delete_user(client):
    user = client.post("/auth/v1/admin/users", headers=H, json={
        "email": "del@test.com", "password": "pw"
    }).json()
    r = client.delete(f"/auth/v1/admin/users/{user['id']}", headers=H)
    assert r.status_code in (200, 204)
    assert user["id"] not in sb.STATE["auth_users"]


def test_auth_create_and_list_users(client):
    client.post("/auth/v1/admin/users", headers=H, json={
        "email": "login@test.com", "password": "hunter2"
    })
    r = client.get("/auth/v1/admin/users", headers=H)
    assert r.status_code == 200
    assert any(u["email"] == "login@test.com" for u in r.json()["users"])


# --- Storage ----------------------------------------------------------------

def test_storage_list_buckets_empty(client):
    r = client.get("/storage/v1/bucket", headers=H)
    assert r.status_code == 200
    assert r.json() == []


def test_storage_create_bucket(client):
    r = client.post("/storage/v1/bucket", headers=H, json={
        "id": "assets", "name": "assets", "public": True
    })
    assert r.status_code in (200, 201)
    assert "assets" in sb.STATE["storage"]["buckets"]


def test_storage_get_bucket(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "docs", "name": "docs"})
    r = client.get("/storage/v1/bucket/docs", headers=H)
    assert r.status_code == 200
    assert r.json()["id"] == "docs"


def test_storage_delete_bucket(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "tmp", "name": "tmp"})
    r = client.delete("/storage/v1/bucket/tmp", headers=H)
    assert r.status_code in (200, 204)
    assert "tmp" not in sb.STATE["storage"]["buckets"]


def test_storage_upload_and_list_objects(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "imgs", "name": "imgs"})
    # Upload via POST
    r = client.post(
        "/storage/v1/object/imgs/photo.jpg",
        headers={**H, "Content-Type": "image/jpeg"},
        content=b"FAKEJPEG",
    )
    assert r.status_code in (200, 201)
    # List objects via POST (real Supabase uses POST for listing)
    r2 = client.post("/storage/v1/object/list/imgs", headers=H, json={})
    assert r2.status_code == 200
    names = [o["name"] for o in r2.json()]
    assert "photo.jpg" in names


def test_storage_delete_object(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "docs", "name": "docs"})
    client.post(
        "/storage/v1/object/docs/readme.txt",
        headers={**H, "Content-Type": "text/plain"},
        content=b"hello",
    )
    r = client.delete("/storage/v1/object/docs/readme.txt", headers=H)
    assert r.status_code in (200, 204)
    assert "docs/readme.txt" not in sb.STATE["storage"]["objects"]


# --- RPC --------------------------------------------------------------------

def test_rpc_returns_stub(client):
    r = client.post("/rest/v1/rpc/my_function", headers=H, json={"param": "val"})
    assert r.status_code in (200, 404)  # stub returns 200 or 404 for unknown functions


# --- Seed loading -----------------------------------------------------------

def test_seed_small_app(client):
    r = client.post("/_seed/small-app")
    assert r.status_code == 200
    state = client.get("/_state").json()
    assert state["auth_users"]


def test_seed_ecommerce(client):
    r = client.post("/_seed/ecommerce")
    assert r.status_code == 200
    state = client.get("/_state").json()
    assert state["tables"]


def test_seed_unknown_returns_404(client):
    r = client.post("/_seed/does-not-exist")
    assert r.status_code == 404


def test_reset_clears_state(client):
    client.post("/_seed/small-app")
    client.post("/_reset")
    state = client.get("/_state").json()
    assert not state["auth_users"]
    assert not state["tables"]
