# Google Workspace Email & Drive Operations

## Prompt

You are an AI assistant helping the Acme engineering team manage their Google Workspace.

Complete all of the following tasks:

1. Send an email to the team (to: team@acme.test) with the subject "Q1 Planning Update" and body "The Q1 roadmap has been finalized. See the attached document for details."
2. Create a Gmail label called "Q1-Planning" and apply it to the email you just sent.
3. Create a new Google Drive document called "Q1 Roadmap Final" (mimeType: application/vnd.google-apps.document).
4. Share the new document with bob@acme.test as an editor (role: writer).
5. Search for files containing "roadmap" in their name and list what you find.
6. Create a draft email to alice@acme.test with subject "Action Required: Review Q1 Doc" and body "Please review the Q1 Roadmap Final document and provide feedback by EOW."

Use the seed state "small-team" which provides an existing email thread, labels, and Drive files.

## Success Criteria

- [D] at least 1 email exists
- [D] at least 1 gmail label exists
- [D] at least 1 drive file exists
- [D] at least 1 draft exists
- [P] An email with subject "Q1 Planning Update" was sent to the team
- [P] A label "Q1-Planning" was created and applied to the sent email
- [P] A new Google Drive document named "Q1 Roadmap Final" was created
- [P] The new document was shared with bob@acme.test as an editor
- [P] A draft email was created for alice@acme.test regarding the Q1 document review
- [P] The file search for "roadmap" returned relevant results

## Config

clones: google-workspace
seed: google-workspace=small-team
timeout: 120
tags: google-workspace, gmail, drive, email
