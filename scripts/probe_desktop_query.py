"""
purpose: robust LOCAL data-source preflight for a migrated model open in Power BI Desktop. Discovers
         the Desktop local Analysis Services port and runs a 1-row DAX probe (EVALUATE TOPN(1, <table>))
         against the loaded model. A returned row proves, in one shot, that credentials are present, the
         source is reachable, and the M/partition is valid - the real gate before building the report.
usage:   python scripts/probe_desktop_query.py [--pid <pbidesktop-pid>] [--table "<table>"] [--port <n>]

If --port is omitted, the port is discovered from the msmdsrv process owned by (a child of) the given
Desktop pid. That scoping is STRICT: a named pid never falls back to another instance's msmdsrv, it
retries briefly (msmdsrv binds its port a moment after Desktop starts) and then fails. With no --pid
the single running msmdsrv is used, and more than one is an error rather than a coin flip. If --table
is omitted, the first non-hidden table is probed.
Emits a final line: PREFLIGHT: DATA_OK (rows returned) / PREFLIGHT: NO_DATA / PREFLIGHT: ERROR <msg>.

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

# Power BI's auto date/time scaffolding: real tables in the engine, never in the model's TMDL.
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


def probe(port: int, table: str | None) -> int:
    """Run EVALUATE TOPN(1, <table>) against localhost:<port>; return process exit code."""
    adomd_connection = _load_adomd()
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        target = table or first_table(conn)
        dax = f"EVALUATE TOPN(1, '{target}')"
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
        print(f"port={port}  table='{target}'  dax={dax}")
        print(f"columns ({len(cols)}): {cols[:8]}")
        if rows:
            print(f"row: {first_values[:8]}")
            print("PREFLIGHT: DATA_OK")
            return 0
        print("PREFLIGHT: NO_DATA (query ran but returned 0 rows - source empty or refresh failed)")
        return 1
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
    parser.add_argument("--table", help="Table to probe (default: first queryable table)")
    parser.add_argument("--port", type=int, help="Local AS port (default: auto-discover)")
    args = parser.parse_args(argv)

    port = args.port or discover_port(args.pid)
    try:
        return probe(port, args.table)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"PREFLIGHT: ERROR {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
