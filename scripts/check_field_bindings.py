"""
purpose: catch the CROSS-LAYER defect neither layer's validator can see - a PBIR field reference
         that resolves to no field in the semantic model beside it, with case-only near-misses
         called out separately because that is the signature of a model-layer rename.
usage:   python scripts/check_field_bindings.py <bundle-or-report-dir> [...]
         python scripts/check_field_bindings.py --model <x.SemanticModel> --report <x.Report>
                                                [--json <file>] [--quiet] [--warn-only]

Why this exists
---------------
Normalising a shared table's column names at the MODEL layer (issue #236: folding Snowflake
identifiers with `Table.TransformColumnNames(..., Text.Upper(_))`) silently invalidates every PBIR
binding already written against the old casing. Both single-layer gates stay green:

    check_datamodel.py   -> the model is structurally fine
    check_pbir_valid.py  -> the report is structurally fine
    Power BI Desktop     -> "Fields that need to be fixed", per visual, at OPEN time

The inconsistency lives BETWEEN the layers, which is exactly where nothing looked. Measured in the
field on a 12-workbook estate: PBIR referenced `SLA_ACPU_Down_Duration` while the post-fix column
was `SLA_ACPU_DOWN_DURATION`; a second workbook showed the same shape across ~15 fields.

Why case-insensitive near-misses are their own category
-------------------------------------------------------
A reference that fails exactly but matches case-insensitively is almost never a missing field - it
is a rename that was applied to one layer only. Printing BOTH spellings turns a per-visual Desktop
modal into a mechanical find-and-replace, so this category is labelled and rendered separately from
a genuinely absent field, which is a different (and usually larger) problem.

Scope: `pbip/` only, and a report is only checked against ITS model
------------------------------------------------------------------
Like `check_pbir_valid.py`, a bundle is scanned through `<bundle>/pbip/` because only that ships;
`<bundle>/reports/` is the engine's reference-only baseline with no model beside it, so every
reference there would report unresolved and say nothing about the deliverable. The model for a
report is resolved from its own `definition.pbir` `datasetReference.byPath`, falling back to a
sibling `<name>.SemanticModel` - never "the first model found nearby", which would silently grade a
report against a model it does not ship with.

What it will NOT tell you
-------------------------
That the report is CORRECT. Names resolving is necessary, not sufficient: a binding can aggregate
the wrong way or filter to nothing. It also cannot see anything a name does not carry - data types,
row counts, or whether the model loads at all (`check_empty_model.py` is the gate for that).

Table agreement (issue #258)
----------------------------
Resolving a name is not the same as resolving it on the RIGHT table, and the difference is a hard
render failure. Field report, verbatim, on a live estate's `IA_Aircraft_Installs`:

    "a field existing under the identical name on BOTH the referenced table and the correct table
     produces no gate warning - it only surfaces later as InvalidUnconstrainedJoin ("Can't
     determine relationships between the fields"), and only after the genuinely-missing fields are
     fixed."

That ordering is what makes a silent pass actively misleading rather than merely incomplete: fix
the real errors this gate found, re-run, get a clean bill of health, and only THEN discover the
report still does not render. So the per-visual question is now "do these fields agree on a table
set Power BI can join?", asked in the SAME pass as the per-field one, and both are reported
together - the masking is what has to go.

`INCOHERENT` is a new top-level status (see `render`), and it exits 1 like `UNRESOLVED`. A caller
testing `status == "OK"` is unaffected; a caller testing `status != "UNRESOLVED"` would now let a
non-rendering report through, which is why the exit code, not the string, is the contract.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from bundle_corpus import shipping_reports

REPORT_NAME = "field-binding-check.json"

# TMDL member declarations. Names are either bare or single-quoted; a measure/calculated column
# carries `= <DAX>` on the same line, which is why the name group stops at `=`. An apostrophe INSIDE
# a quoted name is doubled (`column 'Sondheim''s Work'`, measured in
# examples/broadway-stage-to-screen), so a naive `'[^']*'` truncates the name and then reports the
# report's perfectly good binding as missing - a false positive on committed, shipping data.
_NAME = r"'(?:[^']|'')*'|[^\s=]+"
_TABLE_RE = re.compile(rf"^table\s+(?P<name>{_NAME})\s*$")
_RELATIONSHIP_RE = re.compile(rf"^relationship\s+(?P<name>{_NAME})\s*$")
_MEMBER_RE = re.compile(rf"^(?P<indent>[\t ]+)(?P<kind>column|measure|hierarchy|level)\s+(?P<name>{_NAME})")
_PROPERTY_RE = re.compile(r"^\s*(?P<key>[A-Za-z]+)\s*:\s*(?P<value>.+?)\s*$")

# A table that is DELIBERATELY not joined to anything, and whose presence in a visual therefore
# says nothing about a broken binding. Field parameters (`NAMEOF` inside a calculated partition, or
# Desktop's `ParameterMetadata` marker) and calculation groups are both substituted at query
# generation, so they never form the unconstrained join the table-agreement check hunts for.
_PARAMETER_METADATA_RE = re.compile(r"extendedProperty\s+ParameterMetadata")
_NAMEOF_RE = re.compile(r"\bNAMEOF\s*\(")
_CALC_GROUP_RE = re.compile(r"^\s*calculationGroup\b", re.MULTILINE)

# PBIR reference nodes. `Column`/`Measure` carry `Property`; `HierarchyLevel` carries `Level` and
# wraps a `Hierarchy` node. Everything else (`Aggregation`, `FillRule`, `Subquery`, ...) merely
# nests one of these, so the walk below is generic rather than path-driven.
_SCALAR_KINDS = ("Column", "Measure")

NUMERIC_DATA_TYPES = frozenset(
    {"int64", "int32", "int16", "int8", "integer", "int", "double", "decimal", "currency",
     "single", "float", "real", "numeric"}
)
STRING_DATA_TYPES = frozenset({"string", "text"})
KNOWN_NON_STRING_NON_NUMERIC_DATA_TYPES = frozenset({"boolean", "bool", "datetime", "date", "time", "binary"})
NON_NUMERIC_DATA_TYPES = frozenset(STRING_DATA_TYPES | KNOWN_NON_STRING_NON_NUMERIC_DATA_TYPES)
KNOWN_NON_STRING_DATA_TYPES = frozenset(NUMERIC_DATA_TYPES | KNOWN_NON_STRING_NON_NUMERIC_DATA_TYPES)


@dataclass
class TableFields:
    """Every name a PBIR reference can legally resolve to on one table."""

    columns: set[str] = field(default_factory=set)
    measures: set[str] = field(default_factory=set)
    hierarchies: dict[str, set[str]] = field(default_factory=dict)
    column_types: dict[str, str | None] = field(default_factory=dict)
    measure_types: dict[str, str | None] = field(default_factory=dict)


@dataclass
class Relationship:
    """One TMDL `relationship` block, reduced to the join it declares."""

    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True


@dataclass
class ModelFields:
    """The semantic model reduced to the only things this gate compares: names and joins."""

    tables: dict[str, TableFields] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    # table -> why it may legitimately sit alone in a visual (field parameter, calculation group)
    detached_ok: dict[str, str] = field(default_factory=dict)
    _components: dict[bool, dict[str, str]] = field(default_factory=dict, repr=False)

    def table(self, name: str) -> TableFields | None:
        """Exact-case lookup of one table."""
        return self.tables.get(name)

    def table_ci(self, name: str) -> tuple[str, TableFields] | None:
        """Case-insensitive lookup, returning the model's own spelling."""
        lowered = name.casefold()
        for actual, fields_ in self.tables.items():
            if actual.casefold() == lowered:
                return actual, fields_
        return None

    def field_type(self, entity: str, prop: str) -> tuple[str | None, str | None]:
        """Return (kind, dataType) for entity[prop], or (None, None) if not resolved.

        kind is 'Column' or 'Measure' (or None).
        dataType is e.g. 'string', 'double', or None if untyped in TMDL.
        """
        found = self.table(entity)
        if found is None:
            found_ci = self.table_ci(entity)
            if found_ci is None:
                return None, None
            _, fields_ = found_ci
        else:
            fields_ = found

        if prop in fields_.measures:
            return "Measure", fields_.measure_types.get(prop)
        if prop in fields_.columns:
            return "Column", fields_.column_types.get(prop)

        lowered = prop.casefold()
        for m in fields_.measures:
            if m.casefold() == lowered:
                return "Measure", fields_.measure_types.get(m)
        for c in fields_.columns:
            if c.casefold() == lowered:
                return "Column", fields_.column_types.get(c)

        return None, None

    def components(self, *, include_inactive: bool = False) -> dict[str, str]:
        """Casefolded table name -> the id of the relationship component it belongs to.

        Union-find over relationships, because "can Power BI join these two tables?" is a
        reachability question, not an adjacency one: `Installs -> Aircraft -> Manufacturer` is a
        perfectly ordinary star/snowflake path and must not read as three unrelated tables.
        """
        cached = self._components.get(include_inactive)
        if cached is not None:
            return cached
        parent: dict[str, str] = {name.casefold(): name.casefold() for name in self.tables}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for rel in self.relationships:
            if not (rel.is_active or include_inactive):
                continue
            left, right = find(rel.from_table.casefold()), find(rel.to_table.casefold())
            if left != right:
                parent[left] = right
        resolved = {node: find(node) for node in list(parent)}
        self._components[include_inactive] = resolved
        return resolved

    def group_tables(self, names: Iterable[str], *, include_inactive: bool = False) -> list[list[str]]:
        """Partition table names into the joinable sets Power BI would see."""
        components = self.components(include_inactive=include_inactive)
        groups: dict[str, list[str]] = {}
        for name in names:
            key = components.get(name.casefold(), name.casefold())
            bucket = groups.setdefault(key, [])
            if name not in bucket:
                bucket.append(name)
        return sorted((sorted(bucket) for bucket in groups.values()), key=lambda bucket: bucket[0])

    def tables_carrying(self, prop: str) -> list[str]:
        """Every table whose columns include this name - the "which one did you mean?" candidates."""
        lowered = prop.casefold()
        return sorted(
            name for name, fields_ in self.tables.items() if any(c.casefold() == lowered for c in fields_.columns)
        )


