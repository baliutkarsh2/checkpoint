"""Google Workspace twin REST surface — Gmail, Drive and Calendar."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import google_workspace as gw


@pytest.fixture(autouse=True)
def _reset_state():
    gw.TWIN.reset()
    yield


@pytest.fixture
def client():
    return TestClient(gw.app)


@pytest.fixture
def seeded(client):
    client.post("/_seed/small-team")
    return client


TOKEN = gw.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}


def raw(to="bob@acme.test", subject="Hello", body="Body text", **headers) -> str:
    lines = [f"To: {to}", f"Subject: {subject}"]
    lines += [f"{name.replace('_', '-')}: {value}" for name, value in headers.items()]
    return base64.urlsafe_b64encode(("\r\n".join(lines) + "\r\n\r\n" + body).encode()).decode()


def send(client, **kwargs) -> dict:
    r = client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw(**kwargs)})
    assert r.status_code == 200, r.text
    return r.json()


def b64decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode()


# --- auth -------------------------------------------------------------------

def test_missing_token_returns_401(client):
    r = client.get("/gmail/v1/users/me/profile")
    assert r.status_code == 401
    assert r.json()["error"]["status"] == "UNAUTHENTICATED"


def test_wrong_token_returns_401_under_strict_auth(client):
    client.post("/_config", json={"strict_auth": True})
    r = client.get("/gmail/v1/users/me/profile", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


def test_unknown_route_uses_the_google_error_shape(client):
    r = client.get("/gmail/v1/users/me/nope", headers=H)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == 404
    assert "detail" not in r.json()


# --- Gmail: profile and labels ----------------------------------------------

def test_gmail_get_profile(seeded):
    body = seeded.get("/gmail/v1/users/me/profile", headers=H).json()
    assert body["emailAddress"] == "alice@acme.test"
    assert body["messagesTotal"] == 4


def test_gmail_accepts_the_user_email_as_user_id(seeded):
    assert seeded.get("/gmail/v1/users/alice@acme.test/labels", headers=H).status_code == 200


def test_gmail_rejects_another_users_mailbox(seeded):
    r = seeded.get("/gmail/v1/users/eve@evil.test/labels", headers=H)
    assert r.status_code == 403
    assert "Delegation denied" in r.json()["error"]["message"]


def test_gmail_list_labels_has_system_labels(client):
    ids = {lab["id"] for lab in client.get("/gmail/v1/users/me/labels", headers=H).json()["labels"]}
    assert {"INBOX", "SENT", "TRASH", "SPAM", "DRAFT"} <= ids


def test_gmail_label_lifecycle(client):
    created = client.post("/gmail/v1/users/me/labels", headers=H, json={"name": "Work"}).json()
    assert created["type"] == "user" and created["id"].startswith("Label_")
    fetched = client.get(f"/gmail/v1/users/me/labels/{created['id']}", headers=H).json()
    assert fetched["messagesTotal"] == 0
    renamed = client.patch(f"/gmail/v1/users/me/labels/{created['id']}", headers=H,
                           json={"name": "Work 2"})
    assert renamed.json()["name"] == "Work 2"
    assert client.delete(f"/gmail/v1/users/me/labels/{created['id']}", headers=H).status_code == 204
    assert created["id"] not in gw.STATE["gmail_labels"]


def test_gmail_duplicate_label_conflicts(client):
    client.post("/gmail/v1/users/me/labels", headers=H, json={"name": "Work"})
    r = client.post("/gmail/v1/users/me/labels", headers=H, json={"name": "work"})
    assert r.status_code == 409


def test_gmail_cannot_delete_system_label(client):
    assert client.delete("/gmail/v1/users/me/labels/INBOX", headers=H).status_code == 400


def test_gmail_label_counts_track_messages(client):
    send(client)
    counts = client.get("/gmail/v1/users/me/labels/SENT", headers=H).json()
    assert counts["messagesTotal"] == 1


def test_deleting_a_label_removes_it_from_messages(client):
    label = client.post("/gmail/v1/users/me/labels", headers=H, json={"name": "Temp"}).json()
    message = send(client)
    client.post(f"/gmail/v1/users/me/messages/{message['id']}/modify", headers=H,
                json={"addLabelIds": [label["id"]]})
    client.delete(f"/gmail/v1/users/me/labels/{label['id']}", headers=H)
    assert label["id"] not in gw.STATE["gmail_messages"][message["id"]]["labelIds"]


# --- Gmail: messages ---------------------------------------------------------

def test_gmail_list_messages_empty_omits_the_array(client):
    body = client.get("/gmail/v1/users/me/messages", headers=H).json()
    assert body == {"resultSizeEstimate": 0}


def test_gmail_send_records_the_parsed_message(client):
    message = send(client, to="carol@acme.test", subject="Invoice #42", body="Please pay.")
    stored = gw.STATE["gmail_messages"][message["id"]]
    headers = {h["name"]: h["value"] for h in stored["payload"]["headers"]}
    assert headers["To"] == "carol@acme.test"
    assert headers["Subject"] == "Invoice #42"
    assert headers["From"] == "alice@acme.test" or headers["From"] == gw.DEFAULT_EMAIL
    assert message["labelIds"] == ["SENT"]


def test_gmail_send_requires_a_recipient(client):
    body = base64.urlsafe_b64encode(b"Subject: Nobody\r\n\r\nhi").decode()
    r = client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": body})
    assert r.status_code == 400
    assert "Recipient" in r.json()["error"]["message"]


def test_gmail_send_without_raw_is_rejected(client):
    r = client.post("/gmail/v1/users/me/messages/send", headers=H,
                    json={"to": "bob@acme.test", "subject": "hi"})
    assert r.status_code == 400


def test_gmail_send_threads_a_reply_by_thread_id(seeded):
    r = seeded.post("/gmail/v1/users/me/messages/send", headers=H, json={
        "threadId": "thread-001",
        "raw": raw(to="bob@acme.test", subject="Re: Q1 Planning Meeting", body="ack"),
    })
    assert r.json()["threadId"] == "thread-001"


def test_gmail_send_threads_a_reply_by_references(seeded):
    message = seeded.post("/gmail/v1/users/me/messages/send", headers=H, json={
        "raw": raw(to="bob@acme.test", subject="Re: Q1 Planning Meeting", body="ack",
                   In_Reply_To="<q1-planning-1@acme.test>"),
    }).json()
    assert message["threadId"] == "thread-001"


def test_gmail_message_formats(seeded):
    url = "/gmail/v1/users/me/messages/msg-001"
    full = seeded.get(f"{url}?format=full", headers=H).json()
    assert "Q1 planning" in b64decode(full["payload"]["body"]["data"])
    minimal = seeded.get(f"{url}?format=minimal", headers=H).json()
    assert "payload" not in minimal and minimal["snippet"]
    metadata = seeded.get(f"{url}?format=metadata&metadataHeaders=Subject", headers=H).json()
    assert [h["name"] for h in metadata["payload"]["headers"]] == ["Subject"]
    rfc822 = b64decode(seeded.get(f"{url}?format=raw", headers=H).json()["raw"])
    assert rfc822.startswith("From: Bob Smith")


def test_gmail_date_header_is_rfc_2822(seeded):
    from email.utils import parsedate_to_datetime

    message = seeded.get("/gmail/v1/users/me/messages/msg-001", headers=H).json()
    date = next(h["value"] for h in message["payload"]["headers"] if h["name"] == "Date")
    assert parsedate_to_datetime(date).year == 2025


def test_gmail_attachment_bytes_come_from_attachments_get(seeded):
    message = seeded.get("/gmail/v1/users/me/messages/msg-004", headers=H).json()
    part = next(p for p in message["payload"]["parts"] if p["filename"])
    assert "data" not in part["body"] and part["body"]["attachmentId"]
    attachment = seeded.get(
        f"/gmail/v1/users/me/messages/msg-004/attachments/{part['body']['attachmentId']}",
        headers=H).json()
    assert "Invoice NW-1042" in b64decode(attachment["data"])


def test_gmail_raw_round_trip_keeps_the_attachment(seeded):
    rfc822 = seeded.get("/gmail/v1/users/me/messages/msg-004?format=raw", headers=H).json()["raw"]
    assert "invoice-NW-1042.pdf" in b64decode(rfc822)
    resent = seeded.post("/gmail/v1/users/me/messages/send", headers=H,
                         json={"raw": rfc822}).json()
    parts = seeded.get(f"/gmail/v1/users/me/messages/{resent['id']}",
                       headers=H).json()["payload"]["parts"]
    assert [(p["mimeType"], p["filename"]) for p in parts] == [
        ("text/plain", ""), ("application/pdf", "invoice-NW-1042.pdf")]


def test_gmail_send_through_the_upload_endpoint(client):
    message = (b"To: bob@acme.test\r\nSubject: Uploaded\r\n\r\nsent as media\r\n")
    r = client.post("/upload/gmail/v1/users/me/messages/send?uploadType=media",
                    headers={**H, "Content-Type": "message/rfc822"}, content=message)
    assert r.status_code == 200 and r.json()["labelIds"] == ["SENT"]
    stored = gw.STATE["gmail_messages"][r.json()["id"]]
    assert gw._header(stored["payload"], "Subject") == "Uploaded"


@pytest.mark.parametrize(("query", "expected"), [
    ("is:unread", {"msg-001", "msg-004"}),
    ("is:read", {"msg-002", "msg-003"}),
    ("from:bob@acme.test", {"msg-001"}),
    ("to:alice@acme.test", {"msg-001", "msg-002", "msg-004"}),
    ("subject:(Q1)", {"msg-001", "msg-002"}),
    ("label:team", {"msg-003"}),
    ("in:sent", {"msg-003"}),
    ("has:attachment", {"msg-004"}),
    ("is:unread from:dana@northwind.test", {"msg-004"}),
    ("from:bob@acme.test OR from:carol@acme.test", {"msg-001", "msg-002"}),
    ("-is:unread subject:(Q1)", {"msg-002"}),
    ('"merging after CI"', {"msg-003"}),
    ("after:2025/01/22", {"msg-004"}),
    ("before:2025/01/21", {"msg-001", "msg-002", "msg-003"}),
    ("newer_than:100000d", {"msg-001", "msg-002", "msg-003", "msg-004"}),
    ("older_than:1d", {"msg-001", "msg-002", "msg-003", "msg-004"}),
    ("invoice", {"msg-004"}),
])
def test_gmail_search_operators(seeded, query, expected):
    body = seeded.get("/gmail/v1/users/me/messages", headers=H, params={"q": query}).json()
    assert {m["id"] for m in body.get("messages", [])} == expected


def test_gmail_repeated_label_ids_are_anded(seeded):
    body = seeded.get("/gmail/v1/users/me/messages?labelIds=INBOX&labelIds=IMPORTANT",
                      headers=H).json()
    assert {m["id"] for m in body["messages"]} == {"msg-001"}


def test_gmail_trash_is_hidden_until_asked_for(seeded):
    seeded.post("/gmail/v1/users/me/messages/msg-001/trash", headers=H)
    listed = seeded.get("/gmail/v1/users/me/messages", headers=H).json()
    assert "msg-001" not in {m["id"] for m in listed["messages"]}
    in_trash = seeded.get("/gmail/v1/users/me/messages?q=in:trash", headers=H).json()
    assert {m["id"] for m in in_trash["messages"]} == {"msg-001"}
    with_trash = seeded.get("/gmail/v1/users/me/messages?includeSpamTrash=true",
                            headers=H).json()
    assert "msg-001" in {m["id"] for m in with_trash["messages"]}


def test_gmail_pagination_terminates(seeded):
    seen, token, pages = [], None, 0
    while True:
        params = {"maxResults": 1, **({"pageToken": token} if token else {})}
        body = seeded.get("/gmail/v1/users/me/messages", headers=H, params=params).json()
        seen += [m["id"] for m in body.get("messages", [])]
        token, pages = body.get("nextPageToken"), pages + 1
        assert pages <= 10, "pagination never terminated"
        if not token:
            break
    assert sorted(seen) == ["msg-001", "msg-002", "msg-003", "msg-004"]


def test_gmail_invalid_page_token_is_rejected(seeded):
    r = seeded.get("/gmail/v1/users/me/messages?pageToken=not-a-token", headers=H)
    assert r.status_code == 400


def test_gmail_modify_untrash_and_batch(seeded):
    modified = seeded.post("/gmail/v1/users/me/messages/msg-001/modify", headers=H, json={
        "addLabelIds": ["STARRED"], "removeLabelIds": ["UNREAD"]}).json()
    assert "STARRED" in modified["labelIds"] and "UNREAD" not in modified["labelIds"]
    trashed = seeded.post("/gmail/v1/users/me/messages/msg-001/trash", headers=H).json()
    assert "TRASH" in trashed["labelIds"] and "INBOX" not in trashed["labelIds"]
    untrashed = seeded.post("/gmail/v1/users/me/messages/msg-001/untrash", headers=H).json()
    assert "TRASH" not in untrashed["labelIds"] and "INBOX" in untrashed["labelIds"]
    assert seeded.post("/gmail/v1/users/me/messages/batchModify", headers=H, json={
        "ids": ["msg-001", "msg-002"], "addLabelIds": ["STARRED"]}).status_code == 204
    assert "STARRED" in gw.STATE["gmail_messages"]["msg-002"]["labelIds"]


def test_gmail_modify_rejects_an_unknown_label(seeded):
    r = seeded.post("/gmail/v1/users/me/messages/msg-001/modify", headers=H,
                    json={"addLabelIds": ["Follow-up"]})
    assert r.status_code == 400 and "Invalid label" in r.json()["error"]["message"]


def test_gmail_batch_delete(seeded):
    assert seeded.post("/gmail/v1/users/me/messages/batchDelete", headers=H,
                       json={"ids": ["msg-001"]}).status_code == 204
    assert "msg-001" not in gw.STATE["gmail_messages"]


def test_gmail_delete_message(client):
    message = send(client)
    assert client.delete(f"/gmail/v1/users/me/messages/{message['id']}",
                         headers=H).status_code == 204
    assert message["id"] not in gw.STATE["gmail_messages"]
    assert message["threadId"] not in gw.STATE["gmail_threads"]


def test_gmail_missing_message_is_404(client):
    r = client.get("/gmail/v1/users/me/messages/nope", headers=H)
    assert r.status_code == 404 and r.json()["error"]["status"] == "NOT_FOUND"


# --- Gmail: threads ----------------------------------------------------------

def test_gmail_threads_list_and_get(seeded):
    listed = seeded.get("/gmail/v1/users/me/threads", headers=H).json()
    assert {t["id"] for t in listed["threads"]} == {"thread-001", "thread-002", "thread-003"}
    thread = seeded.get("/gmail/v1/users/me/threads/thread-001", headers=H).json()
    assert [m["id"] for m in thread["messages"]] == ["msg-001", "msg-002"]


def test_gmail_thread_modify_and_trash(seeded):
    seeded.post("/gmail/v1/users/me/threads/thread-001/modify", headers=H,
                json={"removeLabelIds": ["UNREAD"], "addLabelIds": ["STARRED"]})
    assert all("UNREAD" not in gw.STATE["gmail_messages"][mid]["labelIds"]
               for mid in ("msg-001", "msg-002"))
    trashed = seeded.post("/gmail/v1/users/me/threads/thread-001/trash", headers=H).json()
    assert all("TRASH" in m["labelIds"] for m in trashed["messages"])
    untrashed = seeded.post("/gmail/v1/users/me/threads/thread-001/untrash", headers=H).json()
    assert all("TRASH" not in m["labelIds"] for m in untrashed["messages"])


def test_gmail_delete_thread_removes_its_messages(seeded):
    assert seeded.delete("/gmail/v1/users/me/threads/thread-001", headers=H).status_code == 204
    assert "msg-001" not in gw.STATE["gmail_messages"]
    assert "thread-001" not in gw.STATE["gmail_threads"]


# --- Gmail: drafts -----------------------------------------------------------

def create_draft(client, **kwargs) -> dict:
    r = client.post("/gmail/v1/users/me/drafts", headers=H, json={"message": {"raw": raw(**kwargs)}})
    assert r.status_code == 200, r.text
    return r.json()


def test_gmail_draft_lifecycle(client):
    draft = create_draft(client, to="dave@acme.test", subject="Draft: Q3 plan", body="wip")
    assert draft["message"]["labelIds"] == ["DRAFT"]
    stored = gw.STATE["gmail_messages"][draft["message"]["id"]]
    assert gw._header(stored["payload"], "Subject") == "Draft: Q3 plan"

    listed = client.get("/gmail/v1/users/me/drafts", headers=H).json()
    assert [d["id"] for d in listed["drafts"]] == [draft["id"]]

    updated = client.put(f"/gmail/v1/users/me/drafts/{draft['id']}", headers=H, json={
        "id": draft["id"],
        "message": {"raw": raw(to="dave@acme.test", subject="Draft v2", body="wip2")},
    }).json()
    assert updated["id"] == draft["id"]
    assert updated["message"]["id"] != draft["message"]["id"]

    sent = client.post("/gmail/v1/users/me/drafts/send", headers=H,
                       json={"id": draft["id"]}).json()
    assert sent["labelIds"] == ["SENT"]
    assert draft["id"] not in gw.STATE["gmail_drafts"]
    assert gw._header(gw.STATE["gmail_messages"][sent["id"]]["payload"], "Subject") == "Draft v2"


def test_gmail_delete_draft_removes_its_message(client):
    draft = create_draft(client)
    assert client.delete(f"/gmail/v1/users/me/drafts/{draft['id']}", headers=H).status_code == 204
    assert draft["id"] not in gw.STATE["gmail_drafts"]
    assert draft["message"]["id"] not in gw.STATE["gmail_messages"]


def test_gmail_drafts_are_not_listed_as_inbox_mail(client):
    create_draft(client)
    listed = client.get("/gmail/v1/users/me/messages?labelIds=INBOX", headers=H).json()
    assert listed["resultSizeEstimate"] == 0


# --- Gmail: history ----------------------------------------------------------

def test_gmail_history_reports_changes_since_a_point(seeded):
    start = seeded.get("/gmail/v1/users/me/profile", headers=H).json()["historyId"]
    message = send(seeded, to="bob@acme.test", subject="New")
    body = seeded.get(f"/gmail/v1/users/me/history?startHistoryId={start}", headers=H).json()
    added = [item["message"]["id"] for record in body["history"]
             for item in record.get("messagesAdded", [])]
    assert message["id"] in added
    assert int(body["historyId"]) > int(start)


def test_gmail_history_requires_a_start(seeded):
    assert seeded.get("/gmail/v1/users/me/history", headers=H).status_code == 400


# --- Drive: files ------------------------------------------------------------

def test_drive_list_returns_four_fields_by_default(seeded):
    body = seeded.get("/drive/v3/files", headers=H).json()
    assert body["kind"] == "drive#fileList"
    assert set(body["files"][0]) == {"kind", "id", "name", "mimeType"}


def test_drive_nested_field_mask(seeded):
    body = seeded.get("/drive/v3/files?fields=nextPageToken, files(id, name)", headers=H).json()
    assert set(body) == {"files"}
    assert set(body["files"][0]) == {"id", "name"}


@pytest.mark.parametrize(("query", "expected"), [
    ("name contains 'Roadmap' and trashed = false", {"file-001"}),
    ("name = 'onboarding.md'", {"file-003"}),
    ("mimeType = 'application/vnd.google-apps.folder'", {"folder-001"}),
    ("'folder-001' in parents", {"file-003"}),
    ("fullText contains 'Postgres'", {"file-002"}),
    ("starred = true", {"file-001"}),
    ("starred", {"file-001"}),
    ("not starred = true", {"file-002", "file-003", "folder-001"}),
    ("mimeType = 'text/markdown' or starred = true", {"file-001", "file-003"}),
    ("modifiedTime > '2025-01-16T00:00:00'", {"file-002"}),
    ("'bob@acme.test' in writers", {"file-001"}),
])
def test_drive_query_grammar(seeded, query, expected):
    body = seeded.get("/drive/v3/files", headers=H, params={"q": query}).json()
    assert {f["id"] for f in body["files"]} == expected


def test_drive_invalid_query_is_rejected(seeded):
    r = seeded.get("/drive/v3/files", headers=H, params={"q": "name @@ 'x'"})
    assert r.status_code == 400 and r.json()["error"]["errors"][0]["location"] == "q"


def test_drive_list_includes_trashed_files_unless_excluded(seeded):
    seeded.patch("/drive/v3/files/file-001", headers=H, json={"trashed": True})
    everything = seeded.get("/drive/v3/files", headers=H).json()
    assert "file-001" in {f["id"] for f in everything["files"]}
    live = seeded.get("/drive/v3/files", headers=H, params={"q": "trashed = false"}).json()
    assert "file-001" not in {f["id"] for f in live["files"]}


def test_drive_pagination_terminates(seeded):
    seen, token, pages = [], None, 0
    while True:
        params = {"pageSize": 1, **({"pageToken": token} if token else {})}
        body = seeded.get("/drive/v3/files", headers=H, params=params).json()
        seen += [f["id"] for f in body["files"]]
        token, pages = body.get("nextPageToken"), pages + 1
        assert pages <= 10, "pagination never terminated"
        if not token:
            break
    assert sorted(seen) == ["file-001", "file-002", "file-003", "folder-001"]


def test_drive_order_by(seeded):
    body = seeded.get("/drive/v3/files?orderBy=name", headers=H).json()
    assert [f["name"] for f in body["files"]][0] == "Architecture Decision Records"


def test_drive_create_folder_and_file(client):
    folder = client.post("/drive/v3/files", headers=H, json={
        "name": "Reports", "mimeType": gw.FOLDER_MIME}).json()
    assert folder["mimeType"] == gw.FOLDER_MIME
    child = client.post("/drive/v3/files", headers=H, json={
        "name": "child.txt", "parents": [folder["id"]], "mimeType": "text/plain"}).json()
    assert gw.STATE["drive_files"][child["id"]]["parents"] == [folder["id"]]


def test_drive_create_rejects_a_missing_parent(client):
    r = client.post("/drive/v3/files", headers=H, json={"name": "x", "parents": ["nope"]})
    assert r.status_code == 404


def test_drive_multipart_upload_stores_content(client):
    body = (b"--b\r\nContent-Type: application/json\r\n\r\n"
            b'{"name": "q3.txt"}\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nquarterly numbers\r\n--b--")
    r = client.post("/upload/drive/v3/files?uploadType=multipart",
                    headers={**H, "Content-Type": "multipart/related; boundary=b"}, content=body)
    assert r.status_code == 200
    file_id = r.json()["id"]
    assert gw.STATE["drive_content"][file_id] == "quarterly numbers"
    download = client.get(f"/drive/v3/files/{file_id}?alt=media", headers=H)
    assert download.content == b"quarterly numbers"


def test_drive_media_upload_and_resumable_session(client):
    media = client.post("/upload/drive/v3/files?uploadType=media",
                        headers={**H, "Content-Type": "text/plain"}, content=b"hello")
    assert media.json()["name"] == "Untitled"

    start = client.post("/upload/drive/v3/files?uploadType=resumable", headers=H,
                        json={"name": "big.txt", "mimeType": "text/plain"})
    assert start.status_code == 200 and "upload_id=" in start.headers["location"]
    session = start.headers["location"].split("upload_id=")[1]
    first = client.put(f"/upload/drive/v3/files?uploadType=resumable&upload_id={session}",
                       headers={**H, "Content-Range": "bytes 0-4/10"}, content=b"12345")
    assert first.status_code == 308
    done = client.put(f"/upload/drive/v3/files?uploadType=resumable&upload_id={session}",
                      headers={**H, "Content-Range": "bytes 5-9/10"}, content=b"67890")
    assert done.status_code == 200
    assert gw.STATE["drive_content"][done.json()["id"]] == "1234567890"


def test_drive_export_and_download_rules(seeded):
    exported = seeded.get("/drive/v3/files/file-002/export?mimeType=text/plain", headers=H)
    assert b"ADR-001" in exported.content
    assert seeded.get("/drive/v3/files/file-002?alt=media", headers=H).status_code == 403
    assert seeded.get("/drive/v3/files/file-003/export?mimeType=text/plain",
                      headers=H).status_code == 403


def test_drive_update_moves_and_renames(seeded):
    renamed = seeded.patch("/drive/v3/files/file-003?fields=id,name,parents", headers=H,
                           json={"name": "onboarding-v2.md"}).json()
    assert renamed["name"] == "onboarding-v2.md"
    moved = seeded.patch(
        "/drive/v3/files/file-003?addParents=root&removeParents=folder-001&fields=id,parents",
        headers=H).json()
    assert moved["parents"] == ["root"]


def test_drive_update_rejects_parents_in_the_body(seeded):
    r = seeded.patch("/drive/v3/files/file-003", headers=H, json={"parents": ["root"]})
    assert r.status_code == 403


def test_drive_copy_copies_content(seeded):
    copy = seeded.post("/drive/v3/files/file-003/copy", headers=H,
                       json={"name": "copy.md"}).json()
    assert copy["name"] == "copy.md"
    assert gw.STATE["drive_content"][copy["id"]] == gw.STATE["drive_content"]["file-003"]


def test_drive_delete_folder_deletes_its_children(seeded):
    assert seeded.delete("/drive/v3/files/folder-001", headers=H).status_code == 204
    assert "file-003" not in gw.STATE["drive_files"]


def test_drive_missing_file_is_404(client):
    r = client.get("/drive/v3/files/nope", headers=H)
    assert r.status_code == 404 and "File not found" in r.json()["error"]["message"]


def test_drive_about_requires_fields(seeded):
    assert seeded.get("/drive/v3/about", headers=H).status_code == 400
    about = seeded.get("/drive/v3/about?fields=user,storageQuota", headers=H).json()
    assert about["user"]["emailAddress"] == "alice@acme.test"


# --- Drive: permissions -------------------------------------------------------

def test_drive_share_and_unshare(seeded):
    created = seeded.post("/drive/v3/files/file-002/permissions", headers=H, json={
        "type": "user", "role": "writer", "emailAddress": "bob@acme.test"}).json()
    assert set(created) == {"kind", "id", "type", "role"}
    listed = seeded.get("/drive/v3/files/file-002/permissions", headers=H).json()["permissions"]
    assert any(p.get("emailAddress") == "bob@acme.test" for p in listed)
    assert seeded.get("/drive/v3/files/file-002?fields=shared", headers=H).json()["shared"]
    updated = seeded.patch(f"/drive/v3/files/file-002/permissions/{created['id']}", headers=H,
                           json={"role": "reader"}).json()
    assert updated["role"] == "reader"
    assert seeded.delete(f"/drive/v3/files/file-002/permissions/{created['id']}",
                         headers=H).status_code == 204


def test_drive_permission_requires_type_and_role(seeded):
    r = seeded.post("/drive/v3/files/file-002/permissions", headers=H,
                    json={"emailAddress": "bob@acme.test"})
    assert r.status_code == 400


def test_drive_resharing_updates_the_existing_permission(seeded):
    first = seeded.post("/drive/v3/files/file-002/permissions", headers=H, json={
        "type": "user", "role": "reader", "emailAddress": "bob@acme.test"}).json()
    second = seeded.post("/drive/v3/files/file-002/permissions", headers=H, json={
        "type": "user", "role": "writer", "emailAddress": "bob@acme.test"}).json()
    assert second["id"] == first["id"] and second["role"] == "writer"


# --- Calendar -----------------------------------------------------------------

def test_calendar_list_has_a_primary_calendar(seeded):
    items = seeded.get("/calendar/v3/users/me/calendarList", headers=H).json()["items"]
    assert [c["id"] for c in items] == ["alice@acme.test"]
    assert items[0]["primary"] is True


def test_calendar_event_lifecycle(seeded):
    created = seeded.post("/calendar/v3/calendars/primary/events", headers=H, json={
        "summary": "1:1 with Bob",
        "start": {"dateTime": "2026-09-20T10:00:00Z"},
        "end": {"dateTime": "2026-09-20T10:30:00Z"},
        "attendees": [{"email": "bob@acme.test"}],
    }).json()
    assert created["kind"] == "calendar#event" and created["status"] == "confirmed"
    assert created["attendees"][0]["responseStatus"] == "needsAction"

    fetched = seeded.get(f"/calendar/v3/calendars/primary/events/{created['id']}",
                         headers=H).json()
    assert fetched["summary"] == "1:1 with Bob"

    patched = seeded.patch(f"/calendar/v3/calendars/primary/events/{created['id']}", headers=H,
                           json={"location": "Zoom"}).json()
    assert patched["location"] == "Zoom" and patched["summary"] == "1:1 with Bob"

    assert seeded.delete(f"/calendar/v3/calendars/primary/events/{created['id']}",
                         headers=H).status_code == 204
    assert gw.STATE["calendar_events"][created["id"]]["status"] == "cancelled"
    listed = seeded.get("/calendar/v3/calendars/primary/events", headers=H).json()
    assert created["id"] not in {e["id"] for e in listed["items"]}


def test_calendar_event_requires_a_time_range(seeded):
    r = seeded.post("/calendar/v3/calendars/primary/events", headers=H,
                    json={"summary": "No times"})
    assert r.status_code == 400 and "start" in r.json()["error"]["message"].lower()
    r = seeded.post("/calendar/v3/calendars/primary/events", headers=H, json={
        "summary": "Backwards", "start": {"dateTime": "2026-09-20T11:00:00Z"},
        "end": {"dateTime": "2026-09-20T10:00:00Z"}})
    assert r.status_code == 400


def test_calendar_list_events_filters_by_window(seeded):
    body = seeded.get("/calendar/v3/calendars/primary/events", headers=H, params={
        "timeMin": "2025-01-27T09:30:00Z", "timeMax": "2025-01-28T00:00:00Z",
        "singleEvents": "true", "orderBy": "startTime"}).json()
    assert [e["id"] for e in body["items"]] == ["evt-q1-planning"]


def test_calendar_order_by_start_time_needs_single_events(seeded):
    r = seeded.get("/calendar/v3/calendars/primary/events?orderBy=startTime", headers=H)
    assert r.status_code == 400


def test_calendar_free_busy(seeded):
    body = seeded.post("/calendar/v3/freeBusy", headers=H, json={
        "timeMin": "2025-01-27T00:00:00Z", "timeMax": "2025-01-28T00:00:00Z",
        "items": [{"id": "primary"}]}).json()
    assert body["calendars"]["primary"]["busy"][0]["start"] == "2025-01-27T09:00:00Z"


def test_calendar_unknown_calendar_is_404(seeded):
    assert seeded.get("/calendar/v3/calendars/nope@x.test/events", headers=H).status_code == 404


# --- OAuth, batch -------------------------------------------------------------

def test_oauth_token_endpoint_mints_the_twin_token(client):
    r = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": "x"})
    assert r.status_code == 200
    assert r.json()["access_token"] == gw.DEFAULT_BOOTSTRAP_TOKEN


def test_oauth_token_endpoint_rejects_an_unknown_grant(client):
    r = client.post("/token", data={"grant_type": "nope"})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"


def test_batch_runs_each_part_and_reports_per_part_status(seeded):
    body = (
        "--b\r\nContent-Type: application/http\r\nContent-ID: <x + 1>\r\n\r\n"
        "GET /gmail/v1/users/me/messages/msg-002?format=minimal HTTP/1.1\r\n\r\n\r\n"
        "--b\r\nContent-Type: application/http\r\nContent-ID: <x + 2>\r\n\r\n"
        "GET /gmail/v1/users/me/messages/nope HTTP/1.1\r\n\r\n\r\n--b--")
    r = seeded.post("/batch/gmail/v1", headers={**H, "Content-Type": "multipart/mixed; boundary=b"},
                    content=body.encode())
    assert r.status_code == 200
    assert "multipart/mixed" in r.headers["content-type"]
    assert "HTTP/1.1 200 OK" in r.text and "HTTP/1.1 404 Not Found" in r.text
    assert "<response-x + 1>" in r.text
    # Each sub-request is traced in its own right.
    paths = [e["path"] for e in seeded.get("/_trace").json()]
    assert "/gmail/v1/users/me/messages/msg-002" in paths


def test_batch_with_failing_parts_counts_as_a_failed_call(seeded):
    body = ("--b\r\nContent-Type: application/http\r\nContent-ID: <x + 1>\r\n\r\n"
            "GET /gmail/v1/users/me/messages/nope HTTP/1.1\r\n\r\n\r\n--b--")
    seeded.post("/batch", headers={**H, "Content-Type": "multipart/mixed; boundary=b"},
                content=body.encode())
    entry = next(e for e in reversed(seeded.get("/_trace").json()) if e["path"] == "/batch")
    assert entry["status"] == 200 and entry["ok"] is False


# --- seeds, views, classification ---------------------------------------------

def test_seed_small_team(seeded):
    state = seeded.get("/_state").json()
    assert len(state["gmail_messages"]) == 4
    assert set(state["gmail_threads"]) == {"thread-001", "thread-002", "thread-003"}
    assert state["drive_files"] and state["calendar_events"]


def test_seed_normalizes_plain_text_bodies_to_base64(client):
    client.post("/_seed-file", json={"state": {"gmail_messages": {"m1": {
        "id": "m1", "threadId": "t1", "labelIds": ["INBOX"],
        "payload": {"mimeType": "text/plain", "headers": [
            {"name": "From", "value": "zoe@acme.test"},
            {"name": "To", "value": "user@checkpoint.test"},
            {"name": "Subject", "value": "Plain seed"},
            {"name": "Date", "value": "2025-02-01T09:00:00+00:00"}],
            "body": {"data": "written as plain text"}}}}}})
    message = client.get("/gmail/v1/users/me/messages/m1", headers=H).json()
    assert b64decode(message["payload"]["body"]["data"]) == "written as plain text"
    from email.utils import parsedate_to_datetime
    date = next(h["value"] for h in message["payload"]["headers"] if h["name"] == "Date")
    assert parsedate_to_datetime(date).year == 2025
    assert client.get("/gmail/v1/users/me/threads/t1", headers=H).status_code == 200


def test_seed_empty(seeded):
    seeded.post("/_seed/empty")
    state = seeded.get("/_state").json()
    assert not state["drive_files"] and not state["gmail_messages"]


def test_seed_unknown_returns_404(client):
    assert client.post("/_seed/nope").status_code == 404


def test_views_expose_parsed_records(seeded):
    views = seeded.get("/_views").json()["collections"]
    message = next(m for m in views["gmail_messages"]["items"] if m["id"] == "msg-001")
    assert message["subject"] == "Q1 Planning Meeting"
    assert message["from"] == "Bob Smith <bob@acme.test>"
    assert message["to"] == "alice@acme.test"
    assert message["unread"] is True and message["trashed"] is False
    assert views["gmail_messages"]["tombstone"] == "trashed"
    assert "email" in views["gmail_messages"]["nouns"]

    file = next(f for f in views["drive_files"]["items"] if f["id"] == "file-003")
    assert file["folder"] == "Engineering Docs" and "Onboarding" in file["content"]
    assert views["drive_permissions"]["items"][0]["file_name"]
    event = views["calendar_events"]["items"][0]
    assert event["calendar_id"] == "alice@acme.test"
    assert {"gmail_threads", "gmail_labels", "gmail_drafts", "calendars"} <= set(views)


def test_views_mark_trashed_records(seeded):
    seeded.post("/gmail/v1/users/me/messages/msg-001/trash", headers=H)
    seeded.patch("/drive/v3/files/file-001", headers=H, json={"trashed": True})
    views = seeded.get("/_views").json()["collections"]
    assert next(m for m in views["gmail_messages"]["items"]
                if m["id"] == "msg-001")["trashed"] is True
    assert next(f for f in views["drive_files"]["items"] if f["id"] == "file-001")["trashed"]


@pytest.mark.parametrize(("method", "path", "body", "expected"), [
    ("POST", "/gmail/v1/users/me/messages/send", None, ("create", "gmail_messages")),
    ("POST", "/gmail/v1/users/me/messages/m1/trash", None, ("delete", "gmail_messages")),
    ("POST", "/gmail/v1/users/me/messages/batchDelete", None, ("delete", "gmail_messages")),
    ("POST", "/gmail/v1/users/me/messages/m1/modify", None, ("update", "gmail_messages")),
    ("POST", "/gmail/v1/users/me/threads/t1/trash", None, ("delete", "gmail_threads")),
    ("POST", "/gmail/v1/users/me/drafts", None, ("create", "gmail_drafts")),
    ("POST", "/gmail/v1/users/me/drafts/send", None, ("create", "gmail_messages")),
    ("GET", "/gmail/v1/users/me/profile", None, ("read", "user_profile")),
    ("POST", "/upload/drive/v3/files", None, ("create", "drive_files")),
    ("PATCH", "/drive/v3/files/f1", {"trashed": True}, ("delete", "drive_files")),
    ("PATCH", "/drive/v3/files/f1", {"name": "x"}, ("update", "drive_files")),
    ("POST", "/drive/v3/files/f1/copy", None, ("create", "drive_files")),
    ("POST", "/drive/v3/files/f1/permissions", None, ("create", "drive_permissions")),
    ("GET", "/drive/v3/about", None, ("read", "drive_about")),
    ("POST", "/calendar/v3/calendars/primary/events", None, ("create", "calendar_events")),
    ("GET", "/calendar/v3/users/me/calendarList", None, ("read", "calendars")),
    ("POST", "/batch/gmail/v1", None, ("other", "batch")),
    ("POST", "/token", None, ("other", "oauth_token")),
])
def test_classify_maps_endpoints_to_op_and_resource(method, path, body, expected):
    assert gw._classify(method, path, body) == expected


def test_trace_records_op_and_resource(client):
    send(client)
    entry = client.get("/_trace").json()[-1]
    assert (entry["op"], entry["resource"]) == ("create", "gmail_messages")
    assert entry["ok"] is True
    assert json.loads(json.dumps(entry["body"]))["raw"]
