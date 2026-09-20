"""Supabase twin driven by supabase-py (postgrest, gotrue/supabase-auth, storage3)."""
from __future__ import annotations

import httpx
import pytest

supabase = pytest.importorskip("supabase")
postgrest_exceptions = pytest.importorskip("postgrest.exceptions")
storage_exceptions = pytest.importorskip("storage3.exceptions")
auth_errors = pytest.importorskip("supabase_auth.errors")

APIError = postgrest_exceptions.APIError
StorageApiError = storage_exceptions.StorageApiError
AuthApiError = auth_errors.AuthApiError

TWIN = "supabase"


@pytest.fixture(scope="module")
def client(live_twin):
    """One client for the module: building an SDK client costs a TLS context each time."""
    return supabase.create_client(
        live_twin.url, live_twin.token,
        supabase.ClientOptions(auto_refresh_token=False),  # no background refresh timer
    )


@pytest.fixture
def sb(live_twin, client):
    """A client on a twin holding the small-app schema (profiles/posts/comments)."""
    live_twin.seed("small-app")  # seeding resets the twin first
    yield client
    client.auth.sign_out()  # a signed-in session must not leak into the next test


# --- read --------------------------------------------------------------------

def test_select_all_rows_and_filter_on_a_boolean(sb):
    assert len(sb.table("posts").select("*").execute().data) == 3
    rows = sb.table("posts").select("id,title").eq("published", True).execute().data
    assert {row["title"] for row in rows} == {"Hello World", "Getting Started with Supabase"}


def test_or_filter_matches_only_its_branches(sb):
    rows = sb.table("posts").select("id").or_("id.eq.post-1,id.eq.post-2").execute().data
    assert sorted(row["id"] for row in rows) == ["post-1", "post-2"]


def test_not_is_null_excludes_null_rows(sb):
    rows = sb.table("profiles").select("id").not_.is_("website", "null").execute().data
    assert [row["id"] for row in rows] == ["user-alice-uuid"]


def test_like_pattern_is_anchored(sb):
    assert sb.table("posts").select("title").like("title", "World%").execute().data == []
    assert len(sb.table("posts").select("title").like("title", "Hello%").execute().data) == 1


def test_in_and_order_and_range(sb):
    rows = (sb.table("posts").select("id,created_at")
            .in_("id", ["post-1", "post-2", "post-3"])
            .order("created_at", desc=True).range(0, 1).execute().data)
    assert [row["id"] for row in rows] == ["post-3", "post-2"]


def test_count_exact_respects_filters_and_works_with_head(sb):
    response = (sb.table("posts").select("*", count="exact")
                .eq("author_id", "user-alice-uuid").execute())
    assert response.count == 2
    assert sb.table("posts").select("*", count="exact", head=True).execute().count == 3


def test_single_and_maybe_single(sb):
    row = sb.table("posts").select("*").eq("id", "post-1").single().execute().data
    assert isinstance(row, dict) and row["title"] == "Hello World"
    with pytest.raises(APIError) as caught:
        sb.table("posts").select("*").eq("id", "nope").single().execute()
    assert caught.value.code == "PGRST116"
    missing = sb.table("posts").select("*").eq("id", "nope").maybe_single().execute()
    assert missing is None or missing.data is None


def test_embedded_select_follows_the_foreign_key(sb):
    row = (sb.table("posts").select("id,title,profiles(username)")
           .eq("id", "post-1").single().execute().data)
    assert row["profiles"] == {"username": "alice"}


def test_text_search_matches_lexemes(sb):
    rows = sb.table("posts").select("id").text_search("body", "firebase").execute().data
    assert [row["id"] for row in rows] == ["post-2"]


# --- write -------------------------------------------------------------------

def test_insert_returns_the_row_with_generated_defaults(sb, live_twin):
    row = sb.table("posts").insert({
        "title": "Launch notes", "body": "v2", "author_id": "user-bob-uuid", "published": False,
    }).execute().data[0]
    assert row["id"] and row["created_at"]
    assert any(r["id"] == row["id"] for r in live_twin.state()["tables"]["posts"]["rows"])


