"""
purpose: measure what the pipeline's STRUCTURAL gates actually catch, by seeding a built PBIP with
         the exact defect classes this toolkit has shipped before and re-running the gates over each
         mutant. The repo asserts "structural validation is necessary, not sufficient" in prose; this
         turns that claim into a reproducible recall number, and gives the fidelity validator a
         ground-truth answer key to be scored against.

         Every defect class below is a REAL bug this project hit (see docs/capabilities-and-
         limitations.md and the agent Gotchas), not an invented one:
           * fmt-scale-100x        - a percent-unit measure given a "0.00%" format (eea-urban-adaptation)
           * illegal-compact-filter- CALCULATE(..., 'T'[Col]=[Measure]) (airline-alliance-activity, x58)
           * flat-lined-measure    - a trend measure replaced by a constant
           * wrong-aggregation     - SUM silently swapped to AVERAGE
           * m-trailing-comma      - the M defect Desktop reports with no file/line
           * m-missing-in          - a `let` with no `in`
           * dropped-visual        - a worksheet silently missing from the report
           * report-version-missing- report.json without reportVersionAtImport (the 0.1.4 CLI floor)
           * measure-name-collision- a measure named identically to a column
           * broken-field-ref      - a visual bound to a field that does not exist in the model

usage:   python scripts/seed_known_defects.py --pbip <folder with .SemanticModel/.Report> --list
         python scripts/seed_known_defects.py --pbip <folder> --out <dir> --run-gates
         python scripts/seed_known_defects.py --pbip <folder> --out <dir> --only fmt-scale-100x

Exit code is 0 when the run completes; the recall table is the output that matters. A defect that no
gate catches is not a bug in this script - it is the point, and it is what the fidelity validator
(pbi-migration-validator) and a Desktop open-test have to cover instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_known_defects")

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Mutation:
    """One seeded defect: what it is, whether it applied, and which gate (if any) caught it."""

    name: str
    detail: str
    kind: str  # "model" | "report"
    expected_catcher: str  # the gate we BELIEVE should catch it ("" = believed uncatchable structurally)
    applied: bool = False
    note: str = ""
    caught_by: list[str] = field(default_factory=list)


def _model_dir(pbip: Path) -> Path:
    """Locate the .SemanticModel folder inside a PBIP project folder."""
    found = sorted(pbip.glob("*.SemanticModel"))
    if not found:
        raise SystemExit(f"no *.SemanticModel folder under {pbip}")
    return found[0]


def _report_dir(pbip: Path) -> Path:
    """Locate the .Report folder inside a PBIP project folder."""
    found = sorted(pbip.glob("*.Report"))
    if not found:
        raise SystemExit(f"no *.Report folder under {pbip}")
    return found[0]


def _tmdl_tables(model: Path) -> list[Path]:
    """Every table TMDL file in the model definition."""
    return sorted((model / "definition" / "tables").glob("*.tmdl"))


def _first_measure(text: str) -> re.Match | None:
    """Find the first `measure <name> = <expr>` in a TMDL table file."""
    return re.search(r"(?m)^(\s*)measure\s+('([^']+)'|([A-Za-z0-9_ ]+))\s*=\s*(.+)$", text)


# --------------------------------------------------------------------------------------- mutations


def mut_fmt_scale_100x(model: Path, _report: Path) -> tuple[bool, str]:
    """REPLACE a measure's format string with a percentage one, without dividing by 100.

    The silent 100x class: Tableau frequently bakes `* 100` into the formula while Power BI keeps
    formatting separate, so a value of 12.83 meaning 12.83% displayed as "0.00%" renders 1283.00%.
    Nothing errors. Note this must REPLACE an existing formatString, not add one - adding a second
    creates a TMDL DuplicatedProperty, which is a different (and loudly fatal) defect. Conflating the
    two would credit a gate for catching a bug it did not catch.
    """
    for tmdl in _tmdl_tables(model):
        text = tmdl.read_text(encoding="utf-8")
        m = re.search(r"(?m)^(\s*)formatString:\s*(?!0\.00%).+$", text)
        if not m:
            continue
        tmdl.write_text(text[: m.start()] + f"{m.group(1)}formatString: 0.00%" + text[m.end() :], encoding="utf-8")
        return True, f"{tmdl.name}: a currency/decimal formatString replaced by 0.00% with no /100"
    return False, "no replaceable formatString found"


def mut_duplicate_property(model: Path, _report: Path) -> tuple[bool, str]:
    """Add a second `formatString` to one measure - TMDL's DuplicatedProperty, a hard load failure.

    Observed for real: this blocked Power BI Desktop from opening the model while BOTH existing gates
    reported green (check_m_syntax reads only M; validate checks only the Report item).
    """
    for tmdl in _tmdl_tables(model):
        text = tmdl.read_text(encoding="utf-8")
        m = re.search(r"(?m)^(\s*)formatString:\s*.+$", text)
        if not m:
            continue
        tmdl.write_text(text[: m.end()] + f"\n{m.group(1)}formatString: 0.00%" + text[m.end() :], encoding="utf-8")
        return True, f"{tmdl.name}: a second formatString added to one measure"
    return False, "no formatString found to duplicate"


def mut_illegal_compact_filter(model: Path, _report: Path) -> tuple[bool, str]:
    """Rewrite a measure to CALCULATE with the illegal `'Table'[Col]=[Measure]` compact filter.

    Deserializes fine; Desktop rejects it with a PLACEHOLDER error only when the model commits.
    """
    for tmdl in _tmdl_tables(model):
        text = tmdl.read_text(encoding="utf-8")
        m = _first_measure(text)
        col = re.search(r"(?m)^\s*column\s+'?([^'\n=]+?)'?\s*$", text)
        if not m or not col:
            continue
        table = tmdl.stem
        expr = f"CALCULATE({m.group(5).strip()}, '{table}'[{col.group(1).strip()}] = [Delay KPI])"
        text = text[: m.start(5)] + expr + text[m.end(5) :]
        tmdl.write_text(text, encoding="utf-8")
        return True, f"{tmdl.name}: measure {m.group(2)} now uses 'T'[Col]=[Measure]"
    return False, "no measure+column pair found"


def mut_flat_lined_measure(model: Path, _report: Path) -> tuple[bool, str]:
    """Replace a measure body with a constant, so its visual renders a flat line."""
    for tmdl in _tmdl_tables(model):
        text = tmdl.read_text(encoding="utf-8")
        m = _first_measure(text)
        if not m:
            continue
        text = text[: m.start(5)] + "1" + text[m.end(5) :]
        tmdl.write_text(text, encoding="utf-8")
        return True, f"{tmdl.name}: measure {m.group(2)} body replaced with the constant 1"
    return False, "no measure found"


def mut_wrong_aggregation(model: Path, _report: Path) -> tuple[bool, str]:
    """Swap SUM for AVERAGE inside a measure EXPRESSION - valid DAX, wrong number.

    Deliberately skips `///` description lines: a naive whole-file replace edited the documentation
    instead of the DAX, which would have scored as a behavioural defect while changing nothing. A
    mutation that does not change behaviour flatters every gate downstream of it.
    """
    for tmdl in _tmdl_tables(model):
        lines = tmdl.read_text(encoding="utf-8").splitlines(keepends=True)
        in_measure = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("///"):
                continue
            if re.match(r"^\s*measure\s", line):
                in_measure = True
            elif stripped and not line.startswith((" ", "\t")):
                in_measure = False
            if in_measure and re.search(r"\bSUM\s*\(", line):
                lines[i] = re.sub(r"\bSUM\s*\(", "AVERAGE (", line, count=1)
                tmdl.write_text("".join(lines), encoding="utf-8")
                return True, f"{tmdl.name}:{i + 1}: SUM( -> AVERAGE( inside a measure expression"
    return False, "no SUM( found inside a measure expression"


def mut_m_trailing_comma(model: Path, _report: Path) -> tuple[bool, str]:
    """Add a trailing comma before a closing bracket in an M expression."""
    for tmdl in sorted((model / "definition" / "tables").glob("*.tmdl")):
        text = tmdl.read_text(encoding="utf-8")
        m = re.search(r"(?m)^(\s*)in\s*$", text)
        if not m or "source =" not in text.lower():
            continue
        before = text[: m.start()].rstrip()
        text = before + ",\n" + text[m.start() :]
        tmdl.write_text(text, encoding="utf-8")
        return True, f"{tmdl.name}: trailing comma inserted before `in`"
    return False, "no M partition with a bare `in` line found"


def mut_m_missing_in(model: Path, _report: Path) -> tuple[bool, str]:
    """Delete the `in` line of an M let-expression."""
    for tmdl in sorted((model / "definition" / "tables").glob("*.tmdl")):
        text = tmdl.read_text(encoding="utf-8")
        m = re.search(r"(?m)^\s*in\s*$\n", text)
        if not m:
            continue
        tmdl.write_text(text[: m.start()] + text[m.end() :], encoding="utf-8")
        return True, f"{tmdl.name}: `in` line deleted from the let-expression"
    return False, "no bare `in` line found"


def mut_measure_name_collision(model: Path, _report: Path) -> tuple[bool, str]:
    """Rename a measure to collide with a column name in the same table (a Tabular uniqueness break)."""
    for tmdl in _tmdl_tables(model):
        text = tmdl.read_text(encoding="utf-8")
        m = _first_measure(text)
        col = re.search(r"(?m)^\s*column\s+'?([^'\n=]+?)'?\s*$", text)
        if not m or not col:
            continue
        name = col.group(1).strip()
        text = text[: m.start(2)] + f"'{name}'" + text[m.end(2) :]
        tmdl.write_text(text, encoding="utf-8")
        return True, f"{tmdl.name}: measure renamed to '{name}', colliding with a column"
    return False, "no measure+column pair found"


def mut_dropped_visual(_model: Path, report: Path) -> tuple[bool, str]:
    """Delete one visual folder outright - a silently missing worksheet."""
    visuals = sorted((report / "definition" / "pages").glob("*/visuals/*"))
    visuals = [v for v in visuals if v.is_dir()]
    if len(visuals) < 2:
        return False, "fewer than 2 visuals; refusing to empty a page"
    victim = visuals[-1]
    shutil.rmtree(victim)
    return True, f"deleted visual {victim.parent.parent.name}/{victim.name}"


def mut_report_version_missing(_model: Path, report: Path) -> tuple[bool, str]:
    """Remove the schema-required reportVersionAtImport from report.json.

    This is the exact defect that older powerbi-report-author builds green-lit with errorCount:0 while
    Power BI Desktop refused to open the report - the reason 0.1.4 is a correctness FLOOR. Note the
    key is nested under themeCollection.<theme>, not at the document root (getting that wrong makes
    the mutation silently skip and flatters the recall number).
    """
    path = report / "definition" / "report.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    removed = []
    for theme_name, theme in (doc.get("themeCollection") or {}).items():
        if isinstance(theme, dict) and theme.pop("reportVersionAtImport", None) is not None:
            removed.append(theme_name)
    if not removed:
        return False, "report.json has no themeCollection.*.reportVersionAtImport to remove"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return True, f"report.json: reportVersionAtImport removed from {', '.join(removed)}"


def mut_broken_field_ref(_model: Path, report: Path) -> tuple[bool, str]:
    """Point a visual at a measure/column that does not exist in the model."""
    for visual in sorted((report / "definition" / "pages").glob("*/visuals/*/visual.json")):
        text = visual.read_text(encoding="utf-8")
        m = re.search(r'"Property"\s*:\s*"([^"]+)"', text)
        if not m:
            continue
        text = text[: m.start(1)] + "ZZZ_Field_That_Does_Not_Exist" + text[m.end(1) :]
        visual.write_text(text, encoding="utf-8")
        return True, f"{visual.parent.name}: a bound Property renamed to a non-existent field"
    return False, "no bound Property found in any visual"


MUTATIONS: dict[str, tuple[str, str, str, callable]] = {
    "fmt-scale-100x": ("model", "silent 100x display scale", "", mut_fmt_scale_100x),
    "duplicate-property": ("model", "same property twice (TMDL load failure)", "check_tmdl", mut_duplicate_property),
    "illegal-compact-filter": ("model", "'T'[Col]=[Measure] in CALCULATE", "", mut_illegal_compact_filter),
    "flat-lined-measure": ("model", "trend measure replaced by a constant", "", mut_flat_lined_measure),
    "wrong-aggregation": ("model", "SUM silently swapped to AVERAGE", "", mut_wrong_aggregation),
    "m-trailing-comma": ("model", "trailing comma before a closer in M", "check_m_syntax", mut_m_trailing_comma),
    "m-missing-in": ("model", "let-expression with no `in`", "check_m_syntax", mut_m_missing_in),
    "measure-name-collision": ("model", "measure named like a column", "check_tmdl", mut_measure_name_collision),
    "dropped-visual": ("report", "a worksheet silently missing", "", mut_dropped_visual),
    "report-version-missing": (
        "report",
        "report.json missing reportVersionAtImport",
        "validate",
        mut_report_version_missing,
    ),
    "broken-field-ref": ("report", "visual bound to a non-existent field", "validate", mut_broken_field_ref),
}


# ------------------------------------------------------------------------------------------- gates


def gate_check_m(model: Path) -> tuple[bool, str]:
    """Run scripts/check_m_syntax.py. Returns (found_a_problem, first line of output)."""
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_m_syntax.py"), str(model)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode != 0, out[-1][:160] if out else ""


def gate_check_tmdl(model: Path) -> tuple[bool, str]:
    """Run scripts/check_tmdl.py. Returns (found_a_problem, last line of output)."""
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_tmdl.py"), str(model)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 1, out[0][:160] if out else ""


def gate_validate(report: Path) -> tuple[bool, str]:
    """Run `powerbi-report-author validate`. Returns (found_a_problem, summary)."""
    proc = subprocess.run(
        ["powerbi-report-author", "validate", str(report)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
        check=False,
    )
    blob = proc.stdout + proc.stderr
    errors = re.search(r'"errorCount"\s*:\s*(\d+)', blob)
    count = int(errors.group(1)) if errors else (0 if proc.returncode == 0 else -1)
    unreachable = "PBIR_SCHEMA_UNREACHABLE" in blob
    summary = f"errorCount={count}{' (SCHEMA UNREACHABLE - schema checks did NOT run)' if unreachable else ''}"
    return count > 0 or proc.returncode != 0, summary


# -------------------------------------------------------------------------------------------- main


def apply_one(name: str, src: Path, out_root: Path, run_gates: bool) -> Mutation:
    """Copy the PBIP, apply one mutation to the copy, and (optionally) run the structural gates."""
    kind, detail, expected, fn = MUTATIONS[name]
    dest = out_root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".pbi", "cache.abf", "*.abf"))
    mutation = Mutation(name=name, detail=detail, kind=kind, expected_catcher=expected)
    ok, note = fn(_model_dir(dest), _report_dir(dest))
    mutation.applied, mutation.note = ok, note
    if ok and run_gates:
        found, msg = gate_check_m(_model_dir(dest))
        if found:
            mutation.caught_by.append(f"check_m_syntax ({msg})")
        found, msg = gate_check_tmdl(_model_dir(dest))
        if found:
            mutation.caught_by.append(f"check_tmdl ({msg})")
        found, msg = gate_validate(_report_dir(dest))
        if found:
            mutation.caught_by.append(f"validate ({msg})")
    return mutation


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbip", type=Path, help="folder holding <Name>.SemanticModel and <Name>.Report")
    ap.add_argument("--out", type=Path, help="where to write the mutant copies")
    ap.add_argument("--only", action="append", help="apply only this mutation (repeatable)")
    ap.add_argument("--run-gates", action="store_true", help="run check_m_syntax + validate on each mutant")
    ap.add_argument("--list", action="store_true", help="list the defect classes and exit")
    args = ap.parse_args(argv)

    if args.list:
        for name, (kind, detail, expected, _) in MUTATIONS.items():
            log.info("%-24s %-7s %-38s expected gate: %s", name, kind, detail, expected or "(none - fidelity only)")
        return 0
    if not args.pbip or not args.out:
        ap.error("--pbip and --out are required unless --list")

    names = args.only or list(MUTATIONS)
    args.out.mkdir(parents=True, exist_ok=True)
    results = [apply_one(n, args.pbip, args.out, args.run_gates) for n in names]

    log.info("")
    log.info("%-24s %-8s %-9s %s", "DEFECT", "APPLIED", "CAUGHT", "BY / NOTE")
    log.info("-" * 108)
    caught = 0
    for r in results:
        verdict = "YES" if r.caught_by else ("no" if r.applied else "-")
        caught += bool(r.caught_by)
        log.info(
            "%-24s %-8s %-9s %s", r.name, "yes" if r.applied else "SKIPPED", verdict, "; ".join(r.caught_by) or r.note
        )
    applied = sum(1 for r in results if r.applied)
    log.info("-" * 108)
    log.info(
        "STRUCTURAL GATE RECALL: %d/%d seeded defects caught (%.0f%%). The rest are invisible to "
        "check_m_syntax + check_tmdl + validate and can only be caught by a Desktop open-test with "
        "data or by pbi-migration-validator's figure-by-figure fidelity pass.",
        caught,
        applied,
        (100.0 * caught / applied) if applied else 0.0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