@dataclass
class FieldRef:
    """One field reference found in PBIR, with enough context to fix it by hand."""

    kind: str
    entity: str
    prop: str
    file: Path
    hierarchy: str | None = None


def _unquote(name: str) -> str:
    """Strip TMDL's single-quoting from an object name."""
    name = name.strip()
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def _split_qualified(value: str) -> tuple[str, str] | None:
    """Split a relationship endpoint (`'Sample Superstore'.'Order Date'`, `Date.Date`) in two.

    Written by hand rather than as a regex: either half may be quoted, and a quoted half may
    contain a dot AND a doubled apostrophe, so `partition(".")` alone mis-splits real committed
    models.
    """
    value = value.strip()
    if value.startswith("'"):
        index = 1
        while index < len(value):
            if value[index] == "'":
                if value[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                break
            index += 1
        else:
            return None
        table, rest = value[: index + 1], value[index + 1 :]
        if not rest.startswith("."):
            return None
        column = rest[1:]
    else:
        table, separator, column = value.partition(".")
        if not separator:
            return None
    if not table.strip() or not column.strip():
        return None
    return _unquote(table), _unquote(column)


def _classify_detached(name: str, body: list[str], model: ModelFields) -> None:
    """Flag a table that is disconnected BY DESIGN, so it cannot be read as a broken binding.

    Two shapes qualify, both substituted at query-generation time rather than joined:

    * a **field parameter** - Desktop stamps `extendedProperty ParameterMetadata`, while the
      engine's own emit (measured: `examples/superstore-sales-performance` `X-Axis`) is a
      `= calculated` partition whose source is a list of `NAMEOF(...)` tuples;
    * a **calculation group**, whose column is a query-time modifier, not a grouping key.

    A plain disconnected slicer table is deliberately NOT here - `Region Parameter` in the same
    model is a real single-select list, and pairing it with fact columns in one visual really would
    be an unconstrained join.
    """
    text = "\n".join(body)
    if _PARAMETER_METADATA_RE.search(text) or ("= calculated" in text and _NAMEOF_RE.search(text)):
        model.detached_ok[name] = "field parameter"
    elif _CALC_GROUP_RE.search(text):
        model.detached_ok[name] = "calculation group"


def parse_model(model_dir: Path) -> ModelFields:
    """Collect table columns, measures and hierarchy levels from a `.SemanticModel`'s TMDL.

    Members are recognised by the block's own minimum indent, so a multi-line DAX expression that
    happens to contain the word `measure` deeper in its body cannot be mistaken for a declaration.
    """
    model = ModelFields()
    definition = model_dir / "definition"
    root = definition if definition.is_dir() else model_dir
    for path in sorted(root.rglob("*.tmdl")):
        _parse_tmdl_file(path, model)
    return model


def _parse_tmdl_file(path: Path, model: ModelFields) -> None:
    """Fold one TMDL file's table AND relationship blocks into `model`.

    `relationship` is a top-level declaration like `table`, so it both opens its own block and
    closes any table block above it - without that, a `model.tmdl` carrying both would file the
    joins under the last table it happened to declare.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    tables: list[tuple[str, list[str]]] = []
    relationships: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        table = _TABLE_RE.match(line)
        relationship = _RELATIONSHIP_RE.match(line)
        if table:
            current = []
            tables.append((_unquote(table.group("name")), current))
        elif relationship:
            current = []
            relationships.append((_unquote(relationship.group("name")), current))
        elif current is not None:
            current.append(line)
    for name, body in tables:
        _parse_table_block(name, body, model)
    for name, body in relationships:
        _parse_relationship_block(name, body, model)


def _parse_relationship_block(name: str, body: list[str], model: ModelFields) -> None:
    """Record one join. A block missing either endpoint is dropped, never guessed at."""
    props: dict[str, str] = {}
    for line in body:
        match = _PROPERTY_RE.match(line)
        if match:
            props.setdefault(match.group("key"), match.group("value"))
    source = _split_qualified(props.get("fromColumn", ""))
    target = _split_qualified(props.get("toColumn", ""))
    if source is None or target is None:
        return
    model.relationships.append(
        Relationship(
            name=name,
            from_table=source[0],
            from_column=source[1],
            to_table=target[0],
            to_column=target[1],
            is_active=props.get("isActive", "true").strip().casefold() != "false",
        )
    )


@dataclass
class _TableParseState:
    last_hierarchy: str | None = None
    current_kind: str | None = None
    current_member: str | None = None


def _record_data_type(types_map: dict[str, str | None], member: str, val: str) -> None:
    """Record dataType for a member; conflicting duplicates mark the type as unassessable (None)."""
    if member not in types_map:
        types_map[member] = val
        return
    existing = types_map[member]
    if existing is not None and existing.casefold() != val.casefold():
        types_map[member] = None


def _process_table_line(
    line: str,
    member_indent: int,
    fields_: TableFields,
    state: _TableParseState,
) -> None:
    """Process a single line inside a table TMDL block."""
    match = _MEMBER_RE.match(line)
    if match and len(match.group("indent").expandtabs(4)) == member_indent:
        state.current_kind = match.group("kind")
        state.current_member = _unquote(match.group("name"))
        if state.current_kind == "column":
            fields_.columns.add(state.current_member)
        elif state.current_kind == "measure":
            fields_.measures.add(state.current_member)
        elif state.current_kind == "hierarchy":
            fields_.hierarchies.setdefault(state.current_member, set())
            state.last_hierarchy = state.current_member
        return
    if match and len(match.group("indent").expandtabs(4)) != member_indent:
        if match.group("kind") == "level" and state.last_hierarchy is not None:
            fields_.hierarchies[state.last_hierarchy].add(_unquote(match.group("name")))
        return
    prop_match = _PROPERTY_RE.match(line)
    if prop_match and state.current_member is not None:
        key = prop_match.group("key")
        val = prop_match.group("value").strip()
        if key.casefold() == "datatype":
            if state.current_kind == "column":
                _record_data_type(fields_.column_types, state.current_member, val)
            elif state.current_kind == "measure":
                _record_data_type(fields_.measure_types, state.current_member, val)


def _parse_table_block(name: str, body: list[str], model: ModelFields) -> None:
    """Record the members declared at the block's own top level."""
    fields_ = model.tables.setdefault(name, TableFields())
    _classify_detached(name, body, model)
    matches = [m for m in (_MEMBER_RE.match(line) for line in body) if m]
    if not matches:
        return
    member_indent = min(len(m.group("indent").expandtabs(4)) for m in matches)
    state = _TableParseState()
    for line in body:
        _process_table_line(line, member_indent, fields_, state)


def iter_references(report_dir: Path) -> list[FieldRef]:
    """Every field reference in a `.Report`, from any JSON the report definition ships.

    Visual query projections are only the common case: filters (including nested subqueries), sort
    definitions, conditional-formatting `FillRule` inputs, data-point selectors and page/report
    level filters all carry the same `Column`/`Measure` node, so the walk is shape-driven.
    """
    refs: list[FieldRef] = []
    definition = report_dir / "definition"
    root = definition if definition.is_dir() else report_dir
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        _walk(payload, {}, path, refs)
    return refs


def _source_scope(node: dict[str, Any], scope: dict[str, str]) -> dict[str, str]:
    """Extend the alias->entity map with this query's `From` clause.

    A filter's `Where`/`OrderBy` refers to tables by the alias its own `From` declares
    (`SourceRef.Source`), and a `Subquery` opens a nested scope. Without this, every aliased
    reference would be reported as an unknown table - a gate that cries wolf on valid PBIR.
    """
    entries = node.get("From")
    if not isinstance(entries, list):
        return scope
    nested = dict(scope)
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("Name"), str) and isinstance(entry.get("Entity"), str):
            nested[entry["Name"]] = entry["Entity"]
    return nested


