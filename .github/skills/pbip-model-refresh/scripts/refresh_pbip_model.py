"""
purpose: Refresh a local PBIP model in Power BI Desktop and PERSIST the result, so the next agent
         (and the next Desktop open) sees real data instead of an empty model.
usage:   python .github/skills/pbip-model-refresh/scripts/refresh_pbip_model.py
             [--pid <pbidesktop-pid>] [--tables "A" "B"] [--timeout-sec 90] [--save]
         (ships inside the `pbip-model-refresh` skill; run it by its path from wherever the folder
          was copied. `scripts/refresh_pbip_model.py` in this repo is a forwarding shim.)

Why this exists
---------------
Two separate gaps kept biting real migrations:

1. **The refresh itself was hand-rolled every time.** The only working path against a local PBIP is
   TOM/XMLA over the child `msmdsrv` port, which means: discover the port, load ADOMD.NET, resolve
   the catalog GUID, then send a TMSL `refresh`. Re-deriving that per migration is slow and easy to
   get subtly wrong.

2. **The refresh was not persisted.** Power BI Desktop caches a PBIP's data in
   `<Name>.SemanticModel/.pbi/cache.abf` (gitignored, it is data), and an XMLA refresh alone only
   populates the *in-memory* model - so if nothing writes that cache, the next agent opens an empty
   model and has to refresh again (hitting the credential prompt all over again).

   **There IS a programmatic save: AMO `Server.ImageSave(databaseId, stream)`.** This writes the
   cache file directly, so no UI is involved and the flow works headless.

   Finding it required disbelieving a plausible answer. The Power BI product group's guidance is
   that the Modeling MCP never touches the Desktop Bridge - Desktop hosts a local Analysis Services
   instance, the MCP matches it by open file name and connects to `localhost:<port>`, so every
   modeling tool acts on the in-memory engine - and that *"writing that state back to the .pbip /
   cache.abf file is a separate Save action that only the Desktop UI performs."* True of the
   MCP/Bridge surface, but NOT of the engine: probing it showed a TMSL `backup` is refused only
   because Desktop runs Analysis Services in **Diskless mode** (*"Backup/Restore ... not
   supported"*), while that same configuration sets `EnableDisklessTMImageSave=1`. AMO exposes the
   corresponding call, and it works.

   **Proven end to end 2026-07-30, with the control that rules out a silent re-refresh:** delete
   `cache.abf` -> refresh in memory -> `ImageSave` -> **kill Desktop with -Force** (no save prompt)
   -> **rename the source data folder away** -> reopen -> `DATA_OK` with real rows. With the source
   absent, that data can only have come from the cache. Output is ~113-114 KB, matching Desktop's
   own save (114.8 KB).

   Two implementation notes: the AMO client raises *"The server sent an unrecognizable response"*
   while writing the file correctly, so success is judged by the FILE (exists, non-empty, newly
   written), never by the absence of an exception; and `database_operations ExportToTmdlFolder`
   persists model *definition* changes but carries no rows, so it cannot substitute for this.

Cache invalidation (important)
------------------------------
Desktop discards `cache.abf` when the model **definition** is newer than the cache. Verified: a
model whose `definition/*.tmdl` were touched a week after the cache was written opened with
`NO_DATA` despite a 113 KB cache sitting right there. So: make ALL model edits FIRST, then refresh,
then save. Anything that rewrites TMDL afterwards - including the host repo's sanitize step (here,
`scripts/set_data_folder.py --sanitize`, which you must run before committing) - invalidates it.

Binding to the right instance (parallel batches)
------------------------------------------------
The destination is resolved from the pid (`cache_file`), but the DATA comes from whatever Analysis
Services instance answers on `port`. If those two ever disagree - one bad `--port`, or a widened
port lookup - this writes a **sibling migration's model into your own correct `cache.abf`**, and
nothing catches it: `image_save` can only check that the file exists, is non-empty and is newly
written, and `row_count` queries that same wrong port, so both signals agree and both are wrong.
Hence `same_model()` runs FIRST, before the refresh: it compares the connected model's tables with
the TMDL that owns the destination cache, and a mismatch aborts with `REFRESH: WRONG_MODEL`. File
metadata fundamentally cannot tell you whose rows are in the blob; only the model's own contents can.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from probe_desktop_query import AUTO_DATE_TABLE_PREFIXES, _load_adomd, discover_port, first_table, table_names

SAVE_SETTLE_SECONDS = 3
SAVE_TIMEOUT_SECONDS = 120
# Bounded so a missing credential fails FAST instead of hanging on an invisible sign-in modal.
# 90s is comfortably above a serverless warehouse cold start (~30-60s) and far below the "agent
# looks busy but is permanently stuck" territory that made this necessary.
REFRESH_TIMEOUT_SECONDS = 90

# A TMDL table declaration sits at column 0 of `definition/tables/<Name>.tmdl`; the name is quoted
# only when it needs to be (spaces, punctuation), so both forms have to be accepted.
TABLE_DECL_RE = re.compile(r"^table\s+(?:'([^']+)'|(\S+))", re.MULTILINE)


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


def refresh(port: int, tables: list[str] | None, timeout_sec: int = REFRESH_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Send a TMSL refresh over XMLA. Returns (ok, message).

    Refreshing named tables is preferred over the whole database: a full refresh can hang for
    minutes on a large table that no report even uses.

    **The timeout is real, but it does NOT bound the case it was added for.** Measured 2026-08-01,
    both halves, each with the timeout verified on readback:

    - ✅ It works at the XMLA layer. An expensive `EVALUATE` under `CommandTimeout = 1` / `5` raised
      `AdomdErrorResponseException: The XML for Analysis request timed out ... Timeout value: N sec`
      after 1.2 s / 6.0 s. Honoured, and precisely.
    - ❌ It does not interrupt a credential wait. A one-table refresh against a Databricks source with
      no cached credential, `CommandTimeout = 45`, ran **past 150 s** (hard wall-clock guard hit, call
      never returned). Desktop stayed `Responding` throughout and no `cache.abf` was written.
      ⚠️ Inferred mechanism: the mashup engine parks in a synchronous wait on a UI dialog in another
      process, which the server cannot preempt the way it aborts a running query.

    So it cuts short a source that is merely *slow*, and never one that is waiting on a human.
    **The caller must run its own clock** — that agent-side "~2 minutes then stop and ask" rule is the
    only thing that actually bounds this.

    Historical note, because it nearly went in the other direction: an earlier attempt to measure this
    was run against the *stale plugin copy* of this file, which had no timeout at all. It "ran past
    90 s" because there was no 90. Never take a timing measurement against a bundle preflight reports
    as STALE.
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
        cmd.CommandTimeout = timeout_sec
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


def _instances() -> list[dict]:
    return _bridge_status().get("instances", [])


def _instance(pid: int | None) -> dict | None:
    """The bridge's record for `pid` - or, with no pid, the ONLY instance (never an arbitrary one).

    Returning the first instance for `pid is None` is fine interactively but ambiguous in a parallel
    batch: it silently binds the run to whichever Desktop the bridge happened to list first.
    """
    instances = _instances()
    if pid is not None:
        return next((inst for inst in instances if inst.get("pid") == pid), None)
    return instances[0] if len(instances) == 1 else None


def _load_amo():
    """Load AMO (Microsoft.AnalysisServices.Tabular) for the ImageSave path.

    Same CoreCLR-hosting constraint as `_load_adomd`: pythonnet must host CoreCLR before `import clr`.
    """
    # pylint: disable=import-outside-toplevel,import-error
    import glob

    from pythonnet import load

    load("coreclr")
    import clr

    base = os.path.expanduser(r"~\.nuget\packages")
    for dll in glob.glob(os.path.join(base, "**", "netcoreapp*", "Microsoft.AnalysisServices*.dll"), recursive=True):
        if "resources" in dll.lower():
            continue
        try:
            # pylint: disable-next=no-member  # clr's members are generated at runtime by pythonnet
            clr.AddReference(dll)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            continue
    from Microsoft.AnalysisServices.Tabular import Server  # noqa: PLC0415

    return Server


def image_save(port: int, cache_path: Path) -> tuple[bool, str]:
    """Persist the in-memory model to `<Name>.SemanticModel/.pbi/cache.abf` via AMO `Server.ImageSave`.

    This is the programmatic equivalent of Desktop's Save, and it removes the need to drive the UI.

    Discovered 2026-07-30 by probing the engine rather than accepting "only the Desktop UI can do it":
    a TMSL `backup` is refused because Desktop runs its Analysis Services instance in **Diskless mode**
    ("Backup/Restore ... not supported"), but that same mode sets `EnableDisklessTMImageSave=1`, and
    AMO exposes `Server.ImageSave(databaseId, Stream)`. Proven end to end: delete cache.abf → refresh
    in memory → ImageSave → **kill Desktop with -Force (no save prompt)** → reopen → DATA_OK, no
    refresh needed. The bytes match Desktop's own save closely (114.1 KB vs 114.8 KB).

    Note the client throws "The server sent an unrecognizable response" while writing correctly, so
    success is judged by the FILE (exists, non-empty, newly written), never by the absence of an
    exception.
    """
    server_type = _load_amo()
    from System.IO import FileAccess, FileMode, FileStream  # noqa: PLC0415  # pylint: disable=import-outside-toplevel,import-error

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    before = cache_path.stat().st_mtime if cache_path.exists() else 0.0

    server = server_type()
    server.Connect(f"Data Source=localhost:{port}")
    try:
        database_id = server.Databases[0].ID
        stream = FileStream(str(cache_path), FileMode.Create, FileAccess.Write)
        try:
            server.ImageSave(database_id, stream)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            # Expected: the response parser trips even on a successful write. Fall through to the
            # file check, which is the real evidence.
            del exc
        finally:
            stream.Close()
    finally:
        server.Disconnect()

    if cache_path.exists() and cache_path.stat().st_size > 0 and cache_path.stat().st_mtime > before:
        return True, f"persisted via AMO ImageSave ({cache_path.stat().st_size / 1024:.1f} KB)"
    return False, "ImageSave did not produce a cache file"


def save(pid: int) -> tuple[bool, str]:
    """Save the Desktop file, then verify the save actually happened.

    LEGACY FALLBACK - image_save() is the default path now. Kept for the case where ImageSave is
    unavailable. Not SendKeys: verified 2026-07-30 that
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
    """Where `<Name>.SemanticModel/.pbi/cache.abf` belongs for the model this instance has open.

    Resolves the DESTINATION, not an existing file: on a first build there is no cache yet, and
    globbing for one returned None and silently sent the save down the UI-Automation fallback.
    """
    inst = _instance(pid)
    if inst is None or not inst.get("currentFilePath"):
        return None
    pbip = Path(inst["currentFilePath"])
    models = sorted(pbip.parent.glob("*.SemanticModel"))
    if not models:
        return None
    # Prefer the model matching the .pbip name when a folder holds several.
    stem = pbip.stem.lower()
    chosen = next((m for m in models if m.stem.lower() == stem), models[0])
    return chosen / ".pbi" / "cache.abf"


