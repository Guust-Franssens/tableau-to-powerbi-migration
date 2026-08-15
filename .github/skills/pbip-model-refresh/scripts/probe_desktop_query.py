"""
purpose: robust LOCAL data-source preflight for a migrated model open in Power BI Desktop. Discovers
         the Desktop local Analysis Services port and runs a 1-row DAX probe (EVALUATE TOPN(1, <table>))
         against the loaded model. A returned row proves, in one shot, that credentials are present, the
         source is reachable, and the M/partition is valid - the real gate before building the report.
usage:   python .github/skills/pbip-model-refresh/scripts/probe_desktop_query.py
             [--pid <pbidesktop-pid>] [--canaries "A" "B"] [--port <n>]
         (ships inside the `pbip-model-refresh` skill; run it by its path from wherever the folder
          was copied. `scripts/probe_desktop_query.py` in this repo is a forwarding shim.)

If --port is omitted, the port is discovered from the msmdsrv process owned by (a child of) the given
Desktop pid. That scoping is STRICT: a named pid never falls back to another instance's msmdsrv, it
retries briefly (msmdsrv binds its port a moment after Desktop starts) and then fails. With no --pid
the single running msmdsrv is used, and more than one is an error rather than a coin flip.

Name one canary table per distinct live source with --canaries: every named source is probed and an
all-non-zero result earns the model-level PREFLIGHT: DATA_OK. If NO table is named, only the first
queryable table is probed and the verdict is downgraded to PREFLIGHT: TABLE_OK '<table>' - a static
parameter/CSV table can return rows while a live source never loaded, so one arbitrary table can
never certify the model. (`--tables`/`--table` are accepted aliases; this script only READS, so
naming canaries here never narrows anything. In `refresh_pbip_model` they are NOT interchangeable:
there `--tables` narrows the refresh itself, which is why `--canaries` exists.)
Emits a final line: PREFLIGHT: DATA_OK (all canaries returned rows) / PREFLIGHT: TABLE_OK '<table>'
(implicit single-table probe only) / PREFLIGHT: NO_DATA / PREFLIGHT: ERROR <msg>.

Windows-only: queries the Desktop's local AS via ADOMD.NET (pythonnet). This is the sanctioned
Windows-API exception to the "committed scripts default to .py/.sh" rule.
"""

from __future__ import annotations

import argparse
import glob
import inspect
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NoReturn

from _credential_modal import (
    CredentialDetection,
    CredentialModal,
    describe_blocking_dialog,
    describe_modal,
    inspect_credential_modal,
)

# ADOMD.NET assembly shipped in the nuget cache (netcore build). Resolved at import time.
_ADOMD_PKG = "microsoft.analysisservices.adomdclient.netcore*"
_ADOMD_DLL = "Microsoft.AnalysisServices.AdomdClient.dll"
_ADOMD_GLOBS = [str(Path.home() / ".nuget/packages" / _ADOMD_PKG / "**" / _ADOMD_DLL)]

# Desktop binds its msmdsrv port a moment AFTER the process appears, so a scoped lookup needs a
# short retry - but a bounded one, because a miss must end in a loud failure, never a fallback.
PORT_DISCOVERY_ATTEMPTS = 6
PORT_DISCOVERY_INTERVAL_SECONDS = 2
PREFLIGHT_CREDENTIAL_POLL_SECONDS = 5.0

# Power BI's auto date/time scaffolding. Present in the engine, and serialized into a PBIP's
# `definition/tables/` too when auto date/time is (or ever was) on - so a comparison of the two
# sides has to strip it from BOTH, not just from the engine.
#
# Matching is by the EXACT generated-name SHAPE, not a name prefix. Power BI always names these
# tables `LocalDateTable_<GUID>` / `DateTableTemplate_<GUID>` with a canonical 8-4-4-4-12 hex GUID,
# so requiring that whole shape is reliable. A prefix test (`startswith`) silently deleted a genuine
# user table like `LocalDateTableSales` - columns and measures and all - from the identity
# fingerprint, hiding real differences (round-3 blocker 4). Anything that is not the exact generated
# shape is KEPT (fail closed toward comparing more, never less).
_AUTO_DATE_TABLE_RE = re.compile(
    r"^(?:LocalDateTable|DateTableTemplate)_[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}$"
)


def is_auto_date_table_name(name: str) -> bool:
    """Is `name` Power BI's auto date/time scaffolding (`LocalDateTable_<GUID>`/`DateTableTemplate_<GUID>`)?

    True ONLY for the exact generated shape - a canonical GUID suffix - so a user table that merely
    starts with those words (e.g. `LocalDateTableSales`) is NOT mistaken for generated scaffolding and
    stays in the fingerprint. Fails closed: unusual input keeps the table rather than dropping it.
    """
    return bool(_AUTO_DATE_TABLE_RE.match(name))


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
            if not is_auto_date_table_name(name):
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


