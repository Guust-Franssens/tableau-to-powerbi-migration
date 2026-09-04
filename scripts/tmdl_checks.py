"""
purpose: TMDL structural checks - the half of the model gate that reads TMDL rather than M.
usage:   imported by scripts/check_datamodel.py (which re-exports these names)
internal: true
internal-reason: a library with no CLI of its own. Agents reach every check here by running
                 `python scripts/check_datamodel.py`, which is the agent-facing entry point and is
                 already named in scripts/README.md.

Split out of check_datamodel.py so neither half needs a `too-many-lines` waiver. The two halves
share only the shape of their output: this module reports `TmdlFinding`, the M scanner reports
`Finding`, and `check_datamodel.main` prints both.

Scope stays deliberately narrow because a false positive is worse than a miss:

  * duplicate scalar TMDL properties within one object
  * measure/column name collisions within one table file
  * a measure name repeated in a DIFFERENT table (model-wide uniqueness - see issue #413: this is
    the one check ported from the drifted `examples/*/fabric/_validation/tmdl_validate/` copies
    - four of the five were pure redundant drift and were retired onto `tools/tmdl_oracle`; the
    fifth, `examples/quadruple-axis-charts`, also checks unresolved DAX [bracket] references and
    is kept for now since neither this module nor the oracle covers that yet. It deserializes
    clean but Desktop refuses the commit, which is exactly the class this module exists to catch
    cheaply, without AMO)
  * empty measure expressions
  * direct CALCULATE/CALCULATETABLE compact filters that compare a column to a measure

Expression LAYOUT - the class where a property is silently swallowed into the preceding DAX/M, or
where the document does not parse at all - is deliberately NOT here. Two hand-written grammars for
it (name matching, then the documented indentation contract) each shipped false negatives AND false
positives; `scripts/tmdl_oracle.py` answers those questions with the real parser instead.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TmdlFinding:
    """One TMDL/data-model problem, located precisely enough to fix without hunting."""

    code: str
    message: str
    file: Path
    line: int

    def render(self, root: Path) -> str:
        """Format as `path:line CODE`, matching the M finding output style."""
        try:
            shown = self.file.relative_to(root)
        except ValueError:
            shown = self.file
        return f"  {shown}:{self.line}  {self.code}\n      {self.message}"


_OBJECT_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kind>table|measure|column|partition|hierarchy|level|role|"
    r"relationship|culture|expression|calculationGroup|calculationItem|perspective|model|database)\b"
    r"(?P<rest>.*)$"
)
_PROPERTY_RE = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*:\s*(?P<value>.*)$")
_REPEATABLE_PROPERTIES = {"annotation", "extendedProperty", "changedProperty", "ref", "variation", "levels"}
_COLUMN_EQ_MEASURE_RE = re.compile(r"^'?[^'\[\]]+'?\s*\[[^\]]+\]\s*(?:=|<>|<=|>=|<|>)\s*\[[^\]]+\]$")
_CALCULATE_RE = re.compile(r"\bCALCULATE(?:TABLE)?\s*\(", re.IGNORECASE)
# TMDL's own quoting grammar: a quoted name is closed only by an apostrophe NOT followed by
# another apostrophe (the doubled-apostrophe escape) - same rule check_field_bindings.py's
# `_NAME`/`_unquote` use for the identical reason (a name like 'O''Brien Sales' is one token).
_QUOTED_NAME_RE = re.compile(r"'(?:[^']|'')*'")


def _split_top_level_args(text: str) -> list[str]:
    """Split a function argument list on commas that are not nested inside another expression."""
    args: list[str] = []
    depth = 0
    start = 0
    in_string = False
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch == '"':
            if in_string and idx + 1 < len(text) and text[idx + 1] == '"':
                idx += 2
                continue
            in_string = not in_string
        elif not in_string:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                args.append(text[start:idx])
                start = idx + 1
        idx += 1
    args.append(text[start:])
    return args


def find_compact_filters(expression: str) -> bool:
    """Return True for direct CALCULATE filters like `'T'[Col] = [Measure]`.

    The same predicate is legal inside FILTER(...), so the check only inspects direct filter
    arguments after the first CALCULATE/CALCULATETABLE expression argument.
    """
    for match in _CALCULATE_RE.finditer(expression):
        depth = 1
        end: int | None = None
        in_string = False
        idx = match.end()
        while idx < len(expression):
            ch = expression[idx]
            if ch == '"':
                if in_string and idx + 1 < len(expression) and expression[idx + 1] == '"':
                    idx += 2
                    continue
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = idx
                        break
            idx += 1
        if end is None:
            continue
        for arg in _split_top_level_args(expression[match.end() : end])[1:]:
            if _COLUMN_EQ_MEASURE_RE.match(arg.strip()):
                return True
    return False


def _strip_tmdl_comment(line: str) -> str:
    """Remove a TMDL description/comment line so prose cannot be mistaken for a property."""
    return "" if line.lstrip().startswith("///") else line


def _object_name(rest: str) -> str | None:
    """Extract a TMDL object's name from the remainder of its header line, unescaped."""
    head = rest.split("=", 1)[0].strip()
    if head.startswith("'"):
        match = _QUOTED_NAME_RE.match(head)
        return match.group(0)[1:-1].replace("''", "'") if match else None
    parts = head.split()
    return parts[0] if parts else None