def test_bulk_insert(sb):
    rows = sb.table("comments").insert(
        [{"post_id": "post-1", "body": "a"}, {"post_id": "post-1", "body": "b"}],
    ).execute().data
    assert len(rows) == 2


def test_insert_rejects_duplicate_keys_and_unknown_columns(sb):
    with pytest.raises(APIError) as duplicate:
        sb.table("posts").insert({"id": "post-1", "title": "dup"}).execute()
    assert duplicate.value.code == "23505"
    with pytest.raises(APIError) as unknown:
        sb.table("posts").insert({"title": "x", "nonexistent": 1}).execute()
    assert unknown.value.code == "PGRST204"


def test_upsert_on_the_primary_key_updates_in_place(sb, live_twin):
    before = len(live_twin.state()["tables"]["profiles"]["rows"])
    sb.table("profiles").upsert({"id": "user-alice-uuid", "username": "alice2"}).execute()
    rows = live_twin.state()["tables"]["profiles"]["rows"]
    assert len(rows) == before
    alice = next(row for row in rows if row["id"] == "user-alice-uuid")
    assert alice["username"] == "alice2" and alice["full_name"] == "Alice Chen"


def test_update_matching_rows_only(sb, live_twin):
    updated = sb.table("posts").update({"published": True}).eq("id", "post-3").execute().data
    assert len(updated) == 1
    published = [row["published"] for row in live_twin.state()["tables"]["posts"]["rows"]]
    assert published.count(True) == 3


def test_delete_with_or_only_removes_the_named_rows(sb, live_twin):
    deleted = sb.table("posts").delete().or_("id.eq.post-2,id.eq.post-3").execute().data
    assert len(deleted) == 2
    assert [row["id"] for row in live_twin.state()["tables"]["posts"]["rows"]] == ["post-1"]


def test_query_against_a_missing_table_is_a_404(sb):
    with pytest.raises(APIError) as caught:
        sb.table("no_such_table").select("*").execute()
    assert caught.value.code == "PGRST205"


def test_unparseable_filter_is_rejected_instead_of_matching_everything(sb, live_twin):
    with pytest.raises(APIError) as caught:
        sb.table("posts").delete().filter("id", "bogus", "post-1").execute()
    assert caught.value.code == "PGRST100"
    assert len(live_twin.state()["tables"]["posts"]["rows"]) == 3


def test_rpc_returns_the_seeded_result(sb, live_twin):
    httpx.post(f"{live_twin.url}/_seed-file", json={"state": {"rpc_stubs": {
        "count_posts": {"returns": 3}}}}, timeout=10).raise_for_status()
    assert sb.rpc("count_posts", {}).execute().data == 3


# --- auth --------------------------------------------------------------------

def test_admin_lists_seeded_users(sb):
    assert {user.email for user in sb.auth.admin.list_users()} == {
        "alice@acme.test", "bob@acme.test"}


def test_admin_user_lifecycle(sb, live_twin):
    created = sb.auth.admin.create_user({
        "email": "new@acme.test", "password": "s3cret-pass", "email_confirm": True}).user
    assert created.email == "new@acme.test" and created.aud == "authenticated"
    assert sb.auth.admin.get_user_by_id(created.id).user.email == "new@acme.test"
    updated = sb.auth.admin.update_user_by_id(created.id, {"user_metadata": {"plan": "pro"}}).user
    assert updated.user_metadata == {"plan": "pro"}
    sb.auth.admin.delete_user(created.id)
    assert created.id not in live_twin.state()["auth_users"]


def test_admin_create_user_rejects_a_duplicate_email(sb):
    with pytest.raises(AuthApiError) as caught:
        sb.auth.admin.create_user({"email": "alice@acme.test", "password": "s3cret-pass"})
    assert caught.value.code == "email_exists"


def test_sign_up_then_sign_in_and_read_back_the_user(sb):
    assert sb.auth.sign_up({"email": "su@acme.test", "password": "s3cret-pass"}).session
    sb.auth.sign_out()
    session = sb.auth.sign_in_with_password(
        {"email": "su@acme.test", "password": "s3cret-pass"}).session
    assert session.access_token and session.token_type == "bearer"
    assert sb.auth.get_user(session.access_token).user.email == "su@acme.test"


