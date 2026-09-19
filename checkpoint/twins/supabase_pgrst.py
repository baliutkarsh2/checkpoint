"""PostgREST query semantics for the Supabase twin.

Supabase's database API is PostgREST: the whole query lives in the URL, so a
twin that mis-parses one operator answers the wrong rows. The danger is
silence — a filter an implementation does not recognise and skips turns
``delete().or_("id.eq.1,id.eq.2")`` into "delete every row". Everything here
therefore either implements an operator or raises :class:`PgrstError`, and
nothing falls through to "match".

Implemented over plain dicts so the route handlers stay thin: filters with
three-valued (SQL) logic, ``and``/``or``/``not`` trees, ``select`` with
aliases, casts, JSON paths and embedded resources, ordering, and ranges.
"""
from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

# Query parameters that are not filters.
RESERVED = frozenset({"select", "order", "limit", "offset", "on_conflict", "columns"})

# Operators taking a scalar right-hand side, mapped to how they compare.
_SCALAR_OPS = frozenset({
    "eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "match", "imatch", "isdistinct",
})
_ARRAY_OPS = frozenset({"cs", "cd", "ov"})
_FTS_OPS = frozenset({"fts", "plfts", "phfts", "wfts"})
# Range operators: valid PostgREST, but a twin has no range-typed columns.
_RANGE_OPS = frozenset({"sl", "sr", "nxl", "nxr", "adj"})
_QUANTIFIABLE = frozenset({"eq", "like", "ilike", "gt", "gte", "lt", "lte", "match", "imatch"})
KNOWN_OPS = _SCALAR_OPS | _ARRAY_OPS | _FTS_OPS | _RANGE_OPS | {"in", "is", "not"}

_OP_RE = re.compile(r"^(?P<op>[a-z]+)(?:\((?P<arg>[^)]*)\))?\.(?P<value>.*)$", re.S)
_TRUE_WORDS = frozenset({"true", "t", "yes", "y", "on", "1"})
_FALSE_WORDS = frozenset({"false", "f", "no", "n", "off", "0"})


