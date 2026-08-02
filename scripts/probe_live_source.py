"""
purpose: prove a live data source is actually reachable FROM POWER BI, by building a one-table
         probe model, refreshing it, and requiring a real row back.
usage:   python scripts/probe_live_source.py --spec <migration-spec.json> [--source-index 0]
                                             [--timeout-sec 180] [--keep]

Why this exists
---------------
The credential check it replaces was a **string classifier**: it read `connection.class` from the
spec, saw `databricks`, and declared "needs a credential, STOP". That fires identically whether or
not the credential works, so it could never distinguish "cannot connect" from "nobody asked". An
agent using it reported `CANNOT CONNECT` for a warehouse it had never contacted - a conclusion that
happened to be true only because nothing had been configured yet.

This asks the question instead of assuming the answer.

Why it cannot just run `SELECT 1` from a shell
----------------------------------------------
Because that tests the WRONG CREDENTIAL, and a false green light is worse than no test at all.

`databricks sql`, an ODBC call or an `az` token all authenticate as the *agent's* shell identity.
Power BI does not use any of them: it uses a credential cached per-Windows-user in Power BI
Desktop's DPAPI store, keyed by data source, and there is no `powerbi test-connection` verb that
reaches it. Measured 2026-08-01: the `databricks` CLI could query the warehouse happily while Power
BI had never authenticated to it at all.

So the probe has to go *through* Power BI, and the smallest thing Power BI can execute is a model.
Hence: one table, `Table.FirstN(..., 1)`, refresh, require a row. That IS the `SELECT 1` - it just
has to be spelled as a partition instead of a shell command.

Outcomes (last line, machine-readable; exit 0 only on DATA_OK)
-------------------------------------------------------------
    PROBE: DATA_OK <n> row(s) from <table>     the source is genuinely reachable, build for real
    PROBE: SKIPPED <reason>                    not a live source - nothing to prove
    PROBE: NO_CREDENTIAL <detail>              a human must sign in; no retry can fix this
    PROBE: UNREACHABLE <detail>                refresh failed for a non-credential reason
    PROBE: ERROR <detail>                      the probe itself could not run
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("probe_live_source")

SKILL_SCRIPTS = Path(__file__).parent.parent / ".github" / "skills" / "pbip-model-refresh" / "scripts"

# A credential block does not surface as a clean "auth failed". Measured: the mashup engine raises
# the credential exception and then crashes posting it back over the named pipe, so the client sees
# a socket error and the real text is destroyed in transit. These fragments are what actually
# reaches us, across both the clean and the crashed path.
_SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
_PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"
)

CREDENTIAL_MARKERS = (
    "credential",
    "sign in",
    "signed in",
    "authentication",
    "unauthorized",
    "access token",
    "forbidden",
    "login",
    "oauth",
    "10054",
    "forcibly closed",
    "unrecognizable response",
)


def _pbip_files(name: str, m_query: str, table: str, column: str) -> dict[str, str]:
    """The minimum PBIP that Power BI Desktop will open: one table, one column, one partition."""
    indented = "\n".join("\t\t\t\t" + line for line in m_query.split("\n"))
    return {
        f"{name}.pbip": json.dumps(
            {"version": "1.0", "artifacts": [{"report": {"path": f"{name}.Report"}}]},
            indent=2,
        ),
        f"{name}.SemanticModel/.platform": json.dumps(
            {
                "$schema": _PLATFORM_SCHEMA,
                "metadata": {"type": "SemanticModel", "displayName": name},
                "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
            },
            indent=2,
        ),
        f"{name}.SemanticModel/definition.pbism": json.dumps({"version": "4.0", "settings": {}}, indent=2),
        f"{name}.SemanticModel/definition/database.tmdl": "database\n\tcompatibilityLevel: 1567\n",
        f"{name}.SemanticModel/definition/model.tmdl": (
            "model Model\n"
            "\tculture: en-US\n"
            "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
            "\tsourceQueryCulture: en-US\n\n"
            f"ref table {table}\n"
        ),
        f"{name}.SemanticModel/definition/tables/{table}.tmdl": (
            f"table {table}\n\n"
            f"\tcolumn {column}\n"
            "\t\tdataType: string\n"
            f"\t\tlineageTag: {uuid.uuid4()}\n"
            "\t\tsummarizeBy: none\n"
            f"\t\tsourceColumn: {column}\n\n"
            f"\tpartition {table} = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            f"{indented}\n"
        ),
        f"{name}.Report/.platform": json.dumps(
            {
                "$schema": _PLATFORM_SCHEMA,
                "metadata": {"type": "Report", "displayName": name},
                "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
            },
            indent=2,
        ),
        f"{name}.Report/definition.pbir": json.dumps(
            {"version": "4.0", "datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}}},
            indent=2,
        ),
        f"{name}.Report/definition/version.json": json.dumps(
            {
                "$schema": _SCHEMA_BASE + "versionMetadata/1.0.0/schema.json",
                "version": "4.0",
            },
            indent=2,
        ),
        f"{name}.Report/definition/report.json": json.dumps(
            {
                "$schema": _SCHEMA_BASE + "report/1.0.0/schema.json",
                "themeCollection": {},
                "layoutOptimization": "None",
                "resourcePackages": [],
                "reportVersionAtImport": "5.55",
            },
            indent=2,
        ),
        f"{name}.Report/definition/pages/pages.json": json.dumps(
            {
                "$schema": _SCHEMA_BASE + "pagesMetadata/1.0.0/schema.json",
                "pageOrder": ["p"],
                "activePageName": "p",
            },
            indent=2,
        ),
        f"{name}.Report/definition/pages/p/page.json": json.dumps(
            {
                "$schema": _SCHEMA_BASE + "page/1.0.0/schema.json",
                "name": "p",
                "displayName": "Probe",
                "displayOption": "FitToPage",
                "height": 720,
                "width": 1280,
            },
            indent=2,
        ),
    }


def build_m_query(conn: dict, table: str, column: str) -> tuple[str, str]:
    """Return (m_query, note) for a one-row read of `table`.

    Names no secret: every connector below defers to Power BI's own credential store, which is the
    entire point - the probe must exercise the SAME credential path the real model will use, or it
    proves nothing about the real model.
    """
    klass = (conn.get("class") or "").lower()
    server = conn.get("server") or ""
    database = conn.get("database") or ""
    schema = conn.get("schema") or "default"

    if klass == "databricks":
        http_path = conn.get("http_path")
        if not http_path:
            raise ValueError(
                "Databricks source has no 'http_path' in the spec (the SQL warehouse path). "
                "Re-parse the workbook with the current parse_tableau.py, which captures it."
            )
        m = (
            "let\n"
            f'    Source = Databricks.Catalogs("{server}", "{http_path}", null),\n'
            f'    db = Source{{[Name="{database}",Kind="Database"]}}[Data],\n'
            f'    sch = db{{[Name="{schema}",Kind="Schema"]}}[Data],\n'
            f'    tbl = sch{{[Name="{table}",Kind="Table"]}}[Data],\n'
            f'    one = Table.FirstN(Table.SelectColumns(tbl, {{"{column}"}}), 1)\n'
            "in\n"
            "    one"
        )
        return m, f"Databricks {server}{http_path} :: {database}.{schema}.{table}"

    if klass in {"sqlserver", "azure_sql_dw", "azuresqldw"}:
        m = (
            "let\n"
            f'    Source = Sql.Database("{server}", "{database}"),\n'
            f'    tbl = Source{{[Schema="{schema}",Item="{table}"]}}[Data],\n'
            f'    one = Table.FirstN(Table.SelectColumns(tbl, {{"{column}"}}), 1)\n'
            "in\n"
            "    one"
        )
        return m, f"SQL Server {server} :: {database}.{schema}.{table}"

    if klass == "snowflake":
        m = (
            "let\n"
            f'    Source = Snowflake.Databases("{server}", "{conn.get("warehouse") or ""}"),\n'
            f'    db = Source{{[Name="{database}"]}}[Data],\n'
            f'    sch = db{{[Name="{schema}"]}}[Data],\n'
            f'    tbl = sch{{[Name="{table}"]}}[Data],\n'
            f'    one = Table.FirstN(Table.SelectColumns(tbl, {{"{column}"}}), 1)\n'
            "in\n"
            "    one"
        )
        return m, f"Snowflake {server} :: {database}.{schema}.{table}"

    raise ValueError(f"no probe connector for connection class '{klass}' - add one in build_m_query()")


def _classify_failure(text: str) -> tuple[str, str]:
    """Map a refresh failure to NO_CREDENTIAL vs UNREACHABLE.

    The distinction is the point of the whole script: NO_CREDENTIAL is final and needs a human, while
    UNREACHABLE may be transient and is worth one retry. Getting it backwards either stalls forever
    on something only a person can fix, or gives up on a warehouse that was merely cold-starting.
    """
    low = text.lower()
    # "no catalog" reaching here means the model failed to load DESPITE the readiness wait, so it is
    # a genuine load failure rather than the race it used to be confused with.
    if "no catalog" in low or "no model folder resolved" in low:
        return (
            "UNREACHABLE",
            "the probe model failed to load even after waiting - the data source did not resolve. "
            "Check server and http_path in the spec before treating this as a credential problem. "
            "Raw: " + text[-200:],
        )
    if any(marker in low for marker in CREDENTIAL_MARKERS):
        return "NO_CREDENTIAL", text
    return "UNREACHABLE", text


def _npx(args: list[str], timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        ["npx", "--yes", "@microsoft/powerbi-desktop-bridge-cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _desktop_pids() -> set[int]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "(Get-Process PBIDesktop* -EA SilentlyContinue).Id"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {int(line) for line in proc.stdout.split() if line.strip().isdigit()}


def _close(pid: int) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -EA SilentlyContinue"],
        capture_output=True,
        check=False,
    )


def _resolve_probe_target(spec_path: Path, source_index: int) -> tuple[dict, str, str] | None:
    """Pick the source, table and column to probe. Returns None when there is nothing to probe."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    sources = spec.get("data_sources", [])
    if source_index >= len(sources):
        log.error("PROBE: ERROR source index %d out of range (%d sources)", source_index, len(sources))
        raise SystemExit(1)
    source = sources[source_index]
    conn = source.get("connection", {}) or {}

    if (conn.get("powerbi_target") or "") != "live_source":
        # SKIPPED, not DATA_OK. Exit 0 either way, but the verdicts mean different things - "nothing
        # to prove" is not "proven reachable" - and conflating two verdicts into one word is exactly
        # the defect class this script exists to fix. An orchestrator reading the last line would
        # otherwise report a CSV source as a verified live connection.
        log.info("PROBE: SKIPPED not a live source ('%s') - nothing to probe", conn.get("powerbi_target"))
        return None

    tables = source.get("tables") or []
    fields = [f for f in source.get("fields", []) if f.get("kind") == "column"]
    if not tables or not fields:
        log.error("PROBE: ERROR source has no table/column to probe")
        raise SystemExit(1)
    return conn, tables[0]["name"], fields[0]["internal_name"].strip("[]")


