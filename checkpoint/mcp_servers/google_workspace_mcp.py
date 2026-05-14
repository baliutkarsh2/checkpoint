"""Google Workspace MCP server — wraps `checkpoint.twins.google_workspace`.

Tools cover Gmail (threads, messages, labels, drafts, send) and Google Drive
(files, folders, permissions, copy, search). Each tool is a thin REST shim
sharing STATE with the twin.

Mounted onto the twin's FastAPI app at `/mcp` by `mount_on(app)`.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from ._shim import make_shim, mount_mcp_on_fastapi


GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN = "ya29.checkpoint_google_workspace_token_aabbccddeeff"


def build_mcp(app: FastAPI) -> FastMCP:
    """Build (but don't mount) the FastMCP instance for the Google Workspace twin."""
    token = os.environ.get("GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN", GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN)
    shim = make_shim(app, token, auth_scheme="Bearer")

    mcp = FastMCP(
        name="checkpoint-google-workspace",
        instructions="Stateful synthetic Google Workspace (Gmail + Drive). Tool names mirror the official Google Workspace MCP server.",
        stateless_http=True,
        streamable_http_path="/",
    )

    # ===== Gmail =============================================================

    # ----- Profile -----------------------------------------------------------

    @mcp.tool()
    async def gmail_get_profile() -> Any:
        """Get the authenticated user's Gmail profile."""
        return await shim("GET", "/gmail/v1/users/me/profile")

    # ----- Labels ------------------------------------------------------------

    @mcp.tool()
    async def gmail_list_labels() -> Any:
        """List all Gmail labels (system + user-created)."""
        return await shim("GET", "/gmail/v1/users/me/labels")

    @mcp.tool()
    async def gmail_get_label(label_id: str) -> Any:
        """Get a Gmail label by ID."""
        return await shim("GET", f"/gmail/v1/users/me/labels/{label_id}")

    @mcp.tool()
    async def gmail_create_label(
        name: str,
        message_list_visibility: str = "show",
        label_list_visibility: str = "labelShow",
        color: dict | None = None,
    ) -> Any:
        """Create a new Gmail label.

        color: optional dict with 'textColor' and 'backgroundColor' hex strings.
        """
        body: dict[str, Any] = {
            "name": name,
            "messageListVisibility": message_list_visibility,
            "labelListVisibility": label_list_visibility,
        }
        if color is not None:
            body["color"] = color
        return await shim("POST", "/gmail/v1/users/me/labels", json=body)

    @mcp.tool()
    async def gmail_update_label(
        label_id: str,
        name: str | None = None,
        message_list_visibility: str | None = None,
        label_list_visibility: str | None = None,
        color: dict | None = None,
    ) -> Any:
        """Update a Gmail label."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if message_list_visibility is not None:
            body["messageListVisibility"] = message_list_visibility
        if label_list_visibility is not None:
            body["labelListVisibility"] = label_list_visibility
        if color is not None:
            body["color"] = color
        return await shim("PATCH", f"/gmail/v1/users/me/labels/{label_id}", json=body)

    @mcp.tool()
    async def gmail_delete_label(label_id: str) -> Any:
        """Delete a Gmail label."""
        return await shim("DELETE", f"/gmail/v1/users/me/labels/{label_id}")

    # ----- Threads -----------------------------------------------------------

    @mcp.tool()
    async def gmail_list_threads(
        max_results: int = 100,
        label_ids: list[str] | None = None,
        q: str | None = None,
        page_token: str | None = None,
    ) -> Any:
        """List email threads.

        q: Gmail search query (e.g. 'from:alice subject:meeting').
        label_ids: filter by label IDs (e.g. ['INBOX', 'UNREAD']).
        """
        params: dict[str, Any] = {"maxResults": max_results}
        if label_ids:
            params["labelIds"] = ",".join(label_ids)
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        return await shim("GET", "/gmail/v1/users/me/threads", params=params)

    @mcp.tool()
    async def gmail_get_thread(thread_id: str) -> Any:
        """Get a full email thread including all messages."""
        return await shim("GET", f"/gmail/v1/users/me/threads/{thread_id}")

    @mcp.tool()
    async def gmail_modify_thread(
        thread_id: str,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> Any:
        """Add or remove labels from all messages in a thread."""
        body: dict[str, Any] = {
            "addLabelIds": add_label_ids or [],
            "removeLabelIds": remove_label_ids or [],
        }
        return await shim("POST", f"/gmail/v1/users/me/threads/{thread_id}/modify", json=body)

    @mcp.tool()
    async def gmail_trash_thread(thread_id: str) -> Any:
        """Move a thread to the Trash."""
        return await shim("POST", f"/gmail/v1/users/me/threads/{thread_id}/trash")

    @mcp.tool()
    async def gmail_delete_thread(thread_id: str) -> Any:
        """Permanently delete a thread."""
        return await shim("DELETE", f"/gmail/v1/users/me/threads/{thread_id}")

    # ----- Messages ----------------------------------------------------------

    @mcp.tool()
    async def gmail_list_messages(
        max_results: int = 100,
        label_ids: list[str] | None = None,
        q: str | None = None,
    ) -> Any:
        """List email messages.

        q: Gmail search query (e.g. 'from:bob is:unread').
        """
        params: dict[str, Any] = {"maxResults": max_results}
        if label_ids:
            params["labelIds"] = ",".join(label_ids)
        if q:
            params["q"] = q
        return await shim("GET", "/gmail/v1/users/me/messages", params=params)

    @mcp.tool()
    async def gmail_get_message(message_id: str) -> Any:
        """Get a single email message by ID."""
        return await shim("GET", f"/gmail/v1/users/me/messages/{message_id}")

    @mcp.tool()
    async def gmail_send_message(
        to: str,
        subject: str,
        body: str,
        from_address: str | None = None,
        thread_id: str | None = None,
        cc: str | None = None,
    ) -> Any:
        """Send an email message.

        to: recipient email address.
        subject: email subject line.
        body: plain-text message body.
        thread_id: if replying, include the thread ID.
        """
        payload: dict[str, Any] = {
            "to": to,
            "subject": subject,
            "body": body,
            "headers": [
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ],
        }
        if from_address:
            payload["from"] = from_address
            payload["headers"].append({"name": "From", "value": from_address})
        if cc:
            payload["headers"].append({"name": "Cc", "value": cc})
        if thread_id:
            payload["threadId"] = thread_id
        return await shim("POST", "/gmail/v1/users/me/messages/send", json=payload)

    @mcp.tool()
    async def gmail_modify_message(
        message_id: str,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> Any:
        """Add or remove labels from a message (e.g. mark as read/unread, star)."""
        body: dict[str, Any] = {
            "addLabelIds": add_label_ids or [],
            "removeLabelIds": remove_label_ids or [],
        }
        return await shim("POST", f"/gmail/v1/users/me/messages/{message_id}/modify", json=body)

    @mcp.tool()
    async def gmail_trash_message(message_id: str) -> Any:
        """Move a message to Trash."""
        return await shim("POST", f"/gmail/v1/users/me/messages/{message_id}/trash")

    @mcp.tool()
    async def gmail_delete_message(message_id: str) -> Any:
        """Permanently delete a message."""
        return await shim("DELETE", f"/gmail/v1/users/me/messages/{message_id}")

    # ----- Drafts ------------------------------------------------------------

    @mcp.tool()
    async def gmail_list_drafts(max_results: int = 100) -> Any:
        """List email drafts."""
        return await shim("GET", "/gmail/v1/users/me/drafts", params={"maxResults": max_results})

    @mcp.tool()
    async def gmail_get_draft(draft_id: str) -> Any:
        """Get a draft by ID."""
        return await shim("GET", f"/gmail/v1/users/me/drafts/{draft_id}")

    @mcp.tool()
    async def gmail_create_draft(
        to: str,
        subject: str,
        body: str,
        from_address: str | None = None,
        cc: str | None = None,
    ) -> Any:
        """Create an email draft."""
        headers = [
            {"name": "To", "value": to},
            {"name": "Subject", "value": subject},
        ]
        if from_address:
            headers.append({"name": "From", "value": from_address})
        if cc:
            headers.append({"name": "Cc", "value": cc})
        payload = {
            "message": {
                "headers": headers,
                "to": to,
                "subject": subject,
                "body": body,
                "text": body,
            }
        }
        return await shim("POST", "/gmail/v1/users/me/drafts", json=payload)

    @mcp.tool()
    async def gmail_update_draft(
        draft_id: str,
        to: str | None = None,
        subject: str | None = None,
        body: str | None = None,
    ) -> Any:
        """Update an existing draft."""
        payload: dict[str, Any] = {"message": {}}
        headers = []
        if to:
            headers.append({"name": "To", "value": to})
        if subject:
            headers.append({"name": "Subject", "value": subject})
        if headers:
            payload["message"]["headers"] = headers
        if body:
            payload["message"]["body"] = body
            payload["message"]["text"] = body
        return await shim("PATCH", f"/gmail/v1/users/me/drafts/{draft_id}", json=payload)

    @mcp.tool()
    async def gmail_send_draft(draft_id: str) -> Any:
        """Send a draft."""
        return await shim("POST", f"/gmail/v1/users/me/drafts/{draft_id}/send", json={})

    @mcp.tool()
    async def gmail_delete_draft(draft_id: str) -> Any:
        """Delete a draft."""
        return await shim("DELETE", f"/gmail/v1/users/me/drafts/{draft_id}")

    # ===== Drive =============================================================

    # ----- Files & Folders ---------------------------------------------------

    @mcp.tool()
    async def drive_list_files(
        page_size: int = 100,
        q: str | None = None,
        fields: str = "id,name,mimeType,parents,modifiedTime,shared,starred",
    ) -> Any:
        """List files/folders in Drive.

        q: Drive search query (e.g. "name contains 'report'" or "mimeType='application/vnd.google-apps.folder'").
        """
        params: dict[str, Any] = {"pageSize": page_size, "fields": fields}
        if q:
            params["q"] = q
        return await shim("GET", "/drive/v3/files", params=params)

    @mcp.tool()
    async def drive_get_file(file_id: str, fields: str = "*") -> Any:
        """Get file or folder metadata by ID."""
        return await shim("GET", f"/drive/v3/files/{file_id}", params={"fields": fields})

    @mcp.tool()
    async def drive_create_file(
        name: str,
        mime_type: str = "application/vnd.google-apps.document",
        parent_id: str | None = None,
        description: str | None = None,
    ) -> Any:
        """Create a file or document in Drive.

        Common MIME types:
          application/vnd.google-apps.document    — Google Doc
          application/vnd.google-apps.spreadsheet — Google Sheet
          application/vnd.google-apps.presentation — Google Slides
          application/vnd.google-apps.folder      — Folder
          text/plain                               — Plain text file
        """
        body: dict[str, Any] = {"name": name, "mimeType": mime_type}
        if parent_id:
            body["parents"] = [parent_id]
        if description:
            body["description"] = description
        return await shim("POST", "/drive/v3/files", json=body)

    @mcp.tool()
    async def drive_create_folder(
        name: str,
        parent_id: str | None = None,
        description: str | None = None,
    ) -> Any:
        """Create a folder in Drive."""
        body: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            body["parents"] = [parent_id]
        if description:
            body["description"] = description
        return await shim("POST", "/drive/v3/files", json=body)

    @mcp.tool()
    async def drive_update_file(
        file_id: str,
        name: str | None = None,
        description: str | None = None,
        starred: bool | None = None,
        trashed: bool | None = None,
        add_parents: str | None = None,
        remove_parents: str | None = None,
    ) -> Any:
        """Update file metadata (rename, move, star, trash)."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if starred is not None:
            body["starred"] = starred
        if trashed is not None:
            body["trashed"] = trashed
        params: dict[str, Any] = {}
        if add_parents:
            params["addParents"] = add_parents
        if remove_parents:
            params["removeParents"] = remove_parents
        return await shim("PATCH", f"/drive/v3/files/{file_id}", json=body, params=params or None)

    @mcp.tool()
    async def drive_delete_file(file_id: str) -> Any:
        """Permanently delete a file or folder."""
        return await shim("DELETE", f"/drive/v3/files/{file_id}")

    @mcp.tool()
    async def drive_copy_file(
        file_id: str,
        name: str | None = None,
        parent_id: str | None = None,
    ) -> Any:
        """Copy a file to a new location."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if parent_id:
            body["parents"] = [parent_id]
        return await shim("POST", f"/drive/v3/files/{file_id}/copy", json=body)

    @mcp.tool()
    async def drive_search_files(
        query: str,
        page_size: int = 50,
    ) -> Any:
        """Search Drive files by name, content, or type.

        query: Drive search query string.
        Examples:
          "name contains 'report'"
          "mimeType='application/vnd.google-apps.spreadsheet'"
          "trashed=false and starred=true"
        """
        return await shim("GET", "/drive/v3/files", params={"q": query, "pageSize": page_size})

    # ----- Permissions -------------------------------------------------------

    @mcp.tool()
    async def drive_list_permissions(file_id: str) -> Any:
        """List permissions for a file or folder."""
        return await shim("GET", f"/drive/v3/files/{file_id}/permissions")

    @mcp.tool()
    async def drive_add_permission(
        file_id: str,
        role: str,
        permission_type: str,
        email_address: str | None = None,
        domain: str | None = None,
        allow_file_discovery: bool = False,
        send_notification_email: bool = False,
    ) -> Any:
        """Share a file by adding a permission.

        role: 'owner', 'organizer', 'fileOrganizer', 'writer', 'commenter', 'reader'.
        permission_type: 'user', 'group', 'domain', 'anyone'.
        """
        body: dict[str, Any] = {
            "role": role,
            "type": permission_type,
            "allowFileDiscovery": allow_file_discovery,
        }
        if email_address:
            body["emailAddress"] = email_address
        if domain:
            body["domain"] = domain
        return await shim(
            "POST",
            f"/drive/v3/files/{file_id}/permissions",
            json=body,
            params={"sendNotificationEmail": str(send_notification_email).lower()},
        )

    @mcp.tool()
    async def drive_update_permission(
        file_id: str,
        permission_id: str,
        role: str,
    ) -> Any:
        """Update a permission's role."""
        return await shim(
            "PATCH",
            f"/drive/v3/files/{file_id}/permissions/{permission_id}",
            json={"role": role},
        )

    @mcp.tool()
    async def drive_remove_permission(file_id: str, permission_id: str) -> Any:
        """Remove a permission from a file."""
        return await shim("DELETE", f"/drive/v3/files/{file_id}/permissions/{permission_id}")

    return mcp


def mount_on(app: FastAPI) -> FastMCP:
    """Build the Google Workspace FastMCP server and mount it at `/mcp` on `app`."""
    mcp = build_mcp(app)
    mount_mcp_on_fastapi(app, mcp, "/mcp")
    return mcp
