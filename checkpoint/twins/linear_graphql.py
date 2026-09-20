"""Linear's GraphQL API, served from the twin's state.

Linear has no REST API: every official client — @linear/sdk, the Zapier and
MCP integrations, `gql`-based Python agents — POSTs a GraphQL document to
``/graphql``. This module serves Linear's own published SDL
(``linear_schema.graphql``, vendored from the @linear/sdk package) with
graphql-core, so any *valid* Linear query type-checks here exactly as it does
in production, and any invalid one is rejected the same way.

Resolvers back the entities agents actually work with (issues, teams, users,
workflow states, labels, projects, cycles, comments, the organization).
Everything else in the 1,200-type schema resolves to the empty value its type
allows — null, an empty list, an empty connection — so a query that asks for a
field the twin does not model still returns data instead of an error.
"""
from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from graphql import (
    ExecutionResult,
    GraphQLEnumType,
    GraphQLError,
    GraphQLField,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLSyntaxError,
    OperationType,
    build_schema,
    execute_sync,
    get_operation_ast,
    parse,
    validate,
)

from checkpoint.twins import linear_store as store

SCHEMA_PATH = Path(__file__).parent / "linear_schema.graphql"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 250

Resolver = Callable[..., Any]
"""A field resolver, called by graphql-core as ``(source, info, **arguments)``."""

Records = Callable[[dict, Any], "list[dict]"]
"""Picks the records behind a connection field, from ``(state, source)``."""


# --- error envelopes ---------------------------------------------------------

def error_body(message: str, *, type_: str, code: str, status: int,
               user_message: str | None = None) -> dict:
    """Linear's error envelope: the shape @linear/sdk turns into typed errors."""
    return {"errors": [{"message": message, "extensions": _extensions(
        type_, code, status, user_message or message)}]}


def _extensions(type_: str, code: str, status: int, user_message: str) -> dict:
    return {"type": type_, "code": code, "statusCode": status, "userError": status < 500,
            "userPresentableMessage": user_message}


AUTH = ("authentication error", "AUTHENTICATION_ERROR", 401)
FORBIDDEN = ("forbidden", "FORBIDDEN", 403)
RATELIMITED = ("ratelimited", "RATELIMITED", 429)
INTERNAL = ("internal error", "INTERNAL_SERVER_ERROR", 500)
INVALID_INPUT = ("invalid input", "INVALID_INPUT", 400)
_PARSE_FAILED = ("graphql error", "GRAPHQL_PARSE_FAILED", 400)
_VALIDATION_FAILED = ("graphql error", "GRAPHQL_VALIDATION_FAILED", 400)
_BAD_VARIABLES = ("invalid input", "BAD_USER_INPUT", 400)


# --- schema ------------------------------------------------------------------

@lru_cache(maxsize=1)
def schema() -> GraphQLSchema:
    """Linear's published schema, with the twin's resolvers attached.

    Built on first use: parsing 50k lines of SDL costs over a second, and a
    scenario that never touches Linear should not pay for it.
    """
    built = build_schema(SCHEMA_PATH.read_text(encoding="utf-8"), assume_valid=True)
    for type_name, fields in _resolvers().items():
        graphql_type = built.type_map.get(type_name)
        if not isinstance(graphql_type, GraphQLObjectType):  # pragma: no cover - schema drift
            continue
        for field_name, resolve in fields.items():
            field: GraphQLField | None = graphql_type.fields.get(field_name)
            if field is not None:
                field.resolve = resolve
    return built


# --- request handling --------------------------------------------------------

def execute(state: dict, payload: Any) -> tuple[int, dict]:
    """Run one GraphQL request; returns ``(http_status, response_body)``."""
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
        return 400, error_body("Must provide query string.", type_="graphql error",
                               code="GRAPHQL_PARSE_FAILED", status=400)
    variables = payload.get("variables") or {}
    if not isinstance(variables, dict):
        return 400, error_body("Variables must be an object.", type_="graphql error",
                               code="BAD_USER_INPUT", status=400)
    try:
        document = parse(payload["query"])
    except GraphQLSyntaxError as exc:
        return 400, {"errors": [_format(exc, _PARSE_FAILED)]}

    errors = validate(schema(), document)
    if errors:
        return 400, {"errors": [_format(e, _VALIDATION_FAILED) for e in errors]}

    result = execute_sync(
        schema(),
        document,
        context_value=state,
        variable_values=variables,
        operation_name=payload.get("operationName"),
        field_resolver=_resolve_field,
        type_resolver=_resolve_type,
    )
    return _response(result)


