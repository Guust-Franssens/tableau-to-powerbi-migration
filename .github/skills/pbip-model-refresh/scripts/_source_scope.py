"""
purpose: admit only exact engine connector partitions whose identities equal a caller-supplied set (#780)
usage:   imported by _desktop_actions.py for `refresh_pbip_model.py --automatic-source-scope`; pure except
         load_expected_sources, with no repo imports so the skill folder stays portable
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Second connector argument key per admitted root; Oracle's second argument is a fixed option record.
SECOND_KEY = {
    "Sql.Database": "database",
    "PostgreSQL.Database": "database",
    "MySQL.Database": "database",
    "AmazonRedshift.Database": "database",
    "Snowflake.Databases": "warehouse",
    "Databricks.Catalogs": "http_path",
    "Oracle.Database": None,
}
_BASE_KEYS = {"table", "connector", "server", "navigation"}
_ENTRY_KEYS = _BASE_KEYS | {"database", "warehouse", "http_path"}
_NAV_FIELDS = {"Schema", "Item", "Name", "Kind"}
_KEYWORDS = set("and as each else error if in is let meta not null or otherwise section shared then try type".split())
_TOKEN = re.compile(
    r'(?P<ws>\s+)|(?P<text>"(?:[^"]|"")*")|(?P<qname>#"(?:[^"]|"")*")'
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.]*)|(?P<p>[=,()\[\]{}])"
)
_TEMPLATE = "not the connector template"


class SourceScopeError(ValueError):
    """The model or the expected-sources input is outside the automatic source scope."""


def tokenize(m_text: str) -> list[tuple[str, str]]:
    """Split M into (kind, value) tokens; comments, numbers, operators and other characters refuse."""
    tokens, position = [], 0
    while position < len(m_text):
        match = _TOKEN.match(m_text, position)
        if match is None:
            raise SourceScopeError("unsupported M token")
        position, kind, raw = match.end(), match.lastgroup, match.group()
        if kind in {"text", "qname"}:
            tokens.append(("text" if kind == "text" else "id", raw[raw.index('"') + 1 : -1].replace('""', '"')))
        elif kind == "name":
            tokens.append(("bool" if raw in {"true", "false"} else "kw" if raw in _KEYWORDS else "id", raw))
        elif kind == "p":
            tokens.append(("p", raw))
    return [*tokens, ("end", "")]


class _Cursor:
    """Token cursor whose every mismatch refuses."""

    def __init__(self, m_text: str) -> None:
        self.tokens = tokenize(m_text)
        self.index = 0

    def take(self, kinds: str, value: str | None = None) -> tuple[str, str]:
        """Consume one token of one of the space-separated kinds (and exact value), or refuse."""
        token = self.tokens[self.index]
        if token[0] not in kinds.split() or value not in (None, token[1]):
            raise SourceScopeError(_TEMPLATE)
        self.index += 1
        return token

    def skip(self, kind: str, value: str) -> bool:
        """Consume the next token only when it is exactly (kind, value)."""
        found = self.tokens[self.index] == (kind, value)
        self.index += found
        return found

    def record(self, kinds: str) -> dict[str, str]:
        """Parse `[ F = V (, F = V)* ]` with each V a token of one of the given kinds."""
        self.take("p", "[")
        fields: dict[str, str] = {}
        while True:
            name = self.take("id")[1]
            self.take("p", "=")
            if name in fields:
                raise SourceScopeError(_TEMPLATE)
            fields[name] = self.take(kinds)[1]
            if not self.skip("p", ","):
                self.take("p", "]")
                return fields


def parameter_value(expression: str) -> str:
    """S1: a shared expression must be one text literal, optionally with a literal `meta` record."""
    cursor = _Cursor(expression)
    value = cursor.take("text")[1]
    if cursor.skip("kw", "meta"):
        cursor.record("text bool")
    cursor.take("end")
    return value


def resolve_partition_identity(m_text: str, expressions: dict[str, str]) -> dict[str, object]:
    """S2: resolve one exact engine connector partition to its source identity, or refuse."""
    cursor = _Cursor(m_text)
    cursor.take("kw", "let")
    bindings = [cursor.take("id")[1]]
    cursor.take("p", "=")
    connector = cursor.take("id")[1]
    if connector not in SECOND_KEY:
        raise SourceScopeError("connector is not admitted")
    cursor.take("p", "(")
    arguments = [cursor.take("text id")]
    cursor.take("p", ",")
    if SECOND_KEY[connector] is None and cursor.record("bool") != {"HierarchicalNavigation": "false"}:
        raise SourceScopeError(_TEMPLATE)
    if SECOND_KEY[connector] is not None:
        arguments.append(cursor.take("text id"))
    cursor.take("p", ")")
    navigation: list[dict[str, str]] = []
    while cursor.skip("p", ","):
        step = cursor.take("id")[1]
        cursor.take("p", "=")
        cursor.take("id", bindings[-1])
        cursor.take("p", "{")
        hop = cursor.record("text")
        for kind, value in (("p", "}"), ("p", "["), ("id", "Data"), ("p", "]")):
            cursor.take(kind, value)
        if not set(hop) <= _NAV_FIELDS or step in bindings:
            raise SourceScopeError(_TEMPLATE)
        navigation.append(hop)
        bindings.append(step)
    cursor.take("kw", "in")
    cursor.take("id", bindings[-1])
    cursor.take("end")
    if not 1 <= len(navigation) <= 3:
        raise SourceScopeError(_TEMPLATE)
    values = []
    for kind, value in arguments:
        if kind == "id" and (value in bindings or value not in expressions):
            raise SourceScopeError("argument is not a parameter")
        values.append(parameter_value(expressions[value]) if kind == "id" else value)
    identity: dict[str, object] = {"connector": connector, "server": values[0], "navigation": navigation}
    if SECOND_KEY[connector]:
        identity[SECOND_KEY[connector]] = values[1]
    return identity


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise SourceScopeError("expected sources: input")
    return dict(pairs)


def _valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or not _BASE_KEYS <= set(entry) <= _ENTRY_KEYS:
        return False
    hops = entry["navigation"]
    return (
        all(isinstance(value, str) for key, value in entry.items() if key != "navigation")
        and isinstance(hops, list)
        and all(isinstance(hop, dict) and all(isinstance(v, str) for v in hop.values()) for hop in hops)
    )


def load_expected_sources(path: str | Path) -> dict[str, dict[str, object]]:
    """Read the caller's expected identity set; any unreadable, malformed or unknown input refuses."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_strict_pairs)
    except (OSError, ValueError) as exc:
        raise SourceScopeError("expected sources: input") from exc
    if not isinstance(document, dict) or set(document) != {"version", "sources"} or document["version"] != 1:
        raise SourceScopeError("expected sources: input")
    sources = document["sources"]
    if isinstance(document["version"], (bool, float)) or not isinstance(sources, list) or not sources:
        raise SourceScopeError("expected sources: input")
    if not all(_valid_entry(entry) for entry in sources) or len({entry["table"] for entry in sources}) != len(sources):
        raise SourceScopeError("expected sources: input")
    return {entry["table"]: entry for entry in sources}


