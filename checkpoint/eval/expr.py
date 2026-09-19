"""Checkpoint's assertion language: precise, deterministic checks over a run.

An assertion is a boolean expression over the run's *world*::

    exists(github.issues[title == "Login broken"])
    count(created.github.issues) == 1
    count(deleted.linear.issues) == 0
    github.issues[number == 1].state == "closed"
    "bug" in github.issues[number == 1].labels[*].name
    count(trace[method == "DELETE"]) == 0
    answer ~ /issue #\\d+/i

Roots:
    <twin>.<collection>                      final state (e.g. ``github.issues``)
    seed.<twin>.<collection>                 state before the agent ran
    created|deleted|changed.<twin>.<coll>    what the agent changed (by primary key)
    trace                                    every API call (method, path, status, body, twin, op, ...)
    egress                                   connections to hosts outside the sandbox
    answer                                   the agent's final answer (string)
    task                                     the prompt the agent was given (string)
    exit_code, duration                      process facts

Operators: ``== != < <= > >=``, ``~`` / ``!~`` (regex; ``/re/flags`` or a string),
``in`` / ``not in`` (membership, or substring for strings), ``contains``,
``&& || !`` (also ``and or not``). Filters ``list[pred]`` keep matching items;
inside a filter, bare names are the item's fields. ``list[*].field`` projects.
``list.field`` reads a field from a list that must hold *exactly one* item.
Functions: ``count``, ``exists``, ``any(list, pred)``, ``all(list, pred)``,
``lower``, ``upper``, ``len``.

Evaluation is tri-state: an assertion PASSes, FAILs, or is an ERROR — a typo'd
field, an ambiguous selection, or a type mismatch is never silently false.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ExprSyntaxError(ValueError):
    """The assertion text does not parse."""


class ExprError(ValueError):
    """The assertion parsed but cannot be evaluated against this world."""


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<op>==|!=|<=|>=|&&|\|\||!~|[<>~!()\[\]{},.*])
  | (?P<name>[A-Za-z_][A-Za-z0-9_\-]*)
""", re.VERBOSE)

_REGEX_RE = re.compile(r"/((?:[^/\\\n]|\\.)+)/([imsx]*)")


@dataclass(frozen=True)
class Token:
    kind: str   # number | string | regex | op | name | end
    value: Any
    pos: int


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(text):
        # A regex literal may start wherever an operand may; "/" is not otherwise an operator.
        if text[i] == "/":
            m = _REGEX_RE.match(text, i)
            if not m:
                raise ExprSyntaxError(f"unterminated regex at position {i}: {text[i:i + 20]!r}")
            flags = 0
            for f in m.group(2):
                flags |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X}[f]
            try:
                tokens.append(Token("regex", re.compile(m.group(1), flags), i))
            except re.error as e:
                raise ExprSyntaxError(f"invalid regex /{m.group(1)}/: {e}") from e
            i = m.end()
            continue
        m = _TOKEN_RE.match(text, i)
        if not m:
            raise ExprSyntaxError(f"unexpected character {text[i]!r} at position {i}")
        kind = m.lastgroup
        raw = m.group(kind)  # type: ignore[arg-type]
        if kind == "number":
            tokens.append(Token("number", float(raw) if "." in raw else int(raw), i))
        elif kind == "string":
            tokens.append(Token("string", _unescape(raw[1:-1]), i))
        elif kind == "name":
            lowered = raw.lower()
            if lowered in ("and", "or", "not", "in", "contains", "true", "false", "null"):
                tokens.append(Token("op" if lowered in ("and", "or", "not", "in", "contains") else "name", lowered, i))
            else:
                tokens.append(Token("name", raw, i))
        elif kind == "op":
            tokens.append(Token("op", raw, i))
        i = m.end()
    tokens.append(Token("end", None, len(text)))
    return tokens


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    pos: int


@dataclass(frozen=True)
class Literal(Node):
    value: Any


@dataclass(frozen=True)
class Name(Node):
    name: str


@dataclass(frozen=True)
class Attr(Node):
    target: Node
    name: str


@dataclass(frozen=True)
class Filter(Node):
    target: Node
    predicate: Node


@dataclass(frozen=True)
class Project(Node):
    target: Node
    name: str


@dataclass(frozen=True)
class Call(Node):
    func: str
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Unary(Node):
    op: str
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class ListLit(Node):
    items: tuple[Node, ...]


