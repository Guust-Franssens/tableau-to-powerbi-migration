"""
purpose: robust LOCAL data-source preflight for a migrated model open in Power BI Desktop. Discovers
         the Desktop local Analysis Services port and runs a 1-row DAX probe (EVALUATE TOPN(1, <table>))
         against the loaded model. A returned row proves, in one shot, that credentials are present, the
         source is reachable, and the M/partition is valid - the real gate before building the report.
usage:   python scripts/probe_desktop_query.py [--pid <pbidesktop-pid>] [--table "<table>"] [--port <n>]

If --port is omitted, the port is discovered from the msmdsrv process owned by (a child of) the given
Desktop pid, or the single running msmdsrv. If --table is omitted, the first non-hidden table is probed.
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
from pathlib import Path

# ADOMD.NET assembly shipped in the nuget cache (netcore build). Resolved at import time.
_ADOMD_PKG = "microsoft.analysisservices.adomdclient.netcore*"
_ADOMD_DLL = "Microsoft.AnalysisServices.AdomdClient.dll"
_ADOMD_GLOBS = [str(Path.home() / ".nuget/packages" / _ADOMD_PKG / "**" / _ADOMD_DLL)]


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
            clr.AddReference(dll.stem)
            from Microsoft.AnalysisServices.AdomdClient import AdomdConnection

            return AdomdConnection
    print("PREFLIGHT: ERROR Microsoft.AnalysisServices.AdomdClient.dll not found in the nuget cache")
    sys.exit(2)


def discover_port(desktop_pid: int | None) -> int:
    """Find the local Analysis Services (msmdsrv) TCP port for the Desktop instance."""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$procs = if ($env:PID_FILTER) { Get-CimInstance Win32_Process -Filter \"Name='msmdsrv.exe'\" | "
        "Where-Object { $_.ParentProcessId -eq [int]$env:PID_FILTER } } else { $null };"
        "if (-not $procs) { $procs = Get-CimInstance Win32_Process -Filter \"Name='msmdsrv.exe'\" };"
        "$procs | ForEach-Object { (Get-NetTCPConnection -OwningProcess $_.ProcessId -State Listen | "
        "Select-Object -First 1 -ExpandProperty LocalPort) }"
    )
    env = {"PID_FILTER": str(desktop_pid)} if desktop_pid else {}
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
        env=_merged_env(env),
    ).stdout
    ports = [int(t) for t in out.split() if t.strip().isdigit()]
    if not ports:
        print("PREFLIGHT: ERROR could not find the Desktop local AS (msmdsrv) port")
        sys.exit(2)
    return ports[0]


def _merged_env(extra: dict[str, str]) -> dict[str, str]:
    return {**os.environ, **extra}


def first_table(conn) -> str:
    """Return the first queryable (non-hidden, non-date-template) table name via a DMV query."""
    cmd = conn.CreateCommand()
    cmd.CommandText = "SELECT [Name], [IsHidden] FROM $SYSTEM.TMSCHEMA_TABLES"
    reader = cmd.ExecuteReader()
    names: list[str] = []
    try:
        while reader.Read():
            name = str(reader.GetValue(0))
            is_hidden = bool(reader.GetValue(1))
            if not is_hidden and not name.startswith(("LocalDateTable", "DateTableTemplate")):
                names.append(name)
    finally:
        reader.Close()
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
    parser.add_argument("--pid", type=int, help="Power BI Desktop process id (to disambiguate the msmdsrv)")
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
