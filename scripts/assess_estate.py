"""
purpose: assess a Tableau estate before migrating any of it - what exists, what is actually used,
         how hard each workbook is, and who can see it - and emit a decision, not an inventory.
usage:   python scripts/assess_estate.py --out _assessment [--survey <estate_survey.json>]
                                         [--coverage-target 0.99] [--env .env]

Why this exists
---------------
Every entry point in this toolchain begins AFTER the hardest question has been answered. The
deterministic engine opens at *"D2 - SCOPE: name the workbooks"*; our coordinator takes a folder.
At a customer with 400 workbooks, **which ones** is the engagement.

This is Phase 0. Its output is a **decision** - migrate / consolidate / archive / retire, and a
coverage curve the customer picks a point on - rather than a catalogue.

Four things it will not do, each for a measured reason
-----------------------------------------------------
1. **It does not derive dependencies from the Metadata API.** Measured 2026-08-06: the Metadata API
   reported ``upstreamDatasources`` for **0 of 13** workbooks where REST ``connections`` showed
   ``type: sqlproxy`` on **9**. An estate plan built on it concludes "migrate in any order" and
   produces empty reports. Pass ``--survey`` with the engine's ``estate_survey.py --json`` output,
   which reads REST ground truth. Without it, ordering is reported as **unknown**, never as "none".

2. **It exports IAM; it does not map it.** Every practitioner source agrees permissions can only be
   mapped once the Power BI workspace topology is decided, and that decision is human. A tool that
   maps permissions before topology produces confident nonsense, so this emits the raw grants and
   the specific *hard cases* (Deny, per-view grants, `ViewUnderlyingData` split from `Read`, local
   groups with no Entra counterpart) as decisions someone has to take.

3. **It does not retire anything on a metric.** Usage proposes; the owner disposes. A quarterly board
   pack has near-zero views and is business-critical, so low usage produces a *candidate*, never a
   verdict, and anything carrying a subscription, alert or custom view is held back from the
   retire tier regardless of view count.

4. **It does not claim a usage window it does not have.** Tableau **Cloud** REST returns
   ``usage.totalViewCount`` as a LIFETIME figure - there is no "last 90 days" without Admin Insights
   or (on Server) the Postgres repository. It is labelled ``lifetime`` everywhere.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tableau_env import env_source, pat_secret, require, resolve_env  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("assess")

SESSION_LOST = "401002"
MAX_REAUTH = 2

# Tableau calc idioms that drive migration effort. LOD and table calcs are weighted heaviest because
# they are what DAX translation actually struggles with - the same weighting an existing open-source
# complexity scorer arrived at independently.
LOD_RE = re.compile(r"\{\s*(fixed|include|exclude)\b", re.I)
TABLE_CALC_RE = re.compile(
    r"\b(window_\w+|running_\w+|index|rank|rank_dense|rank_modified|rank_unique|lookup|total|"
    r"first|last|size|previous_value|script_\w+)\s*\(",
    re.I,
)
WEIGHTS = {"sheets": 1.0, "dashboards": 2.0, "calcs": 1.0, "lods": 5.0, "table_calcs": 5.0}


class Site:
    """Read-only Tableau client. Re-authenticates on mid-run session loss and records that it did."""

    def __init__(self, env: dict[str, str]) -> None:
        self.base = env["TABLEAU_SERVER_URL"].rstrip("/")
        self.version = env.get("TABLEAU_REST_API_VERSION", "3.21")
        self.site = env["TABLEAU_SITE"]
        self._pat = (env["TABLEAU_PAT_NAME"], pat_secret(env))
        self.token: str | None = None
        self.site_id: str | None = None
        self.reauths = 0

    def _raw(self, method: str, path: str, body: dict | None = None, root: str | None = None):
        url = f"{self.base}{root or f'/api/{self.version}'}{path}"
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method)
        request.add_header("Accept", "application/json")
        if body:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("X-Tableau-Auth", self.token)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def sign_in(self) -> None:
        """Exchange the PAT for a session token."""
        status, payload = self._raw(
            "POST",
            "/auth/signin",
            {
                "credentials": {
                    "personalAccessTokenName": self._pat[0],
                    "personalAccessTokenSecret": self._pat[1],
                    "site": {"contentUrl": self.site},
                }
            },
        )
        if status != 200:
            raise RuntimeError(f"sign-in failed: HTTP {status} (check the PAT NAME and SECRET - two values)")
        creds = json.loads(payload)["credentials"]
        self.token, self.site_id = creds["token"], creds["site"]["id"]

    def sign_out(self) -> None:
        """Best-effort release."""
        if self.token:
            self._raw("POST", "/auth/signout")
            self.token = None

    def get(self, path: str) -> dict[str, Any] | None:
        """GET a metadata endpoint, recovering from mid-run session loss.

        Returns ``None`` on a permission or not-found failure rather than raising: one 403 on one
        workbook's permissions must not void an estate-wide assessment.
        """
        for _ in range(MAX_REAUTH + 1):
            status, payload = self._raw("GET", path)
            if status == 200:
                return json.loads(payload)
            if SESSION_LOST in payload.decode("utf-8", "replace"):
                self.reauths += 1
                self.sign_in()
                continue
            return None
        return None

    def paged(self, path: str, collection: str, item: str) -> list[dict]:
        """Follow REST pagination to completion. A survey that stops at page 1 under-reports."""
        out: list[dict] = []
        page = 1
        while page <= 1000:
            sep = "&" if "?" in path else "?"
            payload = self.get(f"{path}{sep}pageSize=1000&pageNumber={page}")
            if not payload:
                break
            block = payload.get(collection) or {}
            rows = block.get(item) or []
            rows = [rows] if isinstance(rows, dict) else rows
            out.extend(rows)
            total = int((payload.get("pagination") or {}).get("totalAvailable", 0))
            if not rows or len(out) >= total:
                break
            page += 1
        return out

    def graphql(self, query: str) -> dict[str, Any]:
        """One Metadata API call. Used for STRUCTURE only - never for dependencies (see module doc)."""
        request = urllib.request.Request(
            f"{self.base}/api/metadata/graphql", data=json.dumps({"query": query}).encode(), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Tableau-Auth", self.token)
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())


# `role` and `dataType` live on the CONCRETE types, not the Field interface - `fields { role }`
# fails with FieldUndefined. Inline fragments are required.
STRUCTURE_QUERY = """
{ workbooks {
    name projectName
    sheets { name }
    dashboards { name }
    embeddedDatasources {
      name hasUserReference
      fields { name __typename
        ... on ColumnField     { role dataType }
        ... on CalculatedField { role dataType formula } } } }
  publishedDatasources {
    name isCertified hasExtracts extractLastRefreshTime
    upstreamTables { fullName } } }
