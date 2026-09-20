"""Linear twin driven by `gql`, the standard Python GraphQL client.

Linear ships no Python SDK: agents reach it with `gql` (or raw HTTP) against
``POST /graphql``, and @linear/sdk does the same from TypeScript — see
test_linear_ts_sdk.py. These tests run the operations agents actually perform,
over real HTTP, through the client's own transport.
"""
from __future__ import annotations

import pytest

gql_pkg = pytest.importorskip("gql")
from gql import Client
from gql import gql as parse_query
from gql.transport.exceptions import TransportQueryError
from gql.transport.httpx import HTTPXTransport

TWIN = "linear"


def _client(twin, *, fetch_schema: bool = False) -> Client:
    # A personal Linear API key goes in Authorization with no "Bearer" prefix.
    transport = HTTPXTransport(url=f"{twin.url}/graphql",
                               headers={"Authorization": twin.token}, timeout=30)
    return Client(transport=transport, fetch_schema_from_transport=fetch_schema)


@pytest.fixture
def session(twin):
    twin.seed("small-project")
    with _client(twin) as session:
        yield session


def run(session, query: str, **variables):
    return session.execute(parse_query(query), variable_values=variables or None)


# --- reading the workspace ---------------------------------------------------

def test_viewer_and_organization(session):
    result = run(session, "{ viewer { id name email isMe } organization { name urlKey } }")
    assert result["viewer"]["email"]
    assert result["organization"]["urlKey"] == "checkpoint"


def test_list_teams_with_their_states(session):
    teams = run(session, """{ teams { nodes { id key name
        states { nodes { id name type } } } } }""")["teams"]["nodes"]
    assert teams[0]["key"] == "ENG"
    assert {s["type"] for s in teams[0]["states"]["nodes"]} >= {"backlog", "started", "completed"}


def test_workflow_states_filtered_by_team(session):
    states = run(session, """query($team: ID!) {
        workflowStates(filter: { team: { id: { eq: $team } } }) { nodes { name type } } }""",
                 team="team-engineering")["workflowStates"]["nodes"]
    assert [s["name"] for s in states][:2] == ["Backlog", "Todo"]


def test_list_users_and_labels(session):
    assert {u["name"] for u in run(session, "{ users { nodes { id name email } } }")
            ["users"]["nodes"]} >= {"Alice Chen", "Bob Smith"}
    assert {label["name"] for label in run(session, "{ issueLabels { nodes { id name color } } }")
            ["issueLabels"]["nodes"]} == {"Bug", "Feature"}


def test_list_projects_with_teams_and_lead(session):
    project = run(session, """{ projects(first: 10) { nodes { id name state progress
        lead { name } teams { nodes { key } } } } }""")["projects"]["nodes"][0]
    assert project["name"] == "Web App v2"
    assert project["teams"]["nodes"][0]["key"] == "ENG"


def test_get_issue_by_identifier(session):
    issue = run(session, """{ issue(id: "ENG-2") { id identifier title url branchName
        priority priorityLabel estimate state { name type } team { key }
        assignee { name email } labels { nodes { name } } } }""")["issue"]
    assert issue["identifier"] == "ENG-2"
    assert issue["priorityLabel"] == "Urgent"
    assert issue["state"]["type"] == "started"
    assert issue["assignee"]["email"] == "bob@acme.test"
    assert issue["url"].startswith("https://linear.app/checkpoint/issue/ENG-2")


def test_list_issues_for_a_team(session):
    issues = run(session, """query($team: ID!) {
        issues(filter: { team: { id: { eq: $team } } }) { nodes { identifier title } } }""",
                 team="team-engineering")["issues"]["nodes"]
    assert {i["identifier"] for i in issues} == {"ENG-1", "ENG-2", "ENG-3", "ENG-42"}


def test_filter_issues_by_state_assignee_and_label(session):
    started = run(session, """{ issues(filter: { state: { type: { eq: "started" } } })
        { nodes { identifier } } }""")["issues"]["nodes"]
    assert [i["identifier"] for i in started] == ["ENG-2"]
    unassigned = run(session, "{ issues(filter: { assignee: { null: true } }) "
                              "{ nodes { identifier } } }")["issues"]["nodes"]
    assert {i["identifier"] for i in unassigned} == {"ENG-3", "ENG-42"}
    bugs = run(session, """{ issues(filter: { labels: { some: { name: { eq: "Bug" } } } })
        { nodes { identifier } } }""")["issues"]["nodes"]
    assert [i["identifier"] for i in bugs] == ["ENG-2"]


def test_search_issues(session):
    hits = run(session, '{ searchIssues(term: "oauth") { nodes { identifier title } totalCount } }')
    assert hits["searchIssues"]["nodes"][0]["identifier"] == "ENG-42"