def _response(result: ExecutionResult) -> tuple[int, dict]:
    if not result.errors:
        return 200, {"data": result.data}
    # Errors raised before any field ran (unknown operation, bad variables) are
    # request errors: Linear answers those with 400 and no data key at all.
    if result.data is None and all(e.path is None for e in result.errors):
        return 400, {"errors": [_format(e, _BAD_VARIABLES) for e in result.errors]}
    return 200, {"data": result.data,
                 "errors": [_format(e, INTERNAL) for e in result.errors]}


def _format(error: GraphQLError, fallback: tuple[str, str, int]) -> dict:
    """One GraphQL error in Linear's shape, keeping locations and path."""
    original = getattr(error, "original_error", None)
    extensions = dict(error.extensions or {})
    if isinstance(original, (store.LinearNotFound, store.LinearInvalidInput)):
        extensions = _extensions(*INVALID_INPUT, original.user_message)
    elif not extensions:
        extensions = _extensions(*fallback, error.message)
    body: dict[str, Any] = {"message": error.message, "extensions": extensions}
    if error.locations:
        body["locations"] = [{"line": loc.line, "column": loc.column} for loc in error.locations]
    if error.path:
        body["path"] = list(error.path)
    return body


@lru_cache(maxsize=256)
def classify(query: str, operation_name: str | None) -> tuple[str, str] | None:
    """Map a GraphQL document to the ``(op, resource)`` the trace records.

    Cached because it runs per request on documents an SDK sends over and over,
    and it has to parse them a second time to see the operation.
    """
    try:
        operation = get_operation_ast(parse(query), operation_name)
    except GraphQLSyntaxError:
        return None
    if operation is None:
        return None
    fields = [s for s in operation.selection_set.selections if hasattr(s, "name")]
    if not fields:
        return None
    root = fields[0].name.value
    if operation.operation is OperationType.MUTATION:
        entity, verb = _split_mutation(root)
        return _MUTATION_OPS.get(verb, "update"), _resource(entity)
    return "read", _resource(root)


# Root-field / mutation prefix -> the state collection it acts on, so the trace
# says "create issues" whether the agent used GraphQL or the REST surface.
_RESOURCE_OF = {
    "issue": "issues", "issueSearch": "issues", "searchIssues": "issues",
    "comment": "comments", "team": "teams", "user": "users", "viewer": "users",
    "workflowState": "workflow_states", "issueLabel": "labels",
    "project": "projects", "cycle": "cycles", "organization": "organization",
}
_MUTATION_OPS = {"Create": "create", "Update": "update", "Delete": "delete",
                 "Archive": "delete", "Unarchive": "update"}
# Longest first so issueLabelCreate resolves to issueLabel, not issue.
_ENTITIES_BY_LENGTH = tuple(sorted(_RESOURCE_OF, key=len, reverse=True))


def _resource(name: str) -> str:
    if name.startswith("__"):  # introspection: a query about the schema, not a collection
        return name
    singular = name[:-1] if name.endswith("s") and name[:-1] in _RESOURCE_OF else name
    return _RESOURCE_OF.get(singular, f"{name}s" if not name.endswith("s") else name)


def _split_mutation(name: str) -> tuple[str, str]:
    """``issueLabelCreate`` -> ``("issueLabel", "Create")``."""
    for entity in _ENTITIES_BY_LENGTH:
        if name.startswith(entity) and name[len(entity):len(entity) + 1].isupper():
            return entity, name[len(entity):]
    # An entity the twin does not model still has Linear's verb suffix.
    for verb in _MUTATION_OPS:
        if name.endswith(verb) and len(name) > len(verb):
            return name[:-len(verb)], verb
    return name, ""


# --- generic resolution ------------------------------------------------------

def _resolve_field(source: Any, info: Any, **_args: Any) -> Any:
    """Read the field off the record, falling back to the type's empty value."""
    value = source.get(info.field_name) if isinstance(source, dict) else None
    if value is None:
        return _empty(info.return_type)
    return value


def _resolve_type(value: Any, info: Any, abstract_type: Any) -> str | None:
    """Pick a concrete type for interfaces/unions the twin does not model."""
    if isinstance(value, dict) and value.get("__typename"):
        return value["__typename"]
    possible = info.schema.get_possible_types(abstract_type)
    return possible[0].name if possible else None


def _empty(type_: Any) -> Any:
    """The empty value a non-null field can be answered with, else None.

    Linear's schema marks most fields non-null, so a twin that returned null
    for everything it does not model would fail the whole query. An empty
    object is enough: its own fields resolve to empty values in turn, which
    makes an unmodelled connection an empty connection.
    """
    if not isinstance(type_, GraphQLNonNull):
        return None
    inner = type_.of_type
    if isinstance(inner, GraphQLList):
        return []
    if isinstance(inner, GraphQLScalarType):
        return _EMPTY_SCALARS.get(inner.name, "")
    if isinstance(inner, GraphQLEnumType):
        return next(iter(inner.values))
    return {}