# --------------------------------------------------------------------------
# Parser (precedence climbing)
# --------------------------------------------------------------------------

_BINARY_PRECEDENCE = {
    "||": 1, "or": 1,
    "&&": 2, "and": 2,
    "==": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4,
    "~": 4, "!~": 4, "in": 4, "not in": 4, "contains": 4,
}
_NORMALIZE = {"or": "||", "and": "&&"}


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def next(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, value: str) -> Token:
        tok = self.next()
        if tok.value != value:
            raise ExprSyntaxError(f"expected {value!r} at position {tok.pos}, found {tok.value!r}")
        return tok

    def parse(self) -> Node:
        node = self.expression(0)
        tok = self.peek()
        if tok.kind != "end":
            raise ExprSyntaxError(f"unexpected {tok.value!r} at position {tok.pos}")
        return node

    def _binary_op(self) -> str | None:
        tok = self.peek()
        if tok.kind != "op":
            return None
        if tok.value == "not" and self.peek(1).value == "in":
            return "not in"
        return tok.value if tok.value in _BINARY_PRECEDENCE else None

    def expression(self, min_prec: int) -> Node:
        left = self.unary()
        while True:
            op = self._binary_op()
            if op is None or _BINARY_PRECEDENCE[op] < min_prec:
                return left
            tok = self.next()
            if op == "not in":
                self.next()
            prec = _BINARY_PRECEDENCE[op]
            right = self.expression(prec + 1)
            left = Binary(tok.pos, _NORMALIZE.get(op, op), left, right)

    def unary(self) -> Node:
        tok = self.peek()
        if tok.kind == "op" and tok.value in ("!", "not") and not (tok.value == "not" and self.peek(1).value == "in"):
            self.next()
            return Unary(tok.pos, "!", self.unary())
        return self.postfix(self.primary())

    def primary(self) -> Node:
        tok = self.next()
        if tok.kind in ("number", "string", "regex"):
            return Literal(tok.pos, tok.value)
        if tok.kind == "name":
            if tok.value in ("true", "false", "null"):
                return Literal(tok.pos, {"true": True, "false": False, "null": None}[tok.value])
            if self.peek().value == "(":
                self.next()
                args: list[Node] = []
                if self.peek().value != ")":
                    args.append(self.expression(0))
                    while self.peek().value == ",":
                        self.next()
                        args.append(self.expression(0))
                self.expect(")")
                return Call(tok.pos, tok.value.lower(), tuple(args))
            return Name(tok.pos, tok.value)
        if tok.value == "(":
            node = self.expression(0)
            self.expect(")")
            return node
        if tok.value == "[":
            items: list[Node] = []
            if self.peek().value != "]":
                items.append(self.expression(0))
                while self.peek().value == ",":
                    self.next()
                    items.append(self.expression(0))
            self.expect("]")
            return ListLit(tok.pos, tuple(items))
        raise ExprSyntaxError(f"unexpected {tok.value!r} at position {tok.pos}")

    def postfix(self, node: Node) -> Node:
        while True:
            tok = self.peek()
            if tok.value == ".":
                self.next()
                name = self.next()
                if name.kind != "name":
                    raise ExprSyntaxError(f"expected a field name after '.' at position {name.pos}")
                node = Attr(tok.pos, node, name.value)
            elif tok.value == "[":
                self.next()
                if self.peek().value == "*":
                    self.next()
                    self.expect("]")
                    self.expect(".")
                    name = self.next()
                    if name.kind != "name":
                        raise ExprSyntaxError(f"expected a field name after '[*].' at position {name.pos}")
                    node = Project(tok.pos, node, name.value)
                else:
                    pred = self.expression(0)
                    self.expect("]")
                    node = Filter(tok.pos, node, pred)
            else:
                return node


def parse(text: str) -> Node:
    """Parse assertion text into an AST (raises ExprSyntaxError)."""
    if not text or not text.strip():
        raise ExprSyntaxError("empty assertion")
    return _Parser(text.strip()).parse()


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


class Items(list):
    """A list of records drawn from a named source (for error messages and field checks)."""

    def __init__(self, items: Sequence[Any], source: str, fields: frozenset[str] | None = None) -> None:
        super().__init__(items)
        self.source = source
        self.fields = fields


