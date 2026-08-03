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

usage:   python scripts/preflight_source_credentials.py --spec migrations/<slug>/migration-spec.json
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
        server = connection.get("server") or "?"
        return (
            "needs-credential",
            f"LIVE database ('{klass}' @ {server}); Power BI needs a bound connection + credential",
        )
    return "review", f"unrecognised connection class '{klass}' (mode='{mode}'); review manually"


def cmd_classify(spec_path: Path) -> int:
    """Classify every data source in a migration-spec.json for credential needs."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    sources = spec.get("data_sources", [])
    if not sources:
        log.info("No data_sources in %s", spec_path)
        return 0

    needs = 0
    review = 0
    log.info("Data-source credential preflight for %s", spec_path)
    for i, src in enumerate(sources):
        conn = src.get("connection", {}) or {}
        verdict, reason = classify_source(conn)
        name = src.get("name") or conn.get("hyper_file") or f"source[{i}]"
        marker = {"no-creds": "  OK ", "needs-credential": " !!! ", "review": "  ?  "}[verdict]
        log.info("%s %-28s %s", marker, str(name)[:28], reason)
        needs += verdict == "needs-credential"
        review += verdict == "review"

    log.info("-" * 60)
    if needs:
        # Order is load-bearing: the STOP directive goes FIRST. Measured 2026-08-01, an agent hit
        # this gate, paused correctly, marked its own todo blocked - then overrode itself with
        # "applying the non-interactive fallback ... rather than leaving the requested artifacts
        # unbuilt", and shipped a model for a warehouse it never contacted. Leading with remediation
        # invites exactly that reading ("the human fixes it later, I continue now"). Persona prose
        # already said "Unconditional" and lost, so the imperative lives here, in tool output, which
        # agents follow far more literally than their own instructions.
        _print_stop_directive(needs)
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
    log.info("refresh succeeded: credentials are configured; safe to proceed.")
    return 0


def _poll_refresh(ws_id: str, ds_id: str, attempts: int = 12, delay: int = 5) -> tuple[str, str]:
    """Poll the latest refresh until it leaves 'Unknown' (in-progress); return (status, error)."""
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


def _print_stop_directive(needs: int) -> None:
    """Print the agent-facing PROOF-REQUIRED directive.

    This is printed FIRST, before any remediation detail, and restated at the end. Both placements
    are deliberate: a shell tool's output is often truncated in the middle (the failed run showed
    the whole gate collapsed to "17 lines"), so an imperative that appears only at the bottom can be
    cut before the agent ever reads it. Leading with it also sets the frame - remediation steps read
    as "the user will handle this later" when they come first.

    This directive must NOT tell the agent to terminate. It once did, and that was a defect
    (measured 2026-08-02): this script is a *static classifier* - it opens no socket and cannot know
    whether a credential exists - yet it fired an unconditional "STOP - A HUMAN MUST ACT / TERMINATE
    THE RUN NOW" for every live source. Agents obeyed it literally: 10 of 15 models never reached
    `probe_live_source.py` at all, and `claude-opus-5` refused a FULLY CREDENTIALED, reachable
    warehouse on the happy path - a migration that would have succeeded in seconds.

    That is the prose analogue of the v1 filesystem deadlock: the step that decides was never
    allowed to run. The terminal "a human must act" wording now lives in the PROBE, the only
    component that actually knows. Here we say: do not build, and go measure.
    """
    log.warning(
        "\n"
        "################################################################\n"
        "#  PROOF REQUIRED - %d live source(s) UNPROVEN. Do NOT build.\n"
        "################################################################\n"
        "  DIRECTIVE TO THE AGENT READING THIS - NOT ADVISORY, NOT A WARNING:\n"
        "    1. You may NOT build the semantic model or the report YET. TMDL for an\n"
        "       unreachable source is byte-identical to TMDL that works, so building now\n"
        "       hands over something indistinguishable from a correct model that nobody\n"
        "       can tell is broken.\n"
        "    2. This check opened NO connection. It is a classifier, not a test. It does\n"
        "       NOT know whether a credential exists, so it is NOT telling you to stop.\n"
        "    3. Your NEXT action is the measurement. Run it now:\n"
        "         python scripts/probe_live_source.py --spec <this spec>\n"
        "       It builds a one-table model, opens Power BI Desktop, and reads one real\n"
        "       row. It lifts the gate itself on DATA_OK.\n"
        "\n"
        "  DO NOT stop and ask the user yet. A verdict of 'only a human can fix this' is the\n"
        "  PROBE's to give, not this script's - and only the probe can tell a missing credential\n"
        "  (a human IS needed) from a wrong hostname (nobody needs to sign in). Reporting\n"
        "  'credential required' without probing sends the user hunting for a sign-in they\n"
        "  may not need, and refuses migrations that would have worked.\n"
        "\n"
        "  Then let the RESULT decide:\n"
        "    DATA_OK        -> gate lifts automatically; build for real.\n"
        "    NO_CREDENTIAL  -> STOP and ask a human. No retry can conjure a credential.\n"
        "    UNREACHABLE    -> STOP; report the address/network fault. Do not build.\n"
        "\n"
        "  'Defer validation' is a choice only the USER can make, AFTER a probe result.\n"
        "  It NEVER means 'skip the test and build anyway'.\n"
        "################################################################",
        needs,
    )


def _print_remediation() -> None:
    log.warning(
        "\nIF the probe comes back NO_CREDENTIAL, this is how a human fixes it:\n"
        "  Option A (UI): Power BI service > the semantic model > Settings > Data source credentials\n"
        "                 > Edit credentials > enter the token/login. Power BI stores it server-side.\n"
        "                 Locally: sign in interactively once in Power BI Desktop.\n"
        "  Option B (API): create a Fabric cloud connection with the credential and bind the model:\n"
        "                 POST /v1/connections (connectivityType=ShareableCloud, the matching\n"
        "                 creationMethod + parameters, credentialType=Key/Basic/OAuth2), then\n"
        "                 POST /v1.0/myorg/groups/<ws>/datasets/<model>/Default.BindToGateway\n"
        "                 { gatewayObjectId=<connection.gatewayId>, datasourceObjectIds=[<connection.id>] }.\n"
        "  The agent cannot reuse a user's locally-cached Power BI Desktop credential, so this step is\n"
        "  the user's to complete (or the user must supply the secret for Option B).\n"
        "\n"
        "============================================================\n"
        "REMINDER (see the full directive above): do NOT build yet, and do NOT\n"
        "  report a credential problem yet either - none has been observed. This\n"
        "  check never opened a connection. Run the probe; it decides:\n"
        "      python scripts/probe_live_source.py --spec <this spec>\n"
        "============================================================"
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
