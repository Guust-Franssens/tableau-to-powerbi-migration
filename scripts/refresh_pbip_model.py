"""
purpose: Refresh a local PBIP model in Power BI Desktop and PERSIST the result, so the next agent
         (and the next Desktop open) sees real data instead of an empty model.
usage:   python scripts/refresh_pbip_model.py [--pid <pbidesktop-pid>] [--tables "A" "B"] [--no-save]

Why this exists
---------------
Two separate gaps kept biting real migrations:

1. **The refresh itself was hand-rolled every time.** The only working path against a local PBIP is
   TOM/XMLA over the child `msmdsrv` port, which means: discover the port, load ADOMD.NET, resolve
   the catalog GUID, then send a TMSL `refresh`. Re-deriving that per migration is slow and easy to
   get subtly wrong.

2. **The refresh was not persisted.** Power BI Desktop DOES cache a PBIP's data - in
   `<Name>.SemanticModel/.pbi/cache.abf` (gitignored, it is data) - but only when the file is
   **saved**. An XMLA refresh populates the in-memory model and leaves the file dirty, so if nobody
   saves, the cache is never written and the next agent opens an empty model and has to refresh
   again (hitting the credential prompt all over again). The Desktop Bridge CLI has **no save verb**
   (`status`/`manifest`/`open`/`reload`/`screenshot`/`screenshot-all`), which is exactly why this
   kept being missed - it was a missing capability, not carelessness. We send Ctrl+S via the Windows
   UI and then VERIFY with the bridge's own `hasUnsavedChanges` flag plus the cache file's timestamp.

Cache invalidation (important)
------------------------------
Desktop discards `cache.abf` when the model **definition** is newer than the cache. Verified: a
model whose `definition/*.tmdl` were touched a week after the cache was written opened with
`NO_DATA` despite a 113 KB cache sitting right there. So: make ALL model edits FIRST, then refresh,
then save. Anything that rewrites TMDL afterwards - including this repo's own
`scripts/set_data_folder.py --sanitize`, which you must run before committing - invalidates it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from probe_desktop_query import _load_adomd, discover_port, first_table

SAVE_SETTLE_SECONDS = 3
SAVE_TIMEOUT_SECONDS = 120


def _catalog_id(conn) -> str:
    """The database GUID a TMSL command must name, read from the server's own catalog DMV."""
    cmd = conn.CreateCommand()
    cmd.CommandText = "SELECT [CATALOG_NAME] FROM $SYSTEM.DBSCHEMA_CATALOGS"
    reader = cmd.ExecuteReader()
    try:
        if not reader.Read():
            raise RuntimeError("no catalog found on the Desktop Analysis Services instance")
        return str(reader.GetValue(0))
    finally:
        reader.Close()


def refresh(port: int, tables: list[str] | None) -> tuple[bool, str]:
    """Send a TMSL refresh over XMLA. Returns (ok, message).

    Refreshing named tables is preferred over the whole database: a full refresh can hang for
    minutes on a large table that no report even uses.
    """
    adomd_connection = _load_adomd()
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        catalog = _catalog_id(conn)
        if tables:
            objects = [{"database": catalog, "table": t} for t in tables]
        else:
            objects = [{"database": catalog}]
        tmsl = json.dumps({"refresh": {"type": "full", "objects": objects}})
        cmd = conn.CreateCommand()
        cmd.CommandText = tmsl
        cmd.ExecuteNonQuery()
        return True, f"refreshed {'/'.join(tables) if tables else 'entire database'} (catalog {catalog})"
    finally:
        conn.Close()


def _bridge_status() -> dict:
    """The Desktop Bridge's view of the running instances, including `hasUnsavedChanges`."""
    result = subprocess.run(
        ["npx", "--yes", "@microsoft/powerbi-desktop-bridge-cli", "status"],
        capture_output=True,
        text=True,
        check=False,
        shell=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _instance(pid: int | None) -> dict | None:
    for inst in _bridge_status().get("instances", []):
        if pid is None or inst.get("pid") == pid:
            return inst
    return None


def save(pid: int) -> tuple[bool, str]:
    """Save the Desktop file, then verify the save actually happened.

    The bridge has no save verb, so this drives the UI - but NOT with SendKeys. Verified 2026-07-30:
    `SetForegroundWindow` (even with the `AttachThreadInput` workaround) is refused in this context,
    so the Ctrl+S keystroke silently lands on whatever window has focus and the model stays dirty.

    Instead we use **UI Automation (UIA)** - the Windows *accessibility* framework, the API screen
    readers use to enumerate and activate controls on behalf of users with disabilities. It exposes
    the app as a tree of elements with invokable patterns, and `InvokePattern` needs no foreground
    focus, which is why it works here. `probe_desktop_credential.ps1` already uses it against Desktop.

    Be clear-eyed that this is a WORKAROUND, not a design: an accessibility surface is not an
    automation contract. It depends on an element literally named "Save", so it breaks on ribbon
    changes and on non-English installs, it cannot run headless, and a modal dialog swallows the
    invoke silently. Hence the result is never assumed - it is confirmed against the bridge's own
    `hasUnsavedChanges` flag. If a real `save` verb ever ships, delete this and call it.
    """
    before = _instance(pid)
    if before is None:
        return False, f"no Desktop Bridge instance for pid {pid}"
    if not before.get("hasUnsavedChanges"):
        return True, "nothing to save (hasUnsavedChanges already false)"

    script = f"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, {pid})
$win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
if (-not $win) {{ Write-Output 'NO_WINDOW'; exit 1 }}
$nameCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, 'Save')
foreach ($el in $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $nameCond)) {{
    try {{
        $p = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $p.Invoke(); Write-Output 'INVOKED'; exit 0
    }} catch {{ }}
}}
Write-Output 'NO_INVOKABLE_SAVE'; exit 1
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=False)

    deadline = time.time() + SAVE_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(SAVE_SETTLE_SECONDS)
        current = _instance(pid)
        if current is not None and not current.get("hasUnsavedChanges"):
            return True, "saved via UI Automation (hasUnsavedChanges went false)"
    return False, (
        f"still dirty after {SAVE_TIMEOUT_SECONDS}s - Desktop may be showing a dialog. Ask the user "
        "to press Ctrl+S in Power BI Desktop, then re-run with --verify-only."
    )