def _entity_of(expression: Any, scope: dict[str, str]) -> str | None:
    """Resolve a `SourceRef` to a table name, through an alias when necessary."""
    if not isinstance(expression, dict):
        return None
    source_ref = expression.get("SourceRef")
    if not isinstance(source_ref, dict):
        return None
    entity = source_ref.get("Entity")
    if isinstance(entity, str):
        return entity
    alias = source_ref.get("Source")
    if isinstance(alias, str):
        return scope.get(alias)
    return None


def _walk(node: Any, scope: dict[str, str], path: Path, refs: list[FieldRef]) -> None:
    """Depth-first walk collecting reference nodes under the alias scope in force."""
    if isinstance(node, list):
        for item in node:
            _walk(item, scope, path, refs)
        return
    if not isinstance(node, dict):
        return
    scope = _source_scope(node, scope)
    for kind in _SCALAR_KINDS:
        inner = node.get(kind)
        if isinstance(inner, dict) and isinstance(inner.get("Property"), str):
            entity = _entity_of(inner.get("Expression"), scope)
            if entity:
                refs.append(FieldRef(kind=kind, entity=entity, prop=inner["Property"], file=path))
    level = node.get("HierarchyLevel")
    if isinstance(level, dict) and isinstance(level.get("Level"), str):
        hierarchy = level.get("Expression", {}).get("Hierarchy") if isinstance(level.get("Expression"), dict) else None
        if isinstance(hierarchy, dict) and isinstance(hierarchy.get("Hierarchy"), str):
            entity = _entity_of(hierarchy.get("Expression"), scope)
            if entity:
                refs.append(
                    FieldRef(
                        kind="HierarchyLevel",
                        entity=entity,
                        prop=level["Level"],
                        file=path,
                        hierarchy=hierarchy["Hierarchy"],
                    )
                )
    for value in node.values():
        _walk(value, scope, path, refs)