def _expression_on_measure_header(rest: str) -> bool:
    """Whether a measure header carries a non-empty expression after `=`."""
    return "=" in rest and bool(rest.split("=", 1)[1].strip())


def _nearest_context(seen: dict[int, dict[str, int]], indent: int) -> int | None:
    """Return the nearest open TMDL object context for a property indent."""
    candidates = [depth for depth in seen if depth <= indent]
    return min(candidates, key=lambda depth: indent - depth) if candidates else None


def _empty_measure_finding(path: Path, pending_measure: tuple[str, int]) -> TmdlFinding:
    """Build the repeated empty-measure finding once so narrowing stays simple."""
    name, line = pending_measure
    return TmdlFinding("EMPTY_EXPRESSION", f"measure '{name}' has no expression after '='.", path, line)


def _append_name_collisions(
    findings: list[TmdlFinding], path: Path, table_name: str, measures: dict[str, int], columns: dict[str, int]
) -> None:
    """Report measure/column collisions for one table scope."""
    for name, line in measures.items():
        if name in columns:
            findings.append(
                TmdlFinding(
                    "NAME_COLLISION",
                    f"measure '{name}' in table '{table_name}' has the same name as a column in "
                    f"that table; Tabular names must be unique within a table. Column first seen "
                    f"at line {columns[name]}.",
                    path,
                    line,
                )
            )


