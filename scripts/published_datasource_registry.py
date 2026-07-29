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

# Two peer trees, separated by WHAT THEY PRODUCE - so you can tell what a folder is without opening it:
#
#   migrations/<slug>/    a WORKBOOK migration  (.twbx -> fabric/<Name>.Report)
#   datasources/<slug>/   a DATA SOURCE migration (.tds/.tdsx -> fabric/<Name>.SemanticModel)
#
# A Tableau published data source is consumed by many workbooks, so its model must not live inside any
# one consumer's migration folder - it would look owned by whichever workbook happened to be migrated
# first, and be deleted/rebuilt with it while other reports still bind to it. Giving data sources their
# own tree keeps the model layer independent of every report that uses it, and mirrors the Fabric
# convention of separating semantic models from reports.
#
# A data-source migration otherwise has the SAME shape as a workbook migration (source/ +
# migration-spec.json + fabric/) - it simply has no .Report. The one extra thing it declares is WHICH
# published data source it satisfies: a standalone .tds carries an empty `<repository-location />`, so
# the dedup key cannot be recovered from the file itself and is recorded in this marker.
DATASOURCES_DIR = REPO_ROOT / "datasources"
MARKER_NAME = "published-datasource.json"


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


def find_shared_models(datasources_dir: Path = DATASOURCES_DIR) -> dict[str, dict[str, Any]]:
    """Map dedup key -> the data-source migration that owns the shared semantic model for it.

    Authoritative lookup: a `datasources/<slug>/` declaring `published-datasource.json` and holding a
    built `.SemanticModel` is the single model every consuming report should bind to.
    """
    found: dict[str, dict[str, Any]] = {}
    if not datasources_dir.is_dir():
        return found
    for marker in sorted(datasources_dir.glob(f"*/{MARKER_NAME}")):
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("  !  skipping unreadable marker %s (%s)", marker, exc)
            continue
        key = meta.get("key")
        models = _semantic_models(marker.parent)
        if key and models:
            found[key] = {
                "key": key,
                "name": meta.get("name"),
                "slug": marker.parent.name,
                "model": _rel(models[0]),
            }
    return found


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
        if (slug_dir / MARKER_NAME).exists():
            continue  # a data-source migration OWNS the model; it is not a consumer of it
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


def _by_path_from_fabric(model_rel: str) -> str:
    """Relative byPath to reach a data-source migration's model from migrations/<slug>/fabric/.

    Three hops leave `migrations/<slug>/fabric/` at the repo root, from which `datasources/...` is
    directly addressable. Falls back to the last three components when the path could not be made
    repo-relative (e.g. a test pointing the trees outside the repo).
    """
    path = Path(model_rel)
    tail = path.parts[-4:] if path.is_absolute() else path.parts
    return "../../../" + "/".join(tail)


def _bind_instructions(model_rel: str) -> str:
    """The concrete HOW TO BIND text, pointing at an actual model path."""
    return (
        "\n  ACTION: do NOT rebuild this model. Bind this migration's report to it, and only add\n"
        "  measures the new workbook genuinely needs. A rebuilt copy will drift from the shared one.\n"
        "\n  HOW TO BIND (no copying needed in either target - verified 2026-07):\n"
        "    LOCAL  <Your>.Report/definition.pbir -> a RELATIVE byPath that may point OUTSIDE your\n"
        "           own migration folder:\n"
        f'             {{"byPath": {{"path": "{_by_path_from_fabric(model_rel)}"}}}}\n'
        "           Power BI Desktop resolves cross-folder byPath (confirmed by opening such a .pbip:\n"
        "           the shared model's tables/columns loaded). Never copy the .SemanticModel folder.\n"
        "    CLOUD  publish the model ONCE, then each report uses\n"
        '             {"byConnection": {"connectionString": "semanticmodelid=<model guid>"}}'
    )


def cmd_scan(migrations_dir: Path, datasources_dir: Path) -> int:
    """Print every published data source found across all migrations and who consumes it."""
    index = build_index(migrations_dir)
    shared = find_shared_models(datasources_dir)
    if not index and not shared:
        log.info("No published (sqlproxy) data sources found in any migration-spec.json.")
        log.info("All parsed workbooks use embedded data sources - no shared-model de-duplication needed.")
        return 0
    log.info("Published data sources (consumed by %d workbook migration key(s)):", len(index))
    for key, entries in sorted(index.items()):
        first = entries[0]
        log.info("\n  %s   (%s on site %s)", key, first["name"] or "?", first["site"] or "?")
        registered = shared.get(key)
        log.info(
            "      model: %s",
            f"{registered['model']}  (datasources/{registered['slug']})"
            if registered
            else "<NOT built - no datasources/<slug>/ owns this key yet>",
        )
        for entry in entries:
            log.info("      consumed by: %s", entry["slug"])
        if len(entries) > 1:
            log.info("      => SHARED by %d workbooks: they must bind to ONE semantic model.", len(entries))
    orphan_shared = {k: v for k, v in shared.items() if k not in index}
    if orphan_shared:
        log.info("\nData-source migrations with no consuming workbook parsed yet:")
        for key, meta in sorted(orphan_shared.items()):
            log.info("  %s -> %s", key, meta["model"])
    return 0