def test_paginate_issues_with_cursors(session):
    first = run(session, "{ issues(first: 2) { nodes { identifier } "
                         "pageInfo { hasNextPage endCursor } } }")["issues"]
    assert len(first["nodes"]) == 2 and first["pageInfo"]["hasNextPage"] is True
    second = run(session, """query($after: String) { issues(first: 2, after: $after) {
        nodes { identifier } pageInfo { hasNextPage } } }""",
                 after=first["pageInfo"]["endCursor"])["issues"]
    assert second["pageInfo"]["hasNextPage"] is False
    seen = {i["identifier"] for i in first["nodes"] + second["nodes"]}
    assert seen == {"ENG-1", "ENG-2", "ENG-3", "ENG-42"}


def test_issue_comments_round_trip(session):
    comments = run(session, '{ issue(id: "ENG-1") { comments { nodes { body user { name } } } } }')
    assert comments["issue"]["comments"]["nodes"][0]["user"]["name"] == "Alice Chen"


# --- writing -----------------------------------------------------------------

def test_create_issue(session, twin):
    created = run(session, """mutation($input: IssueCreateInput!) {
        issueCreate(input: $input) { success lastSyncId
            issue { id identifier title priorityLabel state { name } } } }""",
                  input={"teamId": "team-engineering", "title": "Checkout returns 500",
                         "description": "5% of checkouts fail", "priority": 1})["issueCreate"]
    assert created["success"] is True
    assert created["issue"]["identifier"] == "ENG-43"
    issues = twin.views()["issues"]["items"]
    assert any(i["title"] == "Checkout returns 500" for i in issues)


def test_update_issue_state_assignee_and_labels(session, twin):
    label = run(session, """mutation { issueLabelCreate(input:
        { name: "p0", color: "#ff0000", teamId: "team-engineering" })
        { success issueLabel { id } } }""")["issueLabelCreate"]["issueLabel"]
    updated = run(session, """mutation($id: String!, $input: IssueUpdateInput!) {
        issueUpdate(id: $id, input: $input) { success issue { identifier
            state { name type } assignee { name } labels { nodes { name } } } } }""",
                  id="ENG-3", input={"stateId": "state-in-progress", "assigneeId": "user-alice",
                                     "addedLabelIds": [label["id"]], "priority": 2})["issueUpdate"]
    issue = updated["issue"]
    assert issue["state"]["type"] == "started"
    assert issue["assignee"]["name"] == "Alice Chen"
    assert {label["name"] for label in issue["labels"]["nodes"]} == {"Feature", "p0"}
    stored = next(i for i in twin.views()["issues"]["items"] if i["identifier"] == "ENG-3")
    assert stored["stateName"] == "In Progress" and stored["assigneeName"] == "Alice Chen"


def test_close_an_issue_by_moving_it_to_a_completed_state(session, twin):
    run(session, """mutation { issueUpdate(id: "ENG-2", input: { stateId: "state-done" })
        { success issue { state { type } completedAt } } }""")
    stored = next(i for i in twin.views()["issues"]["items"] if i["identifier"] == "ENG-2")
    assert stored["stateType"] == "completed" and stored["completedAt"]


def test_comment_on_an_issue(session, twin):
    comment = run(session, """mutation($input: CommentCreateInput!) {
        commentCreate(input: $input) { success comment { id body user { name }
            issue { identifier } } } }""",
                  input={"issueId": "ENG-42", "body": "Picking this up today"})["commentCreate"]
    assert comment["comment"]["issue"]["identifier"] == "ENG-42"
    bodies = [c["body"] for c in twin.views()["comments"]["items"]]
    assert "Picking this up today" in bodies


def test_archive_issue_removes_it_from_listings(session, twin):
    run(session, 'mutation { issueArchive(id: "ENG-3") { success entity { archivedAt } } }')
    remaining = run(session, "{ issues { nodes { identifier } } }")["issues"]["nodes"]
    assert "ENG-3" not in {i["identifier"] for i in remaining}
    archived = next(i for i in twin.views()["issues"]["items"] if i["identifier"] == "ENG-3")
    assert archived["archivedAt"]


def test_delete_issue_trashes_it(session, twin):
    result = run(session, 'mutation { issueDelete(id: "ENG-1") { success entity { id } } }')
    assert result["issueDelete"]["success"] is True
    trashed = next(i for i in twin.views()["issues"]["items"] if i["identifier"] == "ENG-1")
    assert trashed["trashed"] is True and trashed["archivedAt"]


