"""Supabase MCP server — wraps `checkpoint.twins.supabase` REST surface.

Tool names mirror the Supabase MCP server's tool list: table CRUD via
PostgREST, auth user management, and storage bucket/object operations.
Each tool is a thin REST shim sharing STATE with the twin.

Mounted onto the twin's FastAPI app at `/mcp` by `mount_on(app)`.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from ._shim import make_shim, mount_mcp_on_fastapi


SUPABASE_BOOTSTRAP_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.checkpoint_anon_key_aabbccddeeff"


def build_mcp(app: FastAPI) -> FastMCP:
    """Build (but don't mount) the FastMCP instance for the Supabase twin."""
    token = os.environ.get("SUPABASE_BOOTSTRAP_TOKEN", SUPABASE_BOOTSTRAP_TOKEN)
    shim = make_shim(app, token, auth_scheme="Bearer")

    mcp = FastMCP(
        name="checkpoint-supabase",
        instructions="Stateful synthetic Supabase. Tool names mirror the official Supabase MCP server.",
        stateless_http=True,
        streamable_http_path="/",
    )

    # ----- PostgREST Table Operations -----------------------------------

    @mcp.tool()
    async def supabase_list_tables() -> Any:
        """List all tables in the database."""
        return await shim("GET", "/rest/v1/")

    @mcp.tool()
    async def supabase_query(
        table: str,
        select: str = "*",
        filters: dict[str, str] | None = None,
        order: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        """Query rows from a table with optional filters and pagination.

        filters: dict of {column.operator: value} e.g. {"age.gte": "18", "status.eq": "active"}
        order: column name, optionally suffixed with .asc or .desc
        """
        params: dict[str, Any] = {"select": select}
        if filters:
            params.update(filters)
        if order is not None:
            params["order"] = order
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return await shim("GET", f"/rest/v1/{table}", params=params)

    @mcp.tool()
    async def supabase_insert(
        table: str,
        data: dict[str, Any] | list[dict[str, Any]],
        upsert: bool = False,
    ) -> Any:
        """Insert one or more rows into a table.

        data: a single row dict or a list of row dicts.
        upsert: if True, performs an upsert (insert or update on conflict).
        """
        headers: dict[str, str] = {}
        if upsert:
            headers["Prefer"] = "resolution=merge-duplicates"
        rows = data if isinstance(data, list) else [data]
        return await shim("POST", f"/rest/v1/{table}", json=rows, headers=headers)

    @mcp.tool()
    async def supabase_update(
        table: str,
        data: dict[str, Any],
        filters: dict[str, str],
    ) -> Any:
        """Update rows in a table that match the given filters.

        filters: dict of {column.operator: value} e.g. {"id.eq": "abc-123"}
        """
        return await shim("PATCH", f"/rest/v1/{table}", json=data, params=filters)

    @mcp.tool()
    async def supabase_delete(
        table: str,
        filters: dict[str, str],
    ) -> Any:
        """Delete rows from a table that match the given filters.

        filters: dict of {column.operator: value} e.g. {"id.eq": "abc-123"}
        """
        return await shim("DELETE", f"/rest/v1/{table}", params=filters)

    @mcp.tool()
    async def supabase_upsert(
        table: str,
        data: dict[str, Any] | list[dict[str, Any]],
        on_conflict: str | None = None,
    ) -> Any:
        """Upsert one or more rows (insert or update on conflict).

        on_conflict: comma-separated column names that determine uniqueness.
        """
        params: dict[str, Any] = {}
        if on_conflict is not None:
            params["on_conflict"] = on_conflict
        rows = data if isinstance(data, list) else [data]
        return await shim(
            "POST",
            f"/rest/v1/{table}",
            json=rows,
            params=params,
            headers={"Prefer": "resolution=merge-duplicates"},
        )

    # ----- RPC (Stored Functions) ----------------------------------------

    @mcp.tool()
    async def supabase_rpc(
        function_name: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Call a Postgres stored function via RPC.

        params: key-value arguments passed to the function.
        """
        body = params or {}
        return await shim("POST", f"/rest/v1/rpc/{function_name}", json=body)

    # ----- Auth User Management ------------------------------------------

    @mcp.tool()
    async def supabase_list_auth_users(
        page: int | None = None,
        per_page: int | None = None,
    ) -> Any:
        """List all auth users."""
        qp: dict[str, Any] = {}
        if page is not None:
            qp["page"] = page
        if per_page is not None:
            qp["per_page"] = per_page
        return await shim("GET", "/auth/v1/admin/users", params=qp)

    @mcp.tool()
    async def supabase_get_auth_user(user_id: str) -> Any:
        """Get a single auth user by id."""
        return await shim("GET", f"/auth/v1/admin/users/{user_id}")

    @mcp.tool()
    async def supabase_create_auth_user(
        email: str,
        password: str | None = None,
        role: str = "authenticated",
        user_metadata: dict[str, Any] | None = None,
        app_metadata: dict[str, Any] | None = None,
        email_confirm: bool = True,
    ) -> Any:
        """Create a new auth user."""
        body: dict[str, Any] = {
            "email": email,
            "role": role,
            "email_confirm": email_confirm,
        }
        if password is not None:
            body["password"] = password
        if user_metadata is not None:
            body["user_metadata"] = user_metadata
        if app_metadata is not None:
            body["app_metadata"] = app_metadata
        return await shim("POST", "/auth/v1/admin/users", json=body)

    @mcp.tool()
    async def supabase_update_auth_user(
        user_id: str,
        email: str | None = None,
        password: str | None = None,
        role: str | None = None,
        user_metadata: dict[str, Any] | None = None,
        app_metadata: dict[str, Any] | None = None,
        ban_duration: str | None = None,
    ) -> Any:
        """Update an existing auth user."""
        body: dict[str, Any] = {}
        if email is not None:
            body["email"] = email
        if password is not None:
            body["password"] = password
        if role is not None:
            body["role"] = role
        if user_metadata is not None:
            body["user_metadata"] = user_metadata
        if app_metadata is not None:
            body["app_metadata"] = app_metadata
        if ban_duration is not None:
            body["ban_duration"] = ban_duration
        return await shim("PATCH", f"/auth/v1/admin/users/{user_id}", json=body)

    @mcp.tool()
    async def supabase_delete_auth_user(user_id: str) -> Any:
        """Delete an auth user by id."""
        return await shim("DELETE", f"/auth/v1/admin/users/{user_id}")

    # ----- Storage: Buckets ---------------------------------------------

    @mcp.tool()
    async def supabase_list_buckets() -> Any:
        """List all storage buckets."""
        return await shim("GET", "/storage/v1/bucket")

    @mcp.tool()
    async def supabase_get_bucket(bucket_id: str) -> Any:
        """Get details of a specific storage bucket."""
        return await shim("GET", f"/storage/v1/bucket/{bucket_id}")

    @mcp.tool()
    async def supabase_create_bucket(
        name: str,
        public: bool = False,
        file_size_limit: int | None = None,
        allowed_mime_types: list[str] | None = None,
    ) -> Any:
        """Create a new storage bucket."""
        body: dict[str, Any] = {"name": name, "public": public}
        if file_size_limit is not None:
            body["file_size_limit"] = file_size_limit
        if allowed_mime_types is not None:
            body["allowed_mime_types"] = allowed_mime_types
        return await shim("POST", "/storage/v1/bucket", json=body)

    @mcp.tool()
    async def supabase_update_bucket(
        bucket_id: str,
        public: bool | None = None,
        file_size_limit: int | None = None,
        allowed_mime_types: list[str] | None = None,
    ) -> Any:
        """Update a storage bucket's settings."""
        body: dict[str, Any] = {}
        if public is not None:
            body["public"] = public
        if file_size_limit is not None:
            body["file_size_limit"] = file_size_limit
        if allowed_mime_types is not None:
            body["allowed_mime_types"] = allowed_mime_types
        return await shim("PUT", f"/storage/v1/bucket/{bucket_id}", json=body)

    @mcp.tool()
    async def supabase_delete_bucket(bucket_id: str) -> Any:
        """Delete a storage bucket (must be empty)."""
        return await shim("DELETE", f"/storage/v1/bucket/{bucket_id}")

    @mcp.tool()
    async def supabase_empty_bucket(bucket_id: str) -> Any:
        """Remove all objects from a bucket without deleting the bucket."""
        return await shim("POST", f"/storage/v1/bucket/{bucket_id}/empty")

    # ----- Storage: Objects ---------------------------------------------

    @mcp.tool()
    async def supabase_list_objects(
        bucket_id: str,
        prefix: str = "",
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
    ) -> Any:
        """List objects in a storage bucket."""
        body: dict[str, Any] = {"prefix": prefix}
        if limit is not None:
            body["limit"] = limit
        if offset is not None:
            body["offset"] = offset
        if sort_by is not None:
            body["sort_by"] = {"column": sort_by}
        return await shim(
            "POST", f"/storage/v1/object/list/{bucket_id}", json=body
        )

    @mcp.tool()
    async def supabase_get_object_info(bucket_id: str, path: str) -> Any:
        """Get metadata for a storage object (does not download content)."""
        return await shim("GET", f"/storage/v1/object/info/{bucket_id}/{path}")

    @mcp.tool()
    async def supabase_upload_object(
        bucket_id: str,
        path: str,
        content: str,
        content_type: str = "text/plain",
        upsert: bool = False,
    ) -> Any:
        """Upload a text object to storage.

        content: text content to store.
        upsert: if True, overwrite if the object already exists.
        """
        return await shim(
            "POST",
            f"/storage/v1/object/{bucket_id}/{path}",
            json={"_mcp_content": content, "_mcp_content_type": content_type, "_mcp_upsert": upsert},
        )

    @mcp.tool()
    async def supabase_move_object(
        bucket_id: str,
        source_path: str,
        destination_path: str,
    ) -> Any:
        """Move/rename an object within a bucket."""
        body = {
            "bucketId": bucket_id,
            "sourceKey": source_path,
            "destinationKey": destination_path,
        }
        return await shim("POST", "/storage/v1/object/move", json=body)

    @mcp.tool()
    async def supabase_copy_object(
        bucket_id: str,
        source_path: str,
        destination_path: str,
        destination_bucket: str | None = None,
    ) -> Any:
        """Copy an object to a new path (optionally in a different bucket)."""
        body: dict[str, Any] = {
            "bucketId": bucket_id,
            "sourceKey": source_path,
            "destinationKey": destination_path,
        }
        if destination_bucket is not None:
            body["destinationBucket"] = destination_bucket
        return await shim("POST", "/storage/v1/object/copy", json=body)

    @mcp.tool()
    async def supabase_delete_object(bucket_id: str, paths: list[str]) -> Any:
        """Delete one or more objects from a bucket.

        paths: list of object paths within the bucket.
        """
        return await shim(
            "DELETE",
            f"/storage/v1/object/{bucket_id}",
            json={"prefixes": paths},
        )

    @mcp.tool()
    async def supabase_create_signed_url(
        bucket_id: str,
        path: str,
        expires_in: int = 3600,
        transform: dict[str, Any] | None = None,
    ) -> Any:
        """Create a signed URL for temporary access to a private object.

        expires_in: seconds until the URL expires (default 3600).
        """
        body: dict[str, Any] = {"expiresIn": expires_in}
        if transform is not None:
            body["transform"] = transform
        return await shim(
            "POST",
            f"/storage/v1/object/sign/{bucket_id}/{path}",
            json=body,
        )

    return mcp


def mount_on(app: FastAPI) -> FastMCP:
    """Build the Supabase FastMCP server and mount it at `/mcp` on `app`."""
    mcp = build_mcp(app)
    mount_mcp_on_fastapi(app, mcp, "/mcp")
    return mcp
