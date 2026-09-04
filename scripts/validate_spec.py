"""
purpose: Re-validate migration-spec.json after agents append limitations_encountered entries.
usage:   python scripts/validate_spec.py migrations/workbooks/<slug>/migration-spec.json
         python scripts/validate_spec.py --all
         python scripts/validate_spec.py --all --repair    # ALSO rewrite the file, removing dupes

**This command does not write unless you ask it to.** Repairing exact `limitations_encountered`
duplicates in place used to be the DEFAULT, so `validate_spec.py <spec>` silently rewrote the file it
was asked to inspect and reported the rewrite as `deduped ... removed`. Measured on the 2026-09-03
cold run: an agent ran the documented validate command and its spec changed underneath it. A
validator that writes is a surprise, and a surprise in a tool an agent runs to CHECK something is the
worst place to put one -- so the mutation is now opt-in behind `--repair`.

The reporting default is also what CI needs, and for a reason older than this change: GitHub Actions
never commits a rewrite back, so a mutating run leaves the duplicate in the proposed content and
still exits 0 -- a green gate for exactly the defect it exists to catch (#75). `--check` is kept as
an accepted alias of the default so the workflow and the existing agent docs keep working.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from parse_tableau import MigrationSpecValidationUnavailable
from parse_tableau import collect_spec_validation_errors as collect_in_memory_errors

logging.basicConfig(level=logging.INFO, format="%(message)s")
LOGGER = logging.getLogger("validate_spec")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "migration-spec.schema.json"
SPEC_TREES = ("examples", "migrations/workbooks", "migrations/datasources")
LIMITATION_IDENTITY_FIELDS = ("item", "issue", "severity", "stage")


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


def dedupe_limitations(spec: dict) -> int:
    """Remove exact valid limitation duplicates while preserving their first-seen order."""
    limitations = spec.get("limitations_encountered")
    if not isinstance(limitations, list):
        return 0

    seen: set[tuple[object, ...]] = set()
    deduplicated = []
    for limitation in limitations:
        if not isinstance(limitation, dict) or set(limitation) != set(LIMITATION_IDENTITY_FIELDS):
            deduplicated.append(limitation)
            continue
        identity = tuple(limitation[field] for field in LIMITATION_IDENTITY_FIELDS)
        if identity not in seen:
            seen.add(identity)
            deduplicated.append(limitation)

    removed = len(limitations) - len(deduplicated)
    if removed:
        limitations[:] = deduplicated
    return removed


def _spec_indent(source: str) -> int:
    """Preserve a spec's existing JSON indentation when writing a de-duplicated copy."""
    match = re.search(r'\n( +)"', source)
    return len(match.group(1)) if match else 2


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
    parser.add_argument(
        "--repair",
        action="store_true",
        help="REWRITE each spec in place to drop exact limitation duplicates (off by default)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="accepted alias of the default (report duplicates, write nothing); kept for CI and existing docs",
    )
    args = parser.parse_args(argv)

    targets = _targets(args)
    if not targets:
        parser.error("give one or more specs, or pass --all")

    invalid_count = 0
    duplicate_count = 0
    for spec_path in targets:
        if not spec_path.exists():
            LOGGER.error("MISSING  %s", spec_path)
            invalid_count += 1
            continue
        errors = collect_spec_validation_errors(spec_path)
        if errors:
            invalid_count += 1
            LOGGER.error("INVALID  %s", spec_path)
            for error in errors[:10]:
                LOGGER.error("         %s", error)
            if len(errors) > 10:
                LOGGER.error("         ... and %d more", len(errors) - 10)
            continue
        source = spec_path.read_text(encoding="utf-8")
        spec = json.loads(source)
        removed = dedupe_limitations(spec)
        if removed and not args.repair:
            duplicate_count += 1
            LOGGER.error(
                "DUPLICATE  %s (%d exact limitation duplicate(s); re-run with --repair to remove them)",
                spec_path,
                removed,
            )
        elif removed:
            spec_path.write_text(
                json.dumps(spec, indent=_spec_indent(source), ensure_ascii=False) + "\n", encoding="utf-8"
            )
            LOGGER.info("deduped  %s (%d exact limitation duplicate(s) removed)", spec_path, removed)
        else:
            LOGGER.info("ok       %s", spec_path)

    if invalid_count:
        LOGGER.error(
            "%d of %d spec(s) violate docs/migration-spec.schema.json. Fix the offending append; "
            "do not re-parse, because that discards downstream limitations.",
            invalid_count,
            len(targets),
        )
        return 1
    if duplicate_count:
        # Deliberately a SEPARATE message: an exact duplicate is schema-VALID (that is the whole
        # point of #75), so reporting it as a schema violation would send the reader to the wrong
        # file looking for a constraint that does not exist.
        LOGGER.error(
            "%d of %d spec(s) carry exact limitation duplicates. These are schema-valid, so the "
            "schema will never catch them; re-run with --repair to remove them, and commit the result.",
            duplicate_count,
            len(targets),
        )
        return 1
    LOGGER.info("%d spec(s) valid.", len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
