"""
purpose: Report AI/Copilot-readiness of a migrated semantic model: the share of tables, columns, and
         measures that carry a TMDL description, and flag categorical/dimension columns whose
         description doesn't appear to enumerate its domain (enum) values. A well-described model with
         enumerated categoricals is what lets Power BI Copilot resolve natural-language questions -
         see https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-evaluate-data (DAX
         Copilot reads the first 200 chars of each description).
usage:   python .github/skills/powerbi-ai-readiness/scripts/check_ai_readiness.py <tree>/<slug>
             --all       # every migration, summary
             --strict    # exit 1 if <100% coverage
         (ships inside the `powerbi-ai-readiness` skill; run it by its path from wherever the folder
          was copied. `scripts/check_ai_readiness.py` in this repo is a forwarding shim.)
"""

import argparse
import re
import sys
from pathlib import Path

# The migration trees this scans - see the same constant in set_ai_instructions.py. Kept local rather
# than shared: these are independent one-shot CLIs (see pyproject's duplicate-code rationale).
MIGRATION_TREES = ("examples", "migrations/workbooks", "migrations/datasources")


def host_root(start: Path | None = None) -> Path:
    """The repo that OWNS the migrations, found by walking up for a known tree.

    This file ships inside a skill folder, so its depth below the repo root is NOT fixed - a
    hard-coded `parents[N]` resolves to the skill folder, where every glob matches nothing and
    `--all` reports a clean, empty, entirely fictional pass.
    """
    for parent in (start or Path(__file__).resolve()).parents:
        if any((parent / tree).is_dir() for tree in MIGRATION_TREES):
            return parent
    return Path.cwd()


REPO_ROOT = host_root()
OBJECT_RE = re.compile(r"^(?P<indent>\t*)(?P<kind>table|column|measure)\s+(?P<name>'[^']+'|[^\s=]+)")
# A hint that a description enumerates its domain (e.g. "One of: A, B, C" or "values: X, Y").
DOMAIN_HINT_RE = re.compile(r"(one of|values?:|categories|domain|:contains|e\.g\.)", re.IGNORECASE)

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_SKIPPED = 3


def _iter_tmdl(model_dir: Path):
    yield from model_dir.glob("definition/*.tmdl")
    yield from model_dir.glob("definition/tables/*.tmdl")


def audit_model(model_dir: Path) -> dict:
    """Return per-kind description coverage + a list of categorical columns lacking domain values."""
    counts = {k: {"total": 0, "described": 0} for k in ("table", "column", "measure")}
    categorical_gaps: list[str] = []
    for tmdl in _iter_tmdl(model_dir):
        lines = tmdl.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = OBJECT_RE.match(line)
            if not m:
                continue
            kind = m.group("kind")
            # A column with an "=" is a calculated column; still a column.
            prev = lines[i - 1].strip() if i > 0 else ""
            described = prev.startswith("///")
            counts[kind]["total"] += 1
            if described:
                counts[kind]["described"] += 1
            if kind == "column":
                block = "\n".join(lines[i : i + 8])
                is_categorical = "dataType: string" in block and "summarizeBy: none" in block and "isKey" not in block
                if is_categorical:
                    name = m.group("name").strip("'")
                    if not described or not DOMAIN_HINT_RE.search(prev):
                        categorical_gaps.append(f"{tmdl.stem}[{name}]")
    return {"counts": counts, "categorical_gaps": categorical_gaps}


def _model_dirs(target: Path):
    return sorted(target.glob("fabric/*.SemanticModel"))


def _print_model(slug: str, model_dir: Path, result: dict) -> str:
    counts = result["counts"]
    total = sum(c["total"] for c in counts.values())
    described = sum(c["described"] for c in counts.values())
    pct = 100.0 * described / total if total else 0.0
    print(f"\n=== {slug} / {model_dir.name} ===")
    for kind in ("table", "column", "measure"):
        c = counts[kind]
        p = 100.0 * c["described"] / c["total"] if c["total"] else 0.0
        print(f"  {kind + 's':<9} {c['described']:>3}/{c['total']:<3} described ({p:5.1f}%)")
    print(f"  {'overall':<9} {described:>3}/{total:<3} described ({pct:5.1f}%)")
    if total == 0:
        print("  SKIPPED - nothing measured (no tables, columns, or measures found)")
        return "SKIPPED"
    gaps = result["categorical_gaps"]
    if gaps:
        print(f"  categorical columns missing enumerated domain values ({len(gaps)}):")
        for g in gaps[:25]:
            print(f"    - {g}")
    return "OK" if pct >= 100.0 and not gaps else "FINDINGS"


def main() -> None:
    """Audit one migration or all migrations for AI-readiness."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("migration", nargs="?", help="path to <tree>/<slug> (omit with --all)")
    parser.add_argument("--all", action="store_true", help="audit every migration")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any model is below 100%% coverage")
    args = parser.parse_args()

    if args.all:
        # All three migration trees: examples/ (this repo's worked examples) plus the user's own
        # migrations/workbooks/ and migrations/datasources/.
        targets = sorted(p for tree in MIGRATION_TREES for p in (REPO_ROOT / tree).glob("*") if (p / "fabric").is_dir())
    elif args.migration:
        targets = [REPO_ROOT / args.migration] if not Path(args.migration).is_absolute() else [Path(args.migration)]
    else:
        parser.error("provide a <tree>/<slug> path or --all")
        return

    statuses = []
    for target in targets:
        model_dirs = _model_dirs(target)
        if not model_dirs:
            print(f"(no semantic model under {target.name}/fabric/) SKIPPED - nothing measured")
            statuses.append("SKIPPED")
            continue
        for model_dir in model_dirs:
            statuses.append(_print_model(target.name, model_dir, audit_model(model_dir)))

    if any(status == "FINDINGS" for status in statuses):
        if args.strict:
            sys.exit(EXIT_FINDINGS)
    elif not statuses or any(status == "SKIPPED" for status in statuses):
        sys.exit(EXIT_SKIPPED)


if __name__ == "__main__":
    main()
