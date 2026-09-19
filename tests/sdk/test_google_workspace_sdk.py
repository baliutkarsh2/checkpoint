"""Google Workspace twin driven by google-api-python-client, Google's own SDK.

The SDK builds its requests from Google's discovery documents, so pointing
``rootUrl`` at the twin is exactly what the sandbox's proxy does at the network
layer: every path, verb and parameter below is the one the real service sees.
"""
from __future__ import annotations

import base64
import io
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

pytest.importorskip("googleapiclient")
pytest.importorskip("google.oauth2")

from google.oauth2.credentials import Credentials  # noqa: E402
from googleapiclient import discovery_cache  # noqa: E402
from googleapiclient.discovery import build_from_document  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402
from googleapiclient.http import MediaIoBaseUpload  # noqa: E402

TWIN = "google-workspace"

_DOCUMENTS: dict[str, dict] = {}


def _document(name: str, version: str) -> dict:
    """The SDK's bundled discovery document for one API."""
    if (name, version) not in _DOCUMENTS:
        path = Path(discovery_cache.__file__).parent / "documents" / f"{name}.{version}.json"
        _DOCUMENTS[name, version] = json.loads(path.read_text(encoding="utf-8"))
    return json.loads(json.dumps(_DOCUMENTS[name, version]))


def _service(name: str, version: str, twin, *, credentials=None):
    document = _document(name, version)
    document["rootUrl"] = f"{twin.url}/"
    document.pop("mtlsRootUrl", None)
    return build_from_document(document, credentials=credentials or _credentials(twin))


def _credentials(twin) -> Credentials:
    # A full credential: batch requests always refresh once, which the twin's
    # /token endpoint (oauth2.googleapis.com) answers.
    return Credentials(token=twin.token, refresh_token="1//checkpoint-refresh",
                       token_uri=f"{twin.url}/token", client_id="checkpoint",
                       client_secret="s3cret")


# One client per test: each gets its own connection pool, and the parsed
# discovery documents are cached above, so building is cheap.
@pytest.fixture
def gmail(twin):
    return _service("gmail", "v1", twin)


@pytest.fixture
def users(gmail):
    return gmail.users()


@pytest.fixture
def drive(twin):
    return _service("drive", "v3", twin)


@pytest.fixture
def calendar(twin):
    return _service("calendar", "v3", twin)


def raw_message(to: str, subject: str, body: str, **headers: str) -> str:
    message = EmailMessage()
    message["To"], message["Subject"] = to, subject
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode()


# --- Gmail --------------------------------------------------------------------

def test_get_profile(users, twin):
    twin.seed("small-team")
    profile = users.getProfile(userId="me").execute()
    assert profile["emailAddress"] == "alice@acme.test"
    assert profile["messagesTotal"] == 4


def test_labels_lifecycle(users):
    created = users.labels().create(userId="me", body={"name": "Follow-up"}).execute()
    names = {lab["name"] for lab in users.labels().list(userId="me").execute()["labels"]}
    assert "Follow-up" in names and "INBOX" in names
    fetched = users.labels().get(userId="me", id=created["id"]).execute()
    assert fetched["messagesTotal"] == 0
    users.labels().delete(userId="me", id=created["id"]).execute()
    assert created["id"] not in {lab["id"] for lab
                                 in users.labels().list(userId="me").execute()["labels"]}


def test_send_message_and_read_it_back(users, twin):
    sent = users.messages().send(userId="me", body={
        "raw": raw_message("carol@acme.test", "Invoice #42", "Please find it attached."),
    }).execute()
    assert sent["labelIds"] == ["SENT"]

    full = users.messages().get(userId="me", id=sent["id"], format="full").execute()
    headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
    assert headers["To"] == "carol@acme.test"
    assert headers["Subject"] == "Invoice #42"
    assert "Please find it" in decode(full["payload"]["body"]["data"])

    message = next(m for m in twin.views()["gmail_messages"]["items"] if m["id"] == sent["id"])
    assert message["subject"] == "Invoice #42" and message["to"] == "carol@acme.test"


