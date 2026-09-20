"""Supabase twin REST surface — PostgREST, Auth, Storage, seeds."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import supabase as sb


@pytest.fixture(autouse=True)
def _reset_state():
    sb.TWIN.reset()
    yield


@pytest.fixture
def client():
    return TestClient(sb.app)


TOKEN = sb.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}
REPRESENTATION = {**H, "Prefer": "return=representation"}


# --- auth -------------------------------------------------------------------

def test_missing_token_returns_401(client):
    r = client.get("/rest/v1/users")
    assert r.status_code == 401


def test_wrong_token_returns_401_under_strict_auth(client):
    client.post("/_config", json={"strict_auth": True})
    r = client.get("/rest/v1/users", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


def test_unknown_route_uses_the_service_error_shape(client):
    body = client.get("/rest/v1/products/nope/deeper", headers=H).json()
    assert set(body) == {"code", "details", "hint", "message"}
    assert set(client.get("/storage/v1/nope", headers=H).json()) >= {"statusCode", "error",
                                                                     "message"}


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
    assert len(r.json()) == 3


def test_postgrest_select_unknown_table_is_404(client):
    r = client.get("/rest/v1/nothing?select=*", headers=H)
    assert r.status_code == 404
    assert r.json()["code"] == "PGRST205"


def test_postgrest_filter_eq(client):
    _seed_table(client)
    body = client.get("/rest/v1/products?select=*&id=eq.1", headers=H).json()
    assert [row["name"] for row in body] == ["Widget"]


def test_postgrest_filter_eq_on_boolean(client):
    _seed_table(client)
    body = client.get("/rest/v1/products?select=*&active=eq.false", headers=H).json()
    assert [row["name"] for row in body] == ["Doohickey"]


def test_postgrest_filter_gt(client):
    _seed_table(client)
    body = client.get("/rest/v1/products?select=*&price=gt.10", headers=H).json()
    assert [row["name"] for row in body] == ["Gadget"]


def test_postgrest_filter_like_is_anchored(client):
    _seed_table(client)
    assert client.get("/rest/v1/products?name=like.adget", headers=H).json() == []
    body = client.get("/rest/v1/products?select=name&name=like.%25adget", headers=H).json()
    assert body == [{"name": "Gadget"}]


def test_postgrest_filter_in(client):
    _seed_table(client)
    body = client.get("/rest/v1/products?select=*&id=in.(1,3)", headers=H).json()
    assert {row["id"] for row in body} == {1, 3}


def test_postgrest_or_filter_matches_only_its_branches(client):
    _seed_table(client)
    body = client.get("/rest/v1/products?select=id&or=(id.eq.1,id.eq.3)", headers=H).json()
    assert [row["id"] for row in body] == [1, 3]


def test_postgrest_unknown_operator_is_rejected(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*&id=bogus.1", headers=H)
    assert r.status_code == 400 and r.json()["code"] == "PGRST100"


def test_postgrest_unknown_column_is_rejected(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*&nope=eq.1", headers=H)
    assert r.status_code == 400 and r.json()["code"] == "42703"


def test_postgrest_delete_with_an_unparseable_filter_keeps_every_row(client):
    _seed_table(client)
    r = client.delete("/rest/v1/products?whatever", headers=H)
    assert r.status_code == 400
    assert len(sb.STATE["tables"]["products"]["rows"]) == 3


def test_postgrest_null_comparisons_follow_sql_logic(client):
    sb.STATE["tables"]["notes"] = {"columns": ["id", "body"],
                                   "rows": [{"id": 1, "body": None}, {"id": 2, "body": "hi"}]}
    assert client.get("/rest/v1/notes?body=neq.hi", headers=H).json() == []
    assert client.get("/rest/v1/notes?select=id&body=is.null", headers=H).json() == [{"id": 1}]
    assert client.get("/rest/v1/notes?select=id&body=not.is.null", headers=H).json() == [{"id": 2}]


def test_postgrest_count_exact_counts_filtered_rows(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=*&active=eq.true",
                   headers={**H, "Prefer": "count=exact"})
    assert r.headers["content-range"] == "0-1/2"


def test_postgrest_head_returns_the_count_without_a_body(client):
    _seed_table(client)
    r = client.head("/rest/v1/products?select=*", headers={**H, "Prefer": "count=exact"})
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-range"] == "0-2/3"


def test_postgrest_singular_accept_returns_one_object(client):
    _seed_table(client)
    singular = {**H, "Accept": "application/vnd.pgrst.object+json"}
    assert client.get("/rest/v1/products?id=eq.1", headers=singular).json()["name"] == "Widget"
    r = client.get("/rest/v1/products?id=eq.99", headers=singular)
    assert r.status_code == 406 and r.json()["code"] == "PGRST116"


def test_postgrest_embedded_select(client):
    client.post("/_seed/small-app")
    body = client.get("/rest/v1/posts?select=title,profiles(username)&id=eq.post-1",
                      headers=H).json()
    assert body == [{"title": "Hello World", "profiles": {"username": "alice"}}]


def test_postgrest_embedding_an_unrelated_table_is_rejected(client):
    client.post("/_seed/small-app")
    r = client.get("/rest/v1/posts?select=title,nothing(id)", headers=H)
    assert r.status_code == 400 and r.json()["code"] == "PGRST200"


def test_postgrest_insert_row(client):
    _seed_table(client)
    r = client.post("/rest/v1/products", headers=REPRESENTATION,
                    json={"name": "Thingamajig", "price": 29.99, "active": True})
    assert r.status_code == 201
    assert r.json()[0]["id"] == 4, "integer keys continue after the seeded rows"
    assert len(sb.STATE["tables"]["products"]["rows"]) == 4


def test_postgrest_insert_into_a_missing_table_is_404(client):
    r = client.post("/rest/v1/new_table", headers=H, json={"col": "val"})
    assert r.status_code == 404
    assert "new_table" not in sb.STATE["tables"]


def test_postgrest_insert_duplicate_key_conflicts(client):
    _seed_table(client)
    r = client.post("/rest/v1/products", headers=H, json={"id": 1, "name": "again"})
    assert r.status_code == 409 and r.json()["code"] == "23505"


def test_postgrest_upsert_merges_on_the_primary_key(client):
    _seed_table(client)
    r = client.post("/rest/v1/products",
                    headers={**H, "Prefer": "return=representation,resolution=merge-duplicates"},
                    json={"id": 1, "price": 11.99})
    assert r.status_code == 201
    assert len(sb.STATE["tables"]["products"]["rows"]) == 3
    assert r.json()[0] == {"id": 1, "name": "Widget", "price": 11.99, "active": True}


def test_postgrest_update_rows(client):
    _seed_table(client)
    r = client.patch("/rest/v1/products?id=eq.1", headers=REPRESENTATION, json={"price": 12.99})
    assert r.status_code == 200
    row = next(r for r in sb.STATE["tables"]["products"]["rows"] if r["id"] == 1)
    assert row["price"] == 12.99


def test_postgrest_update_without_representation_is_204(client):
    _seed_table(client)
    r = client.patch("/rest/v1/products?id=eq.1", headers=H, json={"price": 1})
    assert r.status_code == 204


def test_postgrest_delete_rows(client):
    _seed_table(client)
    r = client.delete("/rest/v1/products?id=eq.3", headers=REPRESENTATION)
    assert r.status_code == 200
    assert "Doohickey" not in [r["name"] for r in sb.STATE["tables"]["products"]["rows"]]


def test_postgrest_max_affected_stops_a_wide_delete(client):
    _seed_table(client)
    r = client.delete("/rest/v1/products", headers={**H, "Prefer": "handling=strict,max-affected=1"})
    assert r.status_code == 400 and r.json()["code"] == "PGRST124"
    assert len(sb.STATE["tables"]["products"]["rows"]) == 3


def test_postgrest_limit_offset(client):
    _seed_table(client)
    assert len(client.get("/rest/v1/products?select=*&limit=2&offset=1", headers=H).json()) == 2


def test_postgrest_order(client):
    _seed_table(client)
    prices = [row["price"] for row in
              client.get("/rest/v1/products?select=*&order=price.asc", headers=H).json()]
    assert prices == sorted(prices)


def test_postgrest_csv_accept_returns_a_csv_document(client):
    _seed_table(client)
    r = client.get("/rest/v1/products?select=id,name&order=id.asc",
                   headers={**H, "Accept": "text/csv"})
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[:2] == ["id,name", "1,Widget"]


# --- Auth API ---------------------------------------------------------------

def test_auth_list_users_empty(client):
    r = client.get("/auth/v1/admin/users", headers=H)
    assert r.status_code == 200
    assert r.json()["users"] == []


def test_auth_list_users_ignores_blank_paging_parameters(client):
    client.post("/auth/v1/admin/users", headers=H, json={"email": "a@test.com"})
    r = client.get("/auth/v1/admin/users?page=&per_page=", headers=H)
    assert r.status_code == 200 and len(r.json()["users"]) == 1


def test_auth_create_user(client):
    r = client.post("/auth/v1/admin/users", headers=H,
                    json={"email": "alice@test.com", "password": "secret123"})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "alice@test.com" and body["aud"] == "authenticated"
    assert body["id"] and body["app_metadata"]["provider"] == "email"


def test_auth_create_user_rejects_a_duplicate_email(client):
    client.post("/auth/v1/admin/users", headers=H, json={"email": "dup@test.com"})
    r = client.post("/auth/v1/admin/users", headers=H, json={"email": "dup@test.com"})
    assert r.status_code == 422 and r.json()["error_code"] == "email_exists"


def test_auth_get_user(client):
    user = client.post("/auth/v1/admin/users", headers=H,
                       json={"email": "bob@test.com", "password": "pw12345"}).json()
    r = client.get(f"/auth/v1/admin/users/{user['id']}", headers=H)
    assert r.status_code == 200 and r.json()["email"] == "bob@test.com"


def test_auth_update_user(client):
    user = client.post("/auth/v1/admin/users", headers=H,
                       json={"email": "carol@test.com", "password": "pw12345"}).json()
    r = client.put(f"/auth/v1/admin/users/{user['id']}", headers=H,
                   json={"email": "carol2@test.com"})
    assert r.status_code == 200 and r.json()["email"] == "carol2@test.com"


def test_auth_delete_user(client):
    user = client.post("/auth/v1/admin/users", headers=H,
                       json={"email": "del@test.com", "password": "pw12345"}).json()
    r = client.request("DELETE", f"/auth/v1/admin/users/{user['id']}", headers=H, json={})
    assert r.status_code == 200
    assert user["id"] not in sb.STATE["auth_users"]


def test_auth_soft_delete_keeps_a_tombstone(client):
    user = client.post("/auth/v1/admin/users", headers=H, json={"email": "soft@test.com"}).json()
    client.request("DELETE", f"/auth/v1/admin/users/{user['id']}", headers=H,
                   json={"should_soft_delete": True})
    assert sb.STATE["auth_users"][user["id"]]["deleted_at"]
    assert client.get("/auth/v1/admin/users", headers=H).json()["users"] == []


def test_auth_signup_signin_and_current_user(client):
    signup = client.post("/auth/v1/signup", headers=H,
                         json={"email": "login@test.com", "password": "hunter22"}).json()
    assert signup["access_token"] and signup["user"]["email"] == "login@test.com"
    token = client.post("/auth/v1/token?grant_type=password", headers=H,
                        json={"email": "login@test.com", "password": "hunter22"})
    assert token.status_code == 200
    access = token.json()["access_token"]
    me = client.get("/auth/v1/user", headers={**H, "Authorization": f"Bearer {access}"})
    assert me.json()["email"] == "login@test.com"


def test_auth_signin_with_a_wrong_password_is_rejected(client):
    client.post("/auth/v1/signup", headers=H, json={"email": "x@test.com", "password": "hunter22"})
    r = client.post("/auth/v1/token?grant_type=password", headers=H,
                    json={"email": "x@test.com", "password": "nope1234"})
    assert r.status_code == 400 and r.json()["error_code"] == "invalid_credentials"


def test_auth_signup_rejects_a_weak_password(client):
    r = client.post("/auth/v1/signup", headers=H, json={"email": "w@test.com", "password": "123"})
    assert r.status_code == 422 and r.json()["error_code"] == "weak_password"


def test_auth_refresh_token_grant(client):
    session = client.post("/auth/v1/signup", headers=H,
                          json={"email": "r@test.com", "password": "hunter22"}).json()
    r = client.post("/auth/v1/token?grant_type=refresh_token", headers=H,
                    json={"refresh_token": session["refresh_token"]})
    assert r.status_code == 200 and r.json()["access_token"]


def test_auth_error_uses_the_2024_api_version_shape_when_asked(client):
    r = client.get("/auth/v1/admin/users/missing",
                   headers={**H, "X-Supabase-Api-Version": "2024-01-01"})
    assert r.status_code == 404
    assert r.json() == {"code": "user_not_found", "message": "User not found"}


# --- Storage ----------------------------------------------------------------

def test_storage_list_buckets_empty(client):
    r = client.get("/storage/v1/bucket", headers=H)
    assert r.status_code == 200 and r.json() == []


def test_storage_create_and_get_bucket_carry_every_sdk_field(client):
    client.post("/storage/v1/bucket", headers=H,
                json={"id": "assets", "name": "assets", "public": True, "file_size_limit": "1MB"})
    bucket = client.get("/storage/v1/bucket/assets", headers=H).json()
    assert bucket["public"] is True and bucket["file_size_limit"] == 1000000
    assert {"owner", "allowed_mime_types", "created_at", "updated_at"} <= set(bucket)


def test_storage_delete_bucket(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "tmp", "name": "tmp"})
    r = client.delete("/storage/v1/bucket/tmp", headers=H)
    assert r.status_code == 200 and "tmp" not in sb.STATE["storage"]["buckets"]


def test_storage_delete_bucket_refuses_while_it_holds_objects(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "full", "name": "full"})
    client.post("/storage/v1/object/full/a.txt", headers={**H, "Content-Type": "text/plain"},
                content=b"x")
    r = client.delete("/storage/v1/bucket/full", headers=H)
    assert r.status_code == 409
    assert client.post("/storage/v1/bucket/full/empty", headers=H).status_code == 200
    assert client.delete("/storage/v1/bucket/full", headers=H).status_code == 200


def test_storage_upload_parses_multipart_bodies(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "imgs", "name": "imgs"})
    body = (b'--B\r\nContent-Disposition: form-data; name="cacheControl"\r\n\r\n3600\r\n'
            b'--B\r\nContent-Disposition: form-data; name="file"; filename="photo.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\nFAKEJPEG\r\n--B--\r\n")
    r = client.post("/storage/v1/object/imgs/a/photo.jpg",
                    headers={**H, "Content-Type": "multipart/form-data; boundary=B"}, content=body)
    assert r.status_code == 200 and r.json()["Key"] == "imgs/a/photo.jpg"
    stored = sb.STATE["storage"]["objects"]["imgs/a/photo.jpg"]
    assert base64.b64decode(stored["_content_b64"]) == b"FAKEJPEG"
    assert stored["metadata"]["mimetype"] == "image/jpeg"
    assert client.get("/storage/v1/object/imgs/a/photo.jpg", headers=H).content == b"FAKEJPEG"


def test_storage_list_returns_names_relative_to_the_prefix(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "docs", "name": "docs"})
    client.post("/storage/v1/object/docs/notes/readme.txt",
                headers={**H, "Content-Type": "text/plain"}, content=b"hello")
    listing = client.post("/storage/v1/object/list/docs", headers=H, json={"prefix": "notes"})
    assert [o["name"] for o in listing.json()] == ["readme.txt"]
    root = client.post("/storage/v1/object/list/docs", headers=H, json={"prefix": ""}).json()
    assert root == [{"name": "notes", "id": None, "updated_at": None, "created_at": None,
                     "last_accessed_at": None, "metadata": None}]


def test_storage_upload_conflicts_unless_upsert(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "docs", "name": "docs"})
    client.post("/storage/v1/object/docs/a.txt", headers={**H, "Content-Type": "text/plain"},
                content=b"one")
    again = client.post("/storage/v1/object/docs/a.txt",
                        headers={**H, "Content-Type": "text/plain"}, content=b"two")
    assert again.status_code == 409 and again.json()["error"] == "Duplicate"
    upsert = client.post("/storage/v1/object/docs/a.txt",
                         headers={**H, "Content-Type": "text/plain", "x-upsert": "true"},
                         content=b"two")
    assert upsert.status_code == 200
    assert client.get("/storage/v1/object/docs/a.txt", headers=H).content == b"two"


def test_storage_signed_url_works_without_credentials(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "docs", "name": "docs"})
    client.post("/storage/v1/object/docs/a.txt", headers={**H, "Content-Type": "text/plain"},
                content=b"secret")
    signed = client.post("/storage/v1/object/sign/docs/a.txt", headers=H, json={"expiresIn": 60})
    url = signed.json()["signedURL"]
    assert url.startswith("/object/sign/docs/a.txt?token=")
    assert client.get(f"/storage/v1{url}").content == b"secret"
    assert client.get("/storage/v1/object/sign/docs/a.txt?token=nope").status_code == 400


def test_storage_public_bucket_serves_objects_without_credentials(client):
    client.post("/storage/v1/bucket", headers=H,
                json={"id": "pub", "name": "pub", "public": True})
    client.post("/storage/v1/object/pub/a.txt", headers={**H, "Content-Type": "text/plain"},
                content=b"open")
    assert client.get("/storage/v1/object/public/pub/a.txt").content == b"open"


def test_storage_delete_object(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "docs", "name": "docs"})
    client.post("/storage/v1/object/docs/readme.txt", headers={**H, "Content-Type": "text/plain"},
                content=b"hello")
    r = client.delete("/storage/v1/object/docs/readme.txt", headers=H)
    assert r.status_code == 200
    assert "docs/readme.txt" not in sb.STATE["storage"]["objects"]


def test_storage_missing_object_uses_the_storage_error_shape(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "docs", "name": "docs"})
    r = client.get("/storage/v1/object/docs/missing.txt", headers=H)
    assert r.status_code == 404
    assert r.json() == {"statusCode": "404", "code": "NoSuchKey", "error": "not_found",
                        "message": "Object not found"}


# --- RPC and edge functions --------------------------------------------------

def test_rpc_returns_a_stub_for_an_unseeded_function(client):
    r = client.post("/rest/v1/rpc/my_function", headers=H, json={"param": "val"})
    assert r.status_code == 200 and r.json()["_stub"] is True


def test_rpc_returns_the_seeded_result(client):
    client.post("/_seed/ecommerce")
    r = client.post("/rest/v1/rpc/get_top_products", headers=H, json={})
    assert r.json()[0]["product_id"] == "prod-001"


def test_edge_function_returns_the_seeded_result(client):
    client.post("/_seed-file", json={"state": {"edge_functions": {"hello": {"returns": {"ok": 1}}}}})
    assert client.post("/functions/v1/hello", headers=H, json={}).json() == {"ok": 1}


# --- classification and views ------------------------------------------------

def test_trace_classifies_supabase_writes(client):
    client.post("/_seed/ecommerce")
    client.patch("/rest/v1/products?id=eq.prod-001", headers=H, json={"stock": 1})
    client.post("/storage/v1/bucket/media/empty", headers=H)
    client.post("/auth/v1/token?grant_type=password", headers=H,
                json={"email": "admin@acme.test", "password": "pw"})
    ops = [(e["op"], e["resource"]) for e in client.get("/_trace").json()]
    assert ops == [("update", "products"), ("delete", "storage.objects"),
                   ("create", "auth.sessions")]


def test_views_expose_tables_users_buckets_and_objects(client):
    client.post("/_seed/ecommerce")
    collections = client.get("/_views").json()["collections"]
    assert collections["products"]["nouns"] == ["product", "products"]
    assert [item["_key"] for item in collections["products"]["items"]][0] == "prod-001"
    assert len(collections["auth_users"]["items"]) == 3
    assert collections["auth_users"]["tombstone"] == "deleted_at"
    media = next(b for b in collections["storage_buckets"]["items"] if b["id"] == "media")
    assert media["object_count"] == 2
    assert collections["storage_objects"]["items"][0]["bucket"] == "product-images"


# --- Seed loading -----------------------------------------------------------

def test_seed_small_app(client):
    assert client.post("/_seed/small-app").status_code == 200
    assert client.get("/_state").json()["auth_users"]


def test_seed_ecommerce(client):
    assert client.post("/_seed/ecommerce").status_code == 200
    assert client.get("/_state").json()["tables"]


def test_seeded_passwords_move_out_of_the_user_record(client):
    client.post("/_seed-file", json={"state": {"auth_users": {
        "u1": {"id": "u1", "email": "seed@test.com", "password": "hunter22"}}}})
    assert "password" not in client.get("/_state").json()["auth_users"]["u1"]
    ok = client.post("/auth/v1/token?grant_type=password", headers=H,
                     json={"email": "seed@test.com", "password": "hunter22"})
    bad = client.post("/auth/v1/token?grant_type=password", headers=H,
                      json={"email": "seed@test.com", "password": "wrong123"})
    assert ok.status_code == 200 and bad.status_code == 400


def test_seed_unknown_returns_404(client):
    assert client.post("/_seed/does-not-exist").status_code == 404


def test_reset_clears_state(client):
    client.post("/_seed/small-app")
    client.post("/_reset")
    state = client.get("/_state").json()
    assert not state["auth_users"] and not state["tables"]


def test_state_is_json_serialisable_after_an_upload(client):
    client.post("/storage/v1/bucket", headers=H, json={"id": "docs", "name": "docs"})
    client.post("/storage/v1/object/docs/a.bin", headers={**H, "Content-Type": "image/png"},
                content=b"\x89PNG\r\n\x1a\n")
    json.dumps(client.get("/_state").json())
