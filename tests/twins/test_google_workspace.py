"""Google Workspace twin REST surface — Gmail + Drive."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import google_workspace as gw


@pytest.fixture(autouse=True)
def _reset_state():
    gw.STATE.clear()
    gw.STATE.update(gw._fresh_state())
    gw.TRACE.clear()
    yield


@pytest.fixture
def client():
    return TestClient(gw.app)


TOKEN = gw.DEFAULT_BOOTSTRAP_TOKEN
H = {"Authorization": f"Bearer {TOKEN}"}


# --- auth -------------------------------------------------------------------

def test_missing_token_returns_401(client):
    r = client.get("/gmail/v1/users/me/profile")
    assert r.status_code == 401


def test_wrong_token_returns_401(client):
    r = client.get("/gmail/v1/users/me/profile", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


def test_introspection_bypasses_auth(client):
    assert client.get("/_health").status_code == 200
    assert client.get("/_state").status_code == 200
    assert client.post("/_reset").status_code == 200


# --- Gmail: profile ---------------------------------------------------------

def test_gmail_get_profile(client):
    r = client.get("/gmail/v1/users/me/profile", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "emailAddress" in body
    assert "messagesTotal" in body


# --- Gmail: labels ----------------------------------------------------------

def test_gmail_list_labels_has_system_labels(client):
    r = client.get("/gmail/v1/users/me/labels", headers=H)
    assert r.status_code == 200
    body = r.json()
    ids = {lab["id"] for lab in body["labels"]}
    assert "INBOX" in ids
    assert "SENT" in ids
    assert "TRASH" in ids


def test_gmail_create_label(client):
    r = client.post("/gmail/v1/users/me/labels", headers=H, json={
        "name": "Work", "labelListVisibility": "labelShow"
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Work"
    assert body["type"] == "user"


def test_gmail_get_label(client):
    lab = client.post("/gmail/v1/users/me/labels", headers=H, json={"name": "Project"}).json()
    r = client.get(f"/gmail/v1/users/me/labels/{lab['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "Project"


def test_gmail_update_label(client):
    lab = client.post("/gmail/v1/users/me/labels", headers=H, json={"name": "Old"}).json()
    r = client.patch(f"/gmail/v1/users/me/labels/{lab['id']}", headers=H, json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"


def test_gmail_delete_label(client):
    lab = client.post("/gmail/v1/users/me/labels", headers=H, json={"name": "Temp"}).json()
    r = client.delete(f"/gmail/v1/users/me/labels/{lab['id']}", headers=H)
    assert r.status_code == 204
    assert lab["id"] not in gw.STATE["gmail_labels"]


def test_gmail_cannot_delete_system_label(client):
    r = client.delete("/gmail/v1/users/me/labels/INBOX", headers=H)
    assert r.status_code in (400, 403)


# --- Gmail: messages --------------------------------------------------------

def test_gmail_list_messages_empty(client):
    r = client.get("/gmail/v1/users/me/messages", headers=H)
    assert r.status_code == 200
    assert r.json()["resultSizeEstimate"] == 0


def test_gmail_send_message(client):
    import base64
    raw = base64.urlsafe_b64encode(
        b"From: test@test.com\r\nTo: bob@test.com\r\nSubject: Hello\r\n\r\nBody text"
    ).decode()
    r = client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw})
    assert r.status_code == 200
    body = r.json()
    assert "id" in body
    assert "SENT" in body.get("labelIds", [])


def test_gmail_get_message(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: Test\r\n\r\nHi").decode()
    msg = client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw}).json()
    r = client.get(f"/gmail/v1/users/me/messages/{msg['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["id"] == msg["id"]


def test_gmail_list_messages_filter_label(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: S\r\n\r\nB").decode()
    client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw})
    r = client.get("/gmail/v1/users/me/messages?labelIds=SENT", headers=H)
    assert r.status_code == 200
    assert r.json()["resultSizeEstimate"] >= 1


def test_gmail_modify_message_labels(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: S\r\n\r\nB").decode()
    msg = client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw}).json()
    r = client.post(f"/gmail/v1/users/me/messages/{msg['id']}/modify", headers=H, json={
        "addLabelIds": ["STARRED"],
        "removeLabelIds": [],
    })
    assert r.status_code == 200
    assert "STARRED" in r.json()["labelIds"]


def test_gmail_trash_message(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: S\r\n\r\nB").decode()
    msg = client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw}).json()
    r = client.post(f"/gmail/v1/users/me/messages/{msg['id']}/trash", headers=H)
    assert r.status_code == 200
    assert "TRASH" in r.json()["labelIds"]


def test_gmail_delete_message(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: S\r\n\r\nB").decode()
    msg = client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw}).json()
    r = client.delete(f"/gmail/v1/users/me/messages/{msg['id']}", headers=H)
    assert r.status_code == 204
    assert msg["id"] not in gw.STATE["gmail_messages"]


# --- Gmail: threads ---------------------------------------------------------

def test_gmail_list_threads_empty(client):
    r = client.get("/gmail/v1/users/me/threads", headers=H)
    assert r.status_code == 200
    assert r.json()["resultSizeEstimate"] == 0


def test_gmail_thread_gets_created_with_send(client):
    import base64
    raw = base64.urlsafe_b64encode(
        b"From: a@b.com\r\nTo: c@d.com\r\nSubject: Thread Test\r\n\r\nBody"
    ).decode()
    client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw})
    r = client.get("/gmail/v1/users/me/threads", headers=H)
    assert r.json()["resultSizeEstimate"] >= 1


def test_gmail_trash_thread(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: T\r\n\r\nB").decode()
    msg = client.post("/gmail/v1/users/me/messages/send", headers=H, json={"raw": raw}).json()
    tid = gw.STATE["gmail_messages"][msg["id"]]["threadId"]
    r = client.post(f"/gmail/v1/users/me/threads/{tid}/trash", headers=H)
    assert r.status_code == 200


# --- Gmail: drafts ----------------------------------------------------------

def test_gmail_create_and_list_draft(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: Draft\r\n\r\nBody").decode()
    r = client.post("/gmail/v1/users/me/drafts", headers=H, json={
        "message": {"raw": raw}
    })
    assert r.status_code == 200
    draft = r.json()
    assert "id" in draft
    r2 = client.get("/gmail/v1/users/me/drafts", headers=H)
    assert r2.json()["resultSizeEstimate"] >= 1


def test_gmail_get_draft(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: D\r\n\r\nB").decode()
    draft = client.post("/gmail/v1/users/me/drafts", headers=H, json={"message": {"raw": raw}}).json()
    r = client.get(f"/gmail/v1/users/me/drafts/{draft['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["id"] == draft["id"]


def test_gmail_delete_draft(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: D\r\n\r\nB").decode()
    draft = client.post("/gmail/v1/users/me/drafts", headers=H, json={"message": {"raw": raw}}).json()
    r = client.delete(f"/gmail/v1/users/me/drafts/{draft['id']}", headers=H)
    assert r.status_code == 204
    assert draft["id"] not in gw.STATE["gmail_drafts"]


def test_gmail_send_draft(client):
    import base64
    raw = base64.urlsafe_b64encode(b"From: a@b.com\r\nSubject: D\r\n\r\nB").decode()
    draft = client.post("/gmail/v1/users/me/drafts", headers=H, json={"message": {"raw": raw}}).json()
    # Twin uses /{draft_id}/send pattern
    r = client.post(f"/gmail/v1/users/me/drafts/{draft['id']}/send", headers=H)
    assert r.status_code == 200
    assert draft["id"] not in gw.STATE["gmail_drafts"]
    # Draft message now in sent
    msg_id = r.json()["id"]
    assert "SENT" in gw.STATE["gmail_messages"][msg_id]["labelIds"]


# --- Drive: files -----------------------------------------------------------

def test_drive_list_files_empty(client):
    r = client.get("/drive/v3/files", headers=H)
    assert r.status_code == 200
    assert r.json()["files"] == []


def test_drive_create_file(client):
    r = client.post("/drive/v3/files", headers=H, json={
        "name": "report.pdf",
        "mimeType": "application/pdf",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "report.pdf"
    assert "id" in body


def test_drive_get_file(client):
    f = client.post("/drive/v3/files", headers=H, json={"name": "doc.txt"}).json()
    r = client.get(f"/drive/v3/files/{f['id']}", headers=H)
    assert r.status_code == 200
    assert r.json()["name"] == "doc.txt"


def test_drive_update_file(client):
    f = client.post("/drive/v3/files", headers=H, json={"name": "old.txt"}).json()
    r = client.patch(f"/drive/v3/files/{f['id']}", headers=H, json={"name": "new.txt"})
    assert r.status_code == 200
    assert r.json()["name"] == "new.txt"


def test_drive_delete_file(client):
    f = client.post("/drive/v3/files", headers=H, json={"name": "del.txt"}).json()
    r = client.delete(f"/drive/v3/files/{f['id']}", headers=H)
    assert r.status_code == 204
    assert f["id"] not in gw.STATE["drive_files"]


def test_drive_create_folder(client):
    r = client.post("/drive/v3/files", headers=H, json={
        "name": "My Folder",
        "mimeType": "application/vnd.google-apps.folder",
    })
    assert r.status_code == 200
    assert r.json()["mimeType"] == "application/vnd.google-apps.folder"


def test_drive_copy_file(client):
    f = client.post("/drive/v3/files", headers=H, json={"name": "original.txt"}).json()
    r = client.post(f"/drive/v3/files/{f['id']}/copy", headers=H, json={"name": "copy.txt"})
    assert r.status_code == 200
    assert r.json()["name"] == "copy.txt"
    assert r.json()["id"] != f["id"]
    assert len(gw.STATE["drive_files"]) == 2


def test_drive_list_files_filter_q(client):
    client.post("/drive/v3/files", headers=H, json={"name": "report.pdf"})
    client.post("/drive/v3/files", headers=H, json={"name": "notes.txt"})
    r = client.get("/drive/v3/files?q=name+contains+%27report%27", headers=H)
    assert len(r.json()["files"]) == 1
    assert r.json()["files"][0]["name"] == "report.pdf"


# --- Drive: permissions -----------------------------------------------------

def test_drive_add_and_list_permissions(client):
    f = client.post("/drive/v3/files", headers=H, json={"name": "shared.txt"}).json()
    r = client.post(f"/drive/v3/files/{f['id']}/permissions", headers=H, json={
        "type": "user",
        "role": "reader",
        "emailAddress": "viewer@test.com",
    })
    assert r.status_code == 200
    perm = r.json()
    assert perm["role"] == "reader"
    r2 = client.get(f"/drive/v3/files/{f['id']}/permissions", headers=H)
    # File creation adds owner permission; our added permission is in addition
    perms = r2.json()["permissions"]
    assert any(p["emailAddress"] == "viewer@test.com" for p in perms)


def test_drive_delete_permission(client):
    f = client.post("/drive/v3/files", headers=H, json={"name": "shared.txt"}).json()
    perm = client.post(f"/drive/v3/files/{f['id']}/permissions", headers=H, json={
        "type": "user", "role": "reader", "emailAddress": "v@test.com"
    }).json()
    r = client.delete(f"/drive/v3/files/{f['id']}/permissions/{perm['id']}", headers=H)
    assert r.status_code == 204
    # Only the specific permission should be removed; owner perm may remain
    remaining = gw.STATE["drive_permissions"].get(f["id"], {})
    assert perm["id"] not in remaining


# --- seeds ------------------------------------------------------------------

def test_seed_small_team(client):
    r = client.post("/_seed/small-team")
    assert r.status_code == 200
    state = client.get("/_state").json()
    assert state["drive_files"]
    assert state["gmail_threads"]


def test_seed_empty(client):
    client.post("/_seed/small-team")
    client.post("/_seed/empty")
    state = client.get("/_state").json()
    assert not state["drive_files"]
    assert not state["gmail_threads"]


def test_seed_unknown_returns_404(client):
    r = client.post("/_seed/nope")
    assert r.status_code == 404
