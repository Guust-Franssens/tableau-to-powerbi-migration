"""
purpose: validate a migration-spec.json against docs/migration-spec.schema.json AFTER the agents have
         edited it. The parser validates its own output on write, but `migration-spec.json` is
         explicitly "the contract every stage reads and writes" - pbi-semantic-builder,
         pbi-report-builder and the orchestrator all append `limitations_encountered` entries later,
         and until now NOTHING re-validated the file after those appends.

         That hole is not theoretical: a subagent appended entries with `severity: "critical"`, which
         is not in the schema's enum (`info|low|medium|high`), leaving the contract schema-invalid with
         no error anywhere. The 16 committed example specs happen to be valid, but by luck rather than
         by a gate.

usage:   python scripts/validate_spec.py migrations/workbooks/<slug>/migration-spec.json
         python scripts/validate_spec.py --all            # every spec under examples/ + migrations/

Exit 0 = valid. Exit 1 = invalid (the message names the offending path and the allowed values).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import jsonschema

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("validate_spec")

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "docs" / "migration-spec.schema.json"


def validate_spec(spec_path: Path) -> list[str]:
    """Return a list of human-readable schema errors for one spec ([] means valid)."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"not valid JSON: {exc}"]

    validator = jsonschema.Draft7Validator(schema)
    problems = []
    for err in sorted(validator.iter_errors(spec), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        problems.append(f"{where}: {err.message}")
    return problems


def discover() -> list[Path]:
    """Every migration-spec.json in the repo's three migration trees."""
    roots = [REPO / "examples", REPO / "migrations" / "workbooks", REPO / "migrations" / "datasources"]
    return sorted(p for root in roots if root.exists() for p in root.glob("*/migration-spec.json"))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="*", type=Path, help="migration-spec.json path(s)")
    ap.add_argument("--all", action="store_true", help="validate every spec in the repo's migration trees")
    args = ap.parse_args(argv)

    targets = list(args.spec)
    if args.all:
        targets.extend(discover())
    if not targets:
        ap.error("give a spec path, or --all")

    bad = 0
    for path in targets:
        if not path.exists():
            log.error("MISSING  %s", path)
            bad += 1
            continue
        problems = validate_spec(path)
        if problems:
            bad += 1
            log.error("INVALID  %s", path)
            for p in problems[:10]:
                log.error("           %s", p)
            if len(problems) > 10:
                log.error("           ... and %d more", len(problems) - 10)
        else:
            log.info("ok       %s", path)

    log.info("-" * 60)
    if bad:
        log.error(
            "%d of %d spec(s) violate docs/migration-spec.schema.json. The contract is what every "
            "later stage reads - fix the offending entries (do NOT re-parse, that would destroy the "
            "accumulated limitations).",
            bad,
            len(targets),
        )
        return 1
    log.info("%d spec(s) valid.", len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