def check_tmdl_text(path: Path, text: str) -> list[TmdlFinding]:  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Structurally check one TMDL document."""
    findings: list[TmdlFinding] = []
    seen: dict[int, dict[str, int]] = {}
    context_line: dict[int, int] = {}
    current_table = ""
    measures: dict[str, int] = {}
    columns: dict[str, int] = {}
    pending_measure: tuple[str, int] | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_tmdl_comment(raw)
        if not line.strip():
            continue

        if pending_measure and _OBJECT_RE.match(line):
            findings.append(_empty_measure_finding(path, pending_measure))
            pending_measure = None

        if find_compact_filters(line):
            findings.append(
                TmdlFinding(
                    "COMPACT_FILTER",
                    "direct CALCULATE filter compares a column to a measure; DAX requires a constant "
                    "on the right of a compact predicate. Hoist the measure into a VAR or wrap the "
                    "predicate in FILTER(...).",
                    path,
                    number,
                )
            )

        obj = _OBJECT_RE.match(line)
        if obj:
            indent = len(obj.group("indent").expandtabs(4))
            for depth in [depth for depth in seen if depth > indent]:
                del seen[depth]
            seen[indent + 1] = {}
            context_line[indent + 1] = number
            kind = obj.group("kind")
            rest = obj.group("rest")
            name = _object_name(rest)
            if kind == "table" and name:
                _append_name_collisions(findings, path, current_table, measures, columns)
                current_table = name
                measures = {}
                columns = {}
            elif kind == "measure" and name and current_table:
                measures.setdefault(name, number)
                pending_measure = None if _expression_on_measure_header(rest) else (name, number)
            elif kind == "column" and name and current_table:
                columns.setdefault(name, number)
            continue

        prop = _PROPERTY_RE.match(line)
        if prop:
            indent = len(prop.group("indent").expandtabs(4))
            name = prop.group("name")
            if name not in _REPEATABLE_PROPERTIES:
                bucket = _nearest_context(seen, indent)
                if bucket is not None:
                    if name in seen[bucket]:
                        findings.append(
                            TmdlFinding(
                                "DUPLICATE_PROPERTY",
                                f"'{name}' appears more than once in the object opened at line "
                                f"{context_line[bucket]}; TMDL rejects duplicated scalar properties. "
                                f"First seen at line {seen[bucket][name]}.",
                                path,
                                number,
                            )
                        )
                    else:
                        seen[bucket][name] = number
            if pending_measure:
                findings.append(_empty_measure_finding(path, pending_measure))
                pending_measure = None
            continue

        if pending_measure and line.strip():
            pending_measure = None

    if pending_measure:
        findings.append(_empty_measure_finding(path, pending_measure))

    _append_name_collisions(findings, path, current_table, measures, columns)
    return findings


def _read_tmdl(path: Path) -> tuple[str | None, list[TmdlFinding]]:
    """Decode one TMDL document, reporting what makes it unopenable rather than normalising it.

    Two distinct failures hide here, and both used to read as "clean":

      * undecodable bytes - `errors="replace"` turns a file this checker cannot actually read into
        a reassuring pass, so decoding is strict;
      * a UTF-8 BOM - AMO tolerates it, but Power BI Desktop's project reader does NOT
        (`UTF8EncodingThrowOnBOM.CheckBom` -> "Only text with UTF8 encoding without BOM is
        supported") and the file simply does not open. Stripping it in memory, as this function
        used to, made a broken deliverable pass a gate whose whole purpose is to catch that.
    """
    findings: list[TmdlFinding] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, [TmdlFinding("TMDL_UNREADABLE", f"could not be read ({type(exc).__name__}).", path, 1)]
    if raw.startswith(codecs.BOM_UTF8):
        findings.append(
            TmdlFinding(
                "TMDL_BOM",
                "starts with a UTF-8 BOM. Power BI Desktop rejects it outright ('Only text with "
                "UTF8 encoding without BOM is supported') and the project does not open. Rewrite "
                "the file as UTF-8 without a BOM (Python encoding='utf-8', PowerShell utf8NoBOM).",
                path,
                1,
            )
        )
        raw = raw[len(codecs.BOM_UTF8) :]
    try:
        return raw.decode("utf-8"), findings
    except UnicodeDecodeError:
        findings.append(
            TmdlFinding(
                "TMDL_UNREADABLE",
                "could not be decoded as UTF-8, so its contents were NOT checked. This is not a "
                "pass. Power BI requires TMDL as UTF-8 without a BOM.",
                path,
                1,
            )
        )
        return None, findings


def _iter_measure_declarations(text: str):
    """Yield (line, table, measure name) for every measure header in one document.

    Deliberately independent of `check_tmdl_text`'s own per-table bookkeeping, which resets at each
    table boundary and is not visible outside that function - model-wide uniqueness needs every
    measure name from every file, not just the last table seen in each.
    """
    current_table = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_tmdl_comment(raw)
        if not line.strip():
            continue
        obj = _OBJECT_RE.match(line)
        if not obj:
            continue
        kind = obj.group("kind")
        name = _object_name(obj.group("rest"))
        if kind == "table" and name:
            current_table = name
        elif kind == "measure" and name and current_table:
            yield number, current_table, name


def _duplicate_measure_finding(
    first_seen: dict[str, tuple[Path, int, str]], tmdl: Path, line: int, table: str, name: str
) -> TmdlFinding | None:
    """Record `name`'s first sighting, or report it as a model-wide duplicate on a later one.

    Tabular measure names are case-insensitive and unique across the WHOLE model, not merely
    within a table - unlike the same-table NAME_COLLISION check above, this fires across
    DIFFERENT tables and DIFFERENT files.
    """
    key = name.casefold()
    seen = first_seen.get(key)
    if seen is None:
        first_seen[key] = (tmdl, line, table)
        return None
    first_file, first_line, first_table = seen
    return TmdlFinding(
        "MEASURE_NAME_DUPLICATE",
        f"measure '{name}' in table '{table}' repeats the model-wide name already used "
        f"by table '{first_table}' ({first_file.name}:{first_line}); Tabular measure "
        "names must be unique across the WHOLE model, not just within one table - "
        "Desktop refuses to load/commit this even though it deserializes clean.",
        tmdl,
        line,
    )


def check_tmdl_model(model_dir: Path) -> tuple[list[TmdlFinding], int]:
    """Every TMDL document in one .SemanticModel."""
    findings: list[TmdlFinding] = []
    definition = model_dir / "definition"
    files = sorted(definition.rglob("*.tmdl")) if definition.exists() else []
    first_seen: dict[str, tuple[Path, int, str]] = {}
    for tmdl in files:
        text, problems = _read_tmdl(tmdl)
        findings.extend(problems)
        if text is None:
            continue
        findings.extend(check_tmdl_text(tmdl, text))
        for line, table, name in _iter_measure_declarations(text):
            finding = _duplicate_measure_finding(first_seen, tmdl, line, table, name)
            if finding is not None:
                findings.append(finding)
    return findings, len(files)
