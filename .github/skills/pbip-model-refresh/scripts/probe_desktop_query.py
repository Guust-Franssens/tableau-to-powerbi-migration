"""
purpose: robust LOCAL data-source preflight for a migrated model open in Power BI Desktop. Discovers
         the Desktop local Analysis Services port and runs a 1-row DAX probe (EVALUATE TOPN(1, <table>))
         against the loaded model. A returned row proves, in one shot, that credentials are present, the
         source is reachable, and the M/partition is valid - the real gate before building the report.
usage:   python .github/skills/pbip-model-refresh/scripts/probe_desktop_query.py
             [--pid <pbidesktop-pid>] [--tables "A" "B"] [--port <n>]
         (ships inside the `pbip-model-refresh` skill; run it by its path from wherever the folder
          was copied. `scripts/probe_desktop_query.py` in this repo is a forwarding shim.)

If --port is omitted, the port is discovered from the msmdsrv process owned by (a child of) the given
Desktop pid. That scoping is STRICT: a named pid never falls back to another instance's msmdsrv, it
retries briefly (msmdsrv binds its port a moment after Desktop starts) and then fails. With no --pid
the single running msmdsrv is used, and more than one is an error rather than a coin flip.

Name one canary table per distinct live source with --tables: every named source is probed and an
all-non-zero result earns the model-level PREFLIGHT: DATA_OK. If NO table is named, only the first
queryable table is probed and the verdict is downgraded to PREFLIGHT: TABLE_OK '<table>' - a static
parameter/CSV table can return rows while a live source never loaded, so one arbitrary table can
never certify the model.
Emits a final line: PREFLIGHT: DATA_OK (all canaries returned rows) / PREFLIGHT: TABLE_OK '<table>'
(implicit single-table probe only) / PREFLIGHT: NO_DATA / PREFLIGHT: ERROR <msg>.

Windows-only: queries the Desktop's local AS via ADOMD.NET (pythonnet). This is the sanctioned
Windows-API exception to the "committed scripts default to .py/.sh" rule.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

# ADOMD.NET assembly shipped in the nuget cache (netcore build). Resolved at import time.
_ADOMD_PKG = "microsoft.analysisservices.adomdclient.netcore*"
_ADOMD_DLL = "Microsoft.AnalysisServices.AdomdClient.dll"
_ADOMD_GLOBS = [str(Path.home() / ".nuget/packages" / _ADOMD_PKG / "**" / _ADOMD_DLL)]

# Desktop binds its msmdsrv port a moment AFTER the process appears, so a scoped lookup needs a
# short retry - but a bounded one, because a miss must end in a loud failure, never a fallback.
PORT_DISCOVERY_ATTEMPTS = 6
PORT_DISCOVERY_INTERVAL_SECONDS = 2

# Power BI's auto date/time scaffolding. Present in the engine, and serialized into a PBIP's
# `definition/tables/` too when auto date/time is (or ever was) on - so a comparison of the two
# sides has to strip it from BOTH, not just from the engine.
AUTO_DATE_TABLE_PREFIXES = ("LocalDateTable", "DateTableTemplate")


def _load_adomd():
    """Load the ADOMD.NET AdomdConnection type via pythonnet, or exit with a clear message."""
    # Imports are deliberately inside this function: pythonnet must host CoreCLR (the ADOMD assembly is
    # a netcoreapp build) BEFORE `import clr`, and the .NET types only exist after AddReference.
    # pylint: disable=import-outside-toplevel,import-error
    try:
        from pythonnet import load

        load("coreclr")
        import clr
    except ImportError:
        print("PREFLIGHT: ERROR pythonnet not installed (uv pip install pythonnet)")
        sys.exit(2)

    for pattern in _ADOMD_GLOBS:
        hits = glob.glob(pattern, recursive=True)
        if hits:
            dll = Path(hits[0])
            # AddReference resolves by assembly name with the folder on sys.path (a full path is
            # treated as a name and fails), so add the dir first, then reference by simple name.
            if str(dll.parent) not in sys.path:
                sys.path.append(str(dll.parent))
            # pylint: disable-next=no-member  # clr's members are generated at runtime by pythonnet
            clr.AddReference(dll.stem)
            from Microsoft.AnalysisServices.AdomdClient import AdomdConnection

            return AdomdConnection
    print("PREFLIGHT: ERROR Microsoft.AnalysisServices.AdomdClient.dll not found in the nuget cache")
    sys.exit(2)


def _msmdsrv_ports(desktop_pid: int | None) -> list[int]:
    """Listening TCP ports of the msmdsrv processes, scoped to children of `desktop_pid` when given.

    msmdsrv.exe is spawned as a child of the Power BI Desktop process, so the parent-pid match is
    ground truth rather than a heuristic - which is exactly why the caller must NOT widen the query
    when it comes back empty.
    """
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$procs = Get-CimInstance Win32_Process -Filter \"Name='msmdsrv.exe'\";"
        "if ($env:PID_FILTER) { $procs = $procs | "
        "Where-Object { $_.ParentProcessId -eq [int]$env:PID_FILTER } };"
        "$procs | ForEach-Object { (Get-NetTCPConnection -OwningProcess $_.ProcessId -State Listen | "
        "Select-Object -First 1 -ExpandProperty LocalPort) }"
    )
    # Always set the variable, empty when unscoped: an inherited PID_FILTER from the caller's own
    # environment would otherwise scope a query that was meant to be machine-wide.
    env = {"PID_FILTER": str(desktop_pid) if desktop_pid else ""}
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
        env=_merged_env(env),
    ).stdout
    return sorted({int(t) for t in out.split() if t.strip().isdigit()})


def _fail(message: str) -> NoReturn:
    print(f"PREFLIGHT: ERROR {message}")
    sys.exit(2)


def _child_port(desktop_pid: int) -> int:
    """The port of the msmdsrv owned by `desktop_pid` - retried for startup lag, never widened."""
    for attempt in range(PORT_DISCOVERY_ATTEMPTS):
        ports = _msmdsrv_ports(desktop_pid)
        if len(ports) == 1:
            return ports[0]
        if len(ports) > 1:
            _fail(f"pid {desktop_pid} owns {len(ports)} msmdsrv ports {ports}; name one with --port")
        if attempt + 1 < PORT_DISCOVERY_ATTEMPTS:
            time.sleep(PORT_DISCOVERY_INTERVAL_SECONDS)
    _fail(
        f"no msmdsrv child of Power BI Desktop pid {desktop_pid} after {PORT_DISCOVERY_ATTEMPTS} "
        "attempts - is that pid a Desktop instance that has finished loading? (refusing to fall "
        "back to another instance's model)"
    )


def discover_port(desktop_pid: int | None) -> int:
    """Find the local Analysis Services (msmdsrv) TCP port for the Desktop instance.

    A named pid is AUTHORITATIVE and is never widened. The old code fell back to "any msmdsrv on the
    machine" whenever the scoped query came back empty - harmless on a one-instance box, a
    data-corruption path in a parallel batch: the caller then refreshes, row-counts and (via
    `refresh_pbip_model.image_save`) persists a *sibling's* model into its own correct `cache.abf`.
    Every downstream check still passes, because file metadata cannot tell you whose rows are in the
    blob. The likeliest trigger is not a wrong pid but timing - Desktop is up before its msmdsrv has
    bound a port - so the scoped lookup retries briefly and then fails loudly instead of guessing.
    """
    if desktop_pid:
        return _child_port(desktop_pid)
    ports = _msmdsrv_ports(None)
    if len(ports) > 1:
        _fail(f"{len(ports)} Power BI Desktop models are running {ports}; name yours with --pid (or --port)")
    if not ports:
        _fail("could not find the Desktop local AS (msmdsrv) port")
    return ports[0]


def _merged_env(extra: dict[str, str]) -> dict[str, str]:
    return {**os.environ, **extra}


def table_names(conn, *, include_hidden: bool = False) -> list[str]:
    """Model tables from the TMSCHEMA_TABLES DMV, minus Power BI's auto date-table scaffolding.

    `include_hidden=True` is what an identity check wants - a hidden table is still part of a
    model's fingerprint; the default is the queryable set a data probe should target.
    """
    cmd = conn.CreateCommand()
    cmd.CommandText = "SELECT [Name], [IsHidden] FROM $SYSTEM.TMSCHEMA_TABLES"
    reader = cmd.ExecuteReader()
    names: list[str] = []
    try:
        while reader.Read():
            name = str(reader.GetValue(0))
            if not include_hidden and bool(reader.GetValue(1)):
                continue
            if not name.startswith(AUTO_DATE_TABLE_PREFIXES):
                names.append(name)
    finally:
        reader.Close()
    return names


def first_table(conn) -> str:
    """Return the first queryable (non-hidden, non-date-template) table name via a DMV query."""
    names = table_names(conn)
    if not names:
        raise RuntimeError("no queryable table found in model")
    return names[0]


def _probe_one(port: int, conn, table: str) -> int:
    """Run EVALUATE TOPN(1, '<table>') for one table, print the evidence, and return the row count."""
    dax = f"EVALUATE TOPN(1, '{table}')"
    cmd = conn.CreateCommand()
    cmd.CommandText = dax
    reader = cmd.ExecuteReader()
    cols = [reader.GetName(i) for i in range(reader.FieldCount)]
    rows = 0
    first_values: list[str] = []
    while reader.Read():
        rows += 1
        if rows == 1:
            first_values = [str(reader.GetValue(i)) for i in range(reader.FieldCount)]
    reader.Close()
    print(f"port={port}  table='{table}'  dax={dax}")
    print(f"  columns ({len(cols)}): {cols[:8]}")
    if rows:
        print(f"  row: {first_values[:8]}")
    return rows


def probe(port: int, tables: list[str] | None) -> int:
    """Probe each canary table with a 1-row read against localhost:<port>; return a process exit code.

    With explicit `tables` (one canary per distinct live source) an all-non-zero result earns the
    model-level ``PREFLIGHT: DATA_OK``. With NO table named, only the first queryable table is
    probed and the verdict is ``PREFLIGHT: TABLE_OK '<table>'`` - a single arbitrary table is not a
    model-level guarantee, because a static parameter/CSV table can return rows while a live source
    never loaded (see the powerbi-semantic-model-gotchas rule: prove a REAL read per live source).
    """
    adomd_connection = _load_adomd()
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        implicit = not tables
        targets = list(tables) if tables else [first_table(conn)]
        results = [(target, _probe_one(port, conn, target)) for target in targets]
        empty = [target for target, rows in results if rows <= 0]
        if empty:
            print(
                f"PREFLIGHT: NO_DATA (0 rows from: {', '.join(empty)} - source empty, "
                "credential missing, or refresh failed)"
            )
            return 1
        if implicit:
            only = results[0][0]
            print(f"PREFLIGHT: TABLE_OK '{only}'")
            print(
                f"  note: single-table probe of '{only}' only - NOT a model-level DATA_OK. A static "
                "parameter/CSV table can return rows while a live source never loaded. Pass --tables "
                "<one canary per live source> to certify every source."
            )
            return 0
        print("PREFLIGHT: DATA_OK")
        return 0
    finally:
        conn.Close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pid",
        type=int,
        help="Power BI Desktop process id - required when several instances are open "
        "(`powerbi-desktop status` maps pid -> open file)",
    )
    parser.add_argument("--table", help="Single canary table (legacy alias for --tables with one name)")
    parser.add_argument(
        "--tables",
        nargs="*",
        help="Canary tables to probe, one per distinct live source. With none, only the first "
        "queryable table is probed and the verdict is downgraded to name that single table",
    )
    parser.add_argument("--port", type=int, help="Local AS port (default: auto-discover)")
    args = parser.parse_args(argv)

    # --tables (plural, the canary set) wins; --table stays as a one-name alias for old callers.
    tables = list(args.tables) if args.tables else ([args.table] if args.table else None)
    port = args.port or discover_port(args.pid)
    try:
        return probe(port, tables)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"PREFLIGHT: ERROR {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
