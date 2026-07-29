"""
purpose: discover the Tableau dependency graph BEFORE migrating anything, so a Tableau estate can be
         migrated MODEL-FIRST instead of workbook-by-workbook.

         A Tableau published data source is typically consumed by several workbooks. Migrating
         workbook-by-workbook rebuilds a near-identical semantic model every time, and those copies
         then drift. The correct Power BI shape mirrors Tableau's own: migrate each published data
         source ONCE into a shared semantic model, then bind every downstream report to it.

         This script asks Tableau itself who depends on what:
           * Metadata API (GraphQL) -> publishedDatasources { downstreamWorkbooks } lineage
           * REST API               -> download each .tdsx so the model layer can actually be parsed
         and emits a migration PLAN ordered by leverage (most-consumed data source first).

         The dedup key it prints is the SAME key `scripts/parse_tableau.py` stamps on a parsed
         workbook (`data_sources[].published_datasource.key`), so server-side lineage and locally
         parsed workbooks line up.

usage:   # credentials come from the environment, never argv (which leaks to the process list):
         #   TABLEAU_SERVER=https://10ax.online.tableau.com
         #   TABLEAU_SITE=mysitecontenturl        (empty string for Tableau Server's Default site)
         #   TABLEAU_PAT_NAME=<personal access token name>
         #   TABLEAU_PAT_SECRET=<personal access token secret>
         python scripts/tableau_lineage.py --plan
         python scripts/tableau_lineage.py --plan --download datasources/_downloads

         # offline: re-plan from a previously saved API response, no server needed
         python scripts/tableau_lineage.py --plan --from-json lineage.json

Docs: Metadata API endpoint POST <server>/api/metadata/graphql (help.tableau.com/current/api/
metadata_api/en-us/docs/meta_api_start.html); datasource download GET
/api/<ver>/sites/<site-id>/datasources/<datasource-id>/content.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("tableau_lineage")

DEFAULT_API_VERSION = "3.19"

# Tableau content lineage (workbook -> published datasource) is available WITHOUT the Data Management
# license; only *external* assets (databases/tables upstream of Tableau) require it. This query stays
# on the free side of that line on purpose.
LINEAGE_QUERY = """
query migrationLineage {
  publishedDatasources {
    id
    luid
    name
    projectName
    hasExtracts
    downstreamWorkbooks {
      luid
      name
      projectName
    }
  }
}
"""


def dedup_key(site: str, name: str) -> str:
    """Build the SAME stable key parse_tableau.py stamps on a workbook's published_datasource.

    Keep this in lockstep with `_parse_published_datasource`: '<site>/<name>' lowercased, with the
    site omitted when there isn't one (Tableau Server's Default site publishes no `site=` attribute).
    """
    return "/".join(p for p in (site, name) if p).lower()


class TableauSession(NamedTuple):
    """An authenticated Tableau connection: everything the REST + Metadata calls need to be made."""

    server: str
    token: str
    site_id: str
    api_version: str = DEFAULT_API_VERSION

    @property
    def base(self) -> str:
        """Server root with any trailing slash removed."""
        return self.server.rstrip("/")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """POST JSON and return the decoded JSON response."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in {"Content-Type": "application/json", "Accept": "application/json", **headers}.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - URL comes from env config
        return json.loads(resp.read().decode("utf-8"))


def sign_in(server: str, site: str, pat_name: str, pat_secret: str, api_version: str) -> TableauSession:
    """Authenticate with a Personal Access Token; return an authenticated session.

    The Metadata API shares this token -- there is no separate GraphQL login.
    """
    url = f"{server.rstrip('/')}/api/{api_version}/auth/signin"
    payload = {
        "credentials": {
            "personalAccessTokenName": pat_name,
            "personalAccessTokenSecret": pat_secret,
            "site": {"contentUrl": site},
        }
    }
    creds = _post_json(url, payload, headers={}).get("credentials", {})
    token = creds.get("token")
    site_id = (creds.get("site") or {}).get("id")
    if not token or not site_id:
        raise RuntimeError("sign-in succeeded but returned no token/site id")
    return TableauSession(server=server, token=token, site_id=site_id, api_version=api_version)


def fetch_lineage(session: TableauSession) -> list[dict[str, Any]]:
    """Run the Metadata API lineage query; return the publishedDatasources list."""
    url = f"{session.base}/api/metadata/graphql"
    result = _post_json(url, {"query": LINEAGE_QUERY}, headers={"X-Tableau-Auth": session.token})
    if result.get("errors"):
        raise RuntimeError(f"Metadata API returned errors: {json.dumps(result['errors'])[:400]}")
    return result.get("data", {}).get("publishedDatasources", [])


def download_datasource(session: TableauSession, luid: str, dest: Path) -> Path:
    """Download one published data source's content (.tdsx) so its model layer can be parsed."""
    url = f"{session.base}/api/{session.api_version}/sites/{session.site_id}/datasources/{luid}/content"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Tableau-Auth", session.token)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 - URL comes from env config
        dest.write_bytes(resp.read())
    return dest