@dataclass
class World:
    """What an assertion can see about one run."""

    final: Mapping[str, Mapping[str, list[dict]]] = field(default_factory=dict)
    seed: Mapping[str, Mapping[str, list[dict]]] = field(default_factory=dict)
    keys: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """Primary-key field per twin collection (default ``id``)."""
    tombstones: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """Soft-delete field per twin collection (e.g. Linear ``archivedAt``)."""
    trace: list[dict] = field(default_factory=list)
    egress: list[dict] = field(default_factory=list)
    answer: str = ""
    task: str = ""
    """The prompt the agent was given — the context a judged criterion is read against."""
    exit_code: int | None = 0
    duration: float = 0.0

    def twins(self) -> list[str]:
        return sorted(set(self.final) | set(self.seed))


@dataclass
class Outcome:
    status: str          # "pass" | "fail" | "error"
    detail: str          # human-readable why

    @property
    def passed(self) -> bool:
        return self.status == "pass"


_ROOT_SCALARS = ("answer", "task", "exit_code", "duration")
_DELTA_ROOTS = ("created", "deleted", "changed", "seed")


class _Evaluator:
    def __init__(self, world: World) -> None:
        self.world = world
        self._delta_cache: dict[tuple[str, str], dict[str, list[dict]]] = {}
        self.notes: list[str] = []

    # -- names & paths --------------------------------------------------------

    def resolve_path(self, node: Node) -> Any:
        """Resolve a dotted root path like ``created.github.issues``."""
        parts: list[str] = []
        cur = node
        while isinstance(cur, Attr):
            parts.append(cur.name)
            cur = cur.target
        if not isinstance(cur, Name):
            return None
        parts.append(cur.name)
        parts.reverse()
        return self._root(parts, node.pos)

    def _root(self, parts: list[str], pos: int) -> Any:
        head = parts[0]
        w = self.world
        if head == "trace":
            return self._tail(Items(w.trace, "trace"), parts[1:])
        if head == "egress":
            return self._tail(Items(w.egress, "egress"), parts[1:])
        if head in _ROOT_SCALARS:
            if len(parts) > 1:
                raise ExprError(f"{head} has no fields")
            return {"answer": w.answer, "task": w.task,
                    "exit_code": w.exit_code, "duration": w.duration}[head]
        if head in _DELTA_ROOTS:
            if len(parts) < 3:
                raise ExprError(f"{head} needs a twin and collection, e.g. {head}.github.issues")
            twin, coll = parts[1], parts[2]
            items = self._delta(head, twin, coll)
            return self._tail(items, parts[3:])
        twin = head
        if twin not in w.final and twin not in w.seed:
            raise ExprError(f"unknown name {head!r}; twins in this run: {', '.join(w.twins()) or 'none'}")
        if len(parts) < 2:
            raise ExprError(f"{twin} needs a collection, e.g. {twin}.{self._first_collection(twin)}")
        return self._tail(self._collection(twin, parts[1], "final"), parts[2:])

    def _first_collection(self, twin: str) -> str:
        colls = sorted(self.world.final.get(twin, {}) or self.world.seed.get(twin, {}))
        return colls[0] if colls else "<collection>"

    def _collection(self, twin: str, coll: str, which: str) -> Items:
        state = (self.world.final if which == "final" else self.world.seed).get(twin, {})
        other = (self.world.seed if which == "final" else self.world.final).get(twin, {})
        if coll not in state and coll not in other:
            available = ", ".join(sorted(set(state) | set(other))) or "none"
            raise ExprError(f"{twin} has no collection {coll!r}; available: {available}")
        items = list(state.get(coll, []))
        return Items(items, f"{twin}.{coll}", self._fields(twin, coll))

    def _fields(self, twin: str, coll: str) -> frozenset[str]:
        names: set[str] = set()
        for source in (self.world.seed, self.world.final):
            for item in source.get(twin, {}).get(coll, []):
                if isinstance(item, dict):
                    names.update(item)
        return frozenset(names)

    def _delta(self, kind: str, twin: str, coll: str) -> Items:
        if kind == "seed":
            items = self._collection(twin, coll, "seed")
            return Items(list(items), f"seed.{twin}.{coll}", items.fields)
        key = (twin, coll)
        if key not in self._delta_cache:
            self._collection(twin, coll, "final")  # validates the names
            self._delta_cache[key] = diff_collection(
                self.world.seed.get(twin, {}).get(coll, []),
                self.world.final.get(twin, {}).get(coll, []),
                key_field=self.world.keys.get(twin, {}).get(coll, "id"),
                tombstone=self.world.tombstones.get(twin, {}).get(coll),
            )
        return Items(self._delta_cache[key][kind], f"{kind}.{twin}.{coll}", self._fields(twin, coll))

    def _tail(self, value: Any, rest: list[str]) -> Any:
        for name in rest:
            value = self.attr(value, name)
        return value

    # -- core evaluation ------------------------------------------------------

    def eval(self, node: Node, item: Any = None) -> Any:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, ListLit):
            return [self.eval(n, item) for n in node.items]
        if isinstance(node, Name):
            if item is not None and not self._is_root(node.name, item):
                return self.field(item, node.name)
            return self.resolve_path(node)
        if isinstance(node, Attr):
            if self._pure_path(node) and (item is None or self._is_root(_root_name(node), item)):
                return self.resolve_path(node)
            return self.attr(self.eval(node.target, item), node.name)
        if isinstance(node, Filter):
            base = self.eval(node.target, item)
            if not isinstance(base, list):
                raise ExprError(f"can only filter a list, got {_type(base)}")
            self._check_fields(node.predicate, base)
            kept = [x for x in base if _truthy(self.eval(node.predicate, x))]
            return Items(kept, _source(base) + "[...]", getattr(base, "fields", None))
        if isinstance(node, Project):
            base = self.eval(node.target, item)
            if not isinstance(base, list):
                raise ExprError(f"[*] needs a list, got {_type(base)}")
            out: list[Any] = []
            for x in base:
                v = self.field(x, node.name) if isinstance(x, dict) else None
                out.extend(v if isinstance(v, list) else [v])
            return out
        if isinstance(node, Unary):
            return not _as_bool(self.eval(node.operand, item))
        if isinstance(node, Binary):
            return self.binary(node, item)
        if isinstance(node, Call):
            return self.call(node, item)
        raise ExprError(f"cannot evaluate {type(node).__name__}")

    def _is_root(self, name: str, item: Any) -> bool:
        """Whether a bare name means a world root rather than a field of ``item``.

        Fields of the item being filtered win, so a collection named like a field
        never shadows the data.
        """
        if isinstance(item, dict) and name in item:
            return False
        return (name in _ROOT_SCALARS or name in _DELTA_ROOTS or name in ("trace", "egress")
                or name in self.world.final or name in self.world.seed)

    def _pure_path(self, node: Node) -> bool:
        cur = node
        while isinstance(cur, Attr):
            cur = cur.target
        return isinstance(cur, Name)

    def _check_fields(self, predicate: Node, base: list) -> None:
        """Reject a field name the collection never has: a typo must not read as "no matches"."""
        known = getattr(base, "fields", None)
        if not known:  # unknown shape (empty collection, projection) - nothing to check against
            return
        for node in walk(predicate):
            if isinstance(node, (Filter, Project)):
                break  # a nested selection has its own field namespace
            if isinstance(node, Name) and node.name not in known and not self._is_root(node.name, {}):
                raise ExprError(
                    f"{_source(base)} items have no field {node.name!r}; "
                    f"fields: {', '.join(sorted(known)[:25])}"
                )

    def field(self, item: Any, name: str) -> Any:
        if not isinstance(item, dict):
            raise ExprError(f"cannot read field {name!r} from {_type(item)}")
        return item.get(name)

    def attr(self, value: Any, name: str) -> Any:
        if isinstance(value, Items):
            if value.fields is not None and value and name not in value.fields:
                known = ", ".join(sorted(value.fields)[:25])
                raise ExprError(f"{value.source} items have no field {name!r}; fields: {known}")
            if len(value) != 1:
                what = "no items" if not value else f"{len(value)} items"
                raise ExprError(
                    f"{value.source}.{name} needs exactly one item but the selection has {what}; "
                    "narrow the filter, or use count()/any()/all()"
                )
            return self.field(value[0], name)
        if isinstance(value, dict):
            return value.get(name)
        if isinstance(value, list):
            raise ExprError(f"cannot read {name!r} from a list; use [*].{name} or a filter")
        if value is None:
            return None
        raise ExprError(f"cannot read {name!r} from {_type(value)}")

    def binary(self, node: Binary, item: Any) -> Any:
        op = node.op
        if op == "&&":
            return _as_bool(self.eval(node.left, item)) and _as_bool(self.eval(node.right, item))
        if op == "||":
            return _as_bool(self.eval(node.left, item)) or _as_bool(self.eval(node.right, item))
        left = self.eval(node.left, item)
        right = self.eval(node.right, item)
        return compare(op, left, right)

    def call(self, node: Call, item: Any) -> Any:
        fn, args = node.func, node.args
        if fn in ("count", "len"):
            _arity(fn, args, 1)
            value = self.eval(args[0], item)
            if isinstance(value, (list, str, dict)):
                return len(value)
            raise ExprError(f"{fn}() needs a list or string, got {_type(value)}")
        if fn == "exists":
            _arity(fn, args, 1)
            value = self.eval(args[0], item)
            if not isinstance(value, list):
                raise ExprError(f"exists() needs a list, got {_type(value)}")
            return len(value) > 0
        if fn in ("any", "all"):
            _arity(fn, args, 2)
            base = self.eval(args[0], item)
            if not isinstance(base, list):
                raise ExprError(f"{fn}() needs a list, got {_type(base)}")
            results = [_truthy(self.eval(args[1], x)) for x in base]
            return any(results) if fn == "any" else all(results)
        if fn in ("lower", "upper"):
            _arity(fn, args, 1)
            value = self.eval(args[0], item)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ExprError(f"{fn}() needs a string, got {_type(value)}")
            return value.lower() if fn == "lower" else value.upper()
        raise ExprError(f"unknown function {fn}(); available: count, exists, any, all, lower, upper, len")