_EMPTY_SCALARS: dict[str, Any] = {
    "Int": 0, "Float": 0.0, "Boolean": False, "String": "", "ID": "",
    "DateTime": "1970-01-01T00:00:00.000Z", "TimelessDate": "1970-01-01",
    "JSON": {}, "JSONObject": {}, "UUID": "00000000-0000-0000-0000-000000000000",
}


# --- connections -------------------------------------------------------------

def _cursor(record: dict) -> str:
    return base64.b64encode(str(record.get("id", "")).encode()).decode()


def _cursor_index(records: list[dict], cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        wanted = base64.b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeDecodeError):  # a malformed cursor matches nothing
        wanted = cursor
    return next((i for i, r in enumerate(records) if str(r.get("id")) == wanted), None)


def _connection(state: dict, entity: str, records: list[dict], args: dict) -> dict:
    """Relay-style page over ``records``, applying Linear's filter arguments."""
    items = [r for r in records if args.get("includeArchived") or not r.get("archivedAt")]
    if args.get("filter"):
        items = [r for r in items if matches(state, entity, r, args["filter"])]
    order = args.get("orderBy")
    if order is None and entity in _ORDER_BY_POSITION:
        # A team's states are a board, not a feed: they read in board order.
        items.sort(key=lambda r: (r.get("position") or 0.0, str(r.get("id") or "")))
    else:
        # Linear orders connections newest-first; id breaks ties reproducibly.
        key = order or "createdAt"
        items.sort(key=lambda r: (str(r.get(key) or ""), str(r.get("id") or "")), reverse=True)
    return _page(items, args)


_ORDER_BY_POSITION = frozenset({"workflowState"})