"""


def score_workbook(node: dict[str, Any]) -> dict[str, Any]:
    """Complexity from the workbook's own structure.

    ⚠️ **Understates any workbook backed by a published datasource**, whose calculated fields live in
    the datasource rather than the workbook - measured: a workbook literally named "Calc Gauntlet"
    scored 0 calcs. `--survey` supplies the dependency edges so those are flagged rather than
    silently trusted.
    """
    calcs = [
        f
        for ds in node.get("embeddedDatasources") or []
        for f in ds.get("fields") or []
        if f.get("__typename") == "CalculatedField" and f.get("formula")
    ]
    lods = [f for f in calcs if LOD_RE.search(f["formula"])]
    table_calcs = [f for f in calcs if TABLE_CALC_RE.search(f["formula"])]
    counts = {
        "sheets": len(node.get("sheets") or []),
        "dashboards": len(node.get("dashboards") or []),
        "calcs": len(calcs),
        "lods": len(lods),
        "table_calcs": len(table_calcs),
    }
    counts["has_user_reference"] = any(ds.get("hasUserReference") for ds in node.get("embeddedDatasources") or [])
    counts["complexity"] = round(sum(w * counts[k] for k, w in WEIGHTS.items()), 1)
    return counts


def liveness(views: list[dict], signals: dict[str, int]) -> dict[str, Any]:
    """Usage evidence for one workbook. ``views_lifetime`` is deliberately named: Cloud has no window."""
    return {
        "views_lifetime": sum(int((v.get("usage") or {}).get("totalViewCount") or 0) for v in views),
        "view_count": len(views),
        "subscriptions": signals.get("subscriptions", 0),
        "alerts": signals.get("alerts", 0),
        "custom_views": signals.get("custom_views", 0),
    }


def tier(live: dict[str, Any], complexity: float, cumulative_share: float, target: float) -> tuple[str, str]:
    """Assign a destination and say why. Never retires on a metric alone.

    Deliberate use - a subscription, an alert, a saved custom view - outranks a view count, because
    somebody chose to receive or personalise it. That is also the only available proxy for the
    seasonal report that has near-zero views and is business critical.
    """
    deliberate = live["subscriptions"] + live["alerts"] + live["custom_views"]
    if deliberate:
        return "migrate", f"deliberate use ({deliberate} subscription/alert/custom-view)"
    if cumulative_share <= target:
        return "migrate", f"inside the {target:.0%} usage cut"
    if live["views_lifetime"] > 0:
        return "archive", "used, but outside the coverage cut - static export retains it"
    if complexity >= 40:
        return "review", "no recorded use, but complex enough that a human should confirm"
    return "retire-candidate", "no recorded use, no deliberate use - CONFIRM WITH THE OWNER"


def coverage_curve(rows: list[dict]) -> list[dict]:
    """Order by usage and accumulate. This is the artifact the strategy decision is made on."""
    ordered = sorted(rows, key=lambda r: r["views_lifetime"], reverse=True)
    total = sum(r["views_lifetime"] for r in ordered) or 1
    running = 0
    out = []
    for index, row in enumerate(ordered, 1):
        running += row["views_lifetime"]
        out.append({**row, "rank": index, "cumulative_share": round(running / total, 6)})
    return out


def iam_hard_cases(permissions: list[dict], groups: list[dict]) -> list[dict]:
    """The permission facts that need a HUMAN decision, not a mapping.

    Each is something Power BI's model cannot express directly, so it becomes a decision in the
    topology design rather than a row in a translation table.
    """
    cases = []
    denies = [p for p in permissions if p["mode"].lower() == "deny"]
    if denies:
        cases.append(
            {
                "case": "explicit_deny",
                "count": len(denies),
                "why": "Power BI has no Deny. Each must be resolved to a grant or an absence, by hand.",
            }
        )
    view_grants = [p for p in permissions if p["object_type"] == "view"]
    if view_grants:
        cases.append(
            {
                "case": "per_view_grants",
                "count": len(view_grants),
                "why": "Power BI shares per REPORT, not per page. Different audiences per sheet forces a report split.",
            }
        )
    underlying = [p for p in permissions if p["capability"] in {"ViewUnderlyingData", "ExportData"}]
    if underlying:
        cases.append(
            {
                "case": "data_export_split_from_read",
                "count": len(underlying),
                "why": "Power BI's Build permission is all-or-nothing; 'see the chart, "
                "not the numbers' is not expressible.",
            }
        )
    local = [g for g in groups if (g.get("domain") or {}).get("name") == "local"]
    if local:
        cases.append(
            {
                "case": "local_groups_without_entra",
                "count": len(local),
                "why": "Local Tableau groups have no Entra counterpart. Creating them "
                "needs an identity owner - the long pole.",
                "names": [g["name"] for g in local][:20],
            }
        )
    return cases


SCHEMA = """
CREATE TABLE IF NOT EXISTS workbook (
  luid TEXT PRIMARY KEY, name TEXT, project TEXT, project_luid TEXT, owner_luid TEXT, size_mb INTEGER,
  created_at TEXT, updated_at TEXT,
  sheets INTEGER, dashboards INTEGER, calcs INTEGER, lods INTEGER, table_calcs INTEGER,
  has_user_reference INTEGER, complexity REAL, complexity_understated INTEGER,
  views_lifetime INTEGER, view_count INTEGER, subscriptions INTEGER, alerts INTEGER,
  custom_views INTEGER, rank INTEGER, cumulative_share REAL, tier TEXT, tier_reason TEXT);