def _candidates(fields_: TableFields, ref: FieldRef) -> set[str]:
    """The model names a reference of this kind may legally resolve to.

    A measure is matched against measures AND columns on purpose: PBIR distinguishes the two, but a
    model-layer rename is the defect being hunted, and reporting "this exists, as a column" is far
    more actionable than "missing" when the name is right.
    """
    if ref.kind == "HierarchyLevel":
        return set(fields_.hierarchies.get(ref.hierarchy or "", set()))
    return fields_.columns | fields_.measures


def _finding(ref: FieldRef, status: str, detail: str, model_spelling: str | None = None) -> dict[str, Any]:
    """One machine-readable finding."""
    entry: dict[str, Any] = {
        "status": status,
        "kind": ref.kind,
        "entity": ref.entity,
        "property": ref.prop,
        "report_spelling": f"{ref.entity}[{ref.prop}]",
        "file": str(ref.file),
        "detail": detail,
    }
    if ref.hierarchy:
        entry["hierarchy"] = ref.hierarchy
    if model_spelling is not None:
        entry["model_spelling"] = model_spelling
    return entry


def resolve_reference(model: ModelFields, ref: FieldRef) -> dict[str, Any]:
    """Grade one reference: `resolved`, `near_miss` (case-only) or `missing`."""
    fields_ = model.table(ref.entity)
    entity_spelling = ref.entity
    entity_exact = fields_ is not None
    if fields_ is None:
        found = model.table_ci(ref.entity)
        if found is None:
            return _finding(ref, "missing", f"no table named '{ref.entity}' in the model")
        entity_spelling, fields_ = found

    names = _candidates(fields_, ref)
    if ref.prop in names and entity_exact:
        return _finding(ref, "resolved", "exact match")

    prop_spelling = ref.prop
    if ref.prop not in names:
        lowered = ref.prop.casefold()
        matches = sorted(n for n in names if n.casefold() == lowered)
        if not matches:
            where = f"'{entity_spelling}'"
            if ref.kind == "HierarchyLevel":
                where = f"hierarchy '{ref.hierarchy}' on {where}"
            return _finding(ref, "missing", f"no field named '{ref.prop}' on {where}")
        prop_spelling = matches[0]

    return _finding(
        ref,
        "near_miss",
        "case-only mismatch: the model spells this differently",
        model_spelling=f"{entity_spelling}[{prop_spelling}]",
    )


@dataclass
class VisualQuery:
    """One visual's GROUPING fields - the only ones an unconstrained join can be built from."""

    file: Path
    visual: str
    visual_type: str
    refs: list[FieldRef] = field(default_factory=list)


