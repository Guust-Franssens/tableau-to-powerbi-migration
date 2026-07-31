"""
purpose: preflight the DATA-SOURCE CREDENTIAL gate for a Tableau -> Power BI migration.

         Two checks, matching how a real migration should gate on credentials:
           1. classify (offline): read a migration-spec.json and decide, per data source, whether
              Power BI will need a bound CONNECTION + CREDENTIAL (live DB source) or not (extract /
              flat file, which the toolkit materialises to CSV under a DataFolder).
           2. gate (service): given a published semantic model, read its datasources and trigger a
              bounded refresh. If it fails with `ModelRefreshFailed_CredentialsNotSpecified`, the
              credential has NOT been configured yet -> STOP and prompt the user before proceeding.

         Why this exists: unlike flat files, a live source (Databricks, SQL Server, Snowflake, ...)
         has NO credential in the committed model files. The credential lives server-side (a Fabric
         connection / gateway datasource) and is normally entered once by the user in the UI. An agent
         cannot replicate the user's locally-cached Desktop credential, so for live sources the
         migration must verify connectivity and prompt the user if it is missing.

usage:   python scripts/preflight_source_credentials.py --spec migrations/workbooks/<slug>/migration-spec.json
         python scripts/preflight_source_credentials.py --model "<Workspace>" "<SemanticModel>"

The service gate shells out to the Fabric CLI (`fab`), which must be authenticated (`fab auth status`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("preflight_source_credentials")

# Tableau connection `class` values that are LIVE databases: Power BI needs a connection + credential.
LIVE_DB_CLASSES = {
    "databricks",
    "spark",
    "hive",
    "snowflake",
    "redshift",
    "awsathena",
    "presto",
    "sqlserver",
    "azure-sql-dw",
    "azuresynapse",
    "postgres",
    "mysql",
    "oracle",
    "teradata",
    "vertica",
    "bigquery",
    "google-bigquery",
    "saphana",
    "db2",
    "netezza",
    "exasolution",
    "greenplum",
    "cloudfile",
    "webdata-direct",
}

# Tableau connection `class` values that resolve to a FLAT FILE / path source (no credential; the
# migration materialises these to CSV and binds a DataFolder parameter).
FLAT_FILE_CLASSES = {
    "textscan",
    "excel-direct",
    "excel",
    "msaccess",
    "json",
    "csv",
    "hyper",
    "dataengine",
}

# The exact service error code that means "no credential bound yet" (verified against a live refresh).
CREDENTIALS_NOT_SPECIFIED = "ModelRefreshFailed_CredentialsNotSpecified"


def classify_source(connection: dict) -> tuple[str, str]:
    """Return (verdict, reason) for one data source's connection dict from a migration-spec.

    verdict is one of: "no-creds", "needs-credential", "review".
    """
    klass = (connection.get("class") or "unknown").lower()
    mode = (connection.get("mode") or "live").lower()

    if mode == "extract":
        return "no-creds", f"extract-based ('{klass}' -> packaged .hyper); migrates to CSV, no credential"
    if klass in FLAT_FILE_CLASSES:
        return "no-creds", f"flat-file source ('{klass}'); path-based, no credential"
    if klass in LIVE_DB_CLASSES:
        return (
            "needs-credential",
            f"LIVE database ('{klass}' @ {credential_gate(connection)}); Power BI needs a bound connection "
            "+ credential",
        )
    return "review", f"unrecognised connection class '{klass}' (mode='{mode}'); review manually"


def credential_gate(connection: dict) -> str:
    """Return the key Power BI actually caches a credential under: host **plus** compute path.

    Verified first-hand (2026-07): re-pointing a model from one Databricks warehouse to a freshly
    created warehouse on the *same host* re-triggered the sign-in modal, because the new
    `/sql/1.0/warehouses/<id>` path had no cached credential. So counting "one live host = one
    credential" under-reports the work: each distinct (host, httpPath) pair is its own gate, locally
    in Desktop and again as its own Fabric cloud connection.
    """
    server = connection.get("server") or "?"
    http_path = connection.get("http_path")
    return f"{server}{http_path}" if http_path else server


def source_label(src: dict, index: int) -> str:
    """Human-readable name for a data source, using the keys migration-spec.json actually defines.

    The spec has `caption` / `internal_name` / `id` - never `name`. Reaching for `name` first meant
    every live source printed as `source[0]`, i.e. exactly where the user needs to know WHICH source
    to authenticate, the tool told them nothing.
    """
    conn = src.get("connection", {}) or {}
    return (
        src.get("caption") or src.get("internal_name") or src.get("id") or conn.get("hyper_file") or f"source[{index}]"
    )


def cmd_classify(spec_path: Path) -> int:
    """Classify every data source in a migration-spec.json for credential needs."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    sources = spec.get("data_sources", [])
    if not sources:
        log.info("No data_sources in %s", spec_path)
        return 0

    needs = 0
    review = 0
    gates: dict[str, list[str]] = {}
    log.info("Data-source credential preflight for %s", spec_path)
    for i, src in enumerate(sources):
        conn = src.get("connection", {}) or {}
        verdict, reason = classify_source(conn)
        name = source_label(src, i)
        marker = {"no-creds": "  OK ", "needs-credential": " !!! ", "review": "  ?  "}[verdict]
        log.info("%s %-32s %s", marker, str(name)[:32], reason)
        if verdict == "needs-credential":
            gates.setdefault(credential_gate(conn), []).append(str(name))
        needs += verdict == "needs-credential"
        review += verdict == "review"

    log.info("-" * 60)
    if needs:
        log.warning(
            "%d live data source(s) across %d DISTINCT credential gate(s) need a Power BI connection + "
            "credential BEFORE migration can validate against data.",
            needs,
            len(gates),
        )
        for gate, users in sorted(gates.items()):
            log.warning("  gate: %s   <- %s", gate, ", ".join(users))
        if len(gates) > 1:
            log.warning(
                "NOTE: a credential is cached per (host + compute path), NOT per host. Two sources on the "
                "same server but different warehouses are %d separate sign-ins locally and %d separate "
                "Fabric connections in the cloud - do not assume one covers the others.",
                len(gates),
                len(gates),
            )
        _print_remediation()
    else:
        log.info("No live sources: all extract/flat (CSV + DataFolder). No credential gate for this workbook.")
    if review:
        log.warning("%d source(s) need manual review (unrecognised class).", review)
    return 1 if needs else 0


