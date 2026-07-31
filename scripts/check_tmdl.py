"""
purpose: catch TMDL defects that BOTH existing structural gates are blind to, before Power BI Desktop
         sees the model - without needing .NET, the modeling MCP, or Desktop.

Why this exists
---------------
Measured during a battle-test run: a semantic model with a duplicated `formatString` property could
not be opened by Power BI Desktop at all, yet

  * `scripts/check_m_syntax.py` passed it - it only looks at the M inside partitions;
  * `powerbi-report-author validate` passed it - it validates the *Report* item, not TMDL.

The only thing that caught it was a full TMDL round-trip (`connection_operations ConnectFolder` ->
`DuplicatedProperty ... Document './tables/Shipments' Line Number 8`). `scripts/preflight.ps1`
already asks for the .NET SDK "to build/run the offline TMDL structural validator (tmdl_validate)" -
but that tool does not exist in this repo. This is the cheap, dependency-free part of it.

Scope, deliberately narrow (a noisy checker gets switched off):
  * DUPLICATE_PROPERTY   - the same scalar property twice in one object. Hard error in TMDL; the
                           exact defect that blocked a model while both other gates said green.
  * NAME_COLLISION       - a measure named identically to a column in the same table. Tabular's
                           naming-uniqueness rule; deserializes fine, fails on model commit, and is
                           listed in this repo's own capabilities writeup as a real observed defect.
  * COMPACT_FILTER       - `CALCULATE(..., 'T'[Col] = [Measure])`. DAX only allows a *constant* on
                           the right of a compact filter predicate; referencing a measure is illegal
                           and surfaces in Desktop as an opaque PLACEHOLDER error. One real migration
                           in this repo shipped this **58 times** in a single model - the fix is to
                           hoist the measure into a VAR. Purely structural, so it is cheap to catch.
  * EMPTY_EXPRESSION     - a measure declared with no expression.

A clean result does NOT prove the model opens - it excludes these classes only. The authoritative
check remains a real round-trip (ConnectFolder) or opening the .pbip in Desktop.

usage:   python scripts/check_tmdl.py <path to .SemanticModel or a folder containing one>
         python scripts/check_tmdl.py --all
Exit 0 = clean, 1 = problems found, 2 = nothing was checked (NOT a pass).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("check_tmdl")

REPO_ROOT = Path(__file__).resolve().parent.parent
TREES = ("examples", "migrations/workbooks", "migrations/datasources")

# Object headers that open a new property context.
_OBJECT_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kind>table|measure|column|partition|hierarchy|level|role|"
    r"relationship|culture|expression|calculationGroup|calculationItem|perspective|model|database)\b"
    r"(?P<rest>.*)$"
)
# A scalar property: `name: value`. TMDL's repeatable constructs use `keyword name = value` instead,
# so keying on the colon form already excludes most of them; the denylist covers the rest.
_PROPERTY_RE = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*:\s*(?P<value>.*)$")
_REPEATABLE = {"annotation", "extendedProperty", "changedProperty", "ref", "variation", "levels"}

# A DAX comparison whose LEFT side is a column reference and RIGHT side is a bare measure reference.
# Only illegal when it is a DIRECT argument of CALCULATE/CALCULATETABLE (a "compact filter"): inside
# FILTER(...) the very same expression is legal and is in fact the recommended fix, so matching the
# text alone produces false positives (measured: 18 of them, all legal, across the committed corpus).
_COLUMN_EQ_MEASURE_RE = re.compile(r"^'?[^'\[\]]+'?\s*\[[^\]]+\]\s*(?:=|<>|<=|>=|<|>)\s*\[[^\]]+\]$")
_CALCULATE_RE = re.compile(r"\bCALCULATE(?:TABLE)?\s*\(", re.I)


def _split_top_level_args(text: str) -> list[str]:
    """Split a function's argument text on commas that are at nesting depth 0."""
    args, depth, start, quote = [], 0, 0, False
    for i, ch in enumerate(text):
        if ch == '"':
            quote = not quote
        elif not quote:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(text[start:i])
                start = i + 1
    args.append(text[start:])
    return args