def iter_visual_queries(report_dir: Path) -> list[VisualQuery]:
    """Every `visual.json`'s query projections, kept per visual so agreement can be judged.

    Deliberately narrower than `iter_references`, which sweeps every JSON in the definition. Only
    `visual.query.queryState.<role>.projections` becomes a GROUPING column in the DAX Power BI
    generates, and only grouping columns can raise `InvalidUnconstrainedJoin`. A visual-level
    filter, a sort key or a conditional-formatting input on an unrelated table renders fine (it
    simply filters nothing), so folding those in would invent findings Desktop never raises.
    """
    queries: list[VisualQuery] = []
    definition = report_dir / "definition"
    root = definition if definition.is_dir() else report_dir
    for path in sorted(root.rglob("visual.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        visual = payload.get("visual") if isinstance(payload, dict) else None
        if not isinstance(visual, dict):
            continue
        query = visual.get("query")
        state = query.get("queryState") if isinstance(query, dict) else None
        if not isinstance(state, dict):
            continue
        refs: list[FieldRef] = []
        # `From` is a SIBLING of `queryState`, not inside it, so a walk started at `queryState`
        # never sees it and every aliased projection resolves to None -- silently contributing
        # zero tables to agreement. Seed the scope from the parent `query` node.
        _walk(state, _source_scope(query, {}), path, refs)
        if refs:
            name = payload.get("name") if isinstance(payload.get("name"), str) else path.parent.name
            visual_type = visual.get("visualType") if isinstance(visual.get("visualType"), str) else "unknown"
            queries.append(VisualQuery(file=path, visual=name, visual_type=visual_type, refs=refs))
    return queries


def _grouping_tables(model: ModelFields, query: VisualQuery) -> dict[str, list[FieldRef]]:
    """The model tables this visual GROUPS BY, mapped to the references that put them there.

    Three deliberate exclusions, each of which would otherwise be a false positive:

    * **measures** - a measure is not table-bound the way a column is. It aggregates across the
      whole model, its home table is an organisational choice (a `_Measures` table is disconnected
      on purpose), and it is a value in the query, never a grouping key. Requiring a measure's home
      table to join would fire on every well-built model.
    * **references that do not resolve** - already reported as `missing`/`near_miss`; guessing the
      table agreement of a field that does not exist would just double the noise.
    * **field parameters and calculation groups** - disconnected by design, see `_classify_detached`.
    """
    grouped: dict[str, list[FieldRef]] = {}
    for ref in query.refs:
        if ref.kind == "Measure":
            continue
        found = model.table_ci(ref.entity)
        if found is None:
            continue
        spelling, _ = found
        if spelling in model.detached_ok:
            continue
        if resolve_reference(model, ref)["status"] == "missing":
            continue
        grouped.setdefault(spelling, []).append(ref)
    return grouped


def _ambiguous_bindings(model: ModelFields, grouped: dict[str, list[FieldRef]], groups: list[list[str]]) -> list[dict]:
    """Name the table a field was PROBABLY meant to bind to, when the name exists there too.

    This is the customer's exact shape: `Serial_Number` on both the stranded lookup and the joined
    fact. Candidates are restricted to tables the REST of this visual can already reach, so the
    answer is "rebind here and the visual renders", not a model-wide census of every duplicated
    column name - `Turbine Id` living on both sides of a relationship is normal star-schema design
    and reporting it would drown the finding that matters.
    """
    reachable: dict[str, set[str]] = {}
    for group in groups:
        others = {name for other in groups if other is not group for name in other}
        expanded = {
            name
            for name in model.tables
            if any(model.components().get(name.casefold()) == model.components().get(o.casefold()) for o in others)
        }
        for table in group:
            reachable[table] = expanded
    ambiguous = []
    for table, refs in sorted(grouped.items()):
        for prop in sorted({ref.prop for ref in refs if ref.kind == "Column"}):
            also_on = [t for t in model.tables_carrying(prop) if t != table and t in reachable.get(table, set())]
            if also_on:
                ambiguous.append({"report_spelling": f"{table}[{prop}]", "also_on": also_on})
    return ambiguous


def check_visual_coherence(model: ModelFields, query: VisualQuery) -> dict[str, Any] | None:
    """Does this visual's grouping columns agree on a table set Power BI can actually join?"""
    grouped = _grouping_tables(model, query)
    if len(grouped) < 2:
        return None
    groups = model.group_tables(grouped)
    if len(groups) < 2:
        return None
    inactive_only = len(model.group_tables(grouped, include_inactive=True)) == 1
    detail = "grouping columns span table sets with no active relationship path: " + " | ".join(
        "+".join(group) for group in groups
    )
    if inactive_only:
        detail += " - only an INACTIVE relationship joins them (activate it, or use USERELATIONSHIP in a measure)"
    return {
        "status": "unrelated_tables",
        "visual": query.visual,
        "visual_type": query.visual_type,
        "file": str(query.file),
        "table_groups": groups,
        "fields": sorted({f"{table}[{ref.prop}]" for table, refs in grouped.items() for ref in refs}),
        "inactive_only": inactive_only,
        "ambiguous": _ambiguous_bindings(model, grouped, groups),
        "detail": detail,
    }


def model_for_report(report_dir: Path) -> Path | None:
    """Resolve the model a report actually ships with, via `definition.pbir` then a sibling."""
    pbir = report_dir / "definition.pbir"
    if pbir.is_file():
        try:
            payload = json.loads(pbir.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        by_path = payload.get("datasetReference", {}).get("byPath", {}) if isinstance(payload, dict) else {}
        rel = by_path.get("path") if isinstance(by_path, dict) else None
        if isinstance(rel, str):
            candidate = (report_dir / rel).resolve()
            if candidate.is_dir():
                return candidate
    sibling = report_dir.parent / f"{report_dir.name[: -len('.Report')]}.SemanticModel"
    return sibling if sibling.is_dir() else None


find_reports = shipping_reports


def check_pair(report_dir: Path, model_dir: Path) -> dict[str, Any]:
    """Grade every reference in ONE report against ONE model.

    A pair that yields NOTHING to compare - no model tables, or no field reference anywhere in the
    report - is `SKIPPED`, never `OK`. Review finding: with `--model` and `--report` transposed (a
    one-keystroke slip, both paths perfectly real) the old code parsed no model and found no
    references, then printed "every PBIR field reference resolves" and exited 0 for a report it had
    never opened. An affirmative verdict must mean something was actually checked.
    """
    model = parse_model(model_dir)
    findings = [resolve_reference(model, ref) for ref in iter_references(report_dir)]
    unresolved = [f for f in findings if f["status"] != "resolved"]
    if not findings or not model.tables:
        reason = "no tables parsed from the model" if not model.tables else "no field reference found in the report"
        return {
            "report": str(report_dir),
            "model": str(model_dir),
            "status": "SKIPPED",
            "reason": reason,
            "references": len(findings),
            "near_misses": 0,
            "missing": 0,
            "findings": [],
            "visuals": 0,
            "incoherent_visuals": 0,
            "coherence": [],
            "role_type_violations": 0,
            "role_type_cannot_assess": 0,
            "role_type_findings": [],
            "cannot_assess_findings": [],
        }
    queries = iter_visual_queries(report_dir)
    # Computed in the SAME pass as the per-field grades, and reported alongside them. The field
    # report's core complaint is the ORDERING - the table-agreement failure stayed masked until the
    # genuinely-missing fields were fixed, so "clean bill of health" arrived one round too early.
    coherence = [c for c in (check_visual_coherence(model, q) for q in queries) if c]
    role_findings, cannot_assess = check_visual_roles(model, report_dir)
    return {
        "report": str(report_dir),
        "model": str(model_dir),
        "status": _pair_status(unresolved, coherence, role_findings),
        "references": len(findings),
        "near_misses": sum(1 for f in findings if f["status"] == "near_miss"),
        "missing": sum(1 for f in findings if f["status"] == "missing"),
        "findings": _dedupe(unresolved),
        "visuals": len(queries),
        "incoherent_visuals": len(coherence),
        "coherence": _dedupe_coherence(coherence),
        "role_type_violations": len(role_findings),
        "role_type_cannot_assess": len(cannot_assess),
        "role_type_findings": _dedupe_role_findings(role_findings),
        "cannot_assess_findings": _dedupe_role_findings(cannot_assess),
    }


def is_numeric_visual_role(visual_type: str, role: str) -> bool:
    """Whether this role on this visual type requires numeric data."""
    if role in ("Y", "Y2", "MinValue", "MaxValue", "TargetValue"):
        return True
    if visual_type == "scatterChart" and role in ("X", "Size"):
        return True
    if visual_type == "azureMap" and role == "Size":
        return True
    if visual_type == "treemap" and role == "Values":
        return True
    return False


@dataclass
class _VisualContext:
    path: Path
    visual_name: str
    visual_type: str
    scope: dict[str, str]


@dataclass
class _RoleFindings:
    violations: list[dict[str, Any]]
    cannot_assess: list[dict[str, Any]]


def _evaluate_role_field(
    model: ModelFields,
    ctx: _VisualContext,
    role: str,
    ref: FieldRef,
    findings: _RoleFindings,
) -> None:
    """Evaluate a single field reference on a numeric visual role."""
    kind, data_type = model.field_type(ref.entity, ref.prop)
    if kind is None:
        return
    if data_type is not None:
        normalized = data_type.casefold()
        if normalized in NUMERIC_DATA_TYPES:
            return
        if normalized in NON_NUMERIC_DATA_TYPES:
            findings.violations.append(
                {
                    "status": "non_numeric_role",
                    "visual": ctx.visual_name,
                    "visual_type": ctx.visual_type,
                    "role": role,
                    "kind": kind,
                    "entity": ref.entity,
                    "property": ref.prop,
                    "data_type": data_type,
                    "file": str(ctx.path),
                    "detail": (
                        f"non-numeric {kind.lower()} '{ref.entity}[{ref.prop}]' "
                        f"(dataType: {data_type}) bound to numeric visual role '{role}' "
                        f"on {ctx.visual_type} visual '{ctx.visual_name}'"
                    ),
                }
            )
            return

    # Missing (None), explicit Unknown, Variant, or unsupported type -> cannot_assess
    type_desc = f"(dataType: {data_type}) " if data_type is not None else "has no static dataType in TMDL; "
    findings.cannot_assess.append(
        {
            "status": "cannot_assess",
            "visual": ctx.visual_name,
            "visual_type": ctx.visual_type,
            "role": role,
            "kind": kind,
            "entity": ref.entity,
            "property": ref.prop,
            "file": str(ctx.path),
            "detail": (
                f"{kind.lower()} '{ref.entity}[{ref.prop}]' {type_desc}"
                f"on numeric role '{role}'; cannot assess return type offline"
            ),
        }
    )


def _check_role_projections(
    model: ModelFields,
    ctx: _VisualContext,
    query: dict[str, Any],
    findings: _RoleFindings,
) -> None:
    """Check queryState role projections for non-numeric fields on numeric roles."""
    query_state = query.get("queryState")
    if not isinstance(query_state, dict):
        return
    for role, role_val in query_state.items():
        if not isinstance(role_val, dict) or not is_numeric_visual_role(ctx.visual_type, role):
            continue
        projections = role_val.get("projections")
        if not isinstance(projections, list):
            continue
        for proj in projections:
            if not isinstance(proj, dict) or proj.get("field") is None:
                continue
            refs: list[FieldRef] = []
            _walk(proj["field"], ctx.scope, ctx.path, refs)
            for ref in refs:
                _evaluate_role_field(model, ctx, role, ref, findings)


def _extract_scalar_fill_prop(item: dict[str, Any], scope: dict[str, str]) -> tuple[str, str] | None:
    """Extract (entity, prop) from a scalar Measure/Column in fill.solid.color.expr."""
    if not isinstance(item, dict):
        return None
    fill = item.get("properties", {}).get("fill") if isinstance(item.get("properties"), dict) else None
    if not isinstance(fill, dict):
        return None
    color_expr = fill.get("solid", {}).get("color", {}).get("expr") if isinstance(fill.get("solid"), dict) else None
    if not isinstance(color_expr, dict):
        return None
    for scalar_kind in ("Measure", "Column"):
        if scalar_kind in color_expr:
            inner = color_expr[scalar_kind]
            if isinstance(inner, dict) and isinstance(inner.get("Property"), str):
                entity = _entity_of(inner.get("Expression"), scope)
                if entity:
                    return (entity, inner["Property"])
    return None


def _check_direct_color_fill_item(
    model: ModelFields,
    ctx: _VisualContext,
    item: dict[str, Any],
    findings: _RoleFindings,
) -> None:
    """Check a single dataPoint formatting object item for direct color measure bindings."""
    target = _extract_scalar_fill_prop(item, ctx.scope)
    if target is None:
        return
    entity, prop = target
    kind, data_type = model.field_type(entity, prop)
    if kind is None:
        return
    if data_type is not None:
        normalized = data_type.casefold()
        if normalized in STRING_DATA_TYPES:
            findings.violations.append(
                {
                    "status": "direct_color_measure",
                    "visual": ctx.visual_name,
                    "visual_type": ctx.visual_type,
                    "role": "objects.dataPoint.fill",
                    "kind": kind,
                    "entity": entity,
                    "property": prop,
                    "data_type": data_type,
                    "file": str(ctx.path),
                    "detail": (
                        f"direct {kind.lower()} '{entity}[{prop}]' (dataType: {data_type}) bound as "
                        f"fill.solid.color.expr on {ctx.visual_type} visual '{ctx.visual_name}'; literal color "
                        f"string measures cannot be bound directly into solid.color.expr (use "
                        f"Conditional.Cases rule or Field Value binding)"
                    ),
                }
            )
            return
        if normalized in KNOWN_NON_STRING_DATA_TYPES:
            return

    # Missing (None), explicit Unknown, Variant, or unsupported type -> cannot_assess
    type_desc = f"(dataType: {data_type}) " if data_type is not None else "has no static dataType in TMDL; "
    findings.cannot_assess.append(
        {
            "status": "cannot_assess",
            "visual": ctx.visual_name,
            "visual_type": ctx.visual_type,
            "role": "objects.dataPoint.fill",
            "kind": kind,
            "entity": entity,
            "property": prop,
            "file": str(ctx.path),
            "detail": (
                f"direct {kind.lower()} '{entity}[{prop}]' {type_desc}"
                f"bound as fill.solid.color.expr; cannot assess return type offline"
            ),
        }
    )


def _check_direct_color_fills(
    model: ModelFields,
    ctx: _VisualContext,
    visual: dict[str, Any],
    findings: _RoleFindings,
) -> None:
    """Check dataPoint formatting objects for direct color measure bindings."""
    objects = visual.get("objects")
    if not isinstance(objects, dict):
        return
    data_points = objects.get("dataPoint")
    if not isinstance(data_points, list):
        return
    for item in data_points:
        _check_direct_color_fill_item(model, ctx, item, findings)


def check_visual_roles(
    model: ModelFields, report_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check for role-type mismatches (e.g. non-numeric measures on numeric Y axis or direct color fills).

    Returns (violations, cannot_assess).
    """
    findings = _RoleFindings(violations=[], cannot_assess=[])
    definition = report_dir / "definition"
    root = definition if definition.is_dir() else report_dir

    for path in sorted(root.rglob("visual.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        visual = payload.get("visual")
        if not isinstance(visual, dict):
            continue

        visual_name = payload.get("name") if isinstance(payload.get("name"), str) else path.parent.name
        visual_type = visual.get("visualType") if isinstance(visual.get("visualType"), str) else "unknown"
        query = visual.get("query")
        scope = _source_scope(query, {}) if isinstance(query, dict) else {}
        ctx = _VisualContext(path=path, visual_name=visual_name, visual_type=visual_type, scope=scope)
        if isinstance(query, dict):
            _check_role_projections(model, ctx, query, findings)
        _check_direct_color_fills(model, ctx, visual, findings)

    return findings.violations, findings.cannot_assess


def _dedupe_role_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per distinct role type defect, carrying occurrences and visuals."""
    merged: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for finding in findings:
        key = (
            finding["status"],
            finding["visual_type"],
            finding["role"],
            finding["entity"],
            finding["property"],
        )
        entry = merged.get(key)
        if entry is None:
            entry = {k: v for k, v in finding.items() if k not in ("file", "visual")}
            entry["visuals"] = []
            entry["files"] = []
            entry["occurrences"] = 0
            merged[key] = entry
        entry["occurrences"] += 1
        vis_entry = f"{finding['visual']} ({finding['visual_type']})"
        if vis_entry not in entry["visuals"]:
            entry["visuals"].append(vis_entry)
        if finding["file"] not in entry["files"]:
            entry["files"].append(finding["file"])
    return list(merged.values())


def _pair_status(
    unresolved: list[dict[str, Any]],
    coherence: list[dict[str, Any]],
    role_findings: list[dict[str, Any]] | None = None,
) -> str:
    """`UNRESOLVED` outranks `INCOHERENT` which outranks `NON_NUMERIC_ROLE`."""
    if unresolved:
        return "UNRESOLVED"
    if coherence:
        return "INCOHERENT"
    if role_findings:
        return "NON_NUMERIC_ROLE"
    return "OK"


def _dedupe_coherence(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per distinct table-set disagreement, carrying every visual that shows it.

    A page built by copying one broken visual repeats the same fix N times; the grouping is what
    keeps the verdict a to-do list rather than a wall.
    """
    merged: dict[str, dict[str, Any]] = {}
    for finding in findings:
        key = json.dumps([finding["table_groups"], finding["ambiguous"]], sort_keys=True)
        entry = merged.get(key)
        if entry is None:
            entry = {k: v for k, v in finding.items() if k not in ("file", "visual", "visual_type")}
            entry["visuals"] = []
            entry["files"] = []
            entry["occurrences"] = 0
            merged[key] = entry
        entry["occurrences"] += 1
        entry["visuals"].append(f"{finding['visual']} ({finding['visual_type']})")
        if finding["file"] not in entry["files"]:
            entry["files"].append(finding["file"])
    return list(merged.values())


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per distinct defect, carrying the files it was seen in.

    A renamed column is referenced by every visual that used it, so the raw list is dominated by
    repeats of one fix. Collapsing them keeps the verdict readable without losing the locations.
    """
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for finding in findings:
        key = (finding["status"], finding["kind"], finding["entity"], finding["property"])
        entry = merged.get(key)
        if entry is None:
            entry = {k: v for k, v in finding.items() if k != "file"}
            entry["files"] = []
            entry["occurrences"] = 0
            merged[key] = entry
        entry["occurrences"] += 1
        if finding["file"] not in entry["files"]:
            entry["files"].append(finding["file"])
    return list(merged.values())


def scan(root: Path) -> dict[str, Any]:
    """Check every shipping report under `root` against the model it ships with."""
    pairs = []
    skipped = []
    for report_dir in find_reports(root):
        model_dir = model_for_report(report_dir)
        if model_dir is None:
            skipped.append({"report": str(report_dir), "reason": "no semantic model beside this report"})
            continue
        pairs.append(check_pair(report_dir, model_dir))
    return _merge(pairs, skipped)


def _merge(pairs: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-report results into one verdict, keeping ungraded pairs out of the pass count."""
    graded = [p for p in pairs if p["status"] != "SKIPPED"]
    skipped = list(skipped) + [
        {"report": p["report"], "model": p["model"], "reason": p["reason"]} for p in pairs if p["status"] == "SKIPPED"
    ]
    unresolved = [p for p in graded if p["status"] == "UNRESOLVED"]
    incoherent = [p for p in graded if p["status"] == "INCOHERENT"]
    non_numeric_role = [p for p in graded if p["status"] == "NON_NUMERIC_ROLE"]
    if not graded:
        status = "SKIPPED"
    elif unresolved:
        status = "UNRESOLVED"
    elif incoherent:
        status = "INCOHERENT"
    elif non_numeric_role:
        status = "NON_NUMERIC_ROLE"
    else:
        status = "OK"
    return {
        "status": status,
        "reports_scanned": len(graded),
        "reports_unresolved": len(unresolved),
        "reports_incoherent": len(incoherent),
        "reports_non_numeric_role": len(non_numeric_role),
        "near_misses": sum(p["near_misses"] for p in graded),
        "missing": sum(p["missing"] for p in graded),
        "incoherent_visuals": sum(p.get("incoherent_visuals", 0) for p in graded),
        "role_type_violations": sum(p.get("role_type_violations", 0) for p in graded),
        "role_type_cannot_assess": sum(p.get("role_type_cannot_assess", 0) for p in graded),
        "reports": graded,
        "skipped": skipped,
    }


FAILING_STATUSES = frozenset({"UNRESOLVED", "INCOHERENT", "NON_NUMERIC_ROLE"})


def render(report: dict[str, Any], *, verbose: bool = False) -> str:
    """Human-readable verdict, in the shape the sibling gates use."""
    if report["status"] == "SKIPPED":
        reasons = "; ".join(s["reason"] for s in report.get("skipped", [])) or "no report found"
        return f"FIELD BINDING CHECK: SKIPPED - nothing to check ({reasons})"
    scanned = report["reports_scanned"]
    # A skipped report is invisible in the pass count, so name it here too: "OK - 1 report(s)" on a
    # two-report bundle reads as full coverage when half of it was never checked.
    tail = _skipped_tail(report, verbose=verbose)
    if report["status"] == "OK":
        return (
            f"FIELD BINDING CHECK: OK - every PBIR field reference in {scanned} report(s) resolves in its model.{tail}"
        )
    lines = [_headline(report, scanned, tail)]
    inline_small_evidence = (
        report["reports_unresolved"] + report["reports_incoherent"] + report.get("reports_non_numeric_role", 0) <= 3
    )
    for one in report["reports"]:
        if one["status"] not in FAILING_STATUSES:
            continue
        lines += _render_report_summary(one, verbose=verbose, inline_small_evidence=inline_small_evidence)
    if report["near_misses"]:
        lines.append(
            "  NEAR-MISS = model-layer rename that never reached the report; rewrite PBIR to the model spelling."
        )
    if report["missing"]:
        lines.append(
            "  MISSING   = field/table absent from the model; add it to TMDL or rebind/remove the PBIR reference."
        )
    if report.get("incoherent_visuals"):
        lines.append(
            "  UNRELATED TABLES = visual fields resolve but Power BI cannot join their tables; rebind the odd table "
            "out, or add/activate the relationship."
        )
    if report.get("role_type_violations"):
        lines.append(
            "  NON-NUMERIC ROLE = visual role requires numeric data or proper color rule, but is bound to non-numeric "
            "measure/column; rebind or change calculation."
        )
    if not verbose:
        lines.append("  Run with --verbose to list every field, visual, and skipped report behind these counts.")
    return "\n".join(lines)


def _headline(report: dict[str, Any], scanned: int, tail: str) -> str:
    """The one line a CI log shows, naming which of the defects dominates."""
    if report["status"] == "UNRESOLVED":
        return (
            f"FIELD BINDING CHECK: UNRESOLVED - {report['reports_unresolved']} of {scanned} report(s) "
            f"reference fields their model does not have "
            f"({report['near_misses']} case-only near-miss(es), {report['missing']} missing){tail}"
        )
    if report["status"] == "INCOHERENT":
        return (
            f"FIELD BINDING CHECK: INCOHERENT - every field resolves, but {report['incoherent_visuals']} visual(s) "
            f"in {report.get('reports_incoherent', 0)} of {scanned} report(s) bind fields their model cannot "
            f"join{tail}"
        )
    return (
        f"FIELD BINDING CHECK: NON_NUMERIC_ROLE - every field resolves, but "
        f"{report.get('role_type_violations', 0)} visual role(s) "
        f"in {report.get('reports_non_numeric_role', 0)} of {scanned} report(s) bind non-numeric "
        f"fields to numeric roles or direct color fills{tail}"
    )


def _skipped_tail(report: dict[str, Any], *, verbose: bool) -> str:
    """Name or summarize reports that were NOT graded, so a partial sweep cannot read as a full one."""
    skipped = report.get("skipped") or []
    if not skipped:
        return ""
    if verbose or len(skipped) <= 3:
        names = ", ".join(f"{Path(s['report']).name} ({s['reason']})" for s in skipped)
        return f"\n  {len(skipped)} report(s) SKIPPED, not checked: {names}"
    counts: dict[str, int] = {}
    for item in skipped:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    reasons = "; ".join(f"{count} {reason}" for reason, count in sorted(counts.items()))
    return f"\n  {len(skipped)} report(s) SKIPPED, not checked ({reasons}; use --verbose for names)"


def _render_report_summary(one: dict[str, Any], *, verbose: bool, inline_small_evidence: bool) -> list[str]:
    """Render one report as category counts by default, with lossless evidence in verbose mode."""
    lines = [f"  {Path(one['report']).name}  (model: {Path(one['model']).name})"]
    inline_fields = verbose or (inline_small_evidence and one["near_misses"] + one["missing"] <= 3)
    if one["near_misses"]:
        lines.append(f"    - NEAR-MISS (case only): {one['near_misses']} reference(s)")
    if one["missing"]:
        lines.append(f"    - MISSING: {one['missing']} reference(s)")
    if one.get("incoherent_visuals"):
        lines.append(f"    - UNRELATED TABLES: {one['incoherent_visuals']} visual(s)")
    if one.get("role_type_violations"):
        lines.append(f"    - NON-NUMERIC ROLE / DIRECT COLOR: {one['role_type_violations']} violation(s)")
    if one.get("role_type_cannot_assess"):
        lines.append(f"    - CANNOT ASSESS (untyped measure): {one['role_type_cannot_assess']} binding(s)")
    if inline_fields:
        lines += _render_findings(one["findings"], "near_miss")
        lines += _render_findings(one["findings"], "missing")
    if verbose or one.get("incoherent_visuals"):
        lines += _render_coherence(one.get("coherence") or [])
    if verbose or one.get("role_type_violations"):
        lines += _render_role_findings_summary(one.get("role_type_findings") or [])
    if verbose and one.get("cannot_assess_findings"):
        lines += _render_cannot_assess_summary(one.get("cannot_assess_findings") or [])
    return lines


def _render_role_findings_summary(findings: list[dict[str, Any]]) -> list[str]:
    """Render role type violations with visual and detail."""
    lines = []
    for finding in findings:
        visuals = ", ".join(finding.get("visuals", [])[:4])
        lines.append(f"    - ROLE TYPE VIOLATION: {finding.get('detail', '')}  [x{finding.get('occurrences', 1)}]")
        if visuals:
            lines.append(f"        visuals: {visuals}")
    return lines


def _render_cannot_assess_summary(findings: list[dict[str, Any]]) -> list[str]:
    """Render cannot-assess role type entries."""
    lines = []
    for finding in findings:
        visuals = ", ".join(finding.get("visuals", [])[:4])
        lines.append(f"    - CANNOT ASSESS: {finding.get('detail', '')}  [x{finding.get('occurrences', 1)}]")
        if visuals:
            lines.append(f"        visuals: {visuals}")
    return lines


def _render_findings(findings: list[dict[str, Any]], status: str) -> list[str]:
    """Render one category, printing BOTH spellings for a near-miss."""
    label = "NEAR-MISS (case only)" if status == "near_miss" else "MISSING"
    lines = []
    for finding in findings:
        if finding["status"] != status:
            continue
        detail = f"report: {finding['report_spelling']}"
        if finding.get("model_spelling"):
            detail += f"   model: {finding['model_spelling']}"
        lines.append(f"    - {label}: {detail}  [{finding['kind']} x{finding['occurrences']}]")
    return lines


def _render_coherence(findings: list[dict[str, Any]]) -> list[str]:
    """Render each table-set disagreement with the fields, the visuals and the likely mis-bind."""
    lines = []
    for finding in findings:
        sets = " | ".join("+".join(group) for group in finding["table_groups"])
        lines.append(f"    - UNRELATED TABLES: {sets}  [x{finding['occurrences']} visual(s)]")
        lines.append(f"        fields:  {', '.join(finding['fields'])}")
        lines.append(f"        visuals: {', '.join(finding['visuals'][:4])}")
        for ambiguous in finding["ambiguous"]:
            also = ", ".join(ambiguous["also_on"])
            lines.append(f"        AMBIGUOUS: {ambiguous['report_spelling']} - the same name also exists on {also}")
        if finding["inactive_only"]:
            lines.append("        NOTE: only an INACTIVE relationship joins these tables")
    return lines


def _pair_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Grade the explicitly-named `--model` / `--report` pair."""
    return _merge([check_pair(args.report.resolve(), args.model.resolve())], [])


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle folder(s) or .Report folder(s)")
    parser.add_argument("--model", type=Path, help="explicit .SemanticModel folder (use with --report)")
    parser.add_argument("--report", type=Path, help="explicit .Report folder (use with --model)")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--verbose", action="store_true", help="also list every field, visual, and skipped report")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0")
    args = parser.parse_args(argv)

    if bool(args.model) != bool(args.report):
        parser.error("--model and --report must be given together")
    if not args.paths and not args.model:
        parser.error("give a bundle/report path, or --model with --report")
    # A path that does not exist must NEVER produce a verdict. `rglob` on a missing folder yields
    # nothing, so without this a typo'd `--report` reads as "0 references, all resolved" and the
    # gate prints OK and exits 0 for a report it never opened - the one failure mode that would
    # make this check worse than not running it.
    for label, path in (("--model", args.model), ("--report", args.report), *(("path", p) for p in args.paths)):
        if path is not None and not path.is_dir():
            parser.error(f"{label} {path} is not a directory")

    if args.model:
        merged = _pair_from_args(args)
    else:
        scans = [scan(path) for path in args.paths]
        merged = _merge(
            [one for s in scans for one in s["reports"]],
            [one for s in scans for one in s["skipped"]],
        )

    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged, verbose=args.verbose))
    if args.warn_only or merged["status"] not in FAILING_STATUSES:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