def compare(op: str, left: Any, right: Any) -> bool:
    if op in ("==", "!="):
        if left is not None and right is not None and not _comparable(left, right):
            raise ExprError(
                f"cannot compare {_type(left)} {op} {_type(right)} "
                f"({_short(left)} vs {_short(right)})"
            )
        eq = _loose_equal(left, right)
        return eq if op == "==" else not eq
    if op in ("<", "<=", ">", ">="):
        if left is None or right is None:
            return False
        if isinstance(left, bool) or isinstance(right, bool) or not (
            isinstance(left, (int, float)) and isinstance(right, (int, float))
            or isinstance(left, str) and isinstance(right, str)
        ):
            raise ExprError(f"cannot compare {_type(left)} {op} {_type(right)}")
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
    if op in ("~", "!~"):
        if left is None:
            return op == "!~"
        if not isinstance(left, str):
            raise ExprError(f"regex match needs a string on the left, got {_type(left)}")
        pattern = right if isinstance(right, re.Pattern) else re.compile(str(right))
        matched = pattern.search(left) is not None
        return matched if op == "~" else not matched
    if op in ("in", "not in"):
        if isinstance(right, str):
            result = left is not None and str(left) in right
        elif isinstance(right, list):
            result = any(_loose_equal(left, x) for x in right)
        elif right is None:
            result = False
        else:
            raise ExprError(f"'in' needs a list or string on the right, got {_type(right)}")
        return result if op == "in" else not result
    if op == "contains":
        return compare("in", right, left)
    raise ExprError(f"unknown operator {op}")