def cache_file(pid: int) -> Path | None:
    """`<Name>.SemanticModel/.pbi/cache.abf` for the model this Desktop instance has open."""
    inst = _instance(pid)
    if inst is None or not inst.get("currentFilePath"):
        return None
    pbip = Path(inst["currentFilePath"])
    matches = sorted(pbip.parent.glob("*.SemanticModel/.pbi/cache.abf"))
    return matches[0] if matches else None


def row_count(port: int) -> tuple[int, str]:
    """Rows in the first queryable table - the gate of record.

    A refresh that "succeeded" but returns no rows is not a refresh; only data proves the source was
    reachable, the credential worked and the M was valid.
    """
    adomd_connection = _load_adomd()
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        table = first_table(conn)
        cmd = conn.CreateCommand()
        cmd.CommandText = f"EVALUATE ROW(\"n\", COUNTROWS('{table}'))"
        reader = cmd.ExecuteReader()
        rows = 0
        while reader.Read():
            value = reader.GetValue(0)
            rows = int(value) if value is not None else 0
        reader.Close()
        return rows, table
    finally:
        conn.Close()


def _refresh_and_save(pid: int, port: int, args: argparse.Namespace) -> int | None:
    """Run the refresh and (unless suppressed) persist it. Returns an exit code, or None to continue."""
    try:
        ok, message = refresh(port, args.tables)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"REFRESH: ERROR {type(exc).__name__}: {exc}")
        return 2
    print(f"  refresh: {message}" if ok else f"  refresh FAILED: {message}")

    if not args.no_save:
        saved, save_message = save(pid)
        print(f"  save   : {save_message}")
        if not saved:
            print("REFRESH: NOT_PERSISTED (data is in memory only - the next open will be empty)")
            return 1
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: refresh, save, and prove data is really there."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pid", type=int, help="Power BI Desktop process id")
    parser.add_argument("--port", type=int, help="Local AS port (default: auto-discover)")
    parser.add_argument("--tables", nargs="*", help="Tables to refresh (default: whole database)")
    parser.add_argument("--no-save", action="store_true", help="Refresh only; do NOT persist the cache")
    parser.add_argument("--verify-only", action="store_true", help="Skip refresh/save; just report the state")
    args = parser.parse_args(argv)

    pid = args.pid
    if pid is None:
        inst = _instance(None)
        pid = inst.get("pid") if inst else None
    if pid is None:
        print("REFRESH: ERROR no Power BI Desktop instance found (open the .pbip first)")
        return 2

    port = args.port or discover_port(pid)
    before_cache = cache_file(pid)
    before_stamp = before_cache.stat().st_mtime if before_cache and before_cache.exists() else 0.0

    if not args.verify_only:
        outcome = _refresh_and_save(pid, port, args)
        if outcome is not None:
            return outcome

    rows, table = row_count(port)

    after_cache = cache_file(pid)
    after_stamp = after_cache.stat().st_mtime if after_cache and after_cache.exists() else 0.0
    persisted = after_cache is not None and after_stamp > before_stamp

    print(f"  data   : {rows} row(s) in '{table}'")
    print(f"  cache  : {after_cache if after_cache else '<none>'}{' (updated)' if persisted else ''}")

    if rows <= 0:
        print("REFRESH: NO_DATA (refresh ran but the table is empty - check the source and credentials)")
        return 1
    if not args.no_save and not args.verify_only and not persisted:
        print("REFRESH: NOT_PERSISTED (model has data in memory, but cache.abf did not update)")
        return 1
    print("REFRESH: DATA_OK" + ("" if args.no_save or args.verify_only else " + PERSISTED"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