CREATE TABLE IF NOT EXISTS view (
  luid TEXT PRIMARY KEY, workbook_luid TEXT, name TEXT, content_url TEXT,
  views_lifetime INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS datasource (
  luid TEXT PRIMARY KEY, name TEXT, project TEXT, project_luid TEXT, is_certified INTEGER,
  has_extracts INTEGER, extract_last_refresh TEXT);
CREATE TABLE IF NOT EXISTS upstream_table (datasource_name TEXT, full_name TEXT);
CREATE TABLE IF NOT EXISTS dependency (
  workbook_name TEXT, workbook_luid TEXT, datasource_name TEXT, datasource_luid TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS project (
  luid TEXT PRIMARY KEY, name TEXT, parent_luid TEXT, content_permissions TEXT);
CREATE TABLE IF NOT EXISTS grp (luid TEXT PRIMARY KEY, name TEXT, domain TEXT, members INTEGER);
CREATE TABLE IF NOT EXISTS permission (
  object_type TEXT, object_luid TEXT, object_name TEXT,
  grantee_type TEXT, grantee_luid TEXT, capability TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS flow (luid TEXT PRIMARY KEY, name TEXT, project TEXT);
"""


def _collect_iam(site: Site, projects: list[dict], workbooks: list[dict]) -> list[dict]:
    """Export grants. Per-item grants are only collected where owners were free to diverge."""
    permissions: list[dict] = []
    locked = 0
    for project in projects:
        permissions.extend(_grants(site, "project", project["id"], project.get("name"), f"/projects/{project['id']}"))
        if (project.get("contentPermissions") or "").startswith("LockedToProject"):
            locked += 1
    unlocked = {p["id"] for p in projects if not (p.get("contentPermissions") or "").startswith("LockedToProject")}
    for workbook in workbooks:
        if (workbook.get("project") or {}).get("id") in unlocked:
            permissions.extend(
                _grants(site, "workbook", workbook["id"], workbook.get("name"), f"/workbooks/{workbook['id']}")
            )
    LOG.info(
        "  %d project(s) LockedToProject -> per-item grants skipped there; %d grant rows", locked, len(permissions)
    )
    return permissions


def collect(site: Site, survey: dict | None) -> dict[str, Any]:  # pylint: disable=too-many-locals
    """Run the passes in cost order, cheapest first.

    One local per REST collection - splitting further would only hide the shape of the estate.
    """
    LOG.info("pass 1: inventory")
    workbooks = site.paged(f"/sites/{site.site_id}/workbooks", "workbooks", "workbook")
    views = site.paged(f"/sites/{site.site_id}/views?includeUsageStatistics=true", "views", "view")
    datasources = site.paged(f"/sites/{site.site_id}/datasources", "datasources", "datasource")
    projects = site.paged(f"/sites/{site.site_id}/projects", "projects", "project")
    groups = site.paged(f"/sites/{site.site_id}/groups", "groups", "group")
    flows = site.paged(f"/sites/{site.site_id}/flows", "flows", "flow")
    LOG.info(
        "  %d workbooks, %d views, %d datasources, %d projects, %d groups, %d flows",
        len(workbooks),
        len(views),
        len(datasources),
        len(projects),
        len(groups),
        len(flows),
    )

    for group in groups:
        group["_members"] = len(site.paged(f"/sites/{site.site_id}/groups/{group['id']}/users", "users", "user"))

    LOG.info("pass 1b: deliberate-use signals")
    subs = site.paged(f"/sites/{site.site_id}/subscriptions", "subscriptions", "subscription")
    alerts = site.paged(f"/sites/{site.site_id}/dataAlerts", "dataAlerts", "dataAlert")
    custom = site.paged(f"/sites/{site.site_id}/customviews", "customViews", "customView")

    LOG.info("pass 2: structure (one GraphQL call for the whole estate)")
    structure = site.graphql(STRUCTURE_QUERY)
    if structure.get("errors"):
        LOG.warning("  Metadata API errors: %s", str(structure["errors"])[:200])
    data = structure.get("data") or {}
    by_name = {w["name"]: w for w in data.get("workbooks") or []}

    LOG.info("pass 3: IAM (gated on contentPermissions)")
    permissions = _collect_iam(site, projects, workbooks)

    return {
        "workbooks": workbooks,
        "views": views,
        "datasources": datasources,
        "projects": projects,
        "groups": groups,
        "flows": flows,
        "subscriptions": subs,
        "alerts": alerts,
        "custom_views": custom,
        "structure": data,
        "structure_by_name": by_name,
        "permissions": permissions,
        "survey": survey,
    }


def _grants(site: Site, object_type: str, luid: str, name: str | None, path: str) -> list[dict]:
    """Flatten one object's granteeCapabilities into rows. A 403 yields nothing, never an abort."""
    payload = site.get(f"/sites/{site.site_id}{path}/permissions")
    rows = []
    for grantee in ((payload or {}).get("permissions") or {}).get("granteeCapabilities") or []:
        kind = "group" if "group" in grantee else "user"
        gid = (grantee.get(kind) or {}).get("id")
        for capability in (grantee.get("capabilities") or {}).get("capability") or []:
            rows.append(
                {
                    "object_type": object_type,
                    "object_luid": luid,
                    "object_name": name,
                    "grantee_type": kind,
                    "grantee_luid": gid,
                    "capability": capability.get("name"),
                    "mode": capability.get("mode"),
                }
            )
    return rows


def _aggregate_signals(raw: dict[str, Any]) -> tuple[dict[str, list], dict[str, dict[str, int]]]:
    """Group views by workbook, and roll every deliberate-use signal up to its workbook.

    A subscription/alert/custom view is attached to a VIEW, but the migration decision is taken per
    WORKBOOK, so a signal anywhere inside a workbook has to count for the whole workbook.
    """
    views_by_wb: dict[str, list] = {}
    for view in raw["views"]:
        views_by_wb.setdefault((view.get("workbook") or {}).get("id"), []).append(view)

    signals: dict[str, dict[str, int]] = {}
    for kind in ("subscriptions", "alerts", "custom_views"):
        for item in raw[kind]:
            luid = (item.get("content") or item.get("view") or {}).get("id")
            if luid:
                signals.setdefault(luid, {}).setdefault(kind, 0)
                signals[luid][kind] += 1

    wb_signals: dict[str, dict[str, int]] = {}
    view_owner = {v["id"]: (v.get("workbook") or {}).get("id") for v in raw["views"]}
    for luid, counts in signals.items():
        owner = view_owner.get(luid, luid)
        for key, value in counts.items():
            wb_signals.setdefault(owner, {}).setdefault(key, 0)
            wb_signals[owner][key] += value
    return views_by_wb, wb_signals


def _parse_dependencies(survey: dict | None) -> tuple[set[str], list[dict]]:
    """Read the REST-derived dependency graph. Raises rather than under-reporting."""
    required: set[str] = set()
    dep_rows: list[dict] = []
    if not survey:
        return required, dep_rows
    for wb in survey.get("workbooks") or []:
        if wb.get("complexity_understated"):
            required.add(wb.get("name"))
        for dep in wb.get("published_dependencies") or []:
            # His schema, verified against estate_survey.py output: `datasource_name`. Read it
            # explicitly rather than with a chain of fallbacks - an earlier version guessed at
            # `datasource`/`name`, parsed ZERO edges, and reported "order unknown", which is the
            # very failure this whole survey exists to prevent. A guess that yields nothing is
            # indistinguishable from a genuine absence, so it must not be possible to guess.
            name = dep.get("datasource_name") if isinstance(dep, dict) else None
            if name:
                dep_rows.append(
                    {
                        "workbook_name": wb.get("name"),
                        "workbook_luid": wb.get("luid"),
                        "datasource_name": name,
                        "datasource_luid": dep.get("luid"),
                        "source": f"sqlproxy/{dep.get('status', 'unknown')}",
                    }
                )
    declared = sum(len(wb.get("published_dependencies") or []) for wb in survey.get("workbooks") or [])
    if declared and not dep_rows:
        raise RuntimeError(
            f"the survey declares {declared} dependency entries but none parsed - its schema has "
            "changed. Refusing to report 'no dependencies', which would sequence the migration wrong."
        )
    return required, dep_rows


def assemble(raw: dict[str, Any], target: float) -> dict[str, Any]:
    """Turn collected facts into a scored, tiered backlog plus the coverage curve."""
    views_by_wb, wb_signals = _aggregate_signals(raw)
    survey = raw.get("survey")
    required, dep_rows = _parse_dependencies(survey)

    rows = []
    for wb in raw["workbooks"]:
        node = raw["structure_by_name"].get(wb["name"], {})
        scored = (
            score_workbook(node)
            if node
            else {k: 0 for k in ("sheets", "dashboards", "calcs", "lods", "table_calcs", "complexity")}
        )
        live = liveness(views_by_wb.get(wb["id"], []), wb_signals.get(wb["id"], {}))
        rows.append(
            {
                "luid": wb["id"],
                "name": wb["name"],
                "project": (wb.get("project") or {}).get("name"),
                "project_luid": (wb.get("project") or {}).get("id"),
                "owner_luid": (wb.get("owner") or {}).get("id"),
                "size_mb": wb.get("size"),
                "created_at": wb.get("createdAt"),
                "updated_at": wb.get("updatedAt"),
                **scored,
                **live,
                "complexity_understated": 1 if wb["name"] in required else 0,
            }
        )

    curve = coverage_curve(rows)
    for row in curve:
        row["tier"], row["tier_reason"] = tier(row, row["complexity"], row["cumulative_share"], target)
    return {
        "workbooks": curve,
        "dependencies": dep_rows,
        "survey_supplied": survey is not None,
        "iam_hard_cases": iam_hard_cases(raw["permissions"], raw["groups"]),
    }


def write_store(out: Path, raw: dict[str, Any], assembled: dict[str, Any]) -> Path:
    """Raw JSON as evidence, SQLite as the query layer.

    Raw responses are kept because an assessment is evidence for a COMMERCIAL decision - "retire
    these 40" must be defensible months later, and an API response is not reproducible once the
    estate moves.
    """
    (out / "raw").mkdir(parents=True, exist_ok=True)
    for key in (
        "workbooks",
        "views",
        "datasources",
        "projects",
        "groups",
        "flows",
        "subscriptions",
        "alerts",
        "custom_views",
        "permissions",
        "structure",
    ):
        (out / "raw" / f"{key}.json").write_text(json.dumps(raw[key], indent=2), encoding="utf-8")

    db_path = out / "estate.db"
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO workbook VALUES (:luid,:name,:project,:project_luid,:owner_luid,:size_mb,:created_at,:updated_at,"
        ":sheets,:dashboards,:calcs,:lods,:table_calcs,:has_user_reference,:complexity,"
        ":complexity_understated,:views_lifetime,:view_count,:subscriptions,:alerts,:custom_views,"
        ":rank,:cumulative_share,:tier,:tier_reason)",
        [{**r, "has_user_reference": int(bool(r.get("has_user_reference")))} for r in assembled["workbooks"]],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO view VALUES (?,?,?,?,?,?)",
        [
            (
                v["id"],
                (v.get("workbook") or {}).get("id"),
                v.get("name"),
                v.get("contentUrl"),
                int((v.get("usage") or {}).get("totalViewCount") or 0),
                v.get("updatedAt"),
            )
            for v in raw["views"]
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO datasource VALUES (?,?,?,?,?,?,?)",
        [
            (
                d["id"],
                d.get("name"),
                (d.get("project") or {}).get("name"),
                (d.get("project") or {}).get("id"),
                None,
                None,
                None,
            )
            for d in raw["datasources"]
        ],
    )
    for ds in raw["structure"].get("publishedDatasources") or []:
        conn.execute(
            "UPDATE datasource SET is_certified=?, has_extracts=?, extract_last_refresh=? WHERE name=?",
            (
                int(bool(ds.get("isCertified"))),
                int(bool(ds.get("hasExtracts"))),
                ds.get("extractLastRefreshTime"),
                ds.get("name"),
            ),
        )
        conn.executemany(
            "INSERT INTO upstream_table VALUES (?,?)",
            [(ds.get("name"), t.get("fullName")) for t in ds.get("upstreamTables") or []],
        )
    conn.executemany(
        "INSERT INTO dependency VALUES (:workbook_name,:workbook_luid,:datasource_name,:datasource_luid,:source)",
        assembled["dependencies"],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO project VALUES (?,?,?,?)",
        [(p["id"], p.get("name"), p.get("parentProjectId"), p.get("contentPermissions")) for p in raw["projects"]],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO grp VALUES (?,?,?,?)",
        [(g["id"], g.get("name"), (g.get("domain") or {}).get("name"), g.get("_members", 0)) for g in raw["groups"]],
    )
    conn.executemany(
        "INSERT INTO permission VALUES (:object_type,:object_luid,:object_name,:grantee_type,"
        ":grantee_luid,:capability,:mode)",
        raw["permissions"],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO flow VALUES (?,?,?)",
        [(f["id"], f.get("name"), (f.get("project") or {}).get("name")) for f in raw["flows"]],
    )
    conn.commit()
    conn.close()
    return db_path


def _render_curve(rows: list[dict], target: float) -> list[str]:
    """Header + the sparse-data caveat + the coverage curve."""
    total_views = sum(r["views_lifetime"] for r in rows)
    inside = [r for r in rows if r["cumulative_share"] <= target]
    out = ["# Estate assessment", ""]
    out.append(
        f"**{len(rows)} workbooks**, {sum(r['view_count'] for r in rows)} views, {total_views:,} lifetime view events."
    )
    out.append("")
    # A handful of view events across a whole estate cannot support a tiering decision.
    # Checking `== 0` missed this on a real run (1 event across 13 workbooks) and printed a
    # confident curve built on nothing, which is worse than printing no curve at all.
    if total_views < max(10, len(rows)):
        out.append(
            f"> WARNING: **usage data is too sparse to tier on** ({total_views:,} lifetime view "
            f"event(s) across {len(rows)} workbooks). Either the site is new, or usage statistics "
            "are unavailable. Every tier below is therefore **unproven** - scope by hand, and do "
            "not present this curve to a customer as evidence."
        )
        out.append("")
    out.append(f"## The coverage curve (target {target:.0%})")
    out.append("")
    out.append(f"**{len(inside)} of {len(rows)} workbooks carry {target:.0%} of all usage.**")
    out.append("")
    out.append("| rank | workbook | views (lifetime) | cumulative | complexity | tier |")
    out.append("|---|---|---:|---:|---:|---|")
    for row in rows[:15]:
        out.append(
            f"| {row['rank']} | {row['name']} | {row['views_lifetime']:,} | "
            f"{row['cumulative_share']:.1%} | {row['complexity']:.0f} | {row['tier']} |"
        )
    if len(rows) > 15:
        out.append(f"| … | _{len(rows) - 15} more_ | | | | |")
    out.append("")
    return out


def _render_tiers(rows: list[dict]) -> list[str]:
    """The destinations table and the rule that usage never retires anything on its own."""
    by_tier: dict[str, list] = {}
    for row in rows:
        by_tier.setdefault(row["tier"], []).append(row)
    out = ["## Destinations", "", "| tier | count | meaning |", "|---|---:|---|"]
    meaning = {
        "migrate": "rebuild, validate, sign off",
        "archive": "keep accessible, do not rebuild - static export",
        "review": "a human must decide",
        "retire-candidate": "**candidate only** - confirm with the owner before deleting",
    }
    for name in ("migrate", "archive", "review", "retire-candidate"):
        if name in by_tier:
            out.append(f"| {name} | {len(by_tier[name])} | {meaning[name]} |")
    out.append("")
    out.append(
        "> Usage **proposes**; the owner **disposes**. Nothing here is retired on a metric. A "
        "quarterly board pack has near-zero views and is business-critical, so anything "
        "carrying a subscription, alert or saved custom view is held out of the retire tier "
        "regardless of its view count."
    )
    out.append("")
    return out


def _render_sequencing(assembled: dict[str, Any], rows: list[dict]) -> list[str]:
    """What would make the backlog wrong: understated sizing, and unknown migration order."""
    out: list[str] = []
    understated = [r for r in rows if r["complexity_understated"]]
    if understated:
        out.append(
            f"⚠️ **{len(understated)} workbook(s) have an UNDERSTATED complexity score** - they are "
            "backed by a published datasource whose calculated fields are not counted here. "
            "Size them after the datasource is in scope."
        )
        out.append("")
    if assembled["dependencies"]:
        out.append(f"**{len(assembled['dependencies'])} hard dependency edge(s)** - those datasources migrate first.")
    elif assembled.get("survey_supplied"):
        out.append(
            "**No published-datasource dependencies** - the survey resolved zero edges, so every "
            "workbook is self-contained and may migrate in any order."
        )
    else:
        out.append(
            "⚠️ **Migration ORDER is unknown** - no `--survey` was supplied, so published-datasource "
            "dependencies were not resolved. Reported as unknown rather than as *none*: the Metadata "
            "API answers this question wrongly (measured 0 where REST showed 9), and a workbook whose "
            "datasource has not landed rebuilds to an EMPTY report."
        )
    out.append("")
    return out


def _render_iam(assembled: dict[str, Any], raw: dict[str, Any]) -> list[str]:
    """The grants that will not map themselves, and the refusal to map them here."""
    out = ["## IAM - decisions, not a mapping", ""]
    if assembled["iam_hard_cases"]:
        for case in assembled["iam_hard_cases"]:
            out.append(f"- **{case['case']}** ({case['count']}) — {case['why']}")
    else:
        out.append("- No hard cases detected in the exported grants.")
    out.append("")
    out.append(
        "> Permissions are **exported, not mapped**. Mapping requires the Power BI workspace "
        "topology, and that is a human decision — a tool that maps before topology is fixed "
        "produces confident nonsense. Design the topology against this export, then map."
    )
    out.append("")
    out.append(
        f"Flows (Tableau Prep ETL): **{len(raw['flows'])}** — each is its own dependency chain, "
        "landing before the extracts it produces."
    )
    return out


def render_report(assembled: dict[str, Any], raw: dict[str, Any], target: float) -> str:
    """The customer-facing summary. Leads with the decision, not the inventory."""
    rows = assembled["workbooks"]
    out = _render_curve(rows, target)
    out += _render_tiers(rows)
    out += _render_sequencing(assembled, rows)
    out += _render_iam(assembled, raw)
    return "\n".join(out) + "\n"


def main() -> int:
    """Assess the estate. Exit 1 when nothing could be assessed."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="output directory (should be git-ignored)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials")
    parser.add_argument("--survey", type=Path, help="estate_survey.py --json output: the REST-derived dependency graph")
    parser.add_argument(
        "--coverage-target",
        type=float,
        default=0.99,
        help="share of usage to keep in scope (1.0 = lift-and-shift). Default 0.99",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = resolve_env(args.env)
    require(env)
    LOG.info("credentials: %s from the %s", "TABLEAU_PAT_SECRET", env_source("TABLEAU_PAT_SECRET", args.env))
    site = Site(env)
    site.sign_in()
    LOG.info("signed in to %r (api %s)", site.site, site.version)

    survey = json.loads(args.survey.read_text(encoding="utf-8")) if args.survey else None
    if survey is None:
        LOG.warning("no --survey: migration ORDER will be reported as unknown, never as none")

    started = time.perf_counter()
    raw = collect(site, survey)
    assembled = assemble(raw, args.coverage_target)
    site.sign_out()

    args.out.mkdir(parents=True, exist_ok=True)
    db = write_store(args.out, raw, assembled)
    report = args.out / "report.md"
    report.write_text(render_report(assembled, raw, args.coverage_target), encoding="utf-8")
    (args.out / "assessment.json").write_text(json.dumps(assembled, indent=2) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for row in assembled["workbooks"]:
        counts[row["tier"]] = counts.get(row["tier"], 0) + 1
    LOG.info(
        "\n%d workbook(s) in %.0fs, %d re-auth(s)",
        len(assembled["workbooks"]),
        time.perf_counter() - started,
        site.reauths,
    )
    LOG.info("  tiers: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    LOG.info("  %s", db)
    LOG.info("  %s", report)
    return 0 if assembled["workbooks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
