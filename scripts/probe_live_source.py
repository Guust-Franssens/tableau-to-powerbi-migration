"""
purpose: prove a live data source is actually reachable FROM POWER BI, by building a one-table
         probe model, refreshing it, and requiring a real row back.
usage:   python scripts/probe_live_source.py --spec <migration-spec.json> [--source-index 0]
         python scripts/probe_live_source.py --bundle <engine-output-dir> [--source-index 0]
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

Why not an M native query either
--------------------------------
`Value.NativeQuery(db, "SELECT 1")` looks strictly better - no dependency on a real table, so no
false failure from a wrong table name in the spec. It is rejected for two measured reasons:

1. **Desktop raises its own approval modal for native queries.** That is a second human-in-the-loop
   dependency, on the one code path whose whole job is to tell "needs a human" apart from
   "reachable". It would hang and land on a false NO_CREDENTIAL.
2. **It may not exercise the same credential path.** Power BI can key credentials per connector
   function, so a native query passing would not prove the model the builder generates can connect.

The probe mirrors the builder on purpose: `pbi-semantic-builder` is instructed to emit
`Databricks.Catalogs(host, httpPath, ...)` / `Sql.Database(server, db)`, and `build_m_query` below
uses exactly those. A pass therefore predicts the real model rather than merely resembling it. If the
builder's connector shape changes, change this with it - the alignment is the point, not a detail.

Outcomes (last line, machine-readable; exit 0 only on DATA_OK)
-------------------------------------------------------------
    PROBE: DATA_OK <n> row(s) from <table>     the source is genuinely reachable, build for real
    PROBE: SKIPPED <reason>                    not a live source - nothing to prove
    PROBE: NO_CREDENTIAL <detail>              a human must sign in; no retry can fix this
    PROBE: ACCESS_DENIED <detail>              permissions must change; signing in again is not enough
    PROBE: UNREACHABLE <detail>                refresh failed for a non-credential reason
    PROBE: ERROR <detail>                      the probe itself could not run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from migration_bundle import load_bundle

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

# A "table not found" is a SPEC error, not a reachability one - the connection plainly worked well
# enough for the server to tell us the object is missing. Classifying it as UNREACHABLE would send a
# user hunting for a sign-in they do not need, which is the same mistake as the bad-host case.
BAD_TABLE_MARKERS = (
    "not found",
    "does not exist",
    "cannot be found",
    "invalid object name",
    "table_or_view_not_found",
    "unknown table",
    "no such table",
)

CREDENTIAL_MARKERS = (
    "credential",
    "sign in",
    "signed in",
    "authentication",
    "unauthorized",
    "access token",
    "login",
    "oauth",
    "10054",
    "forcibly closed",
    "unrecognizable response",
)

ACCESS_DENIED_MARKERS = (
    "403",
    "forbidden",
    "access denied",
    "permission denied",
    "insufficient privilege",
    "insufficient privileges",
    "does not have permission",
    "not authorized",
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
        f"{name}.SemanticModel/definition/database.tmdl": (
            # 1702, never lower. Measured 2026-08-03 (a real Power BI Desktop crash, "Frown"
            # feedback): TOM refuses to load a model that requests a LOWER compatibilityLevel than
            # whatever Desktop's current AS instance already has cached ("Tabular databases do not
            # support CompatibilityLevel downgrade"). This template used 1567 - a value that does
            # not appear ANYWHERE else in this repo's real migrations, and is lower even than the
            # 1606 that triggered the crash. This repo's own documented convention (superstore-
            # sales-performance/migration-spec.json) is 1702+ for newly created models; matching it
            # here means the probe's throwaway model can only ever be requesting an UPGRADE
            # relative to whatever baseline Desktop already initialized, never a downgrade.
            "database\n\tcompatibilityLevel: 1702\n"
        ),
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


def normalize_host(server: str) -> str:
    """Reduce a spec's `server` to the bare host Power BI connectors and DNS both expect.

    Load-bearing, not cosmetic. A Snowflake account is routinely written as a URL
    (`https://ORG-ACCOUNT.snowflakecomputing.com/`), and Tableau/`.env`/hand-written specs all carry
    that form. Passing it through un-normalized breaks BOTH consumers in the same run: the DNS
    pre-check fails to resolve `https://host/`, so a perfectly good account is reported UNREACHABLE
    - the exact misdiagnosis class this script exists to prevent - and `Snowflake.Databases` would
    reject it anyway. Strip the scheme, any path, and a trailing dot/slash.
    """
    host = (server or "").strip()
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", host, flags=re.IGNORECASE)
    host = host.split("/", 1)[0].split("?", 1)[0]
    return host.rstrip(".").strip()


def build_m_query(conn: dict, table: str, column: str) -> tuple[str, str]:
    """Return (m_query, note) for a one-row read of `table`.

    Names no secret: every connector below defers to Power BI's own credential store, which is the
    entire point - the probe must exercise the SAME credential path the real model will use, or it
    proves nothing about the real model.
    """
    klass = (conn.get("class") or "").lower()
    server = normalize_host(conn.get("server") or "")
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
        warehouse = (conn.get("warehouse") or "").strip()
        if not warehouse:
            raise ValueError(
                "Snowflake source has no 'warehouse' in the spec. Snowflake cannot execute a query "
                "without a compute warehouse, so the probe would fail for a reason unrelated to "
                "reachability. Re-parse the workbook with the current parse_tableau.py, which "
                "captures it, or add `warehouse` to the source's connection block."
            )
        role = (conn.get("role") or "").strip()
        options = f'[Role="{role}"]' if role else "null"
        m = (
            "let\n"
            f'    Source = Snowflake.Databases("{server}", "{warehouse}", {options}),\n'
            f'    db = Source{{[Name="{database}",Kind="Database"]}}[Data],\n'
            f'    sch = db{{[Name="{schema}",Kind="Schema"]}}[Data],\n'
            f'    tbl = sch{{[Name="{table}",Kind="Table"]}}[Data],\n'
            f'    one = Table.FirstN(Table.SelectColumns(tbl, {{"{column}"}}), 1)\n'
            "in\n"
            "    one"
        )
        return m, f"Snowflake {server} ({warehouse}) :: {database}.{schema}.{table}"

    raise ValueError(f"no probe connector for connection class '{klass}' - add one in build_m_query()")


def _error_excerpt(text: str, limit: int = 1200) -> str:
    """Keep the exception message head; stack traces grow at the tail."""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "\n... [truncated after the message head]"


def _classify_failure(text: str, network_fault_observed: bool) -> tuple[str, str]:  # noqa: PLR0911
    """Map a refresh failure to a verdict.

    The distinction is the point of the whole script: NO_CREDENTIAL is final and needs a human, while
    UNREACHABLE may be transient and is worth one retry. Getting it backwards either stalls forever
    on something only a person can fix, or gives up on a warehouse that was merely cold-starting.

    ERROR is checked FIRST, and it is not a verdict about the source at all - it means the probe
    could not run, so nothing was learned. Measured 2026-08-02 in a serial sweep: two of six runs on
    an identical, known-good address reported UNREACHABLE because the refresh returned
    "model identity unverified / no catalog found on the Desktop Analysis Services instance". That is
    the pid-binding guard refusing to touch a Desktop instance it could not confirm was ours - a
    LOCAL tooling failure - but "no catalog" matched the load-failure branch and came out as a
    confident claim about the customer's data source, telling them to fix a server and http_path that
    were correct. Same conflation this whole script exists to prevent: "I could not measure" is not
    "I measured, and your source is broken".

    One branch per verdict, and the ORDER is load-bearing - ERROR before load-failure before
    BAD_TABLE before NO_CREDENTIAL - because each later marker also appears in the earlier cases'
    text. A dispatch table would hide exactly that.
    """
    # pylint: disable=too-many-return-statements
    low = text.lower()
    raw = _error_excerpt(text)
    # Deliberately "identity unverified" alone, NOT "model identity unverified". Measured
    # 2026-08-03 (gpt-5.6-sol, live happy-path run): the real producer text is
    # "model  : identity unverified (no model folder resolved for this pid)" - note the extra
    # spaces and COLON between "model" and "identity", from the caller's own print formatting.
    # The stricter substring never matched, so this exact failure fell through to the "no catalog"
    # branch below and came out as a confident UNREACHABLE ("check server and http_path") against
    # a warehouse that was reachable seconds earlier and seconds later for sibling runs. The
    # invariant signal from the producer (github/skills/pbip-model-refresh/refresh_pbip_model.py)
    # is "identity unverified" on its own; "model" is decorative context whose punctuation varies.
    if "identity unverified" in low or "wrong_model" in low:
        return (
            "ERROR",
            "the probe could not confirm which Power BI Desktop instance it was bound to, so it "
            "never queried the source. This is a LOCAL tooling failure, not a fact about the data "
            "source - do not report a connection or credential problem from it. Re-run with the "
            "probe's exact PBIP path identifiable in Desktop Bridge status. Raw: " + raw,
        )
    # "no catalog" reaching here means the model failed to load DESPITE the readiness wait, so it is
    # a genuine load failure rather than the race it used to be confused with.
    if "no catalog" in low or "no model folder resolved" in low:
        if not network_fault_observed:
            return (
                "ERROR",
                "unclassified refresh/load failure: Power BI did not produce a model catalog, but "
                "the probe did not observe a network fault. Do not report this as UNREACHABLE or "
                "send anyone to fix server/http_path without stronger evidence. Raw: " + raw,
            )
        return (
            "UNREACHABLE",
            "the probe model failed to load even after waiting - the data source did not resolve. "
            "Check server and http_path in the spec before treating this as a credential problem. "
            "Raw: " + raw,
        )
    # Order matters: check BAD_TABLE before NO_CREDENTIAL. A "not found" message proves the server
    # answered us, so it can never be a credential problem, but the text often also mentions the
    # connection and would otherwise trip a credential marker.
    if any(marker in low for marker in BAD_TABLE_MARKERS):
        return "BAD_TABLE", text
    if any(marker in low for marker in ACCESS_DENIED_MARKERS):
        return "ACCESS_DENIED", text
    if any(marker in low for marker in CREDENTIAL_MARKERS):
        return "NO_CREDENTIAL", text
    return (
        "ERROR",
        "unclassified refresh failure: the probe ran, but the error did not match a credential, "
        "bad-table, or observed network fault signature. Do not report it as UNREACHABLE. Raw: " + raw,
    )


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


def _close(pid: int, pbip: Path) -> bool:
    """Close Desktop only if the PID still uniquely owns this probe PBIP."""
    if _matching_pids_for_file(pbip) != [pid]:
        log.error(
            "PROBE: ERROR not closing Desktop pid %d because bridge status no longer uniquely matches %s",
            pid,
            pbip,
        )
        return False
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -EA SilentlyContinue"],
        capture_output=True,
        check=False,
    )
    return True


def _resolve_probe_target(sources: list[dict], source_index: int) -> tuple[dict, list[str], str] | None:
    """Pick the source, its candidate tables and a column to probe. None when nothing to probe.

    Returns ALL tables, not just the first: a "table not found" is a spec error, and the workbook
    usually names several, so the probe can move on to the next rather than declaring the whole
    source unreachable over one bad name.
    """
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

    tables = [t["name"] for t in (source.get("tables") or []) if t.get("name")]
    fields = [f for f in source.get("fields", []) if f.get("kind") == "column"]
    if not tables or not fields:
        log.error("PROBE: ERROR source has no table/column to probe")
        raise SystemExit(1)
    return conn, tables, fields[0]["internal_name"].strip("[]")


def _write_probe_model(migration: Path, m_query: str, table: str, column: str) -> Path:
    """Materialise the one-table probe PBIP in the migration's `_probe/` sandbox.

    Deliberately a SIBLING of `fabric/`, never a child: the credential gate denies writes to
    `fabric/` and that deny is inherited, so a probe inside it is blocked by the very gate the probe
    exists to satisfy. Keeping the sandbox outside the denied tree needs no grant, no ordering, and
    no heal path. See `credential_gate.probe_dir`.
    """
    probe_root = migration / "_probe" / f"run-{uuid.uuid4().hex}"
    probe_root.mkdir(parents=True, exist_ok=True)
    for rel, content in _pbip_files("Probe", m_query, table, column).items():
        target = probe_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return probe_root


def _matching_pids_for_file(pbip: Path) -> list[int]:
    """Return Desktop PIDs whose bridge status exactly matches this PBIP path."""
    code, out = _npx(["status"], timeout=60)
    if code != 0:
        return []
    try:
        payload = json.loads(out[out.index("{") : out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    target = str(pbip.resolve()).casefold()
    instances = payload.get("instances") or payload.get("Instances") or []
    matches = []
    for inst in instances if isinstance(instances, list) else []:
        current = str(inst.get("currentFilePath") or inst.get("CurrentFilePath") or "").casefold()
        pid = inst.get("pid") or inst.get("Pid")
        if current == target and pid:
            matches.append(int(pid))
    return matches


def _pid_for_file(pbip: Path) -> int | None:
    """Find the Desktop instance that has OUR file open, via the bridge's own status report.

    Preferred over set-difference on the pid list, for two independent reasons:

    1. Set-difference is not concurrency-safe. Two probes starting together both snapshot an empty
       `before`, both see both new pids, and each pops an arbitrary one - so a probe can bind to a
       sibling's Desktop and refresh someone else's model while every downstream signal still looks
       healthy. That is the WRONG_MODEL hazard, and it is what kept the sweep serial.
    2. It is what the bridge actually offers: `status` reports `currentFilePath` per instance, so the
       binding can be *verified* rather than inferred. The gotchas skill states the rule directly -
       trust `status` + `currentFilePath`, never the pid `open` hands back.
    """
    matches = _matching_pids_for_file(pbip)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.error("PROBE: ERROR %d Desktop instances report the same probe PBIP path: %s", len(matches), pbip)
    return None


def _open_desktop(pbip: Path) -> int:
    """Open the probe in Desktop and return OUR instance's pid.

    Never trusts the pid `open` reports: measured, with two instances running it returned a SIBLING
    agent's pid, and binding to that would refresh somebody else's model while every downstream
    signal still looked healthy.
    """
    code, out = _npx(["open", str(pbip), "--timeout", "120"], timeout=240)
    if code != 0:
        log.error("PROBE: ERROR could not open Power BI Desktop: %s", out.strip()[:300])
        raise SystemExit(1)
    for _ in range(20):
        pid = _pid_for_file(pbip)
        if pid:
            return pid
        time.sleep(2)
    log.error(
        "PROBE: ERROR Desktop did not report an instance whose currentFilePath exactly matches %s",
        pbip,
    )
    raise SystemExit(1)


def _wait_for_catalog(pid: int, timeout_sec: int = 240) -> bool:
    """Block until Desktop has actually loaded a model catalog, or give up.

    `powerbi-desktop open` waits for the BRIDGE to answer, which happens well before the model is
    loaded into the Analysis Services instance. Refreshing in that window fails with "no catalog
    found on the Desktop Analysis Services instance" - not a connection error at all, but a race.

    That race cost a wrong conclusion, which is why this exists: an unresolvable host produced the
    same "no catalog" message as a merely-slow load, so a bad-host test appeared to pass while a
    known-good source intermittently failed. Two very different causes were indistinguishable.
    Waiting removes the race, so anything still reporting "no catalog" afterwards genuinely failed
    to load and can be classified honestly.

    ⚠️ The timeout is a FALSE-VERDICT budget, not a convenience. Timing out here reports
    UNREACHABLE - a definitive "your address is wrong" - so a value that is merely too short
    manufactures exactly the misdiagnosis this script exists to prevent. Measured 2026-08-02: at 90s
    the FIRST cell of a four-cell batch reported UNREACHABLE for a known-good Databricks warehouse
    that returned DATA_OK on the very next run of the identical spec. A cold Desktop start (or
    contention right after a sibling instance closed) simply exceeded the budget. Raised to 240s:
    the cost of waiting on a genuinely dead host is a couple of idle minutes, while the cost of
    being too eager is a confident wrong answer sending someone to fix an address that was fine.
    The DNS pre-check in `_probe_one` already catches the truly-unresolvable case in under a second,
    so almost nothing legitimate reaches this timeout anyway.

    Uses the skill's documented CLI rather than importing its internals - the readiness signal is
    "does this stop saying no catalog", which its exit output already carries.
    """
    deadline = time.monotonic() + timeout_sec
    waited = 0.0
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [sys.executable, str(SKILL_SCRIPTS / "probe_desktop_query.py"), "--pid", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if "no catalog" not in (proc.stdout + proc.stderr).lower():
            if waited:
                log.info("model catalog ready after %.0fs", waited)
            return True
        time.sleep(3)
        waited += 3
        # Surface a slow load rather than letting it look like a hang - the shared conventions
        # require reporting elapsed time on anything past ~60s.
        if waited % 60 == 0:
            log.info("still waiting for Desktop to load the probe model (%.0fs)", waited)
    return False


def _classify_catalog_timeout(conn: dict) -> str:
    """Classify a model-load timeout without guessing a network fault."""
    if _network_fault_observed(conn):
        log.error(
            "PROBE: UNREACHABLE Power BI Desktop never finished loading the probe model, "
            "and a DNS/TCP check also observed a network fault. Check server/http_path."
        )
        return "UNREACHABLE"
    log.error(
        "PROBE: ERROR Power BI Desktop never finished loading the probe model, but no "
        "network fault was observed. This is unclassified; do not report UNREACHABLE."
    )
    return "ERROR"


def _refresh_and_classify(pid: int, table: str, timeout_sec: int, network_fault_observed: bool) -> tuple[int, str]:
    """Refresh the probe table and turn the result into a verdict. Returns (exit code, verdict).

    Deliberately does NOT lift the gate - the caller does that, and only once EVERY live source has
    passed. Lifting per-source would re-open the multi-source hole this was written to close.

    YOU run the clock. Measured: a credential-blocked refresh sails past the XMLA CommandTimeout,
    because the mashup engine is parked on a modal in another process that the server cannot
    preempt. Only this outer bound reliably ends it.
    """
    # Announce the bound BEFORE the long wait. Measured 2026-08-03 (gpt-5.6-sol, unhappy path): the
    # agent applied AGENTS.md's "cap an unresponsive external system at ~2 minutes" rule to THIS
    # script, killed it at ~120s, and so recorded NO verdict at all - the gate held and nothing was
    # built, but the audit log could not show a probe had ever run. A sibling (claude-opus-5) let it
    # finish and got a proper NO_CREDENTIAL entry. The cap is a good rule aimed at an agent's own
    # unbounded waiting; it misfires here because this script IS the bounded timer. Saying so in the
    # output is what actually reaches the agent - the same reason the classifier's STOP directive
    # lives in tool output rather than in persona prose.
    log.info(
        "PROBE: refreshing (bounded: this script self-terminates at %ds and ALWAYS prints a verdict).\n"
        "  DO NOT kill this process to satisfy a 2-minute external-system cap - that cap is about "
        "YOUR OWN unbounded waiting, and this is the timer. A hang here IS the measurement: it is "
        "what a modal sign-in dialog looks like from outside. Killing it early destroys the evidence "
        "and records no verdict, which is strictly worse than waiting.",
        timeout_sec,
    )
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
        if network_fault_observed:
            log.error(
                "PROBE: UNREACHABLE refresh did not return within %ds, and a DNS/TCP check also "
                "observed a network fault. Do not classify this as a credential wall.",
                timeout_sec,
            )
            return 1, "UNREACHABLE"
        log.error(
            "PROBE: ERROR refresh did not return within %ds, but no network fault was observed. "
            "A timeout can be a transient source stall or a sign-in modal, so it is not enough "
            "evidence for final NO_CREDENTIAL.",
            timeout_sec,
        )
        return 1, "ERROR"

    log.info("refresh finished in %.0fs", time.monotonic() - started)
    text = (refresh.stdout + refresh.stderr).strip()
    if "DATA_OK" in text:
        log.info("PROBE: DATA_OK 1 row(s) from %s - the source is genuinely reachable", table)
        return 0, "DATA_OK"
    verdict, detail = _classify_failure(text, network_fault_observed=network_fault_observed)
    log.error("PROBE: %s %s", verdict, detail or "refresh returned no data")
    return 1, verdict


def _record_attempt(migration: Path, verdict: str, what: str) -> None:
    """Append the probe's verdict to the audit log, whatever it was.

    Without this the audit only ever recorded SUCCESS (`probe-cleared`), so "never measured" and
    "measured, and the source refused us" were indistinguishable after the fact - a real gap in the
    enforcement record, since the whole promise of the gate is that a decision came from a
    measurement.

    It also fixes a false negative measured 2026-08-02: the harness inferred "did this agent probe?"
    from the presence of the `_probe/` sandbox, but the shared conventions REQUIRE agents to clean up
    scratch directories. `claude-opus-5` probed correctly (136s, NO_CREDENTIAL, and even
    distinguished that from UNREACHABLE), deleted the sandbox as instructed, and was scored "never
    probed". Inferring behaviour from an artifact a well-behaved actor is told to remove punishes
    exactly the behaviour we want.

    ⚠️ CALL THIS THE INSTANT THE VERDICT IS KNOWN, never from an outer frame. It first lived in
    `run_probe`, one level up - which put it AFTER `_probe_one_table`'s `finally: _close(pid)`, a
    Desktop shutdown that takes tens of seconds. Measured 2026-08-03: `claude-opus-4.8` ran the
    probe against its own spec and reported NO_CREDENTIAL, yet its audit log held only `block`
    entries, because the run ended somewhere in that shutdown window. The measurement happened and
    left no trace, which for every later reader is indistinguishable from never having probed.
    Evidence written after slow cleanup is evidence you can lose.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from credential_gate import _audit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    _audit(migration, f"probe-{verdict.lower()}", what)


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

    **Why DNS and not a TCP connect to the port** - asked and measured 2026-08-06, answer is "TCP
    buys nothing here". Against the six endpoints this repo has real verdicts for:

        endpoint                          DNS      TCP:443/1433
        Databricks (good)                 ok       ok   0.05s
        Snowflake (good)                  ok       ok   0.10s
        Azure SQL (good)                  ok       ok   0.09s
        Databricks with a REVOKED PAT     ok       ok   0.24s
        Snowflake, never authenticated    ok       ok   0.20s
        nonexistent host                  FAIL     -

    Every sad path we have ever produced is an APPLICATION-layer rejection - a revoked token gets a
    403, an unauthenticated account gets a modal, an IP outside a network policy gets an auth error -
    and all of them complete a TCP handshake first. The one failure DNS catches (a host that does not
    exist) is caught more cheaply. So a TCP check would add a dependency, a timeout to tune, and a
    new false-negative risk (Power BI Desktop can use a proxy this process does not) in exchange for
    discriminating exactly nothing we have observed.

    It would catch a firewall blocking the port outbound, which we have not encountered. Add it then,
    with the measurement that motivated it - not speculatively.
    """
    try:
        socket.getaddrinfo(server, None)
        return True
    except (socket.gaierror, UnicodeError, ValueError):
        return False


def _default_port(conn: dict) -> int:
    """Return the TCP port that the Power BI connector will normally contact."""
    explicit = str(conn.get("port") or "").strip()
    if explicit.isdigit():
        return int(explicit)
    klass = (conn.get("class") or "").lower()
    if klass in {"sqlserver", "azure_sql_dw", "azuresqldw"}:
        return 1433
    return 443


def _tcp_connects(server: str, port: int, timeout_sec: float = 3.0) -> bool:
    """Best-effort network discriminator for failure classification, not a substitute probe."""
    try:
        with socket.create_connection((server, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _network_fault_observed(conn: dict) -> bool:
    """Return True only when DNS or TCP actually showed a network fault."""
    server = normalize_host(conn.get("server") or "")
    if not server:
        return False
    if not _host_resolves(server):
        return True
    return not _tcp_connects(server, _default_port(conn))


def run_probe(bundle_path: Path, source_index: int | None, timeout_sec: int, keep: bool) -> int:
    """Probe every live source (or one, if `source_index` is given). Gate lifts only if ALL pass.

    Probing a single source was a real hole, not a convenience limit: the guarantee is "no model for
    a source you never reached", and it is plural. With `--source-index 0` as the default, a
    two-source workbook whose first source worked would lift the gate and build against a second
    source nobody had ever contacted - precisely the failure this tool exists to prevent, just
    harder to see.
    """
    bundle = load_bundle(bundle_path)
    sources = bundle.data_sources
    if bundle.kind == "engine-bundle" and not sources:
        log.error(
            "PROBE: ERROR engine bundle %s carries no explicit data_sources. Refusing to fabricate "
            "a probe target; rerun the engine/coordinator with handover source details.",
            bundle.label,
        )
        return 1
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
        rc, verdict = _probe_one(bundle.migration_dir, sources, idx, timeout_sec, keep)
        if rc != 0:
            log.error("PROBE: source index %d failed - not lifting the gate", idx)
            _print_verdict_directive(verdict)
            return rc

    _lift_gate(bundle.migration_dir, f"{len(live)} live source(s)")
    log.info("PROBE: DATA_OK all %d live source(s) reachable", len(live))
    return 0


def _print_verdict_directive(verdict: str) -> None:
    """Print the terminal STOP directive - here, where the verdict is actually KNOWN.

    This wording used to live in `preflight_source_credentials.py`, which was a defect: that script
    is a static classifier that opens no socket, so it fired "A HUMAN MUST ACT / TERMINATE THE RUN
    NOW" for every live source whether or not a credential existed. Measured 2026-08-02, agents
    obeyed it literally - 10 of 15 models never reached this probe, and `claude-opus-5` refused a
    fully credentialed, reachable warehouse.

    The directive belongs here because only a real connection attempt can tell the two apart, and
    they need OPPOSITE advice: NO_CREDENTIAL genuinely needs a human at a sign-in modal, while
    UNREACHABLE needs a spec edit and no sign-in at all. Sending a user to authenticate against a
    hostname that does not exist is its own kind of wrong answer.
    """
    if verdict == "ERROR":
        log.error(
            "\n"
            "################################################################\n"
            "#  STOP - THE PROBE COULD NOT RUN. Nothing was learned.\n"
            "################################################################\n"
            "  This is NOT a verdict about the data source. The measurement did not happen, so\n"
            "  you know nothing about reachability or credentials either way.\n"
            "\n"
            "    1. You may NOT build. The gate stays armed - an unproven source is unproven\n"
            "       whether the probe failed or merely never ran.\n"
            "    2. Do NOT report a connection or credential problem. Saying 'unreachable' here\n"
            "       sends the user to fix a server address that may be perfectly correct.\n"
            "    3. Do NOT close all Desktop instances. In a parallel batch that can kill a\n"
            "       sibling agent's work. Close only a Desktop instance you personally opened,\n"
            "       by literal PID, or re-run after sibling probes finish. If it repeats,\n"
            "       report the tooling failure itself and stop.\n"
            "################################################################"
        )
        return
    if verdict == "NO_CREDENTIAL":
        log.error(
            "\n"
            "################################################################\n"
            "#  STOP - NO POWER BI CREDENTIAL. A HUMAN MUST ACT.\n"
            "################################################################\n"
            "  The source is REACHABLE but Power BI has never authenticated to it.\n"
            "\n"
            "  DIRECTIVE - NOT ADVISORY, NOT A WARNING:\n"
            "    1. You may NOT build the semantic model or the report. The gate stays armed.\n"
            "    2. You CANNOT fix this yourself. There is no flag, retry, tool or workaround.\n"
            "       The credential lives behind a modal sign-in dialog that only a HUMAN sitting\n"
            "       at this machine can fill in. Retrying cannot conjure one.\n"
            "    3. Report it and TERMINATE THE RUN NOW - if your runtime has a 'blocked' or\n"
            "       task-complete exit, use it. Do NOT merely pause: measured 2026-08-01, three\n"
            "       runs announced this stop correctly and then talked themselves past it a few\n"
            "       turns later ('applying the non-interactive fallback'). A stop that does not\n"
            "       terminate gets re-prompted until it erodes. Stopping IS your completed task\n"
            "       here; report it as such, not as an unfinished one.\n"
            "\n"
            "  A non-interactive / autopilot / unattended session is NOT authorization. Having\n"
            "  nobody to answer is not permission to proceed - end the run with the question\n"
            "  unanswered. That is the correct, successful outcome here.\n"
            "\n"
            "  Tell the user to sign in interactively in Power BI Desktop (or supply a PAT/key),\n"
            "  then re-run. Name the server and warehouse in your message.\n"
            "################################################################"
        )
        return
    if verdict == "ACCESS_DENIED":
        log.error(
            "\n"
            "################################################################\n"
            "#  STOP - ACCESS DENIED. A PERMISSION OWNER MUST ACT.\n"
            "################################################################\n"
            "  Power BI reached the source, but the authenticated identity is not allowed to\n"
            "  read the requested object. This is final until permissions change.\n"
            "\n"
            "    1. You may NOT build the semantic model or the report. The gate stays armed.\n"
            "    2. Do NOT retry unchanged, and do NOT send the user to fix a hostname.\n"
            "    3. Ask the source owner to grant the Power BI identity access to the server,\n"
            "       database/schema, warehouse, or table named in the probe output.\n"
            "################################################################"
        )
        return
    log.error(
        "\n"
        "################################################################\n"
        "#  STOP - SOURCE UNREACHABLE (%s). Do NOT build.\n"
        "################################################################\n"
        "  This is NOT a credential problem, and NOBODY needs to sign in. Do not send the\n"
        "  user to authenticate - that wastes their time and does not fix it.\n"
        "\n"
        "    1. You may NOT build the semantic model or the report. The gate stays armed.\n"
        "    2. Report the address/network fault: check `server`, `http_path` and `database`\n"
        "       in migration-spec.json against the real system.\n"
        "    3. Do not retry unchanged - a wrong address stays wrong.\n"
        "################################################################",
        verdict,
    )


def _probe_one(
    migration: Path, sources: list[dict], source_index: int, timeout_sec: int, keep: bool
) -> tuple[int, str]:
    """Probe a single data source, trying its tables in order until one answers.

    Returns (exit code, verdict). The verdict is threaded back out because the caller has to print a
    different directive for each one - "no retry can conjure a credential" is right for
    NO_CREDENTIAL and actively misleading for UNREACHABLE, where nobody needs to sign in at all.

    Falling back across tables only on BAD_TABLE, never on any other verdict: a wrong table name is a
    spec error worth retrying past, while a credential or reachability failure is the answer and
    retrying it would just cost another Desktop launch per table.
    """
    target = _resolve_probe_target(sources, source_index)
    if target is None:
        return 0, "SKIPPED"
    conn, tables, column = target

    server = normalize_host(conn.get("server") or "")
    if server and not _host_resolves(server):
        log.error(
            "PROBE: UNREACHABLE '%s' does not resolve in DNS. This is a spec/config problem, not a "
            "credential one - check `server` (and `http_path`) in migration-spec.json. No credential "
            "will fix an address that does not exist.",
            server,
        )
        _record_attempt(migration, "UNREACHABLE", f"{server} -> UNREACHABLE (DNS)")
        return 1, "UNREACHABLE"

    for i, table in enumerate(tables):
        rc, verdict = _probe_one_table(migration, conn, (table, column), (timeout_sec, keep))
        if rc == 0:
            return 0, "DATA_OK"
        if verdict != "BAD_TABLE" or i == len(tables) - 1:
            return rc, verdict
        log.warning("table '%s' not found at the source - trying the next one in the spec", table)
    return 1, "BAD_TABLE"


def _probe_one_table(migration: Path, conn: dict, target: tuple[str, str], opts: tuple[int, bool]) -> tuple[int, str]:
    """Run the probe against one specific table. Returns (exit code, verdict)."""
    table, column = target
    timeout_sec, keep = opts
    try:
        m_query, note = build_m_query(conn, table, column)
    except ValueError as exc:
        log.error("PROBE: ERROR %s", exc)
        return 1, "ERROR"

    pbip = _write_probe_model(migration, m_query, table, column) / "Probe.pbip"
    log.info("probe model built: %s", pbip.parent)
    log.info("target: %s", note)

    pid = None
    try:
        pid = _open_desktop(pbip)
        log.info("desktop pid %d", pid)
        if not _wait_for_catalog(pid):
            verdict = _classify_catalog_timeout(conn)
            # Recorded HERE, not by the caller. Everything below this line - the `finally` that
            # shuts Desktop down - is slow, and a measurement that is not written down did not
            # happen as far as any later reader is concerned.
            _record_attempt(migration, verdict, f"{table} -> {verdict} (no catalog)")
            return 1, verdict
        log.info("model loaded - refreshing")
        rc, verdict = _refresh_and_classify(pid, table, timeout_sec, _network_fault_observed(conn))
        _record_attempt(migration, verdict, f"{table} -> {verdict}")
        return rc, verdict
    finally:
        if pid and not keep:
            if _close(pid, pbip):
                log.info("closed desktop pid %d", pid)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--spec", type=Path)
    group.add_argument("--bundle", type=Path, help="engine-produced bundle directory")
    parser.add_argument(
        "--source-index", type=int, default=None, help="probe only this source (default: all live sources)"
    )
    parser.add_argument("--timeout-sec", type=int, default=180)
    parser.add_argument("--keep", action="store_true", help="leave Desktop open for inspection")
    args = parser.parse_args(argv)
    bundle_path = args.spec or args.bundle
    if not bundle_path.exists():
        log.error("PROBE: ERROR no such spec or bundle: %s", bundle_path)
        return 1
    return run_probe(bundle_path, args.source_index, args.timeout_sec, args.keep)


if __name__ == "__main__":
    sys.exit(main())