def test_message_formats_and_rfc_2822_date(users, twin):
    twin.seed("small-team")
    from email.utils import parsedate_to_datetime

    metadata = users.messages().get(userId="me", id="msg-001", format="metadata",
                                    metadataHeaders=["Date", "Subject"]).execute()
    headers = {h["name"]: h["value"] for h in metadata["payload"]["headers"]}
    assert set(headers) == {"Date", "Subject"}
    assert parsedate_to_datetime(headers["Date"]).year == 2025

    rfc822 = users.messages().get(userId="me", id="msg-001", format="raw").execute()["raw"]
    assert "Subject: Q1 Planning Meeting" in decode(rfc822)
    minimal = users.messages().get(userId="me", id="msg-001", format="minimal").execute()
    assert "payload" not in minimal


def test_reply_stays_in_the_thread(users, twin):
    twin.seed("small-team")
    reply = users.messages().send(userId="me", body={
        "threadId": "thread-001",
        "raw": raw_message("bob@acme.test", "Re: Q1 Planning Meeting", "Works for me.",
                           In_Reply_To="<q1-planning-1@acme.test>"),
    }).execute()
    assert reply["threadId"] == "thread-001"
    thread = users.threads().get(userId="me", id="thread-001", format="metadata").execute()
    assert [m["id"] for m in thread["messages"]][-1] == reply["id"]


@pytest.mark.parametrize(("query", "expected"), [
    ("is:unread", {"msg-001", "msg-004"}),
    ("from:bob@acme.test", {"msg-001"}),
    ("subject:(Q1) newer_than:3650d", {"msg-001", "msg-002"}),
    ("has:attachment", {"msg-004"}),
    ("label:Team", {"msg-003"}),
])
def test_search_query_operators(users, twin, query, expected):
    twin.seed("small-team")
    found = users.messages().list(userId="me", q=query).execute()
    assert {m["id"] for m in found.get("messages", [])} == expected


def test_label_ids_filter_is_anded(users, twin):
    twin.seed("small-team")
    found = users.messages().list(userId="me", labelIds=["INBOX", "UNREAD"]).execute()
    assert {m["id"] for m in found["messages"]} == {"msg-001", "msg-004"}


def test_messages_pagination_terminates(users, twin):
    twin.seed("small-team")
    request = users.messages().list(userId="me", maxResults=1)
    seen, pages = [], 0
    while request is not None:
        response = request.execute()
        seen += [m["id"] for m in response.get("messages", [])]
        pages += 1
        assert pages <= 10, "list_next never stopped: the twin echoed a page token forever"
        request = users.messages().list_next(request, response)
    assert sorted(seen) == ["msg-001", "msg-002", "msg-003", "msg-004"]


def test_modify_trash_untrash_and_batch_modify(users, twin):
    twin.seed("small-team")
    modified = users.messages().modify(userId="me", id="msg-001", body={
        "removeLabelIds": ["UNREAD"], "addLabelIds": ["STARRED"]}).execute()
    assert "UNREAD" not in modified["labelIds"] and "STARRED" in modified["labelIds"]

    trashed = users.messages().trash(userId="me", id="msg-001").execute()
    assert "TRASH" in trashed["labelIds"]
    assert users.messages().untrash(userId="me", id="msg-001").execute()["labelIds"].count(
        "TRASH") == 0

    users.messages().batchModify(userId="me", body={
        "ids": ["msg-001", "msg-002"], "addLabelIds": ["IMPORTANT"]}).execute()
    state = twin.state()["gmail_messages"]
    assert all("IMPORTANT" in state[mid]["labelIds"] for mid in ("msg-001", "msg-002"))