def _write_probe_model(spec_path: Path, m_query: str, table: str, column: str) -> Path:
    """Materialise the one-table probe PBIP in the migration's `_probe/` sandbox.

    Deliberately a SIBLING of `fabric/`, never a child: the credential gate denies writes to
    `fabric/` and that deny is inherited, so a probe inside it is blocked by the very gate the probe
    exists to satisfy. Keeping the sandbox outside the denied tree needs no grant, no ordering, and
    no heal path. See `credential_gate.probe_dir`.
    """
    probe_root = spec_path.parent / "_probe"
    probe_root.mkdir(parents=True, exist_ok=True)
    for rel, content in _pbip_files("Probe", m_query, table, column).items():
        target = probe_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return probe_root


def _open_desktop(pbip: Path) -> int:
    """Open the probe in Desktop and return OUR instance's pid.

    Resolved by set difference rather than by trusting what `open` reports: measured, with two
    instances running it returned a SIBLING agent's pid, and binding to that would refresh somebody
    else's model while every downstream signal still looked healthy.
    """
    before = _desktop_pids()
    code, out = _npx(["open", str(pbip), "--timeout", "120"], timeout=240)
    if code != 0:
        log.error("PROBE: ERROR could not open Power BI Desktop: %s", out.strip()[:300])
        raise SystemExit(1)
    for _ in range(20):
        new = _desktop_pids() - before
        if new:
            return new.pop()
        time.sleep(2)
    log.error("PROBE: ERROR Desktop did not start a new instance")
    raise SystemExit(1)