def _report_key(key: str, migrations_dir: Path, datasources_dir: Path, exclude_slug: str | None = None) -> int:
    """Look up one dedup key; tell the caller whether to reuse an existing model or build it."""
    registered = find_shared_models(datasources_dir).get(key)
    if registered:
        log.info("ALREADY MIGRATED: published data source '%s'", key)
        log.info("  data-source migration : datasources/%s", registered["slug"])
        log.info("  semantic model        : %s", registered["model"])
        log.info("%s", _bind_instructions(registered["model"]))
        return 0

    entries = [e for e in build_index(migrations_dir).get(key, []) if e["slug"] != exclude_slug]
    stray = _reuse_candidate(entries)
    if stray:
        log.warning("MISPLACED: '%s' was built inside a WORKBOOK migration (%s)", key, stray["slug"])
        for existing in stray["semantic_models"]:
            log.warning("  %s", existing)
        log.warning(
            "\n  A shared data source's model must not live inside one consumer's folder - it looks\n"
            "  owned by that workbook and dies with it. Move it to its own data-source migration:\n"
            "      datasources/<slug>/fabric/<Name>.SemanticModel\n"
            "  then declare the key:\n"
            "      python scripts/published_datasource_registry.py --register %s --name '%s' --slug <slug>",
            key,
            stray["name"] or key,
        )
        return 1

    consumers = ", ".join(e["slug"] for e in entries) if entries else "<none parsed yet>"
    log.info("NOT YET MIGRATED: published data source '%s'", key)
    log.info("  consumed by : %s", consumers)
    log.info("  ACTION: migrate the DATA SOURCE once, in its own tree, before the reports that use it:")
    log.info("      1. export the .tds/.tdsx from Tableau (or scripts/tableau_lineage.py --download)")
    log.info("      2. datasources/<slug>/source/<file>.tdsx")
    log.info("      3. python scripts/parse_tableau.py <file>.tdsx -o datasources/<slug>/migration-spec.json")
    log.info("      4. build fabric/<Name>.SemanticModel from it")
    log.info(
        "      5. python scripts/published_datasource_registry.py --register %s --name '%s' --slug <slug>",
        key,
        (entries[0]["name"] if entries else None) or key.rsplit("/", 1)[-1],
    )
    return 1


def cmd_register(key: str, name: str, slug: str, datasources_dir: Path) -> int:
    """Declare that `datasources/<slug>/` owns the shared semantic model for `key`."""
    slug_dir = datasources_dir / slug
    if not slug_dir.is_dir():
        log.error("No data-source migration at %s", _rel(slug_dir))
        return 1
    if not _semantic_models(slug_dir):
        log.error("No .SemanticModel under %s - build the model before registering it.", _rel(slug_dir / "fabric"))
        return 1
    marker = slug_dir / MARKER_NAME
    marker.write_text(json.dumps({"key": key, "name": name, "slug": slug}, indent=2) + "\n", encoding="utf-8")
    log.info("Registered data-source migration:")
    log.info("  key    : %s", key)
    log.info("  model  : %s", _rel(_semantic_models(slug_dir)[0]))
    log.info("  marker : %s", _rel(marker))
    log.info("\nWorkbook migrations will now discover it via --key / --spec and bind instead of rebuilding.")
    return 0


def cmd_spec(spec_path: Path, migrations_dir: Path, datasources_dir: Path) -> int:
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
        worst = max(worst, _report_key(key, migrations_dir, datasources_dir, exclude_slug=spec_path.parent.name))
    return worst


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", action="store_true", help="List every published data source and its consumers")
    group.add_argument("--key", help="Look up one dedup key, e.g. finance/salesmaster")
    group.add_argument("--spec", type=Path, help="Resolve every published data source in a migration-spec.json")
    group.add_argument("--register", metavar="KEY", help="Declare that a datasources/<slug>/ owns the model for KEY")
    parser.add_argument("--name", help="Published data source name (with --register)")
    parser.add_argument("--slug", default="", help="datasources/<slug> that holds the built model (with --register)")
    parser.add_argument("--migrations-dir", type=Path, default=MIGRATIONS_DIR, help="Override the migrations folder")
    parser.add_argument("--datasources-dir", type=Path, default=DATASOURCES_DIR, help="Override the datasources folder")
    args = parser.parse_args(argv)

    if args.scan:
        return cmd_scan(args.migrations_dir, args.datasources_dir)
    if args.register:
        name = args.name or args.register.rsplit("/", 1)[-1]
        if not args.slug:
            parser.error("--register requires --slug (the datasources/<slug> holding the built model)")
        return cmd_register(args.register, name, args.slug, args.datasources_dir)
    if args.key:
        return _report_key(args.key, args.migrations_dir, args.datasources_dir)
    return cmd_spec(args.spec.resolve(), args.migrations_dir, args.datasources_dir)


if __name__ == "__main__":
    sys.exit(main())