def build_plan(datasources: list[dict[str, Any]], site: str) -> list[dict[str, Any]]:
    """Turn raw lineage into a migration plan ordered by LEVERAGE (most downstream workbooks first).

    Highest fan-out first is deliberate: migrating the data source that 12 workbooks depend on saves
    11 duplicate semantic models, so it is the highest-value unit of work in the estate.
    """
    plan = []
    for ds in datasources:
        downstream = ds.get("downstreamWorkbooks") or []
        plan.append(
            {
                "key": dedup_key(site, ds.get("name") or ""),
                "name": ds.get("name"),
                "luid": ds.get("luid"),
                "project": ds.get("projectName"),
                "has_extracts": ds.get("hasExtracts"),
                "downstream_count": len(downstream),
                "downstream_workbooks": [w.get("name") for w in downstream],
            }
        )
    return sorted(plan, key=lambda p: (-p["downstream_count"], (p["name"] or "").lower()))


def print_plan(plan: list[dict[str, Any]]) -> None:
    """Print the model-first migration plan."""
    if not plan:
        log.info("No published data sources found on this site.")
        log.info("Every workbook embeds its own data source -> migrate workbook-by-workbook as usual.")
        return

    shared = [p for p in plan if p["downstream_count"] > 1]
    orphans = [p for p in plan if p["downstream_count"] == 0]
    workbooks = {w for p in plan for w in p["downstream_workbooks"]}

    log.info("=" * 78)
    log.info("MIGRATION PLAN - model layer first")
    log.info("=" * 78)
    log.info(
        "%d published data source(s) feed %d workbook(s). %d are SHARED by more than one workbook.",
        len(plan),
        len(workbooks),
        len(shared),
    )
    log.info("\nPHASE 1 - migrate these data sources to semantic models (highest leverage first):\n")
    for i, p in enumerate(plan, 1):
        if p["downstream_count"] == 0:
            continue
        saved = max(0, p["downstream_count"] - 1)
        log.info("  %2d. %-38s  %2d workbook(s)   key=%s", i, (p["name"] or "?")[:38], p["downstream_count"], p["key"])
        log.info(
            "      project=%-24s extracts=%-5s saves %d duplicate model(s)", p["project"], p["has_extracts"], saved
        )
        for wb in p["downstream_workbooks"]:
            log.info("        -> %s", wb)
    log.info("\nPHASE 2 - migrate each workbook to a REPORT bound to the model built in phase 1.")
    log.info("          Do NOT rebuild the model per workbook; check first with:")
    log.info("          python scripts/published_datasource_registry.py --spec <spec.json>")
    if orphans:
        log.info("\nNOTE: %d published data source(s) have NO downstream workbooks:", len(orphans))
        for p in orphans:
            log.info("        - %s (%s)", p["name"], p["project"])
        log.info("      Confirm with the customer before migrating - these may be abandoned.")


def _env_config() -> tuple[str, str, str, str]:
    """Read server/site/PAT from the environment; fail with an actionable message if incomplete."""
    server = os.environ.get("TABLEAU_SERVER", "")
    site = os.environ.get("TABLEAU_SITE", "")
    pat_name = os.environ.get("TABLEAU_PAT_NAME", "")
    pat_secret = os.environ.get("TABLEAU_PAT_SECRET", "")
    missing = [
        n
        for n, v in (("TABLEAU_SERVER", server), ("TABLEAU_PAT_NAME", pat_name), ("TABLEAU_PAT_SECRET", pat_secret))
        if not v
    ]
    if missing:
        raise SystemExit(
            "Missing environment variable(s): "
            + ", ".join(missing)
            + "\n  TABLEAU_SERVER    e.g. https://10ax.online.tableau.com"
            + "\n  TABLEAU_SITE      site contentUrl ('' for Tableau Server's Default site)"
            + "\n  TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET  a Personal Access Token"
            + "\n(The agent cannot create these - a Tableau user with access must supply them.)"
        )
    return server, site, pat_name, pat_secret


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", action="store_true", help="Print the model-first migration plan")
    parser.add_argument("--download", type=Path, help="Download every published data source (.tdsx) to this folder")
    parser.add_argument("--from-json", type=Path, help="Re-plan offline from a saved lineage response")
    parser.add_argument("--save-json", type=Path, help="Save the raw lineage response for offline re-planning")
    parser.add_argument(
        "--api-version", default=DEFAULT_API_VERSION, help=f"REST API version (default {DEFAULT_API_VERSION})"
    )
    args = parser.parse_args(argv)

    if args.from_json:
        payload = json.loads(args.from_json.read_text(encoding="utf-8"))
        print_plan(build_plan(payload.get("datasources", []), payload.get("site", "")))
        return 0

    server, site, pat_name, pat_secret = _env_config()
    try:
        session = sign_in(server, site, pat_name, pat_secret, args.api_version)
        log.info("signed in to %s (site '%s')", server, site or "<default>")
        datasources = fetch_lineage(session)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
        log.error("Tableau API call failed: %s", exc)
        return 1

    log.info("found %d published data source(s)", len(datasources))
    if args.save_json:
        args.save_json.write_text(json.dumps({"site": site, "datasources": datasources}, indent=2), encoding="utf-8")
        log.info("raw lineage saved to %s", args.save_json)

    plan = build_plan(datasources, site)
    if args.plan or not args.download:
        print_plan(plan)

    if args.download:
        log.info("\nDownloading %d data source(s) to %s ...", len(plan), args.download)
        for p in plan:
            if not p["luid"]:
                continue
            dest = args.download / f"{p['name']}.tdsx"
            try:
                download_datasource(session, p["luid"], dest)
                log.info("  OK  %s", dest)
            except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                log.warning("  !!  %s failed: %s", p["name"], exc)
        log.info("\nParse each with: python scripts/parse_tableau.py <file>.tdsx -o <spec>.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