def test_thread_modify_marks_every_message(users, twin):
    twin.seed("small-team")
    users.threads().modify(userId="me", id="thread-001",
                           body={"removeLabelIds": ["UNREAD"]}).execute()
    unread = users.messages().list(userId="me", q="is:unread").execute()
    assert {m["id"] for m in unread["messages"]} == {"msg-004"}


def test_draft_create_update_send(users, twin):
    draft = users.drafts().create(userId="me", body={
        "message": {"raw": raw_message("dave@acme.test", "Draft: Q3 plan", "work in progress")},
    }).execute()
    assert draft["message"]["labelIds"] == ["DRAFT"]
    assert [d["id"] for d in users.drafts().list(userId="me").execute()["drafts"]] == [draft["id"]]

    users.drafts().update(userId="me", id=draft["id"], body={
        "message": {"raw": raw_message("dave@acme.test", "Q3 plan", "final")},
    }).execute()
    sent = users.drafts().send(userId="me", body={"id": draft["id"]}).execute()
    assert sent["labelIds"] == ["SENT"]
    assert users.drafts().list(userId="me").execute().get("drafts", []) == []
    view = next(m for m in twin.views()["gmail_messages"]["items"] if m["id"] == sent["id"])
    assert view["subject"] == "Q3 plan" and view["sent"] is True


def test_delete_message(users, twin):
    twin.seed("small-team")
    users.messages().delete(userId="me", id="msg-003").execute()
    assert "msg-003" not in twin.state()["gmail_messages"]
    with pytest.raises(HttpError) as caught:
        users.messages().get(userId="me", id="msg-003").execute()
    assert caught.value.resp.status == 404


def test_history_list_reports_new_mail(users, twin):
    twin.seed("small-team")
    start = users.getProfile(userId="me").execute()["historyId"]
    sent = users.messages().send(userId="me", body={
        "raw": raw_message("bob@acme.test", "Ping", "hi")}).execute()
    history = users.history().list(userId="me", startHistoryId=start).execute()
    added = [item["message"]["id"] for record in history["history"]
             for item in record.get("messagesAdded", [])]
    assert sent["id"] in added


def test_batch_http_request_runs_every_call(gmail, users, twin):
    twin.seed("small-team")
    results: dict[str, object] = {}

    def collect(request_id, response, exception):
        results[request_id] = exception or response

    batch = gmail.new_batch_http_request(callback=collect)
    for message_id in ("msg-001", "msg-002"):
        batch.add(users.messages().get(userId="me", id=message_id, format="metadata"),
                  request_id=message_id)
    batch.add(users.messages().get(userId="me", id="missing"), request_id="missing")
    batch.execute()

    assert results["msg-001"]["id"] == "msg-001"  # type: ignore[index]
    assert isinstance(results["missing"], HttpError)
    assert results["missing"].resp.status == 404  # type: ignore[union-attr]


def test_user_id_may_be_the_email_address(users, twin):
    twin.seed("small-team")
    assert users.labels().list(userId="alice@acme.test").execute()["labels"]


# --- Drive --------------------------------------------------------------------

def test_drive_list_with_field_mask_and_query(drive, twin):
    twin.seed("small-team")
    listed = drive.files().list(pageSize=10, fields="nextPageToken, files(id, name)").execute()
    assert set(listed["files"][0]) == {"id", "name"}

    found = drive.files().list(q="name contains 'Roadmap' and trashed = false").execute()
    assert [f["name"] for f in found["files"]] == ["Q1 Roadmap"]


def test_drive_folder_upload_and_download(drive, twin):
    folder = drive.files().create(
        body={"name": "Reports", "mimeType": "application/vnd.google-apps.folder"},
        fields="id").execute()

    media = MediaIoBaseUpload(io.BytesIO(b"quarterly numbers"), mimetype="text/plain")
    created = drive.files().create(body={"name": "q3.txt", "parents": [folder["id"]]},
                                   media_body=media, fields="id, name, parents").execute()
    assert created["name"] == "q3.txt" and created["parents"] == [folder["id"]]

    in_folder = drive.files().list(q=f"'{folder['id']}' in parents").execute()
    assert [f["id"] for f in in_folder["files"]] == [created["id"]]
    assert drive.files().get_media(fileId=created["id"]).execute() == b"quarterly numbers"