class PgrstError(Exception):
    """A PostgREST error: an HTTP status plus the ``{code, details, hint, message}`` body."""

    def __init__(self, status: int, code: str, message: str, *,
                 details: Any = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.hint = hint

    def body(self) -> dict:
        return {"code": self.code, "details": self.details, "hint": self.hint,
                "message": self.message}


def parse_failure(expression: str, expecting: str) -> PgrstError:
    """The error PostgREST returns for a query string it cannot parse."""
    return PgrstError(
        400, "PGRST100", f'"failed to parse filter ({expression})" (line 1, column 1)',
        details=f"unexpected end of input expecting {expecting}",
    )


# --- schema ------------------------------------------------------------------

@dataclass(frozen=True)
class Table:
    """One table in the twin's state: ``{"columns": [...], "rows": [...]}``."""

    name: str
    spec: dict

    @property
    def rows(self) -> list[dict]:
        return self.spec.setdefault("rows", [])

    @property
    def declared_columns(self) -> tuple[str, ...]:
        raw = self.spec.get("columns") or []
        return tuple(c["name"] if isinstance(c, dict) else str(c) for c in raw)

    @property
    def columns(self) -> tuple[str, ...]:
        """Declared columns plus any column the stored rows actually carry."""
        names = list(self.declared_columns)
        for row in self.rows:
            names.extend(k for k in row if k not in names)
        return tuple(names)

    @property
    def primary_key(self) -> tuple[str, ...]:
        pk = self.spec.get("primary_key")
        if isinstance(pk, str):
            return (pk,)
        if isinstance(pk, (list, tuple)) and pk:
            return tuple(str(c) for c in pk)
        return ("id",) if "id" in self.columns else ()

    @property
    def unique_keys(self) -> tuple[tuple[str, ...], ...]:
        """Column groups that must stay unique: the primary key plus declared ones."""
        keys = [self.primary_key] if self.primary_key else []
        for raw in self.spec.get("unique") or []:
            cols = (raw,) if isinstance(raw, str) else tuple(str(c) for c in raw)
            if cols not in keys:
                keys.append(cols)
        return tuple(keys)

    @property
    def foreign_keys(self) -> dict[str, tuple[str, str]]:
        """``{column: (target table, target column)}`` from the table's ``foreign_keys``."""
        out: dict[str, tuple[str, str]] = {}
        for column, ref in (self.spec.get("foreign_keys") or {}).items():
            target, _, target_column = str(ref).partition(".")
            out[str(column)] = (target, target_column or "id")
        return out

    def knows_columns(self) -> bool:
        """Whether the table can reject unknown columns (an empty, undeclared table cannot)."""
        return bool(self.columns)


class Schema:
    """The set of tables PostgREST exposes, i.e. ``state["tables"]``."""

    def __init__(self, tables: dict) -> None:
        self._tables = tables

    def find(self, name: str) -> Table | None:
        spec = self._tables.get(name)
        return Table(name, spec) if isinstance(spec, dict) else None

    def get(self, name: str) -> Table:
        table = self.find(name)
        if table is None:
            raise PgrstError(
                404, "PGRST205", f"Could not find the table 'public.{name}' in the schema cache",
                hint=self._did_you_mean(name),
            )
        return table

    def names(self) -> list[str]:
        return [n for n, spec in self._tables.items() if isinstance(spec, dict)]

    def _did_you_mean(self, name: str) -> str | None:
        close = difflib.get_close_matches(name, self.names(), n=1, cutoff=0.6)
        return f"Perhaps you meant the table 'public.{close[0]}'" if close else None


# --- filters -----------------------------------------------------------------

@dataclass
class Cond:
    """One ``column=operator.value`` filter."""

    column: str
    op: str
    value: Any
    quantifier: str | None = None  # "any" / "all"
    negate: bool = False


@dataclass
class Logic:
    """An ``and=(...)`` / ``or=(...)`` tree node."""

    kind: Literal["and", "or"]
    children: list[Cond | Logic]
    negate: bool = False


Node = Cond | Logic


@dataclass
class Scope:
    """Filters and range applied to one resource: the table, or an embedded one."""

    conditions: list[Node] = field(default_factory=list)
    order: list[tuple[str, bool, bool]] = field(default_factory=list)
    limit: int | None = None
    offset: int = 0
    children: dict[str, Scope] = field(default_factory=dict)

    def child(self, name: str) -> Scope:
        return self.children.setdefault(name, Scope())


def parse_scope(items: Iterable[tuple[str, str]], *, table: Table | None,
                embeds: Sequence[str] = ()) -> Scope:
    """Turn query parameters into a filter/order/range tree.

    ``embeds`` names the resources the ``select`` embeds, so ``profiles.id=eq.1``
    is routed to the embedded resource instead of being read as a column.
    """
    scope = Scope()
    for key, value in items:
        prefix, _, rest = key.partition(".")
        if rest and prefix in embeds:
            _apply_param(scope.child(prefix), rest, value, table=None)
            continue
        _apply_param(scope, key, value, table=table)
    return scope


def _apply_param(scope: Scope, key: str, value: str, *, table: Table | None) -> None:
    if key in RESERVED:
        if key == "limit":
            scope.limit = _positive_int(key, value)
        elif key == "offset":
            scope.offset = _positive_int(key, value) or 0
        elif key == "order":
            scope.order = parse_order(value)
        return
    if key in ("or", "and", "not.or", "not.and"):
        negate, _, kind = key.rpartition(".")
        scope.conditions.append(_parse_logic(kind, value, negate=bool(negate)))
        return
    scope.conditions.append(parse_condition(key, value, table=table))


def _positive_int(key: str, value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise parse_failure(value, "a positive integer") from None
    if parsed < 0:
        raise parse_failure(value, "a positive integer")
    return parsed


def parse_order(value: str) -> list[tuple[str, bool, bool]]:
    """``created_at.desc,name.asc.nullsfirst`` -> [(column, desc, nulls_first)]."""
    order: list[tuple[str, bool, bool]] = []
    for part in _split_top_level(value, ","):
        column, *modifiers = part.strip().split(".")
        if not column:
            raise parse_failure(value, "a column name")
        unknown = [m for m in modifiers if m not in ("asc", "desc", "nullsfirst", "nullslast")]
        if unknown:
            raise parse_failure(part, "asc, desc, nullsfirst or nullslast")
        desc = "desc" in modifiers
        # Postgres orders NULLs last for ASC and first for DESC unless told otherwise.
        nulls_first = "nullsfirst" in modifiers or (desc and "nullslast" not in modifiers)
        order.append((column, desc, nulls_first))
    return order


def parse_condition(column: str, raw: str, *, table: Table | None) -> Cond:
    """Parse one ``column=[not.]operator[(any)].value`` filter."""
    negate = False
    body = raw
    if body.startswith("not."):
        negate, body = True, body[4:]
    match = _OP_RE.match(body)
    if match is None:
        raise parse_failure(raw, 'an operator, e.g. "eq.1"')
    op, quantifier, value = match["op"], match["arg"], match["value"]
    if op not in KNOWN_OPS or op == "not":
        raise PgrstError(
            400, "PGRST100", f'"failed to parse filter ({raw})" (line 1, column 1)',
            details=f'unexpected "{op}" expecting one of {", ".join(sorted(KNOWN_OPS - {"not"}))}',
        )
    if op in _RANGE_OPS:
        raise PgrstError(
            400, "42883", f"operator does not exist: text {op} unknown",
            hint="No operator matches the given name and argument types. "
                 "Range operators need a range-typed column.",
        )
    if quantifier and op in _QUANTIFIABLE:
        if quantifier not in ("any", "all"):
            raise parse_failure(raw, "any or all")
    elif quantifier and op not in _FTS_OPS:
        # fts(english) names a text-search configuration; the twin matches
        # lexemes the same way whichever one is asked for.
        raise parse_failure(raw, "an operator without a quantifier")
    if table is not None:
        _check_column(table, column)
    return Cond(
        column=column,
        op=op,
        value=_parse_value(op, value),
        quantifier=quantifier if op in _QUANTIFIABLE else None,
        negate=negate,
    )


def _check_column(table: Table, column: str) -> None:
    name = re.split(r"->>?", column, maxsplit=1)[0].strip('"')
    if table.knows_columns() and name not in table.columns:
        raise PgrstError(400, "42703", f"column {table.name}.{name} does not exist")


def _parse_logic(kind: str, raw: str, *, negate: bool) -> Logic:
    body = raw.strip()
    if not (body.startswith("(") and body.endswith(")")):
        raise parse_failure(raw, "a parenthesised list of filters")
    children = [_parse_logic_child(part) for part in _split_top_level(body[1:-1], ",")]
    if not children:
        raise parse_failure(raw, "at least one filter")
    return Logic(kind="or" if kind == "or" else "and", children=children, negate=negate)


def _parse_logic_child(raw: str) -> Node:
    body = raw.strip()
    negate = False
    if body.startswith("not."):
        negate, body = True, body[4:]
    for kind in ("and", "or"):
        if body.startswith(f"{kind}("):
            return _parse_logic(kind, body[len(kind):], negate=negate)
    column, _, rest = body.partition(".")
    if not rest:
        raise parse_failure(raw, 'a filter, e.g. "id.eq.1"')
    condition = parse_condition(column, rest, table=None)
    condition.negate ^= negate
    return condition


def _parse_value(op: str, raw: str) -> Any:
    if op == "in":
        body = raw.strip()
        if not (body.startswith("(") and body.endswith(")")):
            raise parse_failure(raw, "a parenthesised list of values")
        return [_unquote(v) for v in _split_top_level(body[1:-1], ",")]
    if op in _ARRAY_OPS:
        return _parse_composite(raw)
    if op == "is":
        word = raw.strip().lower()
        if word not in ("null", "true", "false", "unknown"):
            raise parse_failure(raw, "null, true, false or unknown")
        return word
    return _unquote(raw)


def _parse_composite(raw: str) -> Any:
    """``{a,b}`` (array literal) or ``{"k": 1}`` / ``[1,2]`` (JSON) on the right of cs/cd/ov."""
    body = raw.strip()
    if body.startswith("[") or (body.startswith("{") and '"' in body and ":" in body):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise parse_failure(raw, "valid JSON") from None
    if body.startswith("{") and body.endswith("}"):
        inner = body[1:-1]
        return [_unquote(v) for v in _split_top_level(inner, ",")] if inner.strip() else []
    raise parse_failure(raw, "an array literal, e.g. {a,b}")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on ``separator`` outside quotes, parentheses and braces."""
    parts, depth, quoted, current = [], 0, False, []
    for char in text:
        if char == '"':
            quoted = not quoted
        elif not quoted and char in "({":
            depth += 1
        elif not quoted and char in ")}":
            depth -= 1
        if char == separator and depth == 0 and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [p for p in parts if p.strip()]


# --- evaluation (three-valued, like SQL) -------------------------------------

def matches(row: dict, conditions: Sequence[Node]) -> bool:
    """Whether ``row`` satisfies every condition; NULL comparisons stay unknown."""
    return _eval_all(row, conditions) is True


def _eval_all(row: dict, conditions: Sequence[Node]) -> bool | None:
    result: bool | None = True
    for node in conditions:
        outcome = _eval(row, node)
        if outcome is False:
            return False
        if outcome is None:
            result = None
    return result


def _eval(row: dict, node: Node) -> bool | None:
    if isinstance(node, Logic):
        outcomes = [_eval(row, child) for child in node.children]
        if node.kind == "and":
            value = (False if False in outcomes
                     else None if None in outcomes else True)
        else:
            value = (True if True in outcomes
                     else None if None in outcomes else False)
    else:
        value = _eval_cond(row, node)
    return None if value is None else (not value if node.negate else value)


def _eval_cond(row: dict, cond: Cond) -> bool | None:
    cell = resolve_path(row, cond.column)
    if cond.op == "is":
        if cond.value == "null":
            return cell is None
        if cond.value == "unknown":
            return cell is None
        return cell is True if cond.value == "true" else cell is False
    if cond.op == "isdistinct":
        return not _equal(cell, cond.value)
    if cond.op == "in":
        if cell is None:
            return None
        return any(_equal(cell, option) for option in cond.value)
    if cond.op in _ARRAY_OPS:
        return _eval_array(cell, cond)
    if cond.op in _FTS_OPS:
        return None if cell is None else _text_search(str(cell), cond)
    if cell is None:
        return None
    if cond.quantifier:
        values = cond.value if isinstance(cond.value, list) else _parse_composite(cond.value)
        outcomes = [_compare(cond.op, cell, v) for v in values]
        return any(outcomes) if cond.quantifier == "any" else all(outcomes)
    return _compare(cond.op, cell, cond.value)


def _compare(op: str, cell: Any, raw: Any) -> bool:
    if op == "eq":
        return _equal(cell, raw)
    if op == "neq":
        return not _equal(cell, raw)
    if op in ("like", "ilike"):
        return _like(str(cell), str(raw), fold=op == "ilike")
    if op in ("match", "imatch"):
        flags = re.IGNORECASE if op == "imatch" else 0
        return re.search(str(raw), str(cell), flags) is not None
    other = _coerce(raw, cell)
    try:
        if op == "gt":
            return cell > other
        if op == "gte":
            return cell >= other
        if op == "lt":
            return cell < other
        return cell <= other
    except TypeError:
        return False


def _equal(cell: Any, raw: Any) -> bool:
    if cell is None:
        return False
    other = _coerce(raw, cell)
    if cell == other:
        return True
    return isinstance(cell, (str, int, float, bool)) and str(cell) == str(raw)


def _coerce(raw: Any, like: Any) -> Any:
    """Read a query-string value as the type of the cell it is compared with."""
    if not isinstance(raw, str):
        return raw
    if isinstance(like, bool):
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        return raw
    if isinstance(like, int):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(like, float):
        try:
            return float(raw)
        except ValueError:
            return raw
    if isinstance(like, (dict, list)):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _like(cell: str, pattern: str, *, fold: bool) -> bool:
    """LIKE/ILIKE: ``%``/``*`` match any run, ``_`` one character, and the match is anchored."""
    regex = []
    for char in pattern:
        if char in "%*":
            regex.append(".*")
        elif char == "_":
            regex.append(".")
        else:
            regex.append(re.escape(char))
    flags = re.IGNORECASE if fold else 0
    return re.fullmatch("".join(regex), cell, flags | re.S) is not None


def _eval_array(cell: Any, cond: Cond) -> bool | None:
    if cell is None:
        return None
    wanted = cond.value
    if isinstance(cell, dict) or isinstance(wanted, dict):
        if not isinstance(cell, dict) or not isinstance(wanted, dict):
            return False
        if cond.op == "cs":
            return all(cell.get(k) == v for k, v in wanted.items())
        if cond.op == "cd":
            return all(k in wanted and wanted[k] == v for k, v in cell.items())
        return any(cell.get(k) == v for k, v in wanted.items())
    cells = cell if isinstance(cell, list) else [cell]
    values = [_coerce(v, cells[0]) if cells else v for v in wanted]
    if cond.op == "cs":
        return all(v in cells for v in values)
    if cond.op == "cd":
        return all(c in values for c in cells)
    return any(v in cells for v in values)


def _text_search(text: str, cond: Cond) -> bool:
    """Approximate to_tsquery matching: lexeme-level AND/OR/NOT over lowercased words."""
    haystack = set(re.findall(r"[\w']+", text.lower()))
    query = str(cond.value)
    if cond.op == "fts":
        groups = [g.strip() for g in query.split("|")]
        return any(_fts_group(haystack, g) for g in groups)
    if cond.op == "wfts":
        terms = re.findall(r'"[^"]*"|\S+', query)
        return all(_fts_term(haystack, t) for t in terms if t.lower() != "or")
    return all(_fts_term(haystack, term) for term in re.findall(r"[\w'*]+", query))


def _fts_group(haystack: set[str], group: str) -> bool:
    return all(_fts_term(haystack, term) for term in group.split("&"))


def _fts_term(haystack: set[str], term: str) -> bool:
    term = term.strip().strip('"').lower()
    negated = term.startswith("!") or term.startswith("-")
    term = term.lstrip("!-")
    if not term:
        return True
    if term.endswith(":*"):
        hit = any(word.startswith(term[:-2]) for word in haystack)
    else:
        hit = all(word in haystack for word in term.split())
    return not hit if negated else hit


def resolve_path(row: dict, column: str) -> Any:
    """Read ``column``, following JSON paths: ``meta->tags->>0``."""
    name, *rest = re.split(r"(->>|->)", column)
    value = row.get(name.strip().strip('"'))
    as_text = False
    for arrow, key in zip(rest[::2], rest[1::2], strict=False):
        as_text = arrow == "->>"
        key = key.strip().strip('"')
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and re.fullmatch(r"-?\d+", key):
            index = int(key)
            value = value[index] if -len(value) <= index < len(value) else None
        else:
            return None
    if as_text and value is not None and not isinstance(value, str):
        return json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    return value


# --- select ------------------------------------------------------------------

@dataclass
class Field:
    out: str
    column: str
    cast: str | None = None


@dataclass
class Embed:
    out: str
    table: str
    hint: str | None
    inner: bool
    fields: list[Field | Embed]


SelectNode = Field | Embed


def parse_select(raw: str | None) -> list[SelectNode]:
    """Parse ``select=id,title,author:profiles!author_id(username)``."""
    if raw is None or not raw.strip():
        return [Field(out="*", column="*")]
    nodes: list[SelectNode] = []
    for item in _split_top_level(raw, ","):
        nodes.append(_parse_select_item(item.strip()))
    return nodes or [Field(out="*", column="*")]


def _parse_select_item(item: str) -> SelectNode:
    if item.startswith("..."):
        raise PgrstError(400, "PGRST100", f'"failed to parse select parameter ({item})"',
                         details="spread embedded resources are not supported by this twin")
    head, sub = _split_sub_select(item)
    alias, head = _split_alias(head)
    head, _, hint = head.partition("!")
    head, cast = _split_cast(head)
    name = head.strip()
    if not name:
        raise parse_failure(item, "a column or embedded resource")
    if sub is not None:
        return Embed(
            out=(alias or name).strip(),
            table=name,
            hint=hint if hint and hint not in ("inner", "left") else None,
            inner=hint == "inner",
            fields=parse_select(sub),
        )
    out = (alias or re.split(r"->>?", name)[-1].strip('"')).strip()
    return Field(out=out, column=name, cast=cast)


def _split_sub_select(item: str) -> tuple[str, str | None]:
    if not item.endswith(")"):
        return item, None
    depth = 0
    for index in range(len(item) - 1, -1, -1):
        if item[index] == ")":
            depth += 1
        elif item[index] == "(":
            depth -= 1
            if depth == 0:
                return item[:index], item[index + 1:-1]
    raise parse_failure(item, "a closing parenthesis")


def _split_alias(head: str) -> tuple[str | None, str]:
    match = re.match(r"^([A-Za-z0-9_ ]+):(?!:)(.*)$", head, re.S)
    return (match[1].strip(), match[2]) if match else (None, head)


def _split_cast(head: str) -> tuple[str, str | None]:
    name, separator, cast = head.partition("::")
    return (name, cast.strip()) if separator else (head, None)


def embed_names(nodes: Sequence[SelectNode]) -> list[str]:
    return [n.out for n in nodes if isinstance(n, Embed)] + \
           [n.table for n in nodes if isinstance(n, Embed) and n.table != n.out]


def check_select(table: Table, nodes: Sequence[SelectNode]) -> None:
    for node in nodes:
        if isinstance(node, Field) and node.column != "*":
            _check_column(table, node.column)


# --- relationships -----------------------------------------------------------

@dataclass(frozen=True)
class Relationship:
    kind: Literal["many_to_one", "one_to_many"]
    source_column: str
    target_column: str
    target: Table


def singular(name: str) -> str:
    if name.endswith("ies"):
        return f"{name[:-3]}y"
    if name.endswith(("ses", "xes", "zes", "ches", "shes")):
        return name[:-2]
    return name[:-1] if name.endswith("s") else name


def relationship(schema: Schema, source: Table, target_name: str,
                 hint: str | None) -> Relationship:
    """Find how ``source`` relates to ``target_name``, the way PostgREST reads FKs."""
    target = schema.find(target_name)
    if target is None:
        raise PgrstError(
            400, "PGRST200",
            f"Could not find a relationship between '{source.name}' and '{target_name}' "
            "in the schema cache",
            details=f"Searched for a foreign key relationship between '{source.name}' and "
                    f"'{target_name}' in the schema 'public', but no matches were found.",
        )
    candidates: list[Relationship] = []
    for column, (referenced, referenced_column) in source.foreign_keys.items():
        if referenced == target_name and (hint in (None, column)):
            candidates.append(Relationship("many_to_one", column, referenced_column, target))
    for column, (referenced, referenced_column) in target.foreign_keys.items():
        if referenced == source.name and (hint in (None, column)):
            candidates.append(Relationship("one_to_many", referenced_column, column, target))
    if not candidates:
        guess = f"{singular(target_name)}_id"
        if guess in source.columns and hint in (None, guess):
            candidates.append(Relationship("many_to_one", guess, target.primary_key[0]
                                           if target.primary_key else "id", target))
        reverse = f"{singular(source.name)}_id"
        if reverse in target.columns and hint in (None, reverse):
            candidates.append(Relationship("one_to_many", source.primary_key[0]
                                           if source.primary_key else "id", reverse, target))
    if not candidates:
        raise PgrstError(
            400, "PGRST200",
            f"Could not find a relationship between '{source.name}' and '{target_name}' "
            "in the schema cache",
            details=f"Searched for a foreign key relationship between '{source.name}' and "
                    f"'{target_name}' in the schema 'public', but no matches were found.",
            hint=f"Declare it in the seed as \"foreign_keys\": {{\"<column>\": "
                 f"\"{target_name}.id\"}} on '{source.name}'.",
        )
    if len(candidates) > 1:
        raise PgrstError(
            400, "PGRST201",
            f"Could not embed because more than one relationship was found for "
            f"'{source.name}' and '{target_name}'",
            details=[{"cardinality": c.kind, "origin": f"public.{source.name}",
                      "target": f"public.{target_name}"} for c in candidates],
            hint=f"Try changing '{target_name}' to one of the following: "
                 f"{', '.join(f'{target_name}!{c.source_column}' for c in candidates)}",
        )
    return candidates[0]


# --- running a query ---------------------------------------------------------

def filter_rows(rows: Sequence[dict], scope: Scope) -> list[dict]:
    return [row for row in rows if matches(row, scope.conditions)]


def order_rows(rows: Sequence[dict], order: Sequence[tuple[str, bool, bool]]) -> list[dict]:
    result = list(rows)
    for column, desc, nulls_first in reversed(order):
        # sort() ascends then reverses for desc, so the NULL rank has to flip with it.
        null_rank = 1 if nulls_first == desc else 0
        result.sort(key=lambda row, c=column, n=null_rank: (
            n if resolve_path(row, c) is None else 1 - n,
            _sort_key(resolve_path(row, c)),
        ), reverse=desc)
    return result


def _sort_key(value: Any) -> tuple[int, Any]:
    """Sort mixed types without raising: numbers, then text, then everything else."""
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    return (3, json.dumps(value, sort_keys=True))


def slice_rows(rows: Sequence[dict], scope: Scope) -> list[dict]:
    start = scope.offset or 0
    stop = None if scope.limit is None else start + scope.limit
    return list(rows[start:stop])


def shape_rows(rows: Sequence[dict], nodes: Sequence[SelectNode], *, table: Table,
               schema: Schema, scope: Scope) -> list[dict]:
    """Project rows through the ``select`` list, resolving embedded resources."""
    # Resolve relationships up front: an unembeddable table is an error even when
    # the query matched no rows.
    relations = {id(node): relationship(schema, table, node.table, node.hint)
                 for node in nodes if isinstance(node, Embed)}
    shaped: list[dict] = []
    for row in rows:
        out: dict[str, Any] = {}
        drop = False
        for node in nodes:
            if isinstance(node, Field):
                if node.column == "*":
                    out.update(row)
                else:
                    out[node.out] = _cast(resolve_path(row, node.column), node.cast)
                continue
            value = _embed(row, node, relation=relations[id(node)], schema=schema,
                           scope=scope.children.get(node.out) or scope.children.get(node.table))
            if node.inner and (value is None or value == []):
                drop = True
                break
            out[node.out] = value
        if not drop:
            shaped.append(out)
    return shaped


def _embed(row: dict, node: Embed, *, relation: Relationship, schema: Schema,
           scope: Scope | None) -> Any:
    key = row.get(relation.source_column)
    related = [r for r in relation.target.rows
               if key is not None and _equal(r.get(relation.target_column), key)]
    sub_scope = scope or Scope()
    related = filter_rows(related, sub_scope)
    related = order_rows(related, sub_scope.order)
    related = slice_rows(related, sub_scope)
    shaped = shape_rows(related, node.fields, table=relation.target, schema=schema,
                        scope=sub_scope)
    if relation.kind == "many_to_one":
        return shaped[0] if shaped else None
    return shaped


def _cast(value: Any, cast: str | None) -> Any:
    if cast is None or value is None:
        return value
    if cast in ("text", "varchar", "char"):
        return value if isinstance(value, str) else json.dumps(value) \
            if isinstance(value, (dict, list)) else str(value)
    if cast in ("int", "int2", "int4", "int8", "integer", "bigint", "smallint"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if cast in ("float4", "float8", "numeric", "real", "double"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if cast in ("bool", "boolean"):
        return bool(value)
    return value