def tmdl_tables(model_dir: Path) -> set[str]:
    """Table names declared by the TMDL on disk - the fingerprint of the model that owns the cache.

    `utf-8-sig` and the auto date-table filter both matter to the *gate*, not just to tidiness:
    Desktop writes TMDL with a BOM, and a BOM immediately followed by the declaration would make
    `^table` miss every file - leaving an empty fingerprint, which `same_model` reports as
    "unverified" and lets through. Auto date tables are serialized into `tables/` when auto
    date/time is (or ever was) on, and they are filtered out of the live side, so leaving them here
    would make every such model look like a stranger.
    """
    definition = model_dir / "definition"
    names: set[str] = set()
    for tmdl in sorted(definition.glob("tables/*.tmdl")):
        text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
        names.update(quoted or bare for quoted, bare in TABLE_DECL_RE.findall(text))
    return {name for name in names if not name.startswith(AUTO_DATE_TABLE_PREFIXES)}


def _live_tables(port: int) -> set[str]:
    """Table names in the model currently served on `port` (hidden included, auto date tables not)."""
    adomd_connection = _load_adomd()
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        return set(table_names(conn, include_hidden=True))
    finally:
        conn.Close()


def same_model(port: int, cache_path: Path | None) -> tuple[bool, str]:
    """Is the model served on `port` the one that owns `cache_path`? Returns (ok, message).

    The only check that can catch a wrong-instance bind. The destination path is resolved from the
    pid so it is always right; the DATA comes from the port, so a widened/mistyped port silently
    writes a sibling migration's model into this migration's cache.abf - and every other signal
    (file exists, non-empty, mtime advanced, row count) agrees with it, because they all read the
    same wrong source. Comparing the model's own tables is what breaks that self-consistency.

    Extra tables in the engine are fine (a field parameter added in-memory, or anything the caller
    has not exported yet) and are only reported, since TMDL that lags the engine is worth knowing
    about. A TMDL table MISSING from the engine is not: either this is a different model, or the
    definition on disk changed after Desktop opened it - and Desktop discards a cache that is older
    than the definition, so persisting then would be pointless anyway.
    """
    if cache_path is None:
        return True, "identity unverified (no model folder resolved for this pid)"
    model_dir = cache_path.parent.parent
    on_disk = tmdl_tables(model_dir)
    if not on_disk:
        return True, f"identity unverified (no TMDL tables under {model_dir / 'definition'})"
    live = {name.casefold() for name in _live_tables(port)}
    missing = sorted(name for name in on_disk if name.casefold() not in live)
    if missing:
        return False, (
            f"port {port} does NOT serve {model_dir.name}: {len(missing)}/{len(on_disk)} of its TMDL "
            f"tables are absent from the connected model (e.g. {missing[:3]})"
        )
    extra = len(live) - len(on_disk)
    note = f", engine has {extra} more not in TMDL" if extra > 0 else ""
    return True, f"{model_dir.name} confirmed on port {port} ({len(on_disk)} TMDL table(s) present{note})"


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