def test_sign_in_with_the_wrong_password_is_rejected(sb):
    sb.auth.sign_up({"email": "pw@acme.test", "password": "s3cret-pass"})
    sb.auth.sign_out()
    with pytest.raises(AuthApiError) as caught:
        sb.auth.sign_in_with_password({"email": "pw@acme.test", "password": "wrong-pass"})
    assert caught.value.code == "invalid_credentials"


# --- storage -----------------------------------------------------------------

def test_bucket_lifecycle(sb):
    assert [bucket.name for bucket in sb.storage.list_buckets()] == ["avatars"]
    sb.storage.create_bucket("reports", options={"public": False})
    assert sb.storage.get_bucket("reports").public is False
    sb.storage.delete_bucket("reports")
    assert [bucket.name for bucket in sb.storage.list_buckets()] == ["avatars"]


def test_upload_download_list_update_move_and_remove(sb, live_twin):
    sb.storage.from_("avatars").upload("u/alice.txt", b"hello world",
                                       {"content-type": "text/plain"})
    assert sb.storage.from_("avatars").download("u/alice.txt") == b"hello world"
    assert [obj["name"] for obj in sb.storage.from_("avatars").list("u")] == ["alice.txt"]
    sb.storage.from_("avatars").update("u/alice.txt", b"v2", {"content-type": "text/plain"})
    assert sb.storage.from_("avatars").download("u/alice.txt") == b"v2"
    sb.storage.from_("avatars").move("u/alice.txt", "u/alice2.txt")
    sb.storage.from_("avatars").remove(["u/alice2.txt"])
    assert live_twin.state()["storage"]["objects"] == {}


def test_signed_url_downloads_without_credentials(sb):
    sb.storage.from_("avatars").upload("u/report.txt", b"private",
                                       {"content-type": "text/plain"})
    signed = sb.storage.from_("avatars").create_signed_url("u/report.txt", 60)
    assert httpx.get(signed["signedURL"], timeout=10).content == b"private"


def test_storage_errors_are_typed(sb):
    sb.storage.from_("avatars").upload("u/a.txt", b"one", {"content-type": "text/plain"})
    with pytest.raises(StorageApiError) as conflict:
        sb.storage.from_("avatars").upload("u/a.txt", b"two", {"content-type": "text/plain"})
    assert conflict.value.status == "409"
    with pytest.raises(StorageApiError) as missing:
        sb.storage.from_("avatars").download("u/missing.txt")
    assert "not found" in missing.value.message.lower()


# --- faults and views --------------------------------------------------------

def test_rate_limit_surfaces_as_an_api_error(sb, live_twin):
    live_twin.configure(rate_limit=0)
    try:
        with pytest.raises(APIError) as caught:
            sb.table("posts").select("*").execute()
    finally:
        live_twin.configure(rate_limit=None)
    assert str(caught.value.code) == "429"


def test_read_only_blocks_writes_with_a_postgres_permission_error(sb, live_twin):
    live_twin.configure(read_only=True)
    try:
        with pytest.raises(APIError) as caught:
            sb.table("posts").insert({"title": "nope"}).execute()
    finally:
        live_twin.configure(read_only=False)
    assert caught.value.code == "42501"


def test_views_and_trace_record_what_the_agent_did(sb, live_twin):
    sb.table("posts").delete().eq("id", "post-3").execute()
    sb.storage.from_("avatars").upload("u/a.txt", b"x", {"content-type": "text/plain"})
    collections = live_twin.views()
    assert [item["_key"] for item in collections["posts"]["items"]] == ["post-1", "post-2"]
    assert collections["storage_objects"]["items"][0]["bucket"] == "avatars"
    assert collections["auth_users"]["tombstone"] == "deleted_at"
    ops = [(entry["op"], entry["resource"]) for entry in live_twin.trace()]
    assert ("delete", "posts") in ops
    assert ("create", "storage.objects") in ops
