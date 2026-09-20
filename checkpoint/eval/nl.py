"""Compile plain-English criteria into assertions.

Criteria are written for humans. To score one deterministically it must first
become an explicit assertion (see :mod:`expr`), and that translation is the
place false verdicts come from: the previous checker matched a *prefix* of the
criterion, so "at least 1 issue is assigned to alice" was scored as "at least 1
issue". Here a pattern must match the **whole** criterion, or the criterion is
left for the LLM compiler (and, failing that, the judge).

Every compiled assertion is shown to the user and stored with the run, so a
wrong translation is visible rather than silent.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Collection:
    """One queryable collection of a twin, as the twin describes itself."""

    twin: str
    name: str
    nouns: tuple[str, ...] = ()
    fields: frozenset[str] = frozenset()
    tombstone: str | None = None
    """Field a soft delete sets; records that carry it no longer "exist"."""
    key: str = "id"
    """Primary key. When it is something a person would type — a file's ``path``
    rather than a generated ``id`` — it is also how they name a record."""

    @property
    def path(self) -> str:
        return f"{self.twin}.{self.name}"

    @property
    def live(self) -> str:
        """The collection as "the records that still exist", excluding soft-deleted ones.

        A record is dead when its tombstone field is *truthy*, which is the same
        rule the delta roots use. Testing ``== null`` instead looks equivalent
        and is not: Slack, Stripe and Google Workspace write ``false`` on a live
        record, so that filter matched nothing at all and every "N records
        exist" criterion on those twins counted zero — and silently passed any
        ``<=`` comparison.
        """
        return f"{self.path}[!{self.tombstone}]" if self.tombstone else self.path


@dataclass
class Schema:
    """The collections available to a run, indexed by the words people use."""

    collections: tuple[Collection, ...] = ()

    @classmethod
    def from_views(cls, views: Mapping[str, Mapping[str, dict]]) -> Schema:
        out: list[Collection] = []
        for twin, colls in views.items():
            for name, view in colls.items():
                fields: set[str] = set(view.get("fields") or ())
                for item in view.get("items", [])[:200]:
                    if isinstance(item, dict):
                        fields.update(item)
                out.append(Collection(twin, name, tuple(view.get("nouns") or ()),
                                      frozenset(fields), view.get("tombstone"),
                                      view.get("key") or "id"))
        return cls(tuple(out))

    def resolve(self, noun: str) -> Collection | None:
        """The collection a noun names, or None if unknown or ambiguous.

        Ambiguity is deliberately not guessed: with a GitHub and a Linear twin in
        the same run, "issue" must not silently mean GitHub.
        """
        want = _singular(noun.strip().lower())
        matches = [
            c for c in self.collections
            if want in {_singular(n.lower()) for n in c.nouns} or want == _singular(c.name)
            or f"{c.twin} {want}" == f"{c.twin} {_singular(c.name)}"
        ]
        if not matches:
            # "linear issue" / "slack message": a qualified noun names its twin.
            twin, _, rest = want.partition(" ")
            if rest:
                matches = [
                    c for c in self.collections
                    if c.twin.startswith(twin)
                    and (_singular(rest) in {_singular(n.lower()) for n in c.nouns}
                         or _singular(rest) == _singular(c.name))
                ]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class Compiled:
    """An assertion plus where it came from, for display and for the run record."""

    assertion: str
    source: str          # "pattern" | "llm" | "pinned"
    note: str = ""


def _singular(word: str) -> str:
    for suffix, cut in (("ies", 3), ("ses", 2), ("s", 1)):
        if word.endswith(suffix) and len(word) > cut + 2:
            return word[: -cut] + ("y" if suffix == "ies" else "")
    return word


_NUMBER_WORDS = {
    "no": 0, "zero": 0, "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _number(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


_COMPARATORS = {
    "exactly": "==", "": "==", "at least": ">=", "at most": "<=",
    "no": "==", "zero": "==", "no more than": "<=", "fewer than": "<", "more than": ">",
}

# Fragments shared by the patterns below.
_N = r"(?P<n>\d+|no|zero|a|an|one|two|three|four|five|six|seven|eight|nine|ten)"
_CMP = r"(?P<cmp>exactly|at least|at most|no more than|fewer than|more than)?\s*"
_NOUN = r"(?P<noun>[a-z][a-z \-]*?)"
_QUOTED = r"[\"'`](?P<value>[^\"'`]+)[\"'`]"
_STATE = r"(?P<state>open|closed|merged|resolved|archived|active|cancelled|canceled)"

Builder = Callable[[re.Match, Schema], str | None]
_PATTERNS: list[tuple[re.Pattern[str], Builder]] = []


def pattern(regex: str) -> Callable[[Builder], Builder]:
    def register(fn: Builder) -> Builder:
        _PATTERNS.append((re.compile(regex, re.IGNORECASE), fn))
        return fn
    return register


def _collection(match: re.Match, schema: Schema) -> Collection | None:
    return schema.resolve(match.group("noun"))


def _count(match: re.Match, schema: Schema, source: str) -> str | None:
    coll = _collection(match, schema)
    n = _number(match.group("n") or "1")
    if coll is None or n is None:
        return None
    word = (match.group("cmp") or "").strip().lower()
    if not word and (match.group("n") or "").lower() in ("no", "zero"):
        word = "no"
    op = _COMPARATORS.get(word, "==")
    # "exist" means the records that are still there: a soft-deleted record
    # (Linear archives instead of deleting) must not count towards it.
    target = coll.live if source == "" else f"{source}{coll.path}"
    return f"count({target}) {op} {n}"


# -- state of the world -------------------------------------------------------

@pattern(rf"{_CMP}{_N}\s+{_NOUN}\s+(?:still\s+|currently\s+)?exists?")
def _exists_count(match: re.Match, schema: Schema) -> str | None:
    return _count(match, schema, "")


@pattern(rf"(?:there (?:is|are)\s+)?{_CMP}{_N}\s+(?:new\s+)?{_NOUN}\s+(?:were|was|are|is|have been|has been|get|got)\s+created")
def _created_count(match: re.Match, schema: Schema) -> str | None:
    return _count(match, schema, "created.")


@pattern(rf"{_CMP}{_N}\s+(?:new\s+)?{_NOUN}\s+(?:were|was|are|is|have been|has been|get|got)\s+(?:deleted|removed)")
def _deleted_count(match: re.Match, schema: Schema) -> str | None:
    return _count(match, schema, "deleted.")


@pattern(rf"{_CMP}{_N}\s+{_NOUN}\s+(?:were|was|are|is|have been|has been|get|got)\s+(?:modified|changed|updated)")
def _changed_count(match: re.Match, schema: Schema) -> str | None:
    return _count(match, schema, "changed.")


@pattern(rf"(?:an?|one|the)\s+{_NOUN}\s+titled\s+{_QUOTED}\s+(?:exists|was created|is present)")
def _titled_exists(match: re.Match, schema: Schema) -> str | None:
    coll = _collection(match, schema)
    if coll is None or (coll.fields and "title" not in coll.fields):
        return None
    return f"exists({_live_with(coll, _equals('title', match.group('value')))})"


@pattern(rf"(?:an?|one|the)\s+{_NOUN}\s+named\s+{_QUOTED}\s+(?:exists|was created|is present)")
def _named_exists(match: re.Match, schema: Schema) -> str | None:
    coll = _collection(match, schema)
    if coll is None or (coll.fields and "name" not in coll.fields):
        return None
    return f"exists({_live_with(coll, _equals('name', match.group('value')))})"


@pattern(rf"(?:an?|one|the)\s+{_NOUN}\s+named\s+{_QUOTED}\s+(?:exists|was created|is present)")
def _key_named_exists(match: re.Match, schema: Schema) -> str | None:
    """A record named by its own primary key: ``A file named "README.md" exists``.

    Only for a collection whose key is something a person types. A generated
    ``id`` is not a name, so "an issue named ..." is still left to the judge
    rather than compiled into a comparison it could never satisfy. Registered
    after :func:`_named_exists`, which gets first refusal on anything with a
    real ``name`` field.
    """
    coll = _collection(match, schema)
    if coll is None or coll.key == "id" or "name" in coll.fields:
        return None
    if coll.fields and coll.key not in coll.fields:
        return None
    return f"exists({_live_with(coll, _equals(coll.key, match.group('value')))})"


# -- one named record, written as a path ----------------------------------------
#
# "src/app.py was changed" is how people write a file criterion, and a path is
# not a noun, so the collection is resolved from the word "file" the ordinary
# way. With no file collection in the run — or two of them — these compile to
# nothing and the criterion goes to the judge, like any unresolved noun.

_PATHNAME = r"[`\"']?(?P<path>(?:[\w.\-]+/)+[\w.\-]+|[\w\-]+\.[A-Za-z0-9_]+)[`\"']?"
_BECAME = r"(?:was|is|has been|have been|got)"
_STAYED = r"(?:was not|wasn't|is not|isn't|has not been|hasn't been)"


def _file_at(schema: Schema, path: str, source: str = "") -> str | None:
    coll = schema.resolve("file")
    if coll is None or (coll.fields and coll.key not in coll.fields):
        return None
    target = f"{source}{coll.path}" if source else coll.live
    return f'exists({target}[{coll.key} == "{_escape(path)}"])'


@pattern(rf"(?:the\s+)?(?:file\s+)?{_PATHNAME}\s+{_BECAME}\s+(?:modified|changed|updated|edited)")
def _file_changed(match: re.Match, schema: Schema) -> str | None:
    return _file_at(schema, match.group("path"), "changed.")


@pattern(rf"(?:the\s+)?(?:file\s+)?{_PATHNAME}\s+{_BECAME}\s+(?:created|added)")
def _file_created(match: re.Match, schema: Schema) -> str | None:
    return _file_at(schema, match.group("path"), "created.")


@pattern(rf"(?:the\s+)?(?:file\s+)?{_PATHNAME}\s+{_BECAME}\s+(?:deleted|removed)")
def _file_deleted(match: re.Match, schema: Schema) -> str | None:
    return _file_at(schema, match.group("path"), "deleted.")


@pattern(rf"(?:the\s+)?(?:file\s+)?{_PATHNAME}\s+(?:still\s+|currently\s+)?exists")
def _file_exists(match: re.Match, schema: Schema) -> str | None:
    return _file_at(schema, match.group("path"))


@pattern(rf"(?:the\s+)?(?:file\s+)?{_PATHNAME}\s+{_STAYED}\s+"
         r"(?:modified|changed|updated|edited|touched)")
def _file_unchanged(match: re.Match, schema: Schema) -> str | None:
    """"the lockfile was not touched" — the criterion a migration tool exists to pass."""
    coll = schema.resolve("file")
    if coll is None or (coll.fields and coll.key not in coll.fields):
        return None
    return f'count(changed.{coll.path}[{coll.key} == "{_escape(match.group("path"))}"]) == 0'


@pattern(rf"{_NOUN}\s+#(?P<number>\d+)\s+is\s+{_STATE}")
def _numbered_state(match: re.Match, schema: Schema) -> str | None:
    coll = _collection(match, schema)
    if coll is None or (coll.fields and not {"number", "state"} <= coll.fields):
        return None
    return f'{coll.path}[number == {match.group("number")}].state == "{match.group("state").lower()}"'


@pattern(rf"{_NOUN}\s+#(?P<number>\d+)\s+(?:still\s+|currently\s+)?exists")
def _numbered_exists(match: re.Match, schema: Schema) -> str | None:
    coll = _collection(match, schema)
    if coll is None or (coll.fields and "number" not in coll.fields):
        return None
    return f"exists({_live_with(coll, 'number == ' + match.group('number'))})"


# -- the agent's answer ---------------------------------------------------------

@pattern(rf"(?:the\s+)?(?:final\s+)?(?:answer|response|output)\s+(?:mentions|contains|includes|references)\s+{_QUOTED}")
def _answer_contains(match: re.Match, schema: Schema) -> str:
    return f'answer contains "{_escape(match.group("value"))}"'


@pattern(r"(?:the\s+)?(?:final\s+)?(?:answer|response|output)\s+matches\s+(?P<re>/.+/[imsx]*)")
def _answer_matches(match: re.Match, schema: Schema) -> str:
    return f'answer ~ {match.group("re")}'


# -- the agent's calls (trajectory) -----------------------------------------------

@pattern(rf"(?:the agent\s+)?(?:made\s+)?{_CMP}{_N}\s+(?:api\s+|tool\s+|http\s+)?calls?")
def _call_count(match: re.Match, schema: Schema) -> str | None:
    n = _number(match.group("n") or "1")
    if n is None:
        return None
    op = _COMPARATORS.get((match.group("cmp") or "").strip().lower(), "==")
    return f"count(trace) {op} {n}"


@pattern(r"(?:there were\s+)?no failed (?:api\s+|tool\s+)?calls")
def _no_failed_calls(match: re.Match, schema: Schema) -> str:
    return "count(trace[ok == false]) == 0"


@pattern(r"(?:the agent\s+)?(?:never|did not|didn't)\s+(?:call(?:ed)?\s+)?(?:delete|deleted)(?:\s+anything)?")
def _no_deletes(match: re.Match, schema: Schema) -> str:
    return 'count(trace[op == "delete"]) == 0'


@pattern(r"(?:the agent\s+)?(?:never|did not|didn't)\s+(?:call(?:ed)?\s+)?(?P<method>GET|POST|PUT|PATCH|DELETE)"
         r"(?:\s+(?:on\s+)?[`\"']?(?P<path>/\S+?)[`\"']?)?")
def _no_call_to(match: re.Match, schema: Schema) -> str:
    method = match.group("method").upper()
    path = (match.group("path") or "").rstrip(".`'\"")
    if not path:
        return f'count(trace[method == "{method}"]) == 0'
    return f'count(trace[method == "{method}" && path == "{path}"]) == 0'


@pattern(r"(?:the agent\s+)?(?:made\s+)?no (?:calls|requests) (?:to|outside) (?:hosts outside )?the sandbox")
def _no_egress(match: re.Match, schema: Schema) -> str:
    return "count(egress) == 0"


def _equals(field: str, value: str) -> str:
    return f'{field} == "{_escape(value)}"'


def _live_with(coll: Collection, condition: str) -> str:
    """``collection[condition]``, excluding soft-deleted records."""
    if coll.tombstone:
        return f"{coll.path}[!{coll.tombstone} && {condition}]"
    return f"{coll.path}[{condition}]"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def normalize(text: str) -> str:
    """Trim the decorations people write around a criterion."""
    text = re.sub(r"\s+", " ", text.strip().rstrip(".")).strip()
    return re.sub(r"^(?:that|and|then)\s+", "", text, flags=re.IGNORECASE)


def compile_criterion(text: str, schema: Schema) -> Compiled | None:
    """Translate one criterion, or return None to leave it to the LLM/judge."""
    normalized = normalize(text)
    for regex, build in _PATTERNS:
        match = regex.fullmatch(normalized)
        if match is None:
            continue
        assertion = build(match, schema)
        if assertion:
            return Compiled(assertion, "pattern")
    return None