def _fab(args: list[str]) -> str:
    """Run a `fab` command and return stdout (UTF-8), raising on non-zero exit."""
    proc = subprocess.run(
        ["fab", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"fab {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _fab_api(endpoint: str, method: str = "get", body: str | None = None) -> dict:
    """Call `fab api -A powerbi` and return the parsed JSON body."""
    args = ["api", "-A", "powerbi", "-X", method, endpoint]
    if body is not None:
        args += ["-i", body]
    out = _fab(args)
    start = out.find("{")
    return json.loads(out[start:]) if start >= 0 else {}


def cmd_gate(workspace: str, model: str) -> int:
    """Check a published model's datasources and refresh state; interpret the credential gate."""
    ws_id = _fab(["get", f"{workspace}.Workspace", "-q", "id"]).strip()
    ds_id = _fab(["get", f"{workspace}.Workspace/{model}.SemanticModel", "-q", "id"]).strip()
    log.info("workspace=%s  model=%s", ws_id, ds_id)

    sources = _fab_api(f"groups/{ws_id}/datasets/{ds_id}/datasources").get("text", {}).get("value", [])
    log.info("datasources: %d", len(sources))
    for src in sources:
        kind = (src.get("connectionDetails") or {}).get("kind", src.get("datasourceType"))
        bound = "bound" if src.get("gatewayId") else "unbound"
        log.info("  - %s (%s)", kind, bound)

    log.info("triggering a bounded refresh to probe credentials ...")
    _fab_api(f"groups/{ws_id}/datasets/{ds_id}/refreshes", method="post", body=_empty_body())
    status, error = _poll_refresh(ws_id, ds_id)
    log.info("refresh status: %s  error: %s", status, error or "<none>")

    if error and CREDENTIALS_NOT_SPECIFIED in error:
        log.warning("CREDENTIAL GATE HIT: %s", CREDENTIALS_NOT_SPECIFIED)
        _print_remediation()
        return 1
    if status == "Failed":
        log.warning("refresh failed for a non-credential reason; inspect the error above.")
        return 2
    if status == "Completed":
        log.info("refresh succeeded: credentials are configured; safe to proceed.")
        return 0
    # status is still 'Unknown' (in-progress) after the poll budget. This is NOT success: a cold
    # serverless warehouse (Databricks) can take longer than the poll window to finish its first
    # refresh, so treating 'Unknown' as green would be a false pass. Report it as inconclusive.
    log.warning(
        "refresh did not reach a terminal state within the poll window (status=%s). This is INCONCLUSIVE, "
        "not a pass -- a cold serverless source may still be starting. Re-run the gate; if it keeps timing "
        "out, verify the refresh in the service before proceeding.",
        status,
    )
    return 3


def _poll_refresh(ws_id: str, ds_id: str, attempts: int = 24, delay: int = 5) -> tuple[str, str]:
    """Poll the latest refresh until it leaves 'Unknown' (in-progress); return (status, error).

    The budget (attempts * delay, default 120s) is deliberately generous: a cold serverless warehouse
    (e.g. Databricks) can take over a minute to complete its first refresh, and a short window would
    time out with a still-'Unknown' status that must NOT be mistaken for success (see cmd_gate).
    """
    for _ in range(attempts):
        time.sleep(delay)
        latest = _fab_api(f"groups/{ws_id}/datasets/{ds_id}/refreshes").get("text", {}).get("value", [{}])[0]
        status = latest.get("status", "Unknown")
        if status != "Unknown":
            return status, latest.get("serviceExceptionJson", "")
    return "Unknown", ""


def _empty_body() -> str:
    path = Path(tempfile.gettempdir()) / "preflight_empty.json"
    path.write_text("{}", encoding="ascii")
    return str(path)


def _print_remediation() -> None:
    log.warning(
        "\nACTION REQUIRED before continuing the migration:\n"
        "  A live source has no Power BI credential yet. Configure it once, then re-run the gate:\n"
        "  Option A (UI): Power BI service > the semantic model > Settings > Data source credentials\n"
        "                 > Edit credentials > enter the token/login. Power BI stores it server-side.\n"
        "  Option B (API): create a Fabric cloud connection with the credential and bind the model:\n"
        "                 POST /v1/connections (connectivityType=ShareableCloud, the matching\n"
        "                 creationMethod + parameters, credentialType=Key/Basic/OAuth2), then\n"
        "                 POST /v1.0/myorg/groups/<ws>/datasets/<model>/Default.BindToGateway\n"
        "                 { gatewayObjectId=<connection.gatewayId>, datasourceObjectIds=[<connection.id>] }.\n"
        "  The agent cannot reuse a user's locally-cached Power BI Desktop credential, so this step is\n"
        "  the user's to complete (or the user must supply the secret for Option B)."
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--spec", type=Path, help="Path to a migration-spec.json (offline source classification)")
    group.add_argument(
        "--model",
        nargs=2,
        metavar=("WORKSPACE", "SEMANTIC_MODEL"),
        help="Workspace + semantic-model display names to probe the credential gate (needs fab auth)",
    )
    args = parser.parse_args(argv)

    if args.spec:
        return cmd_classify(args.spec.resolve())
    return cmd_gate(args.model[0], args.model[1])


if __name__ == "__main__":
    sys.exit(main())