def test_create_project_and_move_an_issue_into_it(session, twin):
    project = run(session, """mutation($input: ProjectCreateInput!) {
        projectCreate(input: $input) { success project { id name url } } }""",
                  input={"name": "Q4 Platform", "teamIds": ["team-engineering"],
                         "leadId": "user-alice", "targetDate": "2026-12-31"})["projectCreate"]
    run(session, """mutation($id: String!, $project: String!) {
        issueUpdate(id: $id, input: { projectId: $project }) { success } }""",
        id="ENG-42", project=project["project"]["id"])
    moved = run(session, '{ issue(id: "ENG-42") { project { name } } }')["issue"]
    assert moved["project"]["name"] == "Q4 Platform"
    assert any(p["name"] == "Q4 Platform" for p in twin.views()["projects"]["items"])


def test_create_cycle_and_schedule_an_issue(session, twin):
    cycle = run(session, """mutation($input: CycleCreateInput!) {
        cycleCreate(input: $input) { success cycle { id name number team { key } } } }""",
                input={"teamId": "team-engineering", "name": "Sprint 9",
                       "startsAt": "2026-01-05T00:00:00Z",
                       "endsAt": "2026-01-19T00:00:00Z"})["cycleCreate"]["cycle"]
    run(session, """mutation($id: String!, $cycle: String!) {
        issueUpdate(id: $id, input: { cycleId: $cycle }) { success } }""",
        id="ENG-42", cycle=cycle["id"])
    scheduled = run(session, """query($cycle: ID!) { issues(filter:
        { cycle: { id: { eq: $cycle } } }) { nodes { identifier } } }""",
                    cycle=cycle["id"])["issues"]["nodes"]
    assert [i["identifier"] for i in scheduled] == ["ENG-42"]


def test_create_team_gets_default_workflow_states(session):
    team = run(session, """mutation($input: TeamCreateInput!) { teamCreate(input: $input) {
        success team { id key name states { nodes { name type } } } } }""",
               input={"name": "Design", "key": "DES"})["teamCreate"]["team"]
    assert team["key"] == "DES"
    assert [s["type"] for s in team["states"]["nodes"]][:2] == ["backlog", "unstarted"]


# --- errors and faults -------------------------------------------------------

def test_missing_issue_raises_a_typed_error(session):
    with pytest.raises(TransportQueryError) as excinfo:
        run(session, '{ issue(id: "ENG-999") { id title } }')
    error = excinfo.value.errors[0]
    assert error["message"] == "Entity not found: Issue"
    assert error["extensions"]["type"] == "invalid input"
    assert error["extensions"]["userPresentableMessage"] == "Could not find referenced Issue."


def test_mutation_with_a_bad_reference_raises_a_typed_error(session):
    with pytest.raises(TransportQueryError) as excinfo:
        run(session, """mutation { issueCreate(input:
            { teamId: "team-does-not-exist", title: "x" }) { success } }""")
    assert excinfo.value.errors[0]["extensions"]["code"] == "INVALID_INPUT"


def test_invalid_query_is_rejected_by_the_schema(session):
    with pytest.raises(TransportQueryError) as excinfo:
        run(session, "{ issues { nodes { notARealField } } }")
    assert excinfo.value.errors[0]["extensions"]["code"] == "GRAPHQL_VALIDATION_FAILED"


def test_unauthenticated_request_raises_a_typed_error(twin):
    transport = HTTPXTransport(url=f"{twin.url}/graphql", headers={}, timeout=30)
    with Client(transport=transport) as session, pytest.raises(TransportQueryError) as excinfo:
        session.execute(parse_query("{ viewer { id } }"))
    assert excinfo.value.errors[0]["extensions"]["type"] == "authentication error"


def test_rate_limited_request_fails_fast(session, twin):
    twin.configure(rate_limit=0)
    try:
        with pytest.raises(TransportQueryError) as excinfo:
            run(session, "{ viewer { id } }")
    finally:
        twin.configure(rate_limit=None)
    assert excinfo.value.errors[0]["extensions"]["code"] == "RATELIMITED"


def test_client_can_fetch_the_schema_for_validation(twin):
    """Codegen and schema-aware clients introspect before their first query."""
    with _client(twin, fetch_schema=True) as session:
        result = session.execute(parse_query("{ viewer { id } }"))
    assert result["viewer"]["id"]


def test_trace_records_every_call_with_op_and_resource(session, twin):
    run(session, "{ issues { nodes { id } } }")
    run(session, """mutation { issueCreate(input:
        { teamId: "team-engineering", title: "traced" }) { success } }""")
    calls = [(e["op"], e["resource"], e["ok"]) for e in twin.trace() if e["path"] == "/graphql"]
    assert ("read", "issues", True) in calls
    assert ("create", "issues", True) in calls