def _page(items: list[dict], args: dict) -> dict:
    after_index = _cursor_index(items, args.get("after"))
    before_index = _cursor_index(items, args.get("before"))
    start = after_index + 1 if after_index is not None else 0
    end = before_index if before_index is not None else len(items)
    window = items[start:end]
    first, last = args.get("first"), args.get("last")
    if first is None and last is not None:
        size = min(int(last), MAX_PAGE_SIZE)
        nodes = window[-size:] if size else []
        has_previous, has_next = len(window) > size, end < len(items)
    else:
        size = min(int(first) if first is not None else DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
        nodes = window[:size]
        has_previous, has_next = start > 0, len(window) > size
    return {
        "nodes": nodes,
        "edges": [{"node": node, "cursor": _cursor(node)} for node in nodes],
        "pageInfo": {
            "hasNextPage": has_next,
            "hasPreviousPage": has_previous,
            "startCursor": _cursor(nodes[0]) if nodes else None,
            "endCursor": _cursor(nodes[-1]) if nodes else None,
        },
    }


def _collection(state: dict, name: str) -> list[dict]:
    return list((state.get(name) or {}).values())


def _connection_field(entity: str, records: Records) -> Resolver:
    """A connection resolver over ``records(state, source)``."""
    def resolve(source: Any, info: Any, **args: Any) -> dict:
        return _connection(info.context, entity, records(info.context, source), args)
    return resolve


# --- filters -----------------------------------------------------------------

@dataclass(frozen=True)
class _Entity:
    """How one entity's filter fields map onto its stored record.

    ``scalars`` maps a filter field to a record key (or to a function deriving
    the value), ``relations`` to ``(foreign key, state collection, entity)``,
    and ``collections`` to ``(records of, entity)``.
    """

    scalars: dict[str, Any] = field(default_factory=dict)
    relations: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    collections: dict[str, tuple[Any, str]] = field(default_factory=dict)


def _issue_text(state: dict, issue: dict) -> str:
    return f"{issue.get('title') or ''} {issue.get('description') or ''}"


def _issue_labels(state: dict, issue: dict) -> list[dict]:
    return [state["labels"][lid] for lid in issue.get("labelIds") or [] if lid in state["labels"]]


def _issue_comments(state: dict, issue: dict) -> list[dict]:
    return store.issue_comments(state, issue["id"])


def _issue_children(state: dict, issue: dict) -> list[dict]:
    return [i for i in state["issues"].values() if i.get("parentId") == issue.get("id")]


def _team_issues(state: dict, team: dict) -> list[dict]:
    return [i for i in state["issues"].values() if i.get("teamId") == team.get("id")]


def _team_members(state: dict, team: dict) -> list[dict]:
    # The twin models a single workspace: every active user is on every team.
    return [u for u in state["users"].values() if u.get("active", True)]


def _entities() -> dict[str, _Entity]:
    common = {"id": "id", "createdAt": "createdAt", "updatedAt": "updatedAt",
              "archivedAt": "archivedAt"}
    return {
        "issue": _Entity(
            scalars={**common, "number": "number", "title": "title",
                     "description": "description", "priority": "priority",
                     "estimate": "estimate", "dueDate": "dueDate",
                     "completedAt": "completedAt", "canceledAt": "canceledAt",
                     "startedAt": "startedAt", "searchableContent": _issue_text},
            relations={"team": ("teamId", "teams", "team"),
                       "state": ("stateId", "workflow_states", "workflowState"),
                       "assignee": ("assigneeId", "users", "user"),
                       "creator": ("creatorId", "users", "user"),
                       "project": ("projectId", "projects", "project"),
                       "cycle": ("cycleId", "cycles", "cycle"),
                       "parent": ("parentId", "issues", "issue")},
            collections={"labels": (_issue_labels, "issueLabel"),
                         "comments": (_issue_comments, "comment"),
                         "children": (_issue_children, "issue")}),
        "team": _Entity(
            scalars={**common, "name": "name", "key": "key", "description": "description",
                     "private": "private"},
            relations={},
            collections={"issues": (_team_issues, "issue"),
                         "members": (_team_members, "user")}),
        "user": _Entity(
            scalars={**common, "name": "name", "displayName": "displayName",
                     "email": "email", "active": "active", "admin": "admin"},
            relations={}, collections={}),
        "workflowState": _Entity(
            scalars={**common, "name": "name", "type": "type", "position": "position",
                     "description": "description"},
            relations={"team": ("teamId", "teams", "team")}, collections={}),
        "issueLabel": _Entity(
            scalars={**common, "name": "name", "description": "description",
                     "isGroup": "isGroup"},
            relations={"team": ("teamId", "teams", "team"),
                       "creator": ("creatorId", "users", "user")},
            collections={}),
        "project": _Entity(
            scalars={**common, "name": "name", "description": "description",
                     "state": "state", "priority": "priority", "slugId": "slugId",
                     "startDate": "startDate", "targetDate": "targetDate",
                     "completedAt": "completedAt", "canceledAt": "canceledAt"},
            relations={"lead": ("leadId", "users", "user")}, collections={}),
        "cycle": _Entity(
            scalars={**common, "name": "name", "number": "number",
                     "startsAt": "startsAt", "endsAt": "endsAt",
                     "completedAt": "completedAt"},
            relations={"team": ("teamId", "teams", "team")}, collections={}),
        "comment": _Entity(
            scalars={**common, "body": "body"},
            relations={"user": ("userId", "users", "user"),
                       "issue": ("issueId", "issues", "issue")},
            collections={}),
    }


ENTITIES = _entities()


def matches(state: dict, entity: str, record: dict, filter_: dict) -> bool:
    """Whether ``record`` satisfies a Linear filter object.

    Filter fields the twin does not model are ignored rather than treated as
    non-matching: dropping records for an unmodelled field would hide data the
    agent legitimately asked for.
    """
    spec = ENTITIES[entity]
    for key, condition in (filter_ or {}).items():
        if condition is None:
            continue
        if key == "and":
            ok = all(matches(state, entity, record, sub) for sub in condition)
        elif key == "or":
            ok = any(matches(state, entity, record, sub) for sub in condition)
        elif key in spec.relations:
            foreign_key, collection, target = spec.relations[key]
            related = (state.get(collection) or {}).get(record.get(foreign_key))
            ok = _matches_related(state, target, related, condition)
        elif key in spec.collections:
            getter, target = spec.collections[key]
            ok = _matches_collection(state, target, getter(state, record), condition)
        elif key in spec.scalars:
            accessor = spec.scalars[key]
            value = accessor(state, record) if callable(accessor) else record.get(accessor)
            ok = _compare(value, condition)
        else:
            ok = True
        if not ok:
            return False
    return True


def _matches_related(state: dict, entity: str, related: dict | None, condition: dict) -> bool:
    rest = {k: v for k, v in condition.items() if k != "null"}
    if "null" in condition and bool(condition["null"]) is (related is not None):
        return False
    if not rest:
        return True
    return related is not None and matches(state, entity, related, rest)


def _matches_collection(state: dict, entity: str, items: list[dict], condition: dict) -> bool:
    direct = {k: v for k, v in condition.items()
              if k not in ("some", "every", "length", "null", "and", "or")}
    for key, value in condition.items():
        if key == "some":
            if not any(matches(state, entity, i, value) for i in items):
                return False
        elif key == "every":
            if not all(matches(state, entity, i, value) for i in items):
                return False
        elif key == "length":
            if not _compare(len(items), value):
                return False
        elif key == "null":
            if bool(value) is bool(items):
                return False
        elif key == "and":
            if not all(_matches_collection(state, entity, items, sub) for sub in value):
                return False
        elif key == "or":
            if not any(_matches_collection(state, entity, items, sub) for sub in value):
                return False
    # Bare comparators on a collection filter mean "some item matches".
    if direct and not any(matches(state, entity, i, direct) for i in items):
        return False
    return True


def _compare(value: Any, comparator: dict) -> bool:
    for op, operand in (comparator or {}).items():
        if operand is None and op != "null":
            continue
        if not _apply(op, value, operand):
            return False
    return True


def _apply(op: str, value: Any, operand: Any) -> bool:
    text = "" if value is None else str(value)
    lowered, operand_lower = text.lower(), str(operand).lower()
    if op == "eq":
        return _equal(value, operand)
    if op == "neq":
        return not _equal(value, operand)
    if op == "in":
        return any(_equal(value, o) for o in operand or [])
    if op == "nin":
        return not any(_equal(value, o) for o in operand or [])
    if op in ("lt", "lte", "gt", "gte"):
        return _ordered(op, value, operand)
    if op == "null":
        return (value is None) is bool(operand)
    if op == "contains":
        return str(operand) in text
    if op == "notContains":
        return str(operand) not in text
    if op in ("containsIgnoreCase", "containsIgnoreCaseAndAccent"):
        return operand_lower in lowered
    if op == "notContainsIgnoreCase":
        return operand_lower not in lowered
    if op == "startsWith":
        return text.startswith(str(operand))
    if op == "startsWithIgnoreCase":
        return lowered.startswith(operand_lower)
    if op == "notStartsWith":
        return not text.startswith(str(operand))
    if op == "endsWith":
        return text.endswith(str(operand))
    if op == "notEndsWith":
        return not text.endswith(str(operand))
    if op == "eqIgnoreCase":
        return lowered == operand_lower
    if op == "neqIgnoreCase":
        return lowered != operand_lower
    if op in ("and", "or"):
        checks = [_compare(value, sub) for sub in operand or []]
        return all(checks) if op == "and" else any(checks)
    return True  # an operator the twin does not model filters nothing out


def _equal(value: Any, operand: Any) -> bool:
    if value is None or operand is None:
        return value is operand
    numbers = _numbers(value, operand)
    if numbers is not None:
        return numbers[0] == numbers[1]
    return str(value) == str(operand)


def _ordered(op: str, value: Any, operand: Any) -> bool:
    if value is None:
        return False
    pair = _dates(value, operand) or _numbers(value, operand) or (str(value), str(operand))
    left, right = pair
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    return left >= right


def _numbers(value: Any, operand: Any) -> tuple[float, float] | None:
    if isinstance(value, bool) or isinstance(operand, bool):
        return None
    try:
        return float(value), float(operand)
    except (TypeError, ValueError):
        return None


def _dates(value: Any, operand: Any) -> tuple[datetime, datetime] | None:
    left, right = _as_datetime(value), _as_datetime(operand)
    return (left, right) if left and right else None


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


# --- queries -----------------------------------------------------------------

def _q_viewer(source: Any, info: Any, **_args: Any) -> dict:
    user = store.viewer(info.context)
    if user is None:
        raise GraphQLError("Authentication required, not authenticated",
                           extensions=_extensions(*AUTH, "You need to authenticate."))
    return user


def _single(collection: str, entity: str) -> Resolver:
    """``team(id:)``-style lookup; Linear errors when the entity is missing."""
    def resolve(source: Any, info: Any, **args: Any) -> dict:
        record = (info.context.get(collection) or {}).get(args.get("id"))
        if record is None:
            raise store.LinearNotFound(entity)
        return record
    return resolve


def _q_issue(source: Any, info: Any, **args: Any) -> dict:
    # Agents pass whatever they have: the UUID or the human identifier (ENG-12).
    return store.require_issue(info.context, args.get("id") or "")


def _q_comment(source: Any, info: Any, **args: Any) -> dict:
    comment = (info.context.get("comments") or {}).get(args.get("id"))
    if comment is None:
        raise store.LinearNotFound("Comment")
    return comment


def _q_search_issues(source: Any, info: Any, **args: Any) -> dict:
    state = info.context
    term = (args.get("term") or "").lower()
    issues = [i for i in state["issues"].values()
              if term in f"{i.get('title', '')} {i.get('description', '')} "
                         f"{i.get('identifier', '')}".lower()]
    if args.get("teamId"):
        issues = [i for i in issues if i.get("teamId") == args["teamId"]]
    page = _connection(state, "issue", issues, args)
    return {**page, "totalCount": float(len(issues))}


def _q_issue_search(source: Any, info: Any, **args: Any) -> dict:
    state = info.context
    query = (args.get("query") or "").lower()
    issues = [i for i in state["issues"].values()
              if query in f"{i.get('title', '')} {i.get('description', '')} "
                          f"{i.get('identifier', '')}".lower()]
    return _connection(state, "issue", issues, args)


# --- mutations ---------------------------------------------------------------

def _payload(state: dict, **fields: Any) -> dict:
    return {"success": True, "lastSyncId": float(state["_counters"]["sync"]), **fields}


def _m_issue_create(source: Any, info: Any, **args: Any) -> dict:
    issue = store.create_issue(info.context, args.get("input") or {})
    return _payload(info.context, issue=issue)


def _m_issue_update(source: Any, info: Any, **args: Any) -> dict:
    issue = store.require_issue(info.context, args.get("id") or "")
    store.update_issue(info.context, issue, args.get("input") or {})
    return _payload(info.context, issue=issue)


def _m_issue_archive(source: Any, info: Any, **args: Any) -> dict:
    issue = store.require_issue(info.context, args.get("id") or "")
    store.archive_issue(info.context, issue, trash=bool(args.get("trash")))
    return _payload(info.context, entity=issue)


def _m_issue_unarchive(source: Any, info: Any, **args: Any) -> dict:
    issue = store.require_issue(info.context, args.get("id") or "")
    store.unarchive_issue(info.context, issue)
    return _payload(info.context, entity=issue)


def _m_issue_delete(source: Any, info: Any, **args: Any) -> dict:
    """Linear's delete moves an issue to the trash; it is archived, not erased."""
    issue = store.require_issue(info.context, args.get("id") or "")
    store.archive_issue(info.context, issue, trash=True)
    if args.get("permanentlyDelete"):
        info.context["issues"].pop(issue["id"], None)
    return _payload(info.context, entity=issue)


def _m_issue_label_toggle(add: bool) -> Resolver:
    def resolve(source: Any, info: Any, **args: Any) -> dict:
        issue = store.require_issue(info.context, args.get("id") or "")
        field = "addedLabelIds" if add else "removedLabelIds"
        store.update_issue(info.context, issue, {field: [args.get("labelId")]})
        return _payload(info.context, issue=issue)
    return resolve


def _m_comment_create(source: Any, info: Any, **args: Any) -> dict:
    comment = store.create_comment(info.context, args.get("input") or {})
    return _payload(info.context, comment=comment)


def _m_comment_update(source: Any, info: Any, **args: Any) -> dict:
    state = info.context
    comment = state["comments"].get(args.get("id"))
    if comment is None:
        raise store.LinearNotFound("Comment")
    body = (args.get("input") or {}).get("body")
    if body is not None:
        comment["body"] = body
    comment["updatedAt"] = comment["editedAt"] = store.now()
    store.bump_sync(state)
    return _payload(state, comment=comment)


def _m_comment_delete(source: Any, info: Any, **args: Any) -> dict:
    state = info.context
    comment = state["comments"].get(args.get("id"))
    if comment is None:
        raise store.LinearNotFound("Comment")
    comment["archivedAt"] = store.now()
    issue = state["issues"].get(comment.get("issueId"))
    if issue is not None:
        issue["commentCount"] = max(0, issue.get("commentCount", 0) - 1)
    store.bump_sync(state)
    return _payload(state, entityId=comment["id"])


def _m_label_create(source: Any, info: Any, **args: Any) -> dict:
    label = store.create_label(info.context, args.get("input") or {})
    return _payload(info.context, issueLabel=label)


def _m_project_create(source: Any, info: Any, **args: Any) -> dict:
    project = store.create_project(info.context, args.get("input") or {})
    return _payload(info.context, project=project)


def _m_project_update(source: Any, info: Any, **args: Any) -> dict:
    state = info.context
    project = state["projects"].get(args.get("id"))
    if project is None:
        raise store.LinearNotFound("Project")
    store.update_project(state, project, args.get("input") or {})
    return _payload(state, project=project)


def _m_team_create(source: Any, info: Any, **args: Any) -> dict:
    team = store.create_team(info.context, args.get("input") or {})
    return _payload(info.context, team=team)


def _m_cycle_create(source: Any, info: Any, **args: Any) -> dict:
    cycle = store.create_cycle(info.context, args.get("input") or {})
    return _payload(info.context, cycle=cycle)


# --- entity field resolvers --------------------------------------------------

def _lookup(collection: str, foreign_key: str) -> Resolver:
    """Resolve a relation by foreign key, or the type's empty value if it is gone."""
    def resolve(source: Any, info: Any, **_args: Any) -> Any:
        record = (info.context.get(collection) or {}).get(source.get(foreign_key))
        return record if record is not None else _empty(info.return_type)
    return resolve


def _issue_fields() -> dict[str, Any]:
    return {
        "state": _lookup("workflow_states", "stateId"),
        "team": _lookup("teams", "teamId"),
        "assignee": _lookup("users", "assigneeId"),
        "creator": _lookup("users", "creatorId"),
        "project": _lookup("projects", "projectId"),
        "cycle": _lookup("cycles", "cycleId"),
        "parent": _lookup("issues", "parentId"),
        "labels": _connection_field("issueLabel", _issue_labels),
        "comments": _connection_field("comment", _issue_comments),
        "children": _connection_field("issue", _issue_children),
        "subscribers": _connection_field("user", lambda state, issue: [
            state["users"][uid_] for uid_ in issue.get("subscriberIds") or []
            if uid_ in state["users"]]),
        "url": lambda source, info, **_a: source.get("url") or store.issue_url(
            info.context, source),
    }


def _project_status(source: Any, info: Any, **_args: Any) -> dict:
    """Linear's modern project status object, derived from the legacy state string."""
    state_name = source.get("state") or "planned"
    types = {"planned": "planned", "started": "started", "paused": "paused",
             "completed": "completed", "canceled": "canceled", "backlog": "backlog"}
    return {"id": f"status-{state_name}", "name": state_name.title(),
            "type": types.get(state_name, "planned"), "color": "#5e6ad2",
            "position": 0.0, "description": None, "indefinite": False}


def _resolvers() -> dict[str, dict[str, Resolver]]:
    issue_fields = _issue_fields()
    return {
        "Query": {
            "viewer": _q_viewer,
            "organization": lambda source, info, **_a: info.context["organization"],
            "issue": _q_issue,
            "issues": _connection_field("issue", lambda state, _s: _collection(state, "issues")),
            "team": _single("teams", "Team"),
            "teams": _connection_field("team", lambda state, _s: _collection(state, "teams")),
            "user": _single("users", "User"),
            "users": _connection_field("user", lambda state, _s: _collection(state, "users")),
            "workflowState": _single("workflow_states", "WorkflowState"),
            "workflowStates": _connection_field(
                "workflowState", lambda state, _s: _collection(state, "workflow_states")),
            "issueLabel": _single("labels", "IssueLabel"),
            "issueLabels": _connection_field(
                "issueLabel", lambda state, _s: _collection(state, "labels")),
            "project": _single("projects", "Project"),
            "projects": _connection_field(
                "project", lambda state, _s: _collection(state, "projects")),
            "cycle": _single("cycles", "Cycle"),
            "cycles": _connection_field("cycle", lambda state, _s: _collection(state, "cycles")),
            "comment": _q_comment,
            "comments": _connection_field(
                "comment", lambda state, _s: _collection(state, "comments")),
            "searchIssues": _q_search_issues,
            "issueSearch": _q_issue_search,
        },
        "Mutation": {
            "issueCreate": _m_issue_create,
            "issueUpdate": _m_issue_update,
            "issueArchive": _m_issue_archive,
            "issueUnarchive": _m_issue_unarchive,
            "issueDelete": _m_issue_delete,
            "issueAddLabel": _m_issue_label_toggle(True),
            "issueRemoveLabel": _m_issue_label_toggle(False),
            "commentCreate": _m_comment_create,
            "commentUpdate": _m_comment_update,
            "commentDelete": _m_comment_delete,
            "issueLabelCreate": _m_label_create,
            "projectCreate": _m_project_create,
            "projectUpdate": _m_project_update,
            "teamCreate": _m_team_create,
            "cycleCreate": _m_cycle_create,
        },
        "Issue": issue_fields,
        # The search payload repeats Issue's fields on its own type.
        "IssueSearchResult": issue_fields,
        "Team": {
            "displayName": lambda source, info, **_a: source.get("name"),
            "organization": lambda source, info, **_a: info.context["organization"],
            "issues": _connection_field("issue", _team_issues),
            "members": _connection_field("user", _team_members),
            "states": _connection_field("workflowState", lambda state, team: [
                s for s in state["workflow_states"].values() if s.get("teamId") == team["id"]]),
            "labels": _connection_field("issueLabel", lambda state, team: [
                lab for lab in state["labels"].values()
                if lab.get("teamId") in (team["id"], None)]),
            "projects": _connection_field("project", lambda state, team: [
                p for p in state["projects"].values() if team["id"] in (p.get("teamIds") or [])]),
            "cycles": _connection_field("cycle", lambda state, team: [
                c for c in state["cycles"].values() if c.get("teamId") == team["id"]]),
            "defaultIssueState": lambda source, info, **_a: info.context[
                "workflow_states"].get(store.default_state_id(info.context, source["id"])),
            "issueCount": lambda source, info, **a: len([
                i for i in info.context["issues"].values()
                if i.get("teamId") == source["id"]
                and (a.get("includeArchived") or not i.get("archivedAt"))]),
        },
        "User": {
            "isMe": lambda source, info, **_a: source.get("id") == info.context.get("_viewer"),
            "initials": lambda source, info, **_a: "".join(
                word[0] for word in str(source.get("name") or "").split()[:2]).upper(),
            "organization": lambda source, info, **_a: info.context["organization"],
            "assignedIssues": _connection_field("issue", lambda state, user: [
                i for i in state["issues"].values() if i.get("assigneeId") == user["id"]]),
            "createdIssues": _connection_field("issue", lambda state, user: [
                i for i in state["issues"].values() if i.get("creatorId") == user["id"]]),
            "teams": _connection_field("team", lambda state, _user: _collection(state, "teams")),
            "createdIssueCount": lambda source, info, **_a: len([
                i for i in info.context["issues"].values()
                if i.get("creatorId") == source["id"]]),
        },
        "WorkflowState": {
            "team": _lookup("teams", "teamId"),
            "issues": _connection_field("issue", lambda state, ws: [
                i for i in state["issues"].values() if i.get("stateId") == ws["id"]]),
        },
        "IssueLabel": {
            "team": _lookup("teams", "teamId"),
            "creator": _lookup("users", "creatorId"),
            "parent": _lookup("labels", "parentId"),
            "organization": lambda source, info, **_a: info.context["organization"],
            "issues": _connection_field("issue", lambda state, label: [
                i for i in state["issues"].values() if label["id"] in (i.get("labelIds") or [])]),
            "children": _connection_field("issueLabel", lambda state, label: [
                lab for lab in state["labels"].values() if lab.get("parentId") == label["id"]]),
        },
        "Project": {
            "lead": _lookup("users", "leadId"),
            "status": _project_status,
            "teams": _connection_field("team", lambda state, project: [
                state["teams"][tid] for tid in project.get("teamIds") or []
                if tid in state["teams"]]),
            "members": _connection_field("user", lambda state, project: [
                state["users"][uid_] for uid_ in project.get("memberIds") or []
                if uid_ in state["users"]]),
            "issues": _connection_field("issue", lambda state, project: [
                i for i in state["issues"].values() if i.get("projectId") == project["id"]]),
        },
        "Cycle": {
            "team": _lookup("teams", "teamId"),
            "issues": _connection_field("issue", lambda state, cycle: [
                i for i in state["issues"].values() if i.get("cycleId") == cycle["id"]]),
            "isActive": lambda source, info, **_a: _cycle_phase(source) == "active",
            "isPast": lambda source, info, **_a: _cycle_phase(source) == "past",
            "isFuture": lambda source, info, **_a: _cycle_phase(source) == "future",
        },
        "Comment": {
            "issue": _lookup("issues", "issueId"),
            "user": _lookup("users", "userId"),
            "parent": _lookup("comments", "parentId"),
            "children": _connection_field("comment", lambda state, comment: [
                c for c in state["comments"].values() if c.get("parentId") == comment["id"]]),
        },
        "Organization": {
            "teams": _connection_field("team", lambda state, _org: _collection(state, "teams")),
            "users": _connection_field("user", lambda state, _org: _collection(state, "users")),
            "labels": _connection_field(
                "issueLabel", lambda state, _org: _collection(state, "labels")),
            "userCount": lambda source, info, **_a: len(info.context["users"]),
            "createdIssueCount": lambda source, info, **_a: len(info.context["issues"]),
        },
    }


def _cycle_phase(cycle: dict) -> str:
    now = datetime.now(UTC)
    starts, ends = _as_datetime(cycle.get("startsAt")), _as_datetime(cycle.get("endsAt"))
    if starts and starts > now:
        return "future"
    if ends and ends < now:
        return "past"
    return "active"