def test_drive_resumable_upload(drive):
    media = MediaIoBaseUpload(io.BytesIO(b"a" * 2048), mimetype="text/plain",
                              chunksize=1024, resumable=True)
    request = drive.files().create(body={"name": "resumable.txt"}, media_body=media,
                                   fields="id, name, size")
    response = None
    while response is None:
        _status, response = request.next_chunk()
    assert response["name"] == "resumable.txt" and response["size"] == "2048"


def test_drive_export_a_google_doc(drive, twin):
    twin.seed("small-team")
    exported = drive.files().export(fileId="file-002", mimeType="text/plain").execute()
    assert b"ADR-001" in exported


def test_drive_rename_move_and_copy(drive, twin):
    twin.seed("small-team")
    renamed = drive.files().update(fileId="file-003", body={"name": "onboarding-v2.md"},
                                   fields="id, name").execute()
    assert renamed["name"] == "onboarding-v2.md"

    moved = drive.files().update(fileId="file-003", addParents="root",
                                 removeParents="folder-001", fields="id, parents").execute()
    assert moved["parents"] == ["root"]

    copied = drive.files().copy(fileId="file-003", body={"name": "copy.md"},
                                fields="id, name").execute()
    assert copied["name"] == "copy.md"
    drive.files().delete(fileId=copied["id"]).execute()
    assert copied["id"] not in twin.state()["drive_files"]


def test_drive_pagination_terminates(drive, twin):
    twin.seed("small-team")
    request = drive.files().list(pageSize=1, fields="nextPageToken, files(id)")
    seen, pages = [], 0
    while request is not None:
        response = request.execute()
        seen += [f["id"] for f in response["files"]]
        pages += 1
        assert pages <= 10, "list_next never stopped: the twin echoed a page token forever"
        request = drive.files().list_next(request, response)
    assert sorted(seen) == ["file-001", "file-002", "file-003", "folder-001"]


def test_drive_share_a_file(drive, twin):
    twin.seed("small-team")
    permission = drive.permissions().create(
        fileId="file-002", body={"type": "user", "role": "writer",
                                 "emailAddress": "bob@acme.test"},
        sendNotificationEmail=False).execute()
    listed = drive.permissions().list(fileId="file-002").execute()["permissions"]
    assert any(p.get("emailAddress") == "bob@acme.test" for p in listed)

    shared = next(p for p in twin.views()["drive_permissions"]["items"]
                  if p["id"] == permission["id"])
    assert shared["file_name"] == "Architecture Decision Records" and shared["role"] == "writer"
    drive.permissions().delete(fileId="file-002", permissionId=permission["id"]).execute()


def test_drive_about(drive, twin):
    twin.seed("small-team")
    about = drive.about().get(fields="user, storageQuota").execute()
    assert about["user"]["emailAddress"] == "alice@acme.test"


def test_drive_trashing_is_recorded_as_a_delete(drive, twin):
    twin.seed("small-team")
    drive.files().update(fileId="file-001", body={"trashed": True}).execute()
    assert ("delete", "drive_files") in [(e["op"], e["resource"]) for e in twin.trace()]
    view = next(f for f in twin.views()["drive_files"]["items"] if f["id"] == "file-001")
    assert view["trashed"] is True


# --- Calendar -----------------------------------------------------------------