# TMSCHEMA_COLUMNS.Type == 3 is the auto-generated per-table RowNumber (index) column. It is NEVER
# serialized into TMDL, so an identity fingerprint that compared it would report every real model as
# a stranger - it must be filtered from the live side, not compared. (1=Data, 2=Calculated,
# 3=RowNumber, 4=CalculatedTableColumn.)
_COLUMN_TYPE_ROWNUMBER = 3


def _table_id_to_name(conn) -> dict[str, str]:
    """Map each table's engine ID to its name, so column/measure rows can name their table."""
    cmd = conn.CreateCommand()
    cmd.CommandText = "SELECT [ID], [Name] FROM $SYSTEM.TMSCHEMA_TABLES"
    reader = cmd.ExecuteReader()
    mapping: dict[str, str] = {}
    try:
        while reader.Read():
            mapping[str(reader.GetValue(0))] = str(reader.GetValue(1))
    finally:
        reader.Close()
    return mapping


def column_names(conn) -> set[tuple[str, str]]:
    """`(table, column)` pairs from TMSCHEMA_COLUMNS, part of a model's identity fingerprint.

    Two classes of AUTO-GENERATED column are filtered so the fingerprint compares only what the TMDL
    actually declares (otherwise a legitimate model would fail its own identity gate):
    * the per-table RowNumber index (`Type == _COLUMN_TYPE_ROWNUMBER`), which is never in TMDL; and
    * every column of the `LocalDateTable_*` / `DateTableTemplate_*` auto date/time scaffolding, which
      is filtered at TABLE level on the disk side too.
    `ExplicitName` is the model name; `InferredName` is the fallback for a column whose name the
    engine inferred. Comparison is case-folded by the caller.
    """
    id_to_name = _table_id_to_name(conn)
    cmd = conn.CreateCommand()
    cmd.CommandText = "SELECT [TableID], [ExplicitName], [InferredName], [Type] FROM $SYSTEM.TMSCHEMA_COLUMNS"
    reader = cmd.ExecuteReader()
    pairs: set[tuple[str, str]] = set()
    try:
        while reader.Read():
            if int(reader.GetValue(3)) == _COLUMN_TYPE_ROWNUMBER:
                continue
            table = id_to_name.get(str(reader.GetValue(0)))
            if table is None or is_auto_date_table_name(table):
                continue
            explicit = reader.GetValue(1)
            name = str(explicit) if explicit not in (None, "") else str(reader.GetValue(2))
            pairs.add((table, name))
    finally:
        reader.Close()
    return pairs


def measure_names(conn) -> set[tuple[str, str]]:
    """`(table, measure)` pairs from TMSCHEMA_MEASURES, part of a model's identity fingerprint.

    Measures on the auto date/time scaffolding are filtered for the same reason as its columns.
    Comparison is case-folded by the caller.
    """
    id_to_name = _table_id_to_name(conn)
    cmd = conn.CreateCommand()
    cmd.CommandText = "SELECT [TableID], [Name] FROM $SYSTEM.TMSCHEMA_MEASURES"
    reader = cmd.ExecuteReader()
    pairs: set[tuple[str, str]] = set()
    try:
        while reader.Read():
            table = id_to_name.get(str(reader.GetValue(0)))
            if table is None or is_auto_date_table_name(table):
                continue
            pairs.add((table, str(reader.GetValue(1))))
    finally:
        reader.Close()
    return pairs


def _probe_one(port: int, conn, table: str, emit=print) -> int:
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
    emit(f"port={port}  table='{table}'  dax={dax}")
    emit(f"  columns ({len(cols)}): {cols[:8]}")
    if rows:
        emit(f"  row: {first_values[:8]}")
    return rows


