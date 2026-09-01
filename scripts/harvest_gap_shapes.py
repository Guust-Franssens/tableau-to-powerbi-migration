"""
purpose: axis 2 of the engine-gap harvest - WHAT a difference is, from a structural JSON-pointer
         diff (PBIR) or a line diff (TMDL). Axis 1, WHO wrote it, lives in `harvest_engine_gaps.py`.
usage:   imported by scripts/harvest_engine_gaps.py; not a user-facing CLI

Split out of `harvest_engine_gaps.py` because the two axes answer independent questions and only
meet at the report: shape classification never needs the engine's hash baseline, and provenance
never needs to parse a visual. The seam also bought both modules headroom under pylint's
`max-module-lines`.

⚠️ **`BINDING_RESOLUTION` is decided PER CHANGED LEAF, never per file.** The first version decided it
once from the file's global before/after entity sets and then discarded EVERY `MODEL_OBJECT_NAMES`
shape in that file. Measured (blind review of PR #399): one visual carrying `Extract -> Orders`
(invalid -> valid, genuinely the binding being resolved), `Orders -> Returns` (valid -> valid, an
unexplained table substitution) and a property `A -> B` was reported as `BINDING_RESOLUTION` alone -
the two real findings were excused by the one benign rebind sitting next to them. A leaf is excused
only when THAT leaf's own before-value is absent from the bound model and its after-value is present.

What this module deliberately cannot demonstrate: `Property` and `nativeQueryRef` changes. The bound
model is read for TABLE names only (`bound_model_tables`), so a column/measure rename can never be
shown to be invalid -> valid and always stays `MODEL_OBJECT_NAMES`. That is honest ignorance, and it
is the direction that over-reports rather than the direction that excuses.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, NamedTuple

_TABLE_DECL = re.compile(r"^\s*table\s+(?:'([^']+)'|(\S+))", re.MULTILINE)

# `Sum(Orders.csv.Sales)` - the engine wraps aggregated projections, and a rebind inside the wrapper
# is invisible to a bare table-prefix test. ⚠️ The trailing group is NOT decoration: Power BI appends
# a disambiguation suffix to a duplicated query reference (`Sum(Orders.csv.Sales) 2`), and requiring
# the closing paren to END the string missed exactly three estate leaves - the 474-vs-477 gap the
# third review adjudicated. It is captured and compared rather than stripped, because a change in the
# suffix is a change to WHICH duplicate is referenced, not a binding being resolved.
_AGGREGATION = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\((.*)\)(.*)$")

# Leaves whose value IS a model object name. Everything else under a query is query SHAPE.
_NAME_LEAVES = frozenset({"Entity", "Property", "queryRef", "nativeQueryRef"})

SHAPE_MODEL_NAMES = "MODEL_OBJECT_NAMES"
SHAPE_BINDING = "BINDING_RESOLUTION"
SHAPE_QUERY = "QUERY_SHAPE"
SHAPE_REBIND = "REBIND_TARGET"
SHAPE_FILTER = "FILTER"
SHAPE_LAYOUT = "LAYOUT"
SHAPE_FORMATTING = "FORMATTING"
SHAPE_VISUAL_TYPE = "VISUAL_TYPE"
SHAPE_PAGE_ORDER = "PAGE_ORDER"
SHAPE_RESOURCES = "RESOURCES"
SHAPE_UNCLASSIFIED = "UNCLASSIFIED"
SHAPE_BINARY = "BINARY_CHANGED"
SHAPE_UNREADABLE_JSON = "UNPARSEABLE_JSON"
SHAPE_UNREADABLE_TEXT = "UNREADABLE_TEXT"
SHAPE_REVERTED = "REVERTED_TO_BASELINE"
SHAPE_POST_ENGINE_CHANGE = "CHANGED_AFTER_ENGINE"

# `.pbism` and `.pbip` are JSON documents despite their suffixes, so they get the structural diff
# rather than being written off as binary.
_JSON_SUFFIXES = frozenset({".json", ".pbir", ".pbip", ".pbism", ".platform"})
_TEXT_SUFFIXES = frozenset({".tmdl", ".md", ".txt", ".dax", ".m"})

# TMDL line kinds, most specific first. Measured on the estate corpus, the ONLY model-layer
# differences were whole `measure` blocks appended to a shared published-datasource model by the
# consuming workbook - a signal that reads as `BINARY_CHANGED` if TMDL is not parsed at all.
_TMDL_LINE_KINDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*lineageTag:"), "TMDL_LINEAGE_TAG"),
    (re.compile(r"^\s*annotation\s"), "TMDL_ANNOTATION"),
    (re.compile(r"^\s*measure\s"), "TMDL_MEASURE"),
    (re.compile(r"^\s*column\s"), "TMDL_COLUMN"),
    (re.compile(r"^\s*table\s"), "TMDL_TABLE"),
    (re.compile(r"^\s*relationship\s"), "TMDL_RELATIONSHIP"),
    (re.compile(r"^\s*(partition|source|mode)\b"), "TMDL_PARTITION"),
)
SHAPE_TMDL_OTHER = "TMDL_OTHER"

# Ordered (prefixes, substrings, shape). First match wins, so the more specific families sit first.
_POINTER_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("/position",), (), SHAPE_LAYOUT),
    ((), ("filter", "Filter"), SHAPE_FILTER),
    ((), ("/visualType",), SHAPE_VISUAL_TYPE),
    (("/datasetReference",), (), SHAPE_REBIND),
    (("/pageOrder", "/activePageName"), (), SHAPE_PAGE_ORDER),
    (("/resourcePackages",), (), SHAPE_RESOURCES),
)


class Difference(NamedTuple):
    """One structural difference between two JSON documents, WITH the values that changed.

    The values are load-bearing, not decoration: without them `BINDING_RESOLUTION` can only be
    decided from whole-file entity sets, which is the defect this module's header describes.
    """

    pointer: str
    kind: str
    before: Any
    after: Any


def json_pointer_diff(a: Any, b: Any, pointer: str = "", out: list[Difference] | None = None) -> list[Difference]:
    """Every difference between two JSON documents, as `Difference` records."""
    if out is None:
        out = []
    if type(a) is not type(b):
        out.append(Difference(pointer, "type", a, b))
        return out
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            child = f"{pointer}/{key}"
            if key not in a:
                out.append(Difference(child, "added", None, b[key]))
            elif key not in b:
                out.append(Difference(child, "removed", a[key], None))
            else:
                json_pointer_diff(a[key], b[key], child, out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(Difference(f"{pointer}[]", "length", a, b))
        for item_a, item_b in zip(a, b):
            json_pointer_diff(item_a, item_b, f"{pointer}[]", out)
    elif a != b:
        out.append(Difference(pointer, "value", a, b))
    return out


def pointer_shape(pointer: str) -> str:
    """Map one JSON pointer to a shape. Buckets derived by measuring the estate corpus.

    Ordered, first-match: `filter` is tested before the query/objects families because a filter
    lives under both and is the more specific answer.
    """
    for prefixes, substrings, shape in _POINTER_RULES:
        if any(pointer.startswith(p) for p in prefixes) or any(s in pointer for s in substrings):
            return shape
    leaf = _leaf(pointer)
    if "/query" in pointer or "/sortDefinition" in pointer:
        return SHAPE_MODEL_NAMES if leaf in _NAME_LEAVES else SHAPE_QUERY
    if "/objects" in pointer:
        return SHAPE_MODEL_NAMES if leaf in _NAME_LEAVES else SHAPE_FORMATTING
    return SHAPE_UNCLASSIFIED


def _leaf(pointer: str) -> str:
    return pointer.rstrip("[]").rsplit("/", 1)[-1].rstrip("[]")


def bound_model_tables(report_dir: Path) -> set[str] | None:
    """Table names of the semantic model a report actually binds, or None when unresolvable.

    None is a real answer and must stay distinguishable from the empty set: it means the refinement
    below cannot run, not that the model has no tables.
    """
    pbir = report_dir / "definition.pbir"
    try:
        reference = json.loads(pbir.read_text(encoding="utf-8")).get("datasetReference") or {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    relative = (reference.get("byPath") or {}).get("path")
    if not isinstance(relative, str) or not relative:
        return None
    tables_dir = (pbir.parent / relative).resolve() / "definition" / "tables"
    if not tables_dir.is_dir():
        return None
    names: set[str] = set()
    for tmdl in tables_dir.glob("*.tmdl"):
        try:
            match = _TABLE_DECL.search(tmdl.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if match:
            names.add(match.group(1) or match.group(2))
    return names


def _split_on_known_table(value: str, tables: set[str]) -> tuple[str, str] | None:
    """Split `Entity.Property` using the LONGEST table name that actually prefixes `value`.

    ⚠️ **A table name can contain dots**, and splitting at the first one is what made this module
    over-report. Measured on the estate corpus (blind review of PR #399): the bound model holds a
    table literally called `HumanResources.csv`, so `partition(".")` read `HumanResources.csv.Status`
    as entity `HumanResources` + property `csv.Status`, decided the property had changed, and
    retained a textbook invalid -> valid rebind as an unexplained `MODEL_OBJECT_NAMES`. 562 of 574
    retained `queryRef` leaves had more than one dot on some side.
    """
    best = ""
    for table in tables:
        if len(table) > len(best) and value.startswith(f"{table}."):
            best = table
    return (best, value[len(best) + 1 :]) if best else None


def _unwrap_aggregation(value: str) -> tuple[str, str, str]:
    """`Sum(Orders.csv.Sales) 2` -> (`Sum`, `Orders.csv.Sales`, ` 2`); anything else -> (``, value, ``).

    Three parts, not two. The engine wraps aggregated projections, so a rebind inside the wrapper is
    invisible to a bare table-prefix test - and Power BI then appends a disambiguation suffix to a
    duplicated reference, which an unwrap anchored at the end of the string cannot see past.
    """
    match = _AGGREGATION.match(value)
    return (match.group(1), match.group(2), match.group(3)) if match else ("", value, "")


def _query_ref_shape(before: str, after: str, tables: set[str]) -> str:
    """`BINDING_RESOLUTION` for a `queryRef` only when JUST the entity was replaced, invalid->valid.

    Everything must line up: the same aggregation wrapper, the same disambiguation suffix, an
    after-value that resolves against a real table in the bound model, an unchanged property, and a
    before-entity that is NOT a table. A projection that moves to a different table AND a different
    column (`Orders.Order_Date` -> `Date.Year`, 97 of 574 on the estate) is a model-shape change and
    stays `MODEL_OBJECT_NAMES`.
    """
    before_wrapper, before_inner, before_suffix = _unwrap_aggregation(before)
    after_wrapper, after_inner, after_suffix = _unwrap_aggregation(after)
    if before_wrapper != after_wrapper or before_suffix != after_suffix:
        return SHAPE_MODEL_NAMES
    resolved = _split_on_known_table(after_inner, tables)
    if resolved is None:
        return SHAPE_MODEL_NAMES
    after_entity, after_property = resolved
    suffix = f".{after_property}"
    if not before_inner.endswith(suffix) or len(before_inner) <= len(suffix):
        return SHAPE_MODEL_NAMES
    before_entity = before_inner[: -len(suffix)]
    if before_entity == after_entity or before_entity in tables:
        return SHAPE_MODEL_NAMES
    return SHAPE_BINDING


def name_change_shape(difference: Difference, tables: set[str] | None) -> str:
    """`BINDING_RESOLUTION` only when THIS leaf is demonstrably an invalid -> valid rebind.

    Per-leaf, deliberately (see the module header): a rebind sitting in the same file as an
    unexplained table substitution must not launder it. Anything that cannot be demonstrated -
    an unknown model, a non-`value` change, a `Property`/`nativeQueryRef` leaf whose validity this
    module cannot check - stays `MODEL_OBJECT_NAMES`.
    """
    if tables is None or difference.kind != "value":
        return SHAPE_MODEL_NAMES
    before, after = difference.before, difference.after
    if not isinstance(before, str) or not isinstance(after, str):
        return SHAPE_MODEL_NAMES
    leaf = _leaf(difference.pointer)
    if leaf == "Entity":
        return SHAPE_BINDING if before not in tables and after in tables else SHAPE_MODEL_NAMES
    if leaf == "queryRef":
        return _query_ref_shape(before, after, tables)
    return SHAPE_MODEL_NAMES


def _tmdl_line_shape(line: str) -> str:
    for pattern, shape in _TMDL_LINE_KINDS:
        if pattern.match(line):
            return shape
    return SHAPE_TMDL_OTHER


def _text_shapes(baseline_file: Path, working_file: Path) -> tuple[list[str], int]:
    """Shapes for a changed TMDL/plain-text file, from the lines that differ.

    A blank line carries no meaning, so it is counted as a difference but never given a shape - it
    would otherwise dominate `TMDL_OTHER` and make the residue bucket look alarming.
    """
    try:
        before = baseline_file.read_text(encoding="utf-8").splitlines()
        after = working_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [SHAPE_UNREADABLE_TEXT], 1
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    shapes: set[str] = set()
    count = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for line in before[i1:i2] + after[j1:j2]:
            count += 1
            if line.strip():
                shapes.add(_tmdl_line_shape(line))
    return sorted(shapes) or [SHAPE_TMDL_OTHER], count


def shapes_for_change(
    baseline_file: Path,
    working_file: Path,
    tables: set[str] | None,
) -> tuple[list[str], int]:
    """Shapes describing one changed file, plus the number of structural differences found."""
    suffix = baseline_file.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return _text_shapes(baseline_file, working_file)
    if suffix not in _JSON_SUFFIXES:
        return [SHAPE_BINARY], 1
    try:
        before = json.loads(baseline_file.read_text(encoding="utf-8"))
        after = json.loads(working_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return [SHAPE_UNREADABLE_JSON], 1
    differences = json_pointer_diff(before, after)
    shapes = set()
    for difference in differences:
        shape = pointer_shape(difference.pointer)
        shapes.add(name_change_shape(difference, tables) if shape == SHAPE_MODEL_NAMES else shape)
    return sorted(shapes), len(differences)


def added_removed_shape(relative: str, added: bool) -> str:
    """Shape for a file present on only one side of a comparison."""
    name = relative.rsplit("/", 1)[-1].lower()
    if name == "page.json":
        return "PAGE_ADDED" if added else "PAGE_REMOVED"
    if name == "visual.json":
        return "VISUAL_ADDED" if added else "VISUAL_REMOVED"
    return "FILE_ADDED" if added else "FILE_REMOVED"