def test_calendar_event_crud(calendar, twin):
    twin.seed("small-team")
    created = calendar.events().insert(calendarId="primary", body={
        "summary": "1:1 with Bob",
        "start": {"dateTime": "2026-09-21T10:00:00Z"},
        "end": {"dateTime": "2026-09-21T10:30:00Z"},
        "attendees": [{"email": "bob@acme.test"}],
    }).execute()
    assert created["status"] == "confirmed"

    patched = calendar.events().patch(calendarId="primary", eventId=created["id"],
                                      body={"location": "Zoom"}).execute()
    assert patched["location"] == "Zoom" and patched["summary"] == "1:1 with Bob"

    listed = calendar.events().list(calendarId="primary", timeMin="2026-09-21T00:00:00Z",
                                    timeMax="2026-09-22T00:00:00Z", singleEvents=True,
                                    orderBy="startTime").execute()
    assert [e["id"] for e in listed["items"]] == [created["id"]]

    calendar.events().delete(calendarId="primary", eventId=created["id"]).execute()
    remaining = calendar.events().list(calendarId="primary").execute()
    assert created["id"] not in {e["id"] for e in remaining["items"]}


def test_calendar_list_and_free_busy(calendar, twin):
    twin.seed("small-team")
    calendars = calendar.calendarList().list().execute()["items"]
    assert calendars[0]["primary"] is True

    busy = calendar.freebusy().query(body={
        "timeMin": "2025-01-27T00:00:00Z", "timeMax": "2025-01-28T00:00:00Z",
        "items": [{"id": "primary"}]}).execute()
    assert busy["calendars"]["primary"]["busy"][0]["start"] == "2025-01-27T09:00:00Z"


# --- errors and faults --------------------------------------------------------

def test_missing_file_raises_http_error_404(drive):
    with pytest.raises(HttpError) as caught:
        drive.files().get(fileId="does-not-exist").execute()
    assert caught.value.resp.status == 404
    assert "File not found" in caught.value.reason


def test_invalid_label_raises_http_error_400(users, twin):
    twin.seed("small-team")
    with pytest.raises(HttpError) as caught:
        users.messages().modify(userId="me", id="msg-001",
                                body={"addLabelIds": ["not-a-label-id"]}).execute()
    assert caught.value.resp.status == 400


def test_bad_credentials_are_rejected(twin):
    from google.auth.exceptions import RefreshError

    twin.configure(strict_auth=True)
    service = _service("gmail", "v1", twin, credentials=Credentials(token="ya29.wrong"))
    # google-auth answers a 401 by refreshing the credential and retrying, so a
    # token-only credential surfaces the refusal as a RefreshError — as it does
    # against the real API.
    with pytest.raises((HttpError, RefreshError)) as caught:
        service.users().getProfile(userId="me").execute()
    if isinstance(caught.value, HttpError):
        assert caught.value.resp.status == 401


def test_rate_limit_fault_surfaces_as_429(users, twin):
    twin.configure(rate_limit=0)
    with pytest.raises(HttpError) as caught:
        users.getProfile(userId="me").execute(num_retries=0)
    assert caught.value.resp.status == 429


def test_read_only_fault_blocks_writes_but_not_reads(users, twin):
    twin.seed("small-team")
    twin.configure(read_only=True)
    assert users.messages().list(userId="me").execute()["resultSizeEstimate"] == 4
    with pytest.raises(HttpError) as caught:
        users.labels().create(userId="me", body={"name": "Nope"}).execute()
    assert caught.value.resp.status == 403


def test_trace_classifies_the_whole_flow(users, drive, twin):
    twin.seed("small-team")
    users.messages().send(userId="me", body={
        "raw": raw_message("bob@acme.test", "Status", "all good")}).execute()
    users.messages().trash(userId="me", id="msg-001").execute()
    drive.files().create(body={"name": "notes.txt", "mimeType": "text/plain"}).execute()
    ops = {(e["op"], e["resource"]) for e in twin.trace()}
    assert {("create", "gmail_messages"), ("delete", "gmail_messages"),
            ("create", "drive_files")} <= ops
    assert all(e["ok"] for e in twin.trace())
