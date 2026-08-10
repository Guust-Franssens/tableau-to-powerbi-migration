"""
purpose: Re-validate migration-spec.json after agents append limitations_encountered entries.
usage:   python scripts/validate_spec.py migrations/workbooks/<slug>/migration-spec.json
         python scripts/validate_spec.py --all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from parse_tableau import MigrationSpecValidationUnavailable
from parse_tableau import collect_spec_validation_errors as collect_in_memory_errors

logging.basicConfig(level=logging.INFO, format="%(message)s")
LOGGER = logging.getLogger("validate_spec")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "migration-spec.schema.json"
SPEC_TREES = ("examples", "migrations/workbooks", "migrations/datasources")


def collect_spec_validation_errors(spec_path: Path) -> list[str]:
    """Return actionable validation errors for one migration-spec.json file."""
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"<root>: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]
    try:
        return collect_in_memory_errors(spec, SCHEMA_PATH, require_jsonschema=True)
    except MigrationSpecValidationUnavailable as exc:
        return [f"<root>: validation unavailable: {exc}"]


def discover_specs() -> list[Path]:
    """Find committed-style migration specs in every migration tree."""
    specs: list[Path] = []
    for tree in SPEC_TREES:
        root = REPO_ROOT / tree
        if root.exists():
            specs.extend(root.glob("*/migration-spec.json"))
    return sorted(specs)


def _targets(args: argparse.Namespace) -> list[Path]:
    targets = [path.resolve() for path in args.spec]
    if args.all:
        targets.extend(discover_specs())
    return targets


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", nargs="*", type=Path, help="migration-spec.json path(s) to validate")
    parser.add_argument("--all", action="store_true", help="validate every spec under examples/ and migrations/")
    args = parser.parse_args(argv)

    targets = _targets(args)
    if not targets:
        parser.error("give one or more specs, or pass --all")

    invalid_count = 0
    for spec_path in targets:
        if not spec_path.exists():
            LOGGER.error("MISSING  %s", spec_path)
            invalid_count += 1
            continue
        errors = collect_spec_validation_errors(spec_path)
        if not errors:
            LOGGER.info("ok       %s", spec_path)
            continue
        invalid_count += 1
        LOGGER.error("INVALID  %s", spec_path)
        for error in errors[:10]:
            LOGGER.error("         %s", error)
        if len(errors) > 10:
            LOGGER.error("         ... and %d more", len(errors) - 10)

    if invalid_count:
        LOGGER.error(
            "%d of %d spec(s) violate docs/migration-spec.schema.json. Fix the offending append; "
            "do not re-parse, because that discards downstream limitations.",
            invalid_count,
            len(targets),
        )
        return 1
    LOGGER.info("%d spec(s) valid.", len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
