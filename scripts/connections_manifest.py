"""
purpose: tell the customer WHICH data sources they must connect after a migration, and what breaks
         until they do - as a deliverable, not as something discovered one failed refresh at a time.
usage:   python scripts/connections_manifest.py --bundle <dir> --out <dir>
         python scripts/connections_manifest.py --bundle <dir> --out <dir> --format json

Why this exists
---------------
Credentials do not travel with a migrated item. Every model backed by a live system needs its
connection re-established in the target workspace, and today the customer learns which ones by
importing everything, hitting a refresh failure, opening the model, reading the connection string,
and repeating. For an estate of dozens that is a day of round trips through the portal.

The information is already in our hands at parse time; it was simply never presented as a list. This
assembles it:

  * ``migration_bundle.load_bundle``  - the data sources, from either tier's contract
  * ``preflight_source_credentials.classify_source`` - live vs flat-file, fail-safe by design
  * the engine's handover slices - which workbook binds to which source (the blast radius)

Two refusals, each from a way this question is normally answered wrongly:

1. **It never emits a secret.** Host, database and account name are configuration; passwords, keys
   and tokens are not. The manifest is meant to be safe to email, and a test proves no
   credential-shaped value reaches it.
2. **It never calls an extract "connected".** A model built from a materialised ``.hyper`` has no
   upstream connection at all - it is a SNAPSHOT, frozen at extract time, that will never refresh.
   Customers consistently read those as broken connections and go looking for a credential that does
   not exist. They are listed separately, and labelled.

Ordering is by blast radius, not alphabetically: a published data source feeding twelve workbooks is
a different task from one feeding a single archived report, and the dependency graph that tells them
apart is already computed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from migration_bundle import load_bundle  # noqa: E402  # pylint: disable=wrong-import-position
from preflight_source_credentials import classify_source  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("connections_manifest")

# Config the customer's platform team needs in order to create a connection. Deliberately an
# ALLOW-list: anything not named here is dropped rather than passed through, so a future connection
# field carrying a token cannot reach the manifest by default. Fail-closed, like connection_target's
# class handling.
SAFE_CONNECTION_FIELDS = ("class", "server", "dbname", "warehouse", "schema", "service", "port")

# Anything whose KEY looks like a credential is dropped even if it appears in the list above. Belt
# and braces: the allow-list is the control, this is the alarm.
SECRET_KEY_PATTERN = re.compile(r"password|secret|token|pwd|credential|apikey|api_key|sas|key$", re.IGNORECASE)

SNAPSHOT = "snapshot (extract - no upstream connection)"
NEEDS_CREDENTIAL = "needs a credential"
REVIEW = "needs review"

# Tableau's own proxy for a PUBLISHED data source: the workbook talks to Tableau Server, and the real
# upstream (Snowflake, Databricks, ...) is defined server-side in the datasource itself. Telling a
# platform engineer to "connect to sqlproxy" is meaningless, so say what it actually is and where the
# answer lives. This is the same shape as the under-reporting `parse_tableau.py` warns about for
# sqlproxy-backed workbooks.
PUBLISHED_PROXY_CLASSES = frozenset({"sqlproxy"})


def safe_connection(connection: dict[str, Any]) -> dict[str, str]:
    """Project a connection down to the fields a platform engineer needs, and nothing else."""
    out: dict[str, str] = {}
    for field in SAFE_CONNECTION_FIELDS:
        value = connection.get(field)
        if value in (None, ""):
            continue
        if SECRET_KEY_PATTERN.search(field):
            continue
        out[field] = str(value)
    return out


def blast_radius(bundle_dir: Path) -> dict[str, list[str]]:
    """Map data-source name -> the workbooks that bind to it, from the engine's handover slices.

    Returns an empty map for a bundle that has no handover (a single-workbook parser spec), which is
    reported as unknown rather than as zero - a source with no known consumers is not the same as a
    source with none, and conflating them would silently deprioritise it.
    """
    consumers: dict[str, set[str]] = defaultdict(set)
    handover = bundle_dir / "handover"
    if not handover.is_dir():
        return {}
    for slice_path in sorted(handover.glob("*.json")):
        try:
            workbook = json.loads(slice_path.read_text(encoding="utf-8")).get("workbook", {})
        except (json.JSONDecodeError, OSError):  # a malformed slice must not abort the manifest
            LOG.warning("could not read %s; its workbook is missing from the blast radius", slice_path.name)
            continue
        name = workbook.get("name") or slice_path.stem
        for source in workbook.get("consolidated_datasources") or workbook.get("embedded_datasources") or []:
            key = source.get("name") if isinstance(source, dict) else str(source)
            if key:
                consumers[key].add(name)
        bound = workbook.get("bound_datasource")
        if bound:
            consumers[bound].add(name)
    return {k: sorted(v) for k, v in consumers.items()}


def build(bundle_path: Path) -> dict[str, Any]:
    """Assemble the manifest. Pure data in, pure data out - no I/O beyond reading the bundle."""
    bundle = load_bundle(bundle_path)
    radius = blast_radius(bundle.path if bundle.path.is_dir() else bundle.path.parent)

    entries: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for source in bundle.data_sources:
        connection = source.get("connection") or {}
        verdict, reason = classify_source(connection)
        name = source.get("name") or "(unnamed)"
        safe = safe_connection(connection)
        # A datasource can appear once per consuming workbook. Collapse identical (name, connection)
        # pairs and merge their consumers, or the customer reads one job as two.
        key = (name, json.dumps(safe, sort_keys=True))
        consumers = radius.get(name, [])
        if key in seen:
            merged = sorted(set(seen[key]["used_by"]) | set(consumers))
            seen[key]["used_by"] = merged
            seen[key]["used_by_count"] = len(merged)
            continue
        entry = {
            "name": name,
            "status": {"needs-credential": NEEDS_CREDENTIAL, "no-creds": SNAPSHOT}.get(verdict, REVIEW),
            "connection": safe,
            "published_datasource": (connection.get("class") or "").lower() in PUBLISHED_PROXY_CLASSES,
            "why": reason,
            "used_by": consumers,
            "used_by_count": len(consumers),
            "blast_radius_known": bool(radius),
        }
        seen[key] = entry
        entries.append(entry)

    # Highest blast radius first, then by name so the order is stable between runs.
    entries.sort(key=lambda e: (-e["used_by_count"], e["name"]))
    return {
        "bundle": str(bundle.path),
        "kind": bundle.kind,
        "total": len(entries),
        "needs_credential": sum(1 for e in entries if e["status"] == NEEDS_CREDENTIAL),
        "snapshots": sum(1 for e in entries if e["status"] == SNAPSHOT),
        "needs_review": sum(1 for e in entries if e["status"] == REVIEW),
        "blast_radius_known": bool(radius),
        "connections": entries,
    }


def _connection_summary(entry: dict[str, Any]) -> str:
    """One-line 'class @ server / database' for the table, without inventing missing parts.

    A published-datasource proxy gets named for what it is instead: `sqlproxy` is Tableau's own
    front end, and its real upstream is defined server-side, so printing the class would tell a
    platform engineer to connect to nothing.
    """
    connection = entry["connection"]
    klass = connection.get("class", "unknown")
    if entry.get("published_datasource"):
        return "**published data source** - upstream defined in Tableau, not in the workbook"
    where = connection.get("server") or ""
    what = connection.get("dbname") or connection.get("warehouse") or ""
    tail = " / ".join(p for p in (where, what) if p)
    return f"`{klass}`" + (f" @ {tail}" if tail else "")


def render(manifest: dict[str, Any]) -> str:
    """Render the human half. Written for a platform engineer who has never heard of Tableau."""
    lines = [
        "# Data source connections required after migration",
        "",
        "Migrated semantic models arrive **without credentials** - connections do not travel between",
        "tenants or workspaces. This lists every data source in the migration, what it connects to,",
        "and which reports stay broken until it is connected.",
        "",
        f"- **{manifest['needs_credential']}** source(s) need a connection before their reports work",
        f"- **{manifest['snapshots']}** source(s) are **snapshots** - extracted data with no upstream to connect",
        f"- **{manifest['needs_review']}** source(s) need a look (we could not classify them confidently)",
        "",
    ]
    if not manifest["blast_radius_known"]:
        lines += [
            "> **Impact column unavailable.** This bundle carries no per-workbook handover, so we",
            "> cannot say which reports depend on which source. Ordering below is alphabetical, not",
            "> by impact.",
            "",
        ]

    needs = [e for e in manifest["connections"] if e["status"] == NEEDS_CREDENTIAL]
    if needs:
        lines += [
            "## Connect these",
            "",
            "Ordered by impact: the number of reports that stay broken until it is connected.",
            "",
            "| Data source | Connect to | Reports affected | Which reports |",
            "|---|---|---:|---|",
        ]
        for e in needs:
            who = ", ".join(e["used_by"][:4]) + ("…" if len(e["used_by"]) > 4 else "")
            count = e["used_by_count"] if manifest["blast_radius_known"] else "?"
            lines.append(f"| **{e['name']}** | {_connection_summary(e)} | {count} | {who or '—'} |")
        lines.append("")

    snapshots = [e for e in manifest["connections"] if e["status"] == SNAPSHOT]
    if snapshots:
        lines += [
            "## Snapshots - nothing to connect",
            "",
            "These were **extracts** in Tableau: a frozen copy of data, not a live connection. The",
            "migrated model holds that same copy. It will not refresh, and there is no credential to",
            "supply. If you need it live, that is a separate exercise - the upstream system has to be",
            "identified and connected for the first time.",
            "",
            "| Data source | Was |",
            "|---|---|",
        ]
        lines += [f"| {e['name']} | {_connection_summary(e)} |" for e in snapshots]
        lines.append("")

    review = [e for e in manifest["connections"] if e["status"] == REVIEW]
    if review:
        lines += [
            "## Needs a look",
            "",
            "We could not classify these with confidence. Treat them as needing a connection until",
            "confirmed otherwise - the failure we are avoiding is a source silently treated as",
            "requiring nothing.",
            "",
            "| Data source | What we saw |",
            "|---|---|",
        ]
        lines += [f"| {e['name']} | {e['why']} |" for e in review]
        lines.append("")

    lines += [
        "---",
        "",
        "*This document contains connection targets (server, database) and never credentials.*",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path, help="estate bundle dir or migration-spec.json")
    parser.add_argument("--out", type=Path, help="write connections.md / connections.json here")
    parser.add_argument("--format", choices=("md", "json", "both"), default="both", help="what to emit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manifest = build(args.bundle)

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        if args.format in ("md", "both"):
            (args.out / "connections.md").write_text(render(manifest), encoding="utf-8")
        if args.format in ("json", "both"):
            (args.out / "connections.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        LOG.info(
            "%d source(s): %d need a connection, %d snapshot(s), %d to review -> %s",
            manifest["total"],
            manifest["needs_credential"],
            manifest["snapshots"],
            manifest["needs_review"],
            args.out,
        )
    else:
        print(render(manifest) if args.format == "md" else json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
