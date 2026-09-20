---
twins: google-workspace
seed: small-team
timeout: 120
tags: google-workspace, gmail, drive, email
---
# Google Workspace — send, label, share and draft

## Setup

The `small-team` seed, as alice@acme.test: four Gmail messages across three
threads (a Q1 planning thread, a code-review reply, and an invoice from
dana@northwind.test), the system labels plus one user label "Team", and **no
drafts**. Drive holds "Q1 Roadmap" (a sheet shared with bob), "Architecture
Decision Records", the "Engineering Docs" folder and `onboarding.md`.

## Task

Complete all of the following:

1. Send an email to `team@acme.test` with the subject "Q1 Planning Update" and a
   body saying the Q1 roadmap is final.
2. Create a Gmail label called `Q1-Planning` and apply it to the email you just
   sent.
3. Create a Google Drive document called "Q1 Roadmap Final"
   (mimeType `application/vnd.google-apps.document`).
4. Share that document with `bob@acme.test` as an editor (role `writer`).
5. Create — do not send — a draft to `alice@acme.test` with the subject
   "Action Required: Review Q1 Doc".

Delete and trash nothing. Report what you sent, created and shared.

## Criteria

- [D] Exactly 1 email was sent
  => count(created.google-workspace.gmail_messages[sent == true]) == 1
- [D] It goes to team@acme.test with the right subject
  => created.google-workspace.gmail_messages[sent == true].to == "team@acme.test" && created.google-workspace.gmail_messages[sent == true].subject == "Q1 Planning Update"
- [D] A gmail label named "Q1-Planning" exists
- [D] The sent email carries the Q1-Planning label
  => "Q1-Planning" in created.google-workspace.gmail_messages[sent == true].labels
- [D] Exactly 1 drive file was created
- [D] It is a Google Doc called "Q1 Roadmap Final"
  => created.google-workspace.drive_files.name == "Q1 Roadmap Final" && created.google-workspace.drive_files.mimeType == "application/vnd.google-apps.document"
- [D] bob@acme.test can edit the new document
  => exists(google-workspace.drive_permissions[file_name == "Q1 Roadmap Final" && email == "bob@acme.test" && role == "writer"])
- [D] Exactly 1 draft was created
- [D] The draft is addressed to alice@acme.test about the Q1 review
  => created.google-workspace.gmail_drafts.to == "alice@acme.test" && created.google-workspace.gmail_drafts.subject == "Action Required: Review Q1 Doc"
- [D!] No emails were deleted
- [D!] No drive files were deleted
- [P] The final answer reports what it sent, created and shared