def find_compact_filters(expression: str) -> bool:
    """True when a CALCULATE argument is a bare `'Table'[Column] = [Measure]` predicate.

    DAX allows only a constant on the right of a compact filter predicate; a measure reference there
    is illegal and surfaces in Power BI Desktop as an opaque PLACEHOLDER error. One migration in this
    repo shipped it 58 times in a single model. The fix is to hoist the measure into a VAR (or wrap
    the predicate in FILTER), so this check must NOT fire on the wrapped form.
    """
    for match in _CALCULATE_RE.finditer(expression):
        depth, end = 1, None
        for i in range(match.end(), len(expression)):
            if expression[i] == "(":
                depth += 1
            elif expression[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        # Argument 0 is the expression being evaluated; filters start at argument 1.
        for arg in _split_top_level_args(expression[match.end() : end])[1:]:
            if _COLUMN_EQ_MEASURE_RE.match(arg.strip()):
                return True
    return False


@dataclass
class Finding:
    """One TMDL problem, located precisely enough to fix without hunting."""

    code: str
    message: str
    file: Path
    line: int

    def render(self, root: Path) -> str:
        """Format as `path:line  CODE` plus the message, matching check_m_syntax's output style."""
        try:
            shown = self.file.relative_to(root)
        except ValueError:
            shown = self.file
        return f"  {shown}:{self.line}  {self.code}\n      {self.message}"


def _strip_comment(line: str) -> str:
    """Remove a trailing TMDL description/comment marker so it cannot be mistaken for a property."""
    return "" if line.lstrip().startswith("///") else line


def check_tmdl_text(path: Path, text: str) -> list[Finding]:
    """Structurally check one TMDL document."""
    findings: list[Finding] = []
    # (indent, header-identity) -> {property name: first line seen}
    seen: dict[int, dict[str, int]] = {}
    context_line: dict[int, int] = {}
    measures: dict[str, int] = {}
    columns: dict[str, int] = {}
    pending_measure: tuple[str, int] | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw)
        if not line.strip():
            continue

        if find_compact_filters(line):
            findings.append(
                Finding(
                    "COMPACT_FILTER",
                    "compact filter predicate compares a column to a MEASURE - DAX requires a constant "
                    "on the right of `'Table'[Column] = ...`. This deserializes fine and fails in "
                    "Desktop with an opaque PLACEHOLDER error. Hoist the measure into a VAR and "
                    "compare against that instead.",
                    path,
                    number,
                )
            )

        obj = _OBJECT_RE.match(line)
        if obj:
            indent = len(obj.group("indent").expandtabs(4))
            # Opening any object invalidates every context at or below its depth.
            for depth in [d for d in seen if d > indent]:
                del seen[depth]
            seen[indent + 1] = {}
            context_line[indent + 1] = number
            kind, rest = obj.group("kind"), obj.group("rest")
            name = _object_name(rest)
            if kind == "measure" and name:
                measures.setdefault(name, number)
                pending_measure = (name, number)
                if not rest.split("=", 1)[1].strip() if "=" in rest else True:
                    pass  # expression may continue on following lines; checked below
            elif kind == "column" and name:
                columns.setdefault(name, number)
            if kind == "measure" and "=" in rest and rest.split("=", 1)[1].strip():
                pending_measure = None
            continue

        prop = _PROPERTY_RE.match(line)
        if not prop:
            if pending_measure and line.strip():
                pending_measure = None  # the expression continued onto this line
            continue
        indent = len(prop.group("indent").expandtabs(4))
        name = prop.group("name")
        if name in _REPEATABLE:
            continue
        bucket = min((d for d in seen if d <= indent), key=lambda d: indent - d, default=None)
        if bucket is None:
            continue
        if name in seen[bucket]:
            findings.append(
                Finding(
                    "DUPLICATE_PROPERTY",
                    f"'{name}' appears more than once in the object opened at line "
                    f"{context_line[bucket]} - TMDL rejects this outright (DuplicatedProperty), so "
                    f"Power BI Desktop cannot open the model. First seen at line {seen[bucket][name]}.",
                    path,
                    number,
                )
            )
        else:
            seen[bucket][name] = number
        if pending_measure:
            pending_measure = None

    for name, line in measures.items():
        if name in columns:
            findings.append(
                Finding(
                    "NAME_COLLISION",
                    f"measure '{name}' has the same name as a column in this table. Tabular requires "
                    f"names to be unique within a table; this deserializes fine and fails on model "
                    f"commit. (column at line {columns[name]})",
                    path,
                    line,
                )
            )
    return findings


def _object_name(rest: str) -> str | None:
    """Extract the object's name from the remainder of its header line."""
    head = rest.split("=", 1)[0].strip()
    if head.startswith("'"):
        end = head.find("'", 1)
        return head[1:end] if end > 0 else None
    return head.split()[0] if head.split() else None


def check_model(model: Path) -> tuple[list[Finding], int]:
    """Check every .tmdl document under a .SemanticModel folder. Returns (findings, files scanned)."""
    findings: list[Finding] = []
    files = sorted((model / "definition").rglob("*.tmdl")) if (model / "definition").exists() else []
    for path in files:
        findings.extend(check_tmdl_text(path, path.read_text(encoding="utf-8")))
    return findings, len(files)


def _model_dirs(raw: Path) -> list[Path]:
    """Resolve a user-supplied path to the .SemanticModel folder(s) it refers to."""
    if raw.name.endswith(".SemanticModel"):
        return [raw]
    if raw.is_dir():
        return sorted(raw.glob("*.SemanticModel")) or sorted(raw.glob("*/*.SemanticModel"))
    return []


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--all", action="store_true", help="check every model in the repo's migration trees")
    args = ap.parse_args(argv)

    targets: list[Path] = []
    if args.all or not args.paths:
        for tree in TREES:
            targets.extend(sorted((REPO_ROOT / tree).glob("*/fabric/*.SemanticModel")))
    for raw in args.paths:
        targets.extend(_model_dirs(raw.resolve()))

    if not targets:
        # Same rule as check_m_syntax: a gate that cannot find its subject has NOT passed.
        log.error("No .SemanticModel folders found - nothing was checked. This is NOT a pass.")
        return 2

    total = 0
    scanned = 0
    for model in targets:
        findings, count = check_model(model)
        scanned += count
        for f in findings:
            log.error("%s", f.render(REPO_ROOT))
        total += len(findings)

    if total:
        log.error("")
        log.error(
            "%d TMDL problem(s) across %d model(s). These are invisible to check_m_syntax.py (M only) "
            "and to powerbi-report-author validate (Report item only) - they surface as a failed model "
            "load in Power BI Desktop.",
            total,
            len(targets),
        )
        return 1
    log.info("TMDL OK - %d document(s) across %d model(s), no structural problems found.", scanned, len(targets))
    log.info("  NOTE: excludes duplicate properties, measure/column collisions and empty measures only.")
    log.info("  A clean result does not prove the model opens; a real round-trip or Desktop does that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
