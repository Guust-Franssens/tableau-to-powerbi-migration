"""
purpose: warn when migration-bundle engine receipts differ from the installed canonical engine.
usage:   python scripts/check_engine_receipts.py [--root <directory>]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine_source import engine_root, engine_version, version_tuple
from migration_bundle import ENGINE_RECEIPT

REPO_ROOT = Path(__file__).resolve().parent.parent


def check_receipts(search_root: Path) -> list[str]:
    """Return receipt-version drift warnings beneath ``search_root``."""
    installed_engine = engine_root()
    installed_version = engine_version(installed_engine)
    if not installed_version:
        raise RuntimeError(f"canonical engine at {installed_engine} has no VERSION")

    warnings: list[str] = []
    for receipt_path in sorted(search_root.rglob(ENGINE_RECEIPT)):
        bundle = receipt_path.parent
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            warnings.append(f"{bundle}: unreadable {ENGINE_RECEIPT}: {error}")
            continue
        engine = receipt.get("engine") if isinstance(receipt, dict) else None
        receipt_version = engine.get("version") if isinstance(engine, dict) else None
        if receipt_version != installed_version:
            shown_version = receipt_version if isinstance(receipt_version, str) and receipt_version else "missing"
            receipt_order = version_tuple(receipt_version if isinstance(receipt_version, str) else None)
            installed_order = version_tuple(installed_version)
            if receipt_order < installed_order:
                relation = "older than"
            elif receipt_order > installed_order:
                relation = "newer than"
            else:
                relation = "different from"
            warnings.append(
                f"{bundle}: receipt engine.version {shown_version} is {relation} "
                f"installed canonical engine {installed_version}"
            )
    return warnings


def main(argv: list[str] | None = None) -> int:
    """Print bundle receipt drift warnings; they are advisory for preflight callers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="directory tree containing migration bundles")
    args = parser.parse_args(argv)

    try:
        warnings = check_receipts(args.root)
    except RuntimeError as error:
        print(f"WARN: cannot compare engine receipts: {error}")
        return 2

    if not warnings:
        print(f"OK: no engine receipt drift under {args.root}")
        return 0
    for warning in warnings:
        print(f"WARN: {warning}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