def _wait_for_catalog(pid: int, timeout_sec: int = 90) -> bool:
    """Block until Desktop has actually loaded a model catalog, or give up.

    `powerbi-desktop open` waits for the BRIDGE to answer, which happens well before the model is
    loaded into the Analysis Services instance. Refreshing in that window fails with "no catalog
    found on the Desktop Analysis Services instance" - not a connection error at all, but a race.

    That race cost a wrong conclusion, which is why this exists: an unresolvable host produced the
    same "no catalog" message as a merely-slow load, so a bad-host test appeared to pass while a
    known-good source intermittently failed. Two very different causes were indistinguishable.
    Waiting removes the race, so anything still reporting "no catalog" afterwards genuinely failed
    to load and can be classified honestly.

    Uses the skill's documented CLI rather than importing its internals - the readiness signal is
    "does this stop saying no catalog", which its exit output already carries.
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [sys.executable, str(SKILL_SCRIPTS / "probe_desktop_query.py"), "--pid", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if "no catalog" not in (proc.stdout + proc.stderr).lower():
            return True
        time.sleep(3)
    return False


def _refresh_and_classify(pid: int, table: str, timeout_sec: int) -> int:
    """Refresh the probe table and turn the result into a verdict.

    Deliberately does NOT lift the gate - the caller does that, and only once EVERY live source has
    passed. Lifting per-source would re-open the multi-source hole this was written to close.

    YOU run the clock. Measured: a credential-blocked refresh sails past the XMLA CommandTimeout,
    because the mashup engine is parked on a modal in another process that the server cannot
    preempt. Only this outer bound reliably ends it.
    """
    started = time.monotonic()
    try:
        refresh = subprocess.run(
            [
                sys.executable,
                str(SKILL_SCRIPTS / "refresh_pbip_model.py"),
                "--pid",
                str(pid),
                "--tables",
                table,
                "--no-save",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.error(
            "PROBE: NO_CREDENTIAL refresh did not return within %ds. A refresh that HANGS (rather "
            "than failing) is the shape of Power BI Desktop waiting on a sign-in dialog that only a "
            "human can answer.",
            timeout_sec,
        )
        return 1

    log.info("refresh finished in %.0fs", time.monotonic() - started)
    text = (refresh.stdout + refresh.stderr).strip()
    if "DATA_OK" in text:
        log.info("PROBE: DATA_OK 1 row(s) from %s - the source is genuinely reachable", table)
        return 0
    verdict, detail = _classify_failure(text)
    log.error("PROBE: %s %s", verdict, detail[-400:] or "refresh returned no data")
    return 1


def _lift_gate(migration: Path, what: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "credential_gate.py"),
            "clear",
            str(migration),
            "--reason",
            f"probe-cleared: DATA_OK from {what}",
            "--earned",
        ],
        capture_output=True,
        check=False,
    )


def _host_resolves(server: str) -> bool:
    """Cheap DNS check, run before any Desktop work.

    Load-bearing for the taxonomy, not just an optimisation. Measured 2026-08-02: an unresolvable
    host loads into Desktop perfectly happily - the M query is not evaluated at load time - and then
    the refresh HANGS exactly like a missing credential does, for the full timeout. So "a hang means
    a sign-in modal" is only true once the host is known to resolve. Checking here separates the two
    causes definitively, and turns a 200-second wrong answer into a sub-second right one.
    """
    try:
        socket.getaddrinfo(server, None)
        return True
    except (socket.gaierror, UnicodeError, ValueError):
        return False


def run_probe(spec_path: Path, source_index: int | None, timeout_sec: int, keep: bool) -> int:
    """Probe every live source (or one, if `source_index` is given). Gate lifts only if ALL pass.

    Probing a single source was a real hole, not a convenience limit: the guarantee is "no model for
    a source you never reached", and it is plural. With `--source-index 0` as the default, a
    two-source workbook whose first source worked would lift the gate and build against a second
    source nobody had ever contacted - precisely the failure this tool exists to prevent, just
    harder to see.
    """
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    sources = spec.get("data_sources", [])
    live = [
        i
        for i, s in enumerate(sources)
        if ((s.get("connection", {}) or {}).get("powerbi_target") or "") == "live_source"
    ]
    if source_index is not None:
        live = [source_index]
    if not live:
        log.info("PROBE: SKIPPED no live sources in this spec - nothing to probe")
        return 0

    log.info("probing %d live source(s): %s", len(live), live)
    for idx in live:
        rc = _probe_one(spec_path, idx, timeout_sec, keep)
        if rc != 0:
            log.error("PROBE: source index %d failed - not lifting the gate", idx)
            return rc

    _lift_gate(spec_path.parent, f"{len(live)} live source(s)")
    log.info("PROBE: DATA_OK all %d live source(s) reachable", len(live))
    return 0


def _probe_one(spec_path: Path, source_index: int, timeout_sec: int, keep: bool) -> int:
    """Build and run the one-table probe for a single data source."""
    target = _resolve_probe_target(spec_path, source_index)
    if target is None:
        return 0
    conn, table, column = target

    server = conn.get("server") or ""
    if server and not _host_resolves(server):
        log.error(
            "PROBE: UNREACHABLE '%s' does not resolve in DNS. This is a spec/config problem, not a "
            "credential one - check `server` (and `http_path`) in migration-spec.json. No credential "
            "will fix an address that does not exist.",
            server,
        )
        return 1

    try:
        m_query, note = build_m_query(conn, table, column)
    except ValueError as exc:
        log.error("PROBE: ERROR %s", exc)
        return 1

    probe_root = _write_probe_model(spec_path, m_query, table, column)
    log.info("probe model built: %s", probe_root)
    log.info("target: %s", note)

    pid = None
    try:
        pid = _open_desktop(probe_root / "Probe.pbip")
        log.info("desktop pid %d", pid)
        if not _wait_for_catalog(pid):
            log.error(
                "PROBE: UNREACHABLE Power BI Desktop never finished loading the probe model. After "
                "waiting, the Analysis Services instance still has no catalog - the data source did "
                "not resolve (unknown host, wrong HTTP path, or no network route). Check server and "
                "http_path in the spec before treating this as a credential problem."
            )
            return 1
        log.info("model loaded - refreshing")
        return _refresh_and_classify(pid, table, timeout_sec)
    finally:
        if pid and not keep:
            _close(pid)
            log.info("closed desktop pid %d", pid)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument(
        "--source-index", type=int, default=None, help="probe only this source (default: all live sources)"
    )
    parser.add_argument("--timeout-sec", type=int, default=180)
    parser.add_argument("--keep", action="store_true", help="leave Desktop open for inspection")
    args = parser.parse_args(argv)
    if not args.spec.is_file():
        log.error("PROBE: ERROR no such spec: %s", args.spec)
        return 1
    return run_probe(args.spec, args.source_index, args.timeout_sec, args.keep)


if __name__ == "__main__":
    sys.exit(main())
