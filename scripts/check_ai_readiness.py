"""
purpose: Report AI/Copilot-readiness of a migrated semantic model: the share of tables, columns, and
         measures that carry a TMDL description, and flag categorical/dimension columns whose
         description doesn't appear to enumerate its domain (enum) values. A well-described model with
         enumerated categoricals is what lets Power BI Copilot resolve natural-language questions -
         see https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-evaluate-data (DAX
         Copilot reads the first 200 chars of each description).
usage:   python scripts/check_ai_readiness.py migrations/<slug>        # one migration
         python scripts/check_ai_readiness.py --all                    # every migration, summary
         python scripts/check_ai_readiness.py migrations/<slug> --strict # exit 1 if <100% coverage
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECT_RE = re.compile(r"^(?P<indent>\t*)(?P<kind>table|column|measure)\s+(?P<name>'[^']+'|[^\s=]+)")
# A hint that a description enumerates its domain (e.g. "One of: A, B, C" or "values: X, Y").
DOMAIN_HINT_RE = re.compile(r"(one of|values?:|categories|domain|:contains|e\.g\.)", re.IGNORECASE)


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


def _print_model(slug: str, model_dir: Path, result: dict) -> bool:
    counts = result["counts"]
    total = sum(c["total"] for c in counts.values())
    described = sum(c["described"] for c in counts.values())
    pct = 100.0 * described / total if total else 100.0
    print(f"\n=== {slug} / {model_dir.name} ===")
    for kind in ("table", "column", "measure"):
        c = counts[kind]
        p = 100.0 * c["described"] / c["total"] if c["total"] else 100.0
        print(f"  {kind + 's':<9} {c['described']:>3}/{c['total']:<3} described ({p:5.1f}%)")
    print(f"  {'overall':<9} {described:>3}/{total:<3} described ({pct:5.1f}%)")
    gaps = result["categorical_gaps"]
    if gaps:
        print(f"  categorical columns missing enumerated domain values ({len(gaps)}):")
        for g in gaps[:25]:
            print(f"    - {g}")
    return pct >= 100.0 and not gaps


def main() -> None:
    """Audit one migration or all migrations for AI-readiness."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("migration", nargs="?", help="path to migrations/<slug> (omit with --all)")
    parser.add_argument("--all", action="store_true", help="audit every migration")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any model is below 100%% coverage")
    args = parser.parse_args()

    if args.all:
        targets = sorted(p for p in (REPO_ROOT / "migrations").glob("*") if (p / "fabric").is_dir())
    elif args.migration:
        targets = [REPO_ROOT / args.migration] if not Path(args.migration).is_absolute() else [Path(args.migration)]
    else:
        parser.error("provide a migrations/<slug> path or --all")
        return

    all_ok = True
    for target in targets:
        model_dirs = _model_dirs(target)
        if not model_dirs:
            print(f"(no semantic model under {target.name}/fabric/)")
            continue
        for model_dir in model_dirs:
            ok = _print_model(target.name, model_dir, audit_model(model_dir))
            all_ok = all_ok and ok

    if args.strict and not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