def _comparable(a: Any, b: Any) -> bool:
    """Whether two non-null values are of comparable kinds (ints and floats are)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return True
    return type(a) is type(b) or (isinstance(a, (list, dict)) and isinstance(b, (list, dict)))


def _loose_equal(a: Any, b: Any) -> bool:
    # Numbers compare across int/float; everything else must match exactly.
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a == b


def diff_collection(seed: list[dict], final: list[dict], *, key_field: str = "id",
                    tombstone: str | None = None) -> dict[str, list[dict]]:
    """Split a collection into created / deleted / changed items by primary key."""
    def keyed(items: list[dict]) -> dict[Any, dict]:
        out: dict[Any, dict] = {}
        for i, item in enumerate(items):
            if isinstance(item, dict):
                out[_hashable(item.get(key_field, f"#{i}"))] = item
        return out

    before, after = keyed(seed), keyed(final)
    created, deleted, changed = [], [], []
    for k, item in after.items():
        dead = bool(tombstone and item.get(tombstone))
        if k not in before:
            if not dead:
                created.append(item)
            continue
        was_dead = bool(tombstone and before[k].get(tombstone))
        if dead and not was_dead:
            deleted.append(before[k])
        elif item != before[k]:
            changed.append(item)
    for k, item in before.items():
        if k not in after:
            deleted.append(item)
    return {"created": created, "deleted": deleted, "changed": changed}


def _hashable(v: Any) -> Any:
    return v if isinstance(v, (str, int, float, bool, type(None))) else repr(v)


def _arity(fn: str, args: Sequence[Node], n: int) -> None:
    if len(args) != n:
        raise ExprError(f"{fn}() takes {n} argument{'s' if n != 1 else ''}, got {len(args)}")


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise ExprError(f"expected true/false, got {_type(v)} ({_short(v)})")


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise ExprError(f"a filter must be a true/false condition, got {_type(v)} ({_short(v)})")


def _type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _short(v: Any, n: int = 60) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 1] + "..."


def _source(v: Any) -> str:
    return getattr(v, "source", "list")


def evaluate(text: str, world: World) -> Outcome:
    """Evaluate assertion ``text`` against ``world``: pass, fail, or error (never raises)."""
    try:
        tree = parse(text)
    except ExprSyntaxError as e:
        return Outcome("error", f"syntax error: {e}")
    ev = _Evaluator(world)
    try:
        value = ev.eval(tree)
    except ExprError as e:
        return Outcome("error", str(e))
    except re.error as e:
        return Outcome("error", f"invalid regex: {e}")
    if not isinstance(value, bool):
        return Outcome("error", f"assertion must be true/false, but evaluates to {_type(value)} ({_short(value)})")
    return Outcome("pass" if value else "fail", explain(tree, ev))


def explain(tree: Node, ev: _Evaluator) -> str:
    """Show the values that decided a comparison, e.g. ``count(created.github.issues) = 2, expected == 1``."""
    if isinstance(tree, Binary) and tree.op not in ("&&", "||"):
        try:
            left = ev.eval(tree.left)
            right = ev.eval(tree.right)
        except ExprError:
            return ""
        return f"{render(tree.left)} = {_short(_display(left))}, expected {tree.op} {_short(_display(right))}"
    if isinstance(tree, Call) and tree.func == "exists":
        try:
            n = len(ev.eval(tree.args[0]))
        except (ExprError, TypeError):
            return ""
        return f"{render(tree.args[0])} matched {n} item{'s' if n != 1 else ''}"
    return ""


def _display(v: Any) -> Any:
    if isinstance(v, re.Pattern):
        return f"/{v.pattern}/"
    if isinstance(v, list) and len(v) > 5:
        return f"[{len(v)} items]"
    return v


def render(node: Node) -> str:
    """Pretty-print an AST back to assertion text."""
    if isinstance(node, Literal):
        v = node.value
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(v, re.Pattern):
            return f"/{v.pattern}/" + ("i" if v.flags & re.I else "")
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)
    if isinstance(node, Name):
        return node.name
    if isinstance(node, Attr):
        return f"{render(node.target)}.{node.name}"
    if isinstance(node, Filter):
        return f"{render(node.target)}[{render(node.predicate)}]"
    if isinstance(node, Project):
        return f"{render(node.target)}[*].{node.name}"
    if isinstance(node, Call):
        return f"{node.func}({', '.join(render(a) for a in node.args)})"
    if isinstance(node, Unary):
        return f"!{render(node.operand)}"
    if isinstance(node, Binary):
        return f"{render(node.left)} {node.op} {render(node.right)}"
    if isinstance(node, ListLit):
        return "[" + ", ".join(render(i) for i in node.items) + "]"
    return "?"


def _root_name(node: Node) -> str:
    while isinstance(node, (Attr, Filter, Project)):
        node = node.target
    return node.name if isinstance(node, Name) else ""


def _children(node: Node) -> list[Node]:
    out: list[Node] = []
    for attr in ("target", "predicate", "operand", "left", "right"):
        child = getattr(node, attr, None)
        if isinstance(child, Node):
            out.append(child)
    for group in ("args", "items"):
        out.extend(getattr(node, group, ()) or ())
    return out


def walk(node: Node):
    """Yield every node in the tree, parents first."""
    yield node
    for child in _children(node):
        yield from walk(child)


def referenced_twins(text: str) -> set[str]:
    """Twin names an assertion reads, so a scenario can be checked against its sandbox."""
    found: set[str] = set()
    for node in walk(parse(text)):
        if not isinstance(node, Attr):
            continue
        parts: list[str] = []
        cur: Node = node
        while isinstance(cur, Attr):
            parts.append(cur.name)
            cur = cur.target
        if not isinstance(cur, Name):
            continue
        parts.append(cur.name)
        parts.reverse()
        if parts[0] in _DELTA_ROOTS and len(parts) > 1:
            found.add(parts[1])
        elif parts[0] not in ("trace", "egress", *_ROOT_SCALARS):
            found.add(parts[0])
    return found
