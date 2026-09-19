"""Google Workspace MCP server — wraps `checkpoint.twins.google_workspace`.

Tools cover Gmail (threads, messages, labels, drafts, send), Google Drive
(files, folders, permissions, copy, search) and Calendar (events). Each tool is
a thin REST shim sharing STATE with the twin, so a tool call hits exactly the
endpoint an SDK would: message tools compose RFC 822 and post it as `raw`.

Mounted onto the twin's FastAPI app at `/mcp` by `mount_on(app)`.
"""
from __future__ import annotations

import base64
import os
import re
from email.message import EmailMessage
from typing import Any

from fastapi import FastAPI

from checkpoint.fake_credentials import FAKE_GOOGLE_WORKSPACE_TOKEN
from checkpoint.mcp_compat import FastMCP, make_server

from ._shim import make_shim, mount_mcp_on_fastapi

GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN = FAKE_GOOGLE_WORKSPACE_TOKEN

# Drive query operators; a search term without one is a plain phrase, which the
# real API would reject, so wrap it the way a person means it.
_DRIVE_QUERY = re.compile(r"\b(contains|in|has)\b|[=<>!]")


def _raw_message(to: str, subject: str, body: str, *, cc: str | None = None,
                 from_address: str | None = None) -> str:
    """An RFC 822 message, base64url encoded the way `messages.send` wants it."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc
    if from_address:
        message["From"] = from_address
    message.set_content(body or "")
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def _header(message: dict, name: str) -> str:
    headers = ((message or {}).get("payload") or {}).get("headers") or []
    return next((h.get("value", "") for h in headers
                 if h.get("name", "").lower() == name.lower()), "")


def _plain_body(message: dict) -> str:
    """The plain-text body of a message returned by the twin."""
    payload = (message or {}).get("payload") or {}
    parts = [payload, *(payload.get("parts") or [])]
    for part in parts:
        data = (part.get("body") or {}).get("data")
        if data and part.get("mimeType", "text/plain") == "text/plain":
            padded = data + "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    return ""


def build_mcp(app: FastAPI) -> FastMCP:
    """Build (but don't mount) the FastMCP instance for the Google Workspace twin."""
    token = os.environ.get("GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN", GOOGLE_WORKSPACE_BOOTSTRAP_TOKEN)
    shim = make_shim(app, token, auth_scheme="Bearer")

    mcp = make_server(
        name="checkpoint-google-workspace",
        instructions="Stateful synthetic Google Workspace (Gmail + Drive). Tool names mirror the official Google Workspace MCP server.",
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
        page_token: str | None = None,
    ) -> Any:
        """List email messages.

        q: Gmail search query (e.g. 'from:bob is:unread').
        page_token: nextPageToken from a previous call.
        """
        params: dict[str, Any] = {"maxResults": max_results}
        if label_ids:
            params["labelIds"] = ",".join(label_ids)
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
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
            "raw": _raw_message(to, subject, body, cc=cc, from_address=from_address),
        }
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

    @mcp.tool()
    async def gmail_untrash_message(message_id: str) -> Any:
        """Restore a message from Trash."""
        return await shim("POST", f"/gmail/v1/users/me/messages/{message_id}/untrash")

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
        raw = _raw_message(to, subject, body, cc=cc, from_address=from_address)
        return await shim("POST", "/gmail/v1/users/me/drafts", json={"message": {"raw": raw}})

    @mcp.tool()
    async def gmail_update_draft(
        draft_id: str,
        to: str | None = None,
        subject: str | None = None,
        body: str | None = None,
    ) -> Any:
        """Update an existing draft (fields left out keep their current value)."""
        current = await shim("GET", f"/gmail/v1/users/me/drafts/{draft_id}")
        if current.get("_status"):
            return current
        message = current.get("message") or {}
        raw = _raw_message(
            to if to is not None else _header(message, "To"),
            subject if subject is not None else _header(message, "Subject"),
            body if body is not None else _plain_body(message),
            cc=_header(message, "Cc") or None,
        )
        # drafts.update replaces the draft's message, so it is a PUT, not a PATCH.
        return await shim("PUT", f"/gmail/v1/users/me/drafts/{draft_id}",
                          json={"id": draft_id, "message": {"raw": raw}})

    @mcp.tool()
    async def gmail_send_draft(draft_id: str) -> Any:
        """Send a draft."""
        return await shim("POST", "/gmail/v1/users/me/drafts/send", json={"id": draft_id})

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
        fields: str = ("nextPageToken,files(id,name,mimeType,parents,modifiedTime,"
                       "shared,starred,trashed)"),
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

        query: a Drive search query, or a plain term to look for in names and content.
        Examples:
          "name contains 'report'"
          "mimeType='application/vnd.google-apps.spreadsheet'"
          "trashed=false and starred=true"
        """
        if not _DRIVE_QUERY.search(query):
            term = query.replace("'", "\\'")
            query = f"name contains '{term}' or fullText contains '{term}'"
        return await shim("GET", "/drive/v3/files", params={"q": query, "pageSize": page_size})

    @mcp.tool()
    async def drive_download_file(file_id: str) -> Any:
        """Download a file's content (Docs editors files are exported as text)."""
        meta = await shim("GET", f"/drive/v3/files/{file_id}", params={"fields": "id,name,mimeType"})
        if meta.get("_status"):
            return meta
        if str(meta.get("mimeType", "")).startswith("application/vnd.google-apps."):
            export = "text/csv" if meta["mimeType"].endswith("spreadsheet") else "text/plain"
            content = await shim("GET", f"/drive/v3/files/{file_id}/export",
                                 params={"mimeType": export})
        else:
            content = await shim("GET", f"/drive/v3/files/{file_id}", params={"alt": "media"})
        return {**meta, "content": content.get("_raw", content) if isinstance(content, dict)
                else content}

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

    # ===== Calendar ==========================================================

    @mcp.tool()
    async def calendar_list_calendars() -> Any:
        """List the calendars on the account."""
        return await shim("GET", "/calendar/v3/users/me/calendarList")

    @mcp.tool()
    async def calendar_list_events(
        calendar_id: str = "primary",
        time_min: str | None = None,
        time_max: str | None = None,
        q: str | None = None,
        max_results: int = 250,
    ) -> Any:
        """List events on a calendar.

        time_min/time_max: RFC 3339 timestamps, e.g. '2026-01-31T00:00:00Z'.
        """
        params: dict[str, Any] = {"maxResults": max_results, "singleEvents": "true",
                                  "orderBy": "startTime"}
        for name, value in (("timeMin", time_min), ("timeMax", time_max), ("q", q)):
            if value:
                params[name] = value
        return await shim("GET", f"/calendar/v3/calendars/{calendar_id}/events", params=params)

    @mcp.tool()
    async def calendar_create_event(
        summary: str,
        start: str,
        end: str,
        calendar_id: str = "primary",
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> Any:
        """Create an event.

        start/end: RFC 3339 timestamps ('2026-01-31T10:00:00Z') or dates for all-day events.
        """
        def slot(value: str) -> dict:
            return {"date": value} if len(value) == 10 else {"dateTime": value}

        body: dict[str, Any] = {"summary": summary, "start": slot(start), "end": slot(end)}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": email} for email in attendees]
        return await shim("POST", f"/calendar/v3/calendars/{calendar_id}/events", json=body)

    @mcp.tool()
    async def calendar_delete_event(event_id: str, calendar_id: str = "primary") -> Any:
        """Delete (cancel) an event."""
        return await shim("DELETE", f"/calendar/v3/calendars/{calendar_id}/events/{event_id}")

    @mcp.tool()
    async def calendar_free_busy(
        time_min: str,
        time_max: str,
        calendar_ids: list[str] | None = None,
    ) -> Any:
        """Busy intervals for one or more calendars in a window."""
        return await shim("POST", "/calendar/v3/freeBusy", json={
            "timeMin": time_min, "timeMax": time_max,
            "items": [{"id": cid} for cid in (calendar_ids or ["primary"])],
        })

    return mcp


def mount_on(app: FastAPI) -> FastMCP:
    """Build the Google Workspace FastMCP server and mount it at `/mcp` on `app`."""
    mcp = build_mcp(app)
    mount_mcp_on_fastapi(app, mcp, "/mcp")
    return mcp
