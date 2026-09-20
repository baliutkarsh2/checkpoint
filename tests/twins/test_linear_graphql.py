"""Linear twin GraphQL surface — the API every real Linear client speaks.

Covers the wire contract SDKs depend on: Linear's own schema validating the
document, Relay pagination, the filter language, mutations writing state, and
the error envelope @linear/sdk turns into typed exceptions.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from checkpoint.twins import linear as ln

TOKEN = ln.DEFAULT_BOOTSTRAP_TOKEN
# Linear takes a personal API key bare, an OAuth token with "Bearer".
H = {"Authorization": TOKEN}


@pytest.fixture(autouse=True)
def _reset_state():
    ln.TWIN.reset()
    yield


@pytest.fixture
def client():
    return TestClient(ln.app)


@pytest.fixture
def seeded(client):
    client.post("/_seed/small-project").raise_for_status()
    return client


def gql(client, query: str, variables: dict | None = None, headers: dict | None = None):
    response = client.post("/graphql", headers=H if headers is None else headers,
                           json={"query": query, "variables": variables or {}})
    return response, response.json()


def data(client, query: str, variables: dict | None = None) -> dict:
    response, body = gql(client, query, variables)
    assert response.status_code == 200, body
    assert "errors" not in body, body["errors"]
    return body["data"]


# --- auth --------------------------------------------------------------------

def test_missing_credential_returns_graphql_error_envelope(client):
    response = client.post("/graphql", json={"query": "{ viewer { id } }"})
    assert response.status_code == 401
    error = response.json()["errors"][0]
    assert error["message"] == "Authentication required, not authenticated"
    assert error["extensions"]["type"] == "authentication error"
    assert error["extensions"]["code"] == "AUTHENTICATION_ERROR"


def test_api_key_and_oauth_header_styles_both_work(client):
    assert data(client, "{ viewer { id } }")["viewer"]["id"]
    response, body = gql(client, "{ viewer { id } }", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200 and "errors" not in body


def test_strict_auth_rejects_a_foreign_key(client):
    client.post("/_config", json={"strict_auth": True})
    response, body = gql(client, "{ viewer { id } }", headers={"Authorization": "lin_api_nope"})
    assert response.status_code == 401
    assert body["errors"][0]["extensions"]["type"] == "authentication error"


# --- queries -----------------------------------------------------------------

def test_viewer_and_organization(seeded):
    result = data(seeded, "{ viewer { id name email isMe } organization { name urlKey } }")
    assert result["viewer"]["isMe"] is True
    assert result["organization"]["urlKey"] == "checkpoint"


def test_issue_by_identifier_and_by_uuid(seeded):
    by_identifier = data(seeded, '{ issue(id: "ENG-2") { id identifier title state { name } '
                                 'assignee { name } labels { nodes { name } } } }')["issue"]
    assert by_identifier["identifier"] == "ENG-2"
    assert by_identifier["state"]["name"] == "In Progress"
    assert by_identifier["assignee"]["name"] == "Bob Smith"
    assert [label["name"] for label in by_identifier["labels"]["nodes"]] == ["Bug"]
    by_uuid = data(seeded, "query($id: String!) { issue(id: $id) { identifier } }",
                   {"id": by_identifier["id"]})
    assert by_uuid["issue"]["identifier"] == "ENG-2"


def test_unknown_issue_is_a_200_with_an_invalid_input_error(seeded):
    response, body = gql(seeded, '{ issue(id: "ENG-999") { id } }')
    assert response.status_code == 200
    assert body["data"] is None
    error = body["errors"][0]
    assert error["message"] == "Entity not found: Issue"
    assert error["path"] == ["issue"]
    assert error["extensions"]["code"] == "INVALID_INPUT"
    assert error["extensions"]["userPresentableMessage"] == "Could not find referenced Issue."


def test_team_exposes_its_states_in_board_order(seeded):
    team = data(seeded, "{ teams { nodes { key states { nodes { name type } } } } }")
    names = [s["name"] for s in team["teams"]["nodes"][0]["states"]["nodes"]]
    assert names == ["Backlog", "Todo", "In Progress", "In Review", "Done", "Canceled"]


def test_nested_relations_resolve_from_state(seeded):
    result = data(seeded, """{ issues(filter: { project: { name: { eq: "Web App v2" } } }) {
        nodes { identifier project { name lead { name } } team { key members { nodes { name } } }
                comments { nodes { body user { name } } } } } }""")
    identifiers = {node["identifier"] for node in result["issues"]["nodes"]}
    assert identifiers == {"ENG-1", "ENG-2", "ENG-3"}
    first = next(n for n in result["issues"]["nodes"] if n["identifier"] == "ENG-1")
    assert first["project"]["lead"]["name"] == "Alice Chen"
    assert first["comments"]["nodes"][0]["user"]["name"] == "Alice Chen"


def test_unmodelled_fields_resolve_empty_instead_of_erroring(seeded):
    result = data(seeded, """{ issue(id: "ENG-1") { attachments { nodes { id } }
        history { nodes { id } } favorite { id } reactionData botActor { id } } }""")
    assert result["issue"]["attachments"]["nodes"] == []
    assert result["issue"]["favorite"] is None


# --- filters -----------------------------------------------------------------

@pytest.mark.parametrize(("filter_", "expected"), [
    ('{ state: { type: { eq: "started" } } }', {"ENG-2"}),
    ('{ team: { key: { eq: "ENG" } }, priority: { in: [1] } }', {"ENG-2"}),
    ('{ assignee: { null: true } }', {"ENG-3", "ENG-42"}),
    ('{ assignee: { email: { eq: "bob@acme.test" } } }', {"ENG-2"}),
    ('{ labels: { some: { name: { eq: "Bug" } } } }', {"ENG-2"}),
    ('{ title: { containsIgnoreCase: "LOGIN" } }', {"ENG-2", "ENG-42"}),
    ('{ searchableContent: { contains: "dark mode" } }', {"ENG-3"}),
    ('{ createdAt: { gte: "2025-01-24T00:00:00Z" } }', {"ENG-3", "ENG-42"}),
    ('{ or: [{ number: { eq: 42 } }, { estimate: { gte: 3 } }] }', {"ENG-1", "ENG-42"}),
    ('{ project: { null: true } }', {"ENG-42"}),
])
def test_issue_filters(seeded, filter_, expected):
    result = data(seeded, f"{{ issues(filter: {filter_}) {{ nodes {{ identifier }} }} }}")
    assert {node["identifier"] for node in result["issues"]["nodes"]} == expected


def test_filter_on_a_field_the_twin_does_not_model_does_not_drop_records(seeded):
    result = data(seeded, "{ issues(filter: { hasBlockedByRelations: { eq: true } }) "
                          "{ nodes { identifier } } }")
    assert len(result["issues"]["nodes"]) == 4


# --- pagination --------------------------------------------------------------

def test_pages_forward_with_first_and_after(seeded):
    page = data(seeded, "{ issues(first: 2) { nodes { identifier } "
                        "pageInfo { hasNextPage endCursor } } }")["issues"]
    assert len(page["nodes"]) == 2
    assert page["pageInfo"]["hasNextPage"] is True
    rest = data(seeded, "query($c: String) { issues(first: 10, after: $c) "
                        "{ nodes { identifier } pageInfo { hasNextPage } } }",
                {"c": page["pageInfo"]["endCursor"]})["issues"]
    assert rest["pageInfo"]["hasNextPage"] is False
    seen = [n["identifier"] for n in page["nodes"]] + [n["identifier"] for n in rest["nodes"]]
    assert sorted(seen) == ["ENG-1", "ENG-2", "ENG-3", "ENG-42"]


def test_pages_backward_with_last_and_before(seeded):
    all_issues = data(seeded, "{ issues { edges { cursor node { identifier } } } }")["issues"]
    last_cursor = all_issues["edges"][-1]["cursor"]
    page = data(seeded, "query($c: String) { issues(last: 1, before: $c) "
                        "{ nodes { identifier } pageInfo { hasPreviousPage } } }",
                {"c": last_cursor})["issues"]
    assert page["nodes"][0]["identifier"] == all_issues["edges"][-2]["node"]["identifier"]


def test_archived_issues_are_hidden_unless_asked_for(seeded):
    data(seeded, 'mutation { issueArchive(id: "ENG-3") { success } }')
    assert {n["identifier"] for n in data(seeded, "{ issues { nodes { identifier } } }")
            ["issues"]["nodes"]} == {"ENG-1", "ENG-2", "ENG-42"}
    with_archived = data(seeded, "{ issues(includeArchived: true) { nodes { identifier } } }")
    assert len(with_archived["issues"]["nodes"]) == 4


# --- mutations ---------------------------------------------------------------

def test_issue_create_update_comment_archive_round_trip(seeded):
    created = data(seeded, """mutation($input: IssueCreateInput!) {
        issueCreate(input: $input) { success lastSyncId
            issue { id identifier title priorityLabel state { name } team { key } } } }""",
                   {"input": {"teamId": "team-engineering", "title": "Checkout 500s",
                              "description": "5% of checkouts fail", "priority": 1}})["issueCreate"]
    assert created["success"] is True
    issue = created["issue"]
    assert issue["identifier"] == "ENG-43"  # the seed's counter continues
    assert issue["priorityLabel"] == "Urgent"
    assert issue["state"]["name"] == "Backlog"

    updated = data(seeded, """mutation($id: String!, $input: IssueUpdateInput!) {
        issueUpdate(id: $id, input: $input) { success
            issue { state { name type } assignee { name } estimate } } }""",
                   {"id": issue["identifier"],
                    "input": {"stateId": "state-done", "assigneeId": "user-bob", "estimate": 2}})
    assert updated["issueUpdate"]["issue"]["state"]["type"] == "completed"
    assert updated["issueUpdate"]["issue"]["assignee"]["name"] == "Bob Smith"

    comment = data(seeded, """mutation($input: CommentCreateInput!) {
        commentCreate(input: $input) { success comment { body issue { identifier } } } }""",
                   {"input": {"issueId": issue["id"], "body": "Shipped in 1.2.3"}})
    assert comment["commentCreate"]["comment"]["issue"]["identifier"] == "ENG-43"

    archived = data(seeded, "mutation($id: String!) { issueArchive(id: $id) "
                            "{ success entity { archivedAt } } }", {"id": issue["id"]})
    assert archived["issueArchive"]["entity"]["archivedAt"]

    stored = ln.STATE["issues"][issue["id"]]
    assert stored["stateId"] == "state-done" and stored["assigneeId"] == "user-bob"
    assert stored["completedAt"] and stored["archivedAt"] and stored["commentCount"] == 1


def test_issue_delete_trashes_and_unarchive_restores(seeded):
    deleted = data(seeded, 'mutation { issueDelete(id: "ENG-1") { success entity { id } } }')
    assert deleted["issueDelete"]["success"] is True
    assert ln.STATE["issues"]["issue-001"]["trashed"] is True
    data(seeded, 'mutation { issueUnarchive(id: "ENG-1") { success } }')
    assert ln.STATE["issues"]["issue-001"]["archivedAt"] is None


def test_label_project_cycle_and_team_mutations_write_state(seeded):
    label = data(seeded, """mutation { issueLabelCreate(input:
        { name: "p0", color: "#ff0000", teamId: "team-engineering" })
        { success issueLabel { id name } } }""")["issueLabelCreate"]["issueLabel"]
    assert any(v["name"] == "p0" for v in ln.STATE["labels"].values())

    data(seeded, """mutation($id: String!) { issueUpdate(id: "ENG-3",
        input: { addedLabelIds: [$id] }) { success } }""", {"id": label["id"]})
    assert label["id"] in ln.STATE["issues"]["issue-003"]["labelIds"]

    project = data(seeded, """mutation { projectCreate(input:
        { name: "Q4 Platform", teamIds: ["team-engineering"], leadId: "user-alice" })
        { success project { id name lead { name } teams { nodes { key } } } } }""")
    assert project["projectCreate"]["project"]["lead"]["name"] == "Alice Chen"

    team = data(seeded, 'mutation { teamCreate(input: { name: "Design", key: "DES" }) '
                        "{ success team { id key states { nodes { name } } } } }")
    assert len(team["teamCreate"]["team"]["states"]["nodes"]) == 5

    cycle = data(seeded, """mutation { cycleCreate(input: { teamId: "team-engineering",
        name: "Sprint 9", startsAt: "2026-01-05T00:00:00Z", endsAt: "2026-01-19T00:00:00Z" })
        { success cycle { id name number team { key } } } }""")
    assert cycle["cycleCreate"]["cycle"]["number"] == 1


def test_mutation_referencing_a_missing_entity_reports_invalid_input(seeded):
    response, body = gql(seeded, """mutation { issueCreate(input:
        { teamId: "team-nope", title: "x" }) { success } }""")
    assert response.status_code == 200
    assert body["errors"][0]["message"] == "Entity not found: Team"
    assert body["errors"][0]["extensions"]["type"] == "invalid input"


def test_search_issues_matches_title_and_description(seeded):
    result = data(seeded, '{ searchIssues(term: "oauth") { nodes { identifier } totalCount } }')
    assert [n["identifier"] for n in result["searchIssues"]["nodes"]] == ["ENG-42"]
    assert result["searchIssues"]["totalCount"] == 1


# --- document errors ---------------------------------------------------------

def test_syntax_error_is_a_400_parse_failure(client):
    response, body = gql(client, "query { issue(id: ")
    assert response.status_code == 400
    assert body["errors"][0]["extensions"]["code"] == "GRAPHQL_PARSE_FAILED"


def test_unknown_field_is_a_400_validation_failure(client):
    response, body = gql(client, "{ issues { nodes { notAField } } }")
    assert response.status_code == 400
    assert body["errors"][0]["extensions"]["code"] == "GRAPHQL_VALIDATION_FAILED"
    assert "notAField" in body["errors"][0]["message"]


def test_variable_of_the_wrong_type_is_rejected_like_the_real_api(client):
    # TeamFilter.id is an IDComparator: a String! variable is not allowed there.
    response, body = gql(client, "query($t: String!) { workflowStates(filter: "
                                 "{ team: { id: { eq: $t } } }) { nodes { id } } }",
                         {"t": "team-engineering"})
    assert response.status_code == 400
    assert body["errors"][0]["extensions"]["code"] == "GRAPHQL_VALIDATION_FAILED"


def test_introspection_works_for_codegen_clients(client):
    result = data(client, "{ __schema { queryType { name } } __type(name: \"Issue\") { name } }")
    assert result["__schema"]["queryType"]["name"] == "Query"
    assert result["__type"]["name"] == "Issue"


# --- faults ------------------------------------------------------------------

def test_rate_limit_is_reported_the_way_linear_reports_it(client):
    client.post("/_config", json={"rate_limit": 0})
    response, body = gql(client, "{ viewer { id } }")
    # Linear answers a rate-limited GraphQL request with HTTP 400 + RATELIMITED.
    assert response.status_code == 400
    assert body["errors"][0]["extensions"]["code"] == "RATELIMITED"
    assert body["errors"][0]["extensions"]["type"] == "ratelimited"
    assert response.headers["Retry-After"] == "60"
    assert response.headers["X-RateLimit-Requests-Remaining"] == "0"


def test_read_only_refuses_mutations_as_forbidden(seeded):
    seeded.post("/_config", json={"read_only": True})
    response, body = gql(seeded, 'mutation { issueArchive(id: "ENG-1") { success } }')
    assert response.status_code == 403
    assert body["errors"][0]["extensions"]["type"] == "forbidden"
    assert ln.STATE["issues"]["issue-001"]["archivedAt"] is None


# --- trace and views ---------------------------------------------------------

def test_trace_classifies_graphql_operations(seeded):
    data(seeded, "{ issues { nodes { id } } }")
    data(seeded, 'mutation { issueCreate(input: { teamId: "team-engineering", title: "t" }) '
                 "{ success } }")
    data(seeded, 'mutation { issueArchive(id: "ENG-1") { success } }')
    data(seeded, 'mutation { commentCreate(input: { issueId: "ENG-2", body: "b" }) { success } }')
    trace = [(e["op"], e["resource"]) for e in seeded.get("/_trace").json()
             if e["path"] == "/graphql"]
    assert trace == [("read", "issues"), ("create", "issues"),
                     ("delete", "issues"), ("create", "comments")]


def test_failed_graphql_calls_are_marked_not_ok(seeded):
    gql(seeded, '{ issue(id: "ENG-999") { id } }')
    assert seeded.get("/_trace").json()[-1]["ok"] is False


def test_views_denormalize_records_for_assertions(seeded):
    views = seeded.get("/_views").json()["collections"]
    assert views["issues"]["tombstone"] == "archivedAt"
    assert views["issues"]["nouns"] == ["issue", "issues"]
    issue = next(i for i in views["issues"]["items"] if i["identifier"] == "ENG-2")
    assert issue["stateName"] == "In Progress"
    assert issue["assigneeName"] == "Bob Smith"
    assert issue["teamKey"] == "ENG"
    assert issue["labelNames"] == ["Bug"]
    assert issue["projectName"] == "Web App v2"
    comment = views["comments"]["items"][0]
    assert comment["issueIdentifier"] == "ENG-1" and comment["userName"] == "Alice Chen"


# --- unknown routes ----------------------------------------------------------

def test_unknown_route_uses_the_services_error_shape(client):
    body = client.get("/v1/nope", headers=H).json()
    assert "detail" not in body and body["error"]


def test_get_on_the_graphql_endpoint_reports_a_graphql_error(client):
    response = client.get("/graphql", headers=H)
    assert response.status_code == 405
    assert response.json()["errors"][0]["extensions"]["type"] == "invalid input"