def _refresh_and_save(pid: int, port: int, cache: Path | None, args: argparse.Namespace) -> int | None:
    """Run the refresh and (unless suppressed) persist it. Returns an exit code, or None to continue.

    `cache` is passed in rather than re-derived: `cache_file` is another Desktop Bridge round trip,
    and the bridge returning nothing (or something else) after the identity gate has run would mean
    writing to a destination that was never verified.
    """
    try:
        ok, message = refresh(port, args.tables, args.timeout_sec)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        text = f"{type(exc).__name__}: {exc}"
        # A timeout has TWO possible causes and this code cannot tell them apart. It used to assert
        # the credential one ("THIS NEEDS A HUMAN. Do not retry"), which is the single most expensive
        # thing it could get wrong: that phrasing names the one blocker an agent is forbidden to
        # retry, so a false positive turns a transient slowdown into a permanent dead end.
        #
        # Measured 2026-08-04: it fired on 2 of 5 Desktop instances opened on the SAME bundle, which
        # refreshed cleanly on the other 3. Real cause: a 246,236-row, 11-table refresh took 38.8s on
        # a good run and over 87s on a slow one, against a 90s ceiling. It cost that run ~45 minutes
        # and produced a headline finding that was flatly wrong until it was re-tested.
        #
        # So: report the observation, offer both hypotheses, and name the arbiter that settles it.
        # Never emit a stop-word instruction from an unverified heuristic.
        if "timeout" in text.lower() or "timed out" in text.lower():
            print(f"REFRESH: TIMEOUT - no result within {args.timeout_sec}s ({text})")
            print(
                "  CAUSE UNKNOWN - this script cannot distinguish these two, and they need\n"
                "  opposite responses:\n"
                "    (a) SLOW: a large model simply needs longer. Re-run with a bigger\n"
                f"        --timeout-sec (this was {args.timeout_sec}s).\n"
                "    (b) BLOCKED: Desktop is showing a data-source sign-in modal no automation\n"
                "        can fill. Retrying cannot dismiss it; a human must sign in once.\n"
                "  SETTLE IT - run the arbiter, do not guess:\n"
                f"    powershell -File scripts/probe_desktop_credential.ps1 -DesktopPid {pid}\n"
                "  A one-row probe bundle also answers this definitively: a refresh limited to a\n"
                "  single row per partition is fast by construction, so a timeout on THAT is\n"
                "  evidence of (b), while a timeout here is not."
            )
            return 3
        print(f"REFRESH: ERROR {text}")
        return 2
    print(f"  refresh: {message}" if ok else f"  refresh FAILED: {message}")

    # Not saving is the DEFAULT. Measured 3-vs-3 (2026-08-04): with a persisted `cache.abf` present,
    # Desktop opened the PBIP as "Untitled - Power BI Desktop" and the bridge reported `Host is not
    # ready to accept operations` (pids 59584, 64668, 50316); with it absent the same bundle loaded
    # correctly (pids 15216, 4888, 37076). Restoring a previously-good cache re-breaks opening, so
    # there is no workaround - which is why the safe behaviour has to be the default rather than an
    # opt-out that callers must remember. `--no-save` is retained as a no-op for existing callers.
    if not args.save:
        return None

    # Preferred: a real API call. Falls back to driving the UI only if it fails, so a change in the
    # engine can never leave the pipeline with no way to persist.
    saved, save_message = (False, "no cache path resolved")
    if cache is not None and not args.ui_save:
        try:
            saved, save_message = image_save(port, cache)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            saved, save_message = False, f"ImageSave unavailable ({type(exc).__name__}); falling back to UI"
    if not saved:
        print(f"  save   : {save_message}")
        saved, save_message = save(pid)
    print(f"  save   : {save_message}")
    if not saved:
        print("REFRESH: NOT_PERSISTED (data is in memory only - the next open will be empty)")
        return 1
    return None