def compare_sources(resolved: dict[str, dict[str, object]], expected: dict[str, dict[str, object]]) -> None:
    """S4: exact set and field equality; the refusal names the table and field, never a value."""
    for table in sorted(set(resolved) | set(expected)):
        if table not in resolved or table not in expected:
            raise SourceScopeError(f"expected sources: {table}: {'missing' if table in expected else 'unexpected'}")
        actual, wanted = resolved[table], expected[table]
        second = SECOND_KEY[str(actual["connector"])]
        for field in ("connector", "server", second, "navigation", *sorted(set(wanted) - _BASE_KEYS - {second})):
            if field and (field not in wanted or field not in actual or wanted[field] != actual[field]):
                raise SourceScopeError(f"expected sources: {table}: {field}")


def check_source_scope(
    partitions: list[tuple[str, str, object]],
    expressions: dict[str, object],
    expected: dict[str, dict[str, object]],
    canaries: list[str],
) -> None:
    """Apply S1-S4 to (table, kind, query) partition rows and name-to-text shared expressions."""
    for name, text in expressions.items():
        try:
            parameter_value(text if isinstance(text, str) else "")
        except SourceScopeError as exc:
            raise SourceScopeError(f"{name}: expression is not a literal parameter") from exc
    resolved: dict[str, dict[str, object]] = {}
    for table, kind, query in partitions:
        if kind == "Calculated":
            continue
        if kind != "M" or not isinstance(query, str) or table in resolved:
            raise SourceScopeError(f"{table}: partition is not one M partition")
        try:
            resolved[table] = resolve_partition_identity(query, expressions)  # type: ignore[arg-type]
        except SourceScopeError as exc:
            raise SourceScopeError(f"{table}: {exc}") from exc
    for canary in canaries:
        if canary not in resolved:
            raise SourceScopeError(f"{canary}: canary is not a connector table")
    compare_sources(resolved, expected)