def probe(port: int, tables: list[str] | None, emit=print) -> int:
    """Probe each canary table with a 1-row read against localhost:<port>; return a process exit code.

    With an explicit canary set (one per distinct live source) an all-non-zero result earns the
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
        results = [(target, _probe_one(port, conn, target, emit)) for target in targets]
        empty = [target for target, rows in results if rows <= 0]
        if empty:
            emit(
                f"PREFLIGHT: NO_DATA (0 rows from: {', '.join(empty)} - source empty, "
                "credential missing, or refresh failed)"
            )
            return 1
        if implicit:
            only = results[0][0]
            emit(f"PREFLIGHT: TABLE_OK '{only}'")
            emit(
                f"  note: single-table probe of '{only}' only - NOT a model-level DATA_OK. A static "
                "parameter/CSV table can return rows while a live source never loaded. Pass --canaries "
                "<one per live source> to certify every source."
            )
            return 0
        emit("PREFLIGHT: DATA_OK")
        return 0
    finally:
        conn.Close()


def _credential_state(pid: int) -> CredentialDetection:
    """Return the credential-modal inspection state for ``pid``."""
    if os.name != "nt":
        return CredentialDetection()
    return inspect_credential_modal(pid)


def _emit_credential_missing(pid: int, modal: CredentialModal) -> None:
    """Print the distinct credential verdict."""
    print(f"PREFLIGHT: CREDENTIAL_MISSING pid={pid}; {describe_modal(modal)}")


def _emit_blocked_by_dialog(pid: int, dialog) -> None:
    """Print the distinct generic blocking-dialog verdict."""
    print(f"PREFLIGHT: BLOCKED_BY_DIALOG pid={pid}; {describe_blocking_dialog(dialog)}")


def _emit_credential_unknown(pid: int, reason: str) -> None:
    """Print an indeterminate credential-check verdict."""
    print(f"PREFLIGHT: UNKNOWN pid={pid}; credential dialog check indeterminate: {reason}")


def _emit_desktop_gone(pid: int, reason: str) -> None:
    """Print the terminal local-error verdict for a dead Desktop."""
    print(f"PREFLIGHT: DESKTOP_GONE pid={pid}; {reason}")


def _emit_desktop_unready(pid: int, reason: str) -> None:
    """Print the local-error verdict for an alive Desktop with no windows."""
    print(f"PREFLIGHT: DESKTOP_UNREADY pid={pid}; {reason}")


def _probe_with_credential_poll(  # pylint: disable=too-many-return-statements
    pid: int | None, port: int, tables: list[str] | None
) -> int:
    """Run ``probe`` while polling for a late credential dialog owned by ``pid``."""
    if pid is None:
        return probe(port, tables)

    early = _credential_state(pid)
    if early.modal is not None:
        _emit_credential_missing(pid, early.modal)
        return 1
    if early.blocking_dialog is not None:
        _emit_blocked_by_dialog(pid, early.blocking_dialog)
        return 1
    if early.process_gone is not None:
        _emit_desktop_gone(pid, early.process_gone)
        return 2
    if early.desktop_unready is not None:
        _emit_desktop_unready(pid, early.desktop_unready)
        return 2
    if early.unknown_reason:
        _emit_credential_unknown(pid, early.unknown_reason)
        return 3

    result: dict[str, int | BaseException] = {}
    captured: list[str] = []

    def run_probe() -> None:
        try:
            if "emit" in inspect.signature(probe).parameters:
                result["outcome"] = probe(port, tables, captured.append)
            else:
                result["outcome"] = probe(port, tables)
        except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            result["outcome"] = exc

    worker = threading.Thread(target=run_probe, name="desktop-query-probe", daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(PREFLIGHT_CREDENTIAL_POLL_SECONDS)
        state = _credential_state(pid)
        if state.modal is not None:
            _emit_credential_missing(pid, state.modal)
            return 1
        if state.blocking_dialog is not None:
            _emit_blocked_by_dialog(pid, state.blocking_dialog)
            return 1
        if state.process_gone is not None:
            _emit_desktop_gone(pid, state.process_gone)
            return 2
        if state.desktop_unready is not None:
            _emit_desktop_unready(pid, state.desktop_unready)
            return 2
        if state.unknown_reason:
            _emit_credential_unknown(pid, state.unknown_reason)
            return 3

    outcome = result.get("outcome")
    if isinstance(outcome, BaseException):
        raise outcome
    if outcome is None:  # pragma: no cover - defensive; the worker always records something
        raise RuntimeError("probe worker returned no result")
    for line in captured:
        print(line)
    return outcome


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pid",
        type=int,
        help="Power BI Desktop process id - required when several instances are open "
        "(`powerbi-desktop status` maps pid -> open file)",
    )
    parser.add_argument("--table", help="Single canary table (legacy alias for --canaries with one name)")
    parser.add_argument(
        "--tables",
        nargs="*",
        help="Alias for --canaries. Kept for existing callers; this script never refreshes, so the "
        "two mean the same thing HERE (they do not in refresh_pbip_model.py)",
    )
    parser.add_argument(
        "--canaries",
        nargs="*",
        help="Canary tables to probe, one per distinct live source. With none, only the first "
        "queryable table is probed and the verdict is downgraded to name that single table",
    )
    parser.add_argument("--port", type=int, help="Local AS port (default: auto-discover)")
    args = parser.parse_args(argv)

    # Preference order: --canaries (the name that means the same thing in both scripts), then
    # --tables (plural), then --table as a one-name alias for old callers.
    tables = list(args.canaries or args.tables or ([args.table] if args.table else [])) or None
    if args.pid is not None:
        early = _credential_state(args.pid)
        if early.modal is not None:
            _emit_credential_missing(args.pid, early.modal)
            return 1
        if early.blocking_dialog is not None:
            _emit_blocked_by_dialog(args.pid, early.blocking_dialog)
            return 1
        if early.process_gone is not None:
            _emit_desktop_gone(args.pid, early.process_gone)
            return 2
        if early.desktop_unready is not None:
            _emit_desktop_unready(args.pid, early.desktop_unready)
            return 2
        if early.unknown_reason:
            _emit_credential_unknown(args.pid, early.unknown_reason)
            return 3
    port = args.port or discover_port(args.pid)
    try:
        return _probe_with_credential_poll(args.pid, port, tables)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"PREFLIGHT: ERROR {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