def _resolve_pid(pid: int | None) -> int | None:
    """The Desktop pid to act on: the one given, or the ONLY running instance - never a guess.

    Picking "the first instance the bridge listed" is fine on a one-instance box and ambiguous in a
    parallel batch, where it silently binds the run to somebody else's migration.
    """
    if pid is not None:
        return pid
    running = _instances()
    if len(running) > 1:
        listed = "; ".join(f"{i.get('pid')} -> {i.get('currentFilePath') or '?'}" for i in running)
        print(f"REFRESH: ERROR {len(running)} Power BI Desktop instances are running - name yours with --pid")
        print(f"  instances: {listed}")
        return None
    pid = running[0].get("pid") if running else None
    if pid is None:
        print("REFRESH: ERROR no Power BI Desktop instance found (open the .pbip first)")
    return pid


def _identity_gate(port: int, cache_path: Path | None) -> bool:
    """Print and enforce `same_model`. False means: do not refresh, query or persist this instance."""
    try:
        ok, message = same_model(port, cache_path)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"REFRESH: ERROR could not read the model on port {port}: {type(exc).__name__}: {exc}")
        return False
    print(f"  model  : {message}")
    if not ok:
        print("REFRESH: WRONG_MODEL (refusing to refresh or persist another instance's model)")
    return ok


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: refresh, save, and prove data is really there."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pid",
        type=int,
        help="Power BI Desktop process id - required when several instances are open "
        "(`powerbi-desktop status` maps pid -> open file)",
    )
    parser.add_argument("--port", type=int, help="Local AS port (default: auto-discover)")
    parser.add_argument("--tables", nargs="*", help="Tables to refresh (default: whole database)")
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=REFRESH_TIMEOUT_SECONDS,
        help=(
            f"XMLA refresh ceiling in seconds (default {REFRESH_TIMEOUT_SECONDS}). Raise it for a "
            "large model: a 246,236-row, 11-table refresh was measured at 38.8s on a good run and "
            "over 87s on a slow one. A timeout is reported as TIMEOUT with the cause UNKNOWN - it "
            "is NOT evidence of a credential modal on its own"
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help=(
            "Persist the refreshed model to .pbi/cache.abf via AMO ImageSave. OFF BY DEFAULT: a "
            "persisted cache was measured (3-vs-3, 2026-08-04) to make the PBIP UNOPENABLE - "
            "Desktop opens it as 'Untitled' with no error and the bridge reports 'Host is not "
            "ready to accept operations'. Only pass this when a later step genuinely needs the "
            "data to survive a Desktop restart, and re-open the PBIP afterwards to confirm it still "
            "loads"
        ),
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Deprecated no-op: not saving is now the default. Kept so existing callers still work",
    )
    parser.add_argument("--verify-only", action="store_true", help="Skip refresh/save; just report the state")
    parser.add_argument(
        "--ui-save",
        action="store_true",
        help="Force the legacy UI-Automation save instead of AMO ImageSave (fallback/diagnostic)",
    )
    args = parser.parse_args(argv)

    pid = _resolve_pid(args.pid)
    if pid is None:
        return 2

    port = args.port or discover_port(pid)
    # Resolved ONCE: the path that gets verified must be the path that gets written, and every
    # re-derivation is another Desktop Bridge round trip that can come back empty.
    cache = cache_file(pid)
    before_stamp = cache.stat().st_mtime if cache and cache.exists() else 0.0

    # Gate everything on identity: refreshing, row-counting or persisting a sibling's model is a
    # fully self-consistent false positive, so it has to be caught BEFORE any of the three.
    if not _identity_gate(port, cache):
        return 2

    if not args.verify_only:
        outcome = _refresh_and_save(pid, port, cache, args)
        if outcome is not None:
            return outcome

    rows, table = row_count(port)

    after_stamp = cache.stat().st_mtime if cache and cache.exists() else 0.0
    persisted = cache is not None and after_stamp > before_stamp

    print(f"  data   : {rows} row(s) in '{table}'")
    print(f"  cache  : {cache if cache else '<none>'}{' (updated)' if persisted else ''}")

    if rows <= 0:
        print("REFRESH: NO_DATA (refresh ran but the table is empty - check the source and credentials)")
        return 1
    if args.save and not args.verify_only and not persisted:
        print("REFRESH: NOT_PERSISTED (model has data in memory, but cache.abf did not update)")
        return 1
    print("REFRESH: DATA_OK" + (" + PERSISTED" if args.save and not args.verify_only else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
