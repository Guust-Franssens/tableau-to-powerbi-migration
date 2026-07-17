"""
purpose: Fix the systematic airline DAX bug where measures use the compact CALCULATE
         boolean-filter form `'Table'[Col] = [SomeMeasure]` (RHS is a measure), which DAX
         rejects with "A function 'PLACEHOLDER' has been used in a True/False expression
         that is used as a table filter expression". For every measure that contains that
         pattern, hoist ALL of its model-measure references into VARs and compare the column
         against the VAR (a constant scalar), which is legal. Kept single-line
         (VAR ... VAR ... RETURN ...). Covers Parameter Value, PM Year/Month Value, and any
         other measure used as a compact-filter RHS.
usage:   python migrations/airline-alliance-activity/_work/fix_placeholder_filters.py
"""

from __future__ import annotations

import re
from pathlib import Path

TABLES_DIR = (
    Path(__file__).resolve().parents[1] / "fabric" / "AirlineAllianceActivity.SemanticModel" / "definition" / "tables"
)

MEASURE_RE = re.compile(r"^(?P<indent>\s*)measure\s+'(?P<name>[^']+)'\s*=\s*(?P<expr>.+)$")
# A compact CALCULATE boolean filter whose right-hand side is a bare [measure]:
#   'Table'[Col] <op> [Measure]
COMPACT_FILTER_RE = re.compile(r"'[^']+'\[[^\]]+\]\s*(?:<=|>=|<>|=|<|>)\s*\[[^\]]+\]")
BARE_MEASURE_RE = re.compile(r"(?<!')\[(?P<name>[^\]]+)\]")


def collect_measure_names(files: list[Path]) -> set[str]:
    names: set[str] = set()
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            m = MEASURE_RE.match(line)
            if m:
                names.add(m.group("name"))
    return names


def var_name(measure: str) -> str:
    return "_m_" + re.sub(r"[^a-z0-9]+", "_", measure.lower()).strip("_")


def transform_expr(expr: str, self_name: str, measures: set[str]) -> str | None:
    if not COMPACT_FILTER_RE.search(expr):
        return None  # no broken compact filter -> leave untouched
    refs: list[str] = []
    for m in BARE_MEASURE_RE.finditer(expr):
        nm = m.group("name")
        if nm in measures and nm != self_name and nm not in refs:
            refs.append(nm)
    if not refs:
        return None
    decls = " ".join(f"VAR {var_name(n)} = [{n}]" for n in refs)
    body = expr
    for n in refs:
        body = re.sub(r"(?<!')\[" + re.escape(n) + r"\]", var_name(n), body)
    return f"{decls} RETURN {body}"


def main() -> None:
    files = sorted(TABLES_DIR.glob("*.tmdl"))
    measures = collect_measure_names(files)
    total = 0
    for f in files:
        lines = f.read_text(encoding="utf-8").splitlines()
        changed = 0
        for i, line in enumerate(lines):
            m = MEASURE_RE.match(line)
            if not m:
                continue
            new_expr = transform_expr(m.group("expr"), m.group("name"), measures)
            if new_expr is None:
                continue
            lines[i] = f"{m.group('indent')}measure '{m.group('name')}' = {new_expr}"
            changed += 1
        if changed:
            f.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"  {f.name}: rewrote {changed} measure(s)")
            total += changed
    print(f"Hoisted compact-filter measure refs into VARs in {total} measures.")


if __name__ == "__main__":
    main()
