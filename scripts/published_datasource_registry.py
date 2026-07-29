"""
purpose: answer "has this Tableau PUBLISHED data source already been migrated?" before a semantic
         model is rebuilt for it.

         A Tableau Published Data Source (connection class 'sqlproxy') is typically shared by SEVERAL
         downstream workbooks. The correct Power BI shape mirrors that: migrate the datasource ONCE
         into a single semantic model, then bind every downstream report to that same model -- not one
         near-identical model per workbook. The parser stamps a stable dedup key
         (`data_sources[].published_datasource.key`, e.g. "finance/salesmaster") on every affected
         spec; this script indexes those keys across all migrations so the orchestrator can reuse an
         existing model instead of rebuilding it.

         It derives everything from the committed migration-spec.json files, so there is no separate
         registry file to keep in sync (and nothing to go stale).

usage:   python scripts/published_datasource_registry.py --scan
         python scripts/published_datasource_registry.py --key finance/salesmaster
         python scripts/published_datasource_registry.py --spec migrations/<slug>/migration-spec.json

Exit codes for --key / --spec: 0 = already migrated somewhere (reuse it), 1 = not yet migrated
(build it once, in this migration), 2 = the spec has no published data source at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("published_datasource_registry")

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _semantic_models(slug_dir: Path) -> list[Path]:
    """Return the semantic model folders built for a migration (empty if none exist yet)."""
    fabric = slug_dir / "fabric"
    return sorted(p for p in fabric.glob("*.SemanticModel") if p.is_dir()) if fabric.is_dir() else []


def _rel(path: Path) -> str:
    """Repo-relative path when possible, else absolute (--migrations-dir may point outside the repo)."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def build_index(migrations_dir: Path = MIGRATIONS_DIR) -> dict[str, list[dict[str, Any]]]:
    """Map every published-datasource dedup key -> the migrations that consume it.

    Each entry records whether that migration actually produced a semantic model, which is what makes
    it a reuse candidate rather than merely another consumer.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    if not migrations_dir.is_dir():
        return index
    for spec_path in sorted(migrations_dir.glob("*/migration-spec.json")):
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("  !  skipping unreadable spec %s (%s)", spec_path, exc)
            continue
        slug_dir = spec_path.parent
        for ds in spec.get("data_sources", []):
            published = ds.get("published_datasource") or {}
            key = published.get("key")
            if not key:
                continue
            index.setdefault(key, []).append(
                {
                    "slug": slug_dir.name,
                    "name": published.get("id"),
                    "site": published.get("site"),
                    "semantic_models": [_rel(p) for p in _semantic_models(slug_dir)],
                }
            )
    return index


def _reuse_candidate(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the first consumer that actually built a semantic model - that model is the one to reuse."""
    return next((e for e in entries if e["semantic_models"]), None)


def cmd_scan(migrations_dir: Path) -> int:
    """Print every published data source found across all migrations and who consumes it."""
    index = build_index(migrations_dir)
    if not index:
        log.info("No published (sqlproxy) data sources found in any migration-spec.json.")
        log.info("All parsed workbooks use embedded data sources - no shared-model de-duplication needed.")
        return 0
    log.info("Published data sources across %d migration(s):", len(index))
    for key, entries in sorted(index.items()):
        first = entries[0]
        log.info("\n  %s   (%s on site %s)", key, first["name"] or "?", first["site"] or "?")
        for entry in entries:
            models = ", ".join(entry["semantic_models"]) or "<no semantic model built>"
            log.info("      - %-40s %s", entry["slug"], models)
        if len(entries) > 1:
            log.info("      => SHARED by %d workbooks: they must bind to ONE semantic model.", len(entries))
    return 0


def _report_key(key: str, migrations_dir: Path, exclude_slug: str | None = None) -> int:
    """Look up one dedup key; tell the caller whether to reuse an existing model or build it."""
    entries = [e for e in build_index(migrations_dir).get(key, []) if e["slug"] != exclude_slug]
    candidate = _reuse_candidate(entries)
    if candidate:
        log.info("ALREADY MIGRATED: published data source '%s'", key)
        log.info("  built in migration : %s", candidate["slug"])
        for model in candidate["semantic_models"]:
            log.info("  semantic model     : %s", model)
        log.info(
            "\n  ACTION: do NOT rebuild this model. Bind this migration's report to the existing\n"
            "  semantic model above, and only add measures the new workbook genuinely needs.\n"
            "  Rebuilding creates a duplicate model that will drift from the shared one.\n"
            "\n  HOW TO BIND (no copying needed in either target - verified 2026-07):\n"
            "    LOCAL  .Report/definition.pbir -> datasetReference.byPath.path with a RELATIVE path\n"
            "           that may point OUTSIDE this migration folder, e.g.\n"
            '             {"byPath": {"path": "../../<other-slug>/fabric/<Name>.SemanticModel"}}\n'
            "           Power BI Desktop resolves cross-folder byPath (confirmed by opening such a\n"
            "           .pbip: the shared model's tables/columns loaded), so ONE model on disk can\n"
            "           serve many reports. Do not copy the .SemanticModel folder per migration.\n"
            "    CLOUD  publish the model ONCE, then each report uses\n"
            '             {"byConnection": {"connectionString": "semanticmodelid=<model guid>"}}\n'
            "           which references the published model directly."
        )
        return 0
    if entries:
        log.info("KNOWN but NOT YET BUILT: '%s' is referenced by %s", key, ", ".join(e["slug"] for e in entries))
        log.info("  ACTION: build the semantic model ONCE here; later workbooks will reuse it.")
        return 1
    log.info("NEW: published data source '%s' has not been migrated yet.", key)
    log.info("  ACTION: build its semantic model ONCE in this migration so later workbooks can reuse it.")
    return 1


def cmd_spec(spec_path: Path, migrations_dir: Path) -> int:
    """Resolve every published data source declared by one migration-spec.json."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    keys = [
        (ds.get("published_datasource") or {}).get("key")
        for ds in spec.get("data_sources", [])
        if (ds.get("published_datasource") or {}).get("key")
    ]
    if not keys:
        log.info("No published (sqlproxy) data source in %s - nothing to de-duplicate.", spec_path)
        return 2
    worst = 0
    for key in keys:
        log.info("")
        worst = max(worst, _report_key(key, migrations_dir, exclude_slug=spec_path.parent.name))
    return worst


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", action="store_true", help="List every published data source and its consumers")
    group.add_argument("--key", help="Look up one dedup key, e.g. finance/salesmaster")
    group.add_argument("--spec", type=Path, help="Resolve every published data source in a migration-spec.json")
    parser.add_argument("--migrations-dir", type=Path, default=MIGRATIONS_DIR, help="Override the migrations folder")
    args = parser.parse_args(argv)

    if args.scan:
        return cmd_scan(args.migrations_dir)
    if args.key:
        return _report_key(args.key, args.migrations_dir)
    return cmd_spec(args.spec.resolve(), args.migrations_dir)


if __name__ == "__main__":
    sys.exit(main())
