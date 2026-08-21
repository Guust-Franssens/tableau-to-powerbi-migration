"""
purpose: Refresh a local PBIP model in Power BI Desktop and PERSIST the result, so the next agent
         (and the next Desktop open) sees real data instead of an empty model.
usage:   python .github/skills/pbip-model-refresh/scripts/refresh_pbip_model.py
             [--pid <pbidesktop-pid>] [--canaries "A" "B"] [--tables "A" "B"]
             [--calculate-only] [--no-save]
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

   !! **That proof holds ONLY when the project's `compatibilityLevel` matches the live database's.**
   Root-caused 2026-08-07: Desktop silently runs the database ABOVE the declared level (measured live
   `1606` vs a declared `1604`), and `ImageSave` serialises the live one - so on a mismatched bundle
   the reopen fails with a CompatibilityLevel *downgrade* error and no visible message. That is why
   the original run succeeded and later ones did not: the difference was the bundle, not the method.
   `image_save()` aligns `database.tmdl` to the live level as part of saving, which is what
   Desktop's own Save does. See the `--save` warning below.

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
Services instance answers on `port`. If those two ever disagree - a widened port lookup, or a
`--port` pointed at another instance - this would write a **sibling migration's model into your own
correct `cache.abf`**, and file metadata could not catch it: `image_save` can only check that the
file exists, is non-empty and is newly written, and `row_counts` queries that same wrong port, so
both signals agree and both are wrong. Two defences run BEFORE the refresh: `--port`, if given, must
equal the port derived from the pid (a mismatch aborts, so it can never bypass pid-based discovery),
and `same_model()` compares the connected model's tables with the TMDL that owns the destination
cache and aborts on any difference with `REFRESH: WRONG_MODEL`. File metadata fundamentally cannot
tell you whose rows are in the blob; only the model's own contents can.
"""

from __future__ import annotations

# pylint: disable=too-many-lines

import argparse
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from probe_desktop_query import (
    _load_adomd,
    column_names,
    discover_port,
    first_table,
    is_auto_date_table_name,
    measure_names,
    nuget_packages_root,
    table_names,
)

# Cache-file integrity + atomic staged-write/rollback primitives live in _abf so this module
# stays under its line cap; re-exported here so callers and tests reach them via refresh_pbip_model.
from _abf import (  # noqa: F401  # pylint: disable=unused-import
    _ABF_BLOCK_MAGIC,
    _ABF_MAX_BLOCK_BYTES,
    _ABF_PREAMBLE,
    CompatRollbackError,
    _abf_rejection_reason,
    _cache_committed,
    _cache_fingerprint,
    _cleanup_staging,
    _is_complete_abf,
    _restore_rollback_snapshot,
    _snapshot_rollback_paths,
    _staged_image_write,
    _staging_path,
)

# Per-model interprocess lock for the persist transaction, in its own module for the same reason;
# re-exported so callers and tests reach it via refresh_pbip_model.
from _lock import (  # noqa: F401  # pylint: disable=unused-import
    ModelLockTimeout,
    model_lock,
)

# The verdict/reporting layer lives in _verdict for the same line-cap reason as _abf and _lock;
# re-exported here so callers and tests reach it via refresh_pbip_model.
from _verdict import (  # noqa: F401  # pylint: disable=unused-import
    _canary_tables,
    _emit_data_verdict,
)
from _credential_modal import (
    CredentialDetection,
    CredentialMissingError,
    CredentialModal,
    CredentialUnknownError,
    DesktopGoneError,
    DesktopUnreadyError,
    DialogBlockedError,
    describe_blocking_dialog,
    describe_modal,
    inspect_credential_modal,
    join_with_credential_poll,
    print_refresh_banner,
    print_refresh_unknown_banner,
    source_hint_from_model,
)

SAVE_SETTLE_SECONDS = 3
SAVE_TIMEOUT_SECONDS = 120
# How long a persist waits for a CONCURRENT persist of the same model to finish before giving up
# (issue #114). A persist writes ~114 KB and takes seconds, so a live peer clears well within this;
# the wait only bites a genuinely stuck LIVE holder (a dead holder's lock is reclaimed at once, never
# waited on). On timeout `_persist_image` raises `ModelLockTimeout` and `_refresh_and_save` REFUSES to
# persist (`NOT_PERSISTED`), naming the concurrent run.
PERSIST_LOCK_TIMEOUT_SECONDS = 120.0
REFRESH_TYPE_FULL = "full"
REFRESH_TYPE_CALCULATE = "calculate"
REFRESH_TYPES = {REFRESH_TYPE_FULL, REFRESH_TYPE_CALCULATE}

# The XMLA refresh ceiling. NOT agent-tunable on purpose - there is no CLI flag for it.
#
# 300s is chosen from measurement, not intuition. A 246,236-row, 11-table refresh took 38.8s on a
# good run and over 87s on a slow one (2026-08-04), so the previous 90s ceiling left a ~3s margin and
# duly false-positived on 2 of 5 Desktop instances opened on the SAME bundle. 5 minutes is
# comfortably clear of that, and still far below the "agent looks busy but is permanently stuck"
# territory this bound exists to prevent.
#
# Why no flag: a knob here is an attractive nuisance. An agent that hits a timeout will reach for a
# bigger number - and the one case where waiting longer never helps is the credential wait, which
# `refresh()` documents this ceiling cannot interrupt anyway. If a model legitimately needs longer,
# the right lever is `--tables` (refresh only what is needed), not a longer wait.
REFRESH_TIMEOUT_SECONDS = 300

# Grace on top of the XMLA ceiling before the outer wall clock gives up. The XMLA layer honours
# `CommandTimeout` precisely for a genuinely slow *query*, so letting it fire first yields a far
# better error ("The XML for Analysis request timed out ... Timeout value: N sec") than a generic
# wall-clock abort. The outer bound only exists to catch the case XMLA provably cannot interrupt:
# a mashup engine parked on a sign-in modal in another process.
REFRESH_WALL_CLOCK_GRACE_SECONDS = 30
REFRESH_CREDENTIAL_POLL_SECONDS = 5.0
REFRESH_HEARTBEAT_SECONDS = 30.0

# A TMDL table declaration sits at column 0 of `definition/tables/<Name>.tmdl`; the name is quoted
# only when it needs to be (spaces, punctuation), so both forms have to be accepted.
TABLE_DECL_RE = re.compile(r"^table\s+(?:'([^']+)'|(\S+))", re.MULTILINE)
# A column/measure declaration sits INDENTED under its table, and is quoted only when it must be.
# The unquoted branch stops at whitespace OR `=`, so a calculated column/measure (`column 'X' = expr`
# or `measure Total = SUM(...)`) yields just the name, never the expression that follows.
COLUMN_DECL_RE = re.compile(r"^\s*column\s+(?:'([^']+)'|([^=\s]+))", re.MULTILINE)
MEASURE_DECL_RE = re.compile(r"^\s*measure\s+(?:'([^']+)'|([^=\s]+))", re.MULTILINE)
GENERATED_ARTIFACTS_KEY = "generated_artifacts"
GENERATED_EDIT_DECLARATIONS = Path("_build") / "generated-edit-declarations.json"


# The credential arbiter that settles a refresh TIMEOUT (slow source vs. sign-in modal). Resolved
# relative to THIS file so the runtime recovery instruction always points at the copy that ships
# INSIDE the bundle - it must not name a path that only exists in the host repo (issue #118).
CREDENTIAL_PROBE = Path(__file__).resolve().parent / "probe_desktop_credential.ps1"


def _credential_state(pid: int) -> CredentialDetection:
    """Return the credential-modal inspection state for ``pid``."""
    if os.name != "nt":
        return CredentialDetection()
    return inspect_credential_modal(pid)


def _raise_if_blocked(pid: int, state: CredentialDetection, source_hint: str | None = None) -> None:
    """Raise the appropriate exception when ``state`` contains a blocking or terminal condition."""
    if state.modal is not None:
        raise CredentialMissingError(pid, state.modal, source_hint)
    if state.blocking_dialog is not None:
        raise DialogBlockedError(pid, state.blocking_dialog)
    if state.process_gone is not None:
        raise DesktopGoneError(pid, state.process_gone)


def _emit_credential_missing(pid: int, modal: CredentialModal, source_hint: str | None = None) -> None:
    """Print the distinct credential verdict."""
    print(f"REFRESH: CREDENTIAL_MISSING pid={pid}; {describe_modal(modal, source_hint)}")


def _emit_blocked_by_dialog(pid: int, dialog) -> None:
    """Print the distinct generic blocking-dialog verdict."""
    print(f"REFRESH: BLOCKED_BY_DIALOG pid={pid}; {describe_blocking_dialog(dialog)}")


def _emit_credential_unknown(pid: int, reason: str) -> None:
    """Print the distinct indeterminate verdict for a latched, unrecoverable UNKNOWN (issue #154).

    ``CREDENTIAL_UNKNOWN`` is a THIRD verdict, deliberately not collapsed into ``BLOCKED_BY_DIALOG``
    (we did not SEE a block) nor into a bare ``TIMEOUT`` (we did not observe a healthy source): the
    owner window went iconic, hiding its owned modal dialogs, and that evidence provably does not come
    back. ``reason`` is the detector's marker-free string, and the guidance below is marker-free too,
    so the classification is carried STRUCTURALLY by the ``REFRESH: CREDENTIAL_UNKNOWN`` verdict line
    (matched by ``probe_live_source.CREDENTIAL_STOP_VERDICT_RE``), never by prose (issue #153).
    """
    print(f"REFRESH: CREDENTIAL_UNKNOWN pid={pid}; {reason}")
    print(
        "  This run stayed indeterminate to the deadline: the owner window was iconic at least once,\n"
        "  which hides its owned modal dialogs from enumeration, and that evidence does not return when\n"
        "  the window is restored. This is NOT a slow source - do not simply wait or retry longer.\n"
        "  SETTLE IT - restore the Power BI Desktop window, complete whatever dialog it is showing, and\n"
        "  run the arbiter that ships beside this script rather than guessing:\n"
        f'    powershell -File "{CREDENTIAL_PROBE}" -DesktopPid {pid}'
    )


def _emit_desktop_gone(pid: int, reason: str) -> None:
    """Print the distinct terminal verdict for a Power BI Desktop that has exited/crashed (issue #158).

    ``DESKTOP_GONE`` is a DEFINITIVE local-environment failure, not a fact about the data source: the
    tracked process enumerated zero windows and is no longer running, so the probe never got to observe
    the source at all. It must never degrade to a slow-source timeout. It is also distinct from
    ``DESKTOP_UNREADY`` (zero windows while the process is still ALIVE - starting up or wedged, so it
    may yet recover) and from ``CREDENTIAL_UNKNOWN`` (a minimized owner window, whose hidden owned
    dialogs make the credential state indeterminate).
    ``reason`` is the detector's marker-free string and the guidance below is marker-free too, so the
    parent classifier keys on the ``REFRESH: DESKTOP_GONE`` verdict line, never on prose (issue #153).
    """
    print(f"REFRESH: DESKTOP_GONE pid={pid}; {reason}")
    print(
        "  Power BI Desktop is no longer running: it enumerated zero windows and the process id is\n"
        "  gone, so this probe never reached the data source. Do NOT report this as a slow or broken\n"
        "  source - the source was never contacted. Re-open the model in Power BI Desktop, confirm it\n"
        "  is running, and re-run the probe against the new process id."
    )


def _emit_desktop_unready(pid: int, reason: str) -> None:
    """Print the terminal local-error verdict for an ALIVE Desktop that owns no window (issue #158).

    A live, working Desktop always owns at least its main window, so zero windows plus a live process
    means it is starting up or wedged: its dialog state cannot be read, and nothing was learned about
    the data source. It is emitted as its own ``DESKTOP_UNREADY`` verdict, at the ERROR-family exit 2,
    for two reasons. It is not ``CREDENTIAL_UNKNOWN`` (exit 3), because no human sign-in fixes a
    window-less process and that verdict would route the run to the credential layer. It is not
    ``DESKTOP_GONE``, because the process may still recover. ``reason`` is the detector's marker-free
    string and the guidance below is marker-free too, so the parent classifier keys on the
    ``REFRESH: DESKTOP_UNREADY`` verdict line, never on prose (issue #153).
    """
    print(f"REFRESH: DESKTOP_UNREADY pid={pid}; {reason}")
    print(
        "  Power BI Desktop is running but has no window, so this probe cannot inspect its local state.\n"
        "  Wait for Desktop to finish starting, or restart it if it is wedged, then re-run the probe.\n"
        "  The data source was not tested; do not report this as a source or sign-in problem."
    )


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


# pylint: disable=too-many-arguments,too-many-statements
def refresh(
    port: int,
    tables: list[str] | None,
    timeout_sec: int = REFRESH_TIMEOUT_SECONDS,
    *,
    refresh_type: str = REFRESH_TYPE_FULL,
    desktop_pid: int | None = None,
    source_hint: str | None = None,
    initial_state: CredentialDetection | None = None,
) -> tuple[bool, str]:
    """Send a TMSL refresh over XMLA. Returns (ok, message).

    Refreshing named tables is preferred over the whole database: a full refresh can hang for
    minutes on a large table that no report even uses. ``refresh_type='calculate'`` is an opt-in
    DAX-only path: it recalculates formulas/relationships/hierarchies without re-reading source rows.

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
    **Hence the outer wall clock below.** This used to say "the caller must run its own clock", and
    that was a real defect dressed up as documentation: `probe_live_source.py` did wrap it
    (`subprocess.run(..., timeout=...)`), but every *direct* caller inherited nothing. Measured
    2026-08-05, a direct `--pid` call against a never-authenticated Azure SQL server sat blocked on a
    modal for **956 s** while `REFRESH_TIMEOUT_SECONDS = 300` never fired — and the caller who forgot
    to wrap it was this repo's own agent. A rule an agent must remember is not a bound; a bound is a
    bound.

    The ADOMD call runs on a **daemon** thread so that a parked mashup engine — which genuinely
    cannot be preempted — no longer keeps the *process* alive. We cannot cancel that work, but we can
    always return control and a verdict, which is all any caller needs.

    Historical note, because it nearly went in the other direction: an earlier attempt to measure this
    was run against the *stale plugin copy* of this file, which had no timeout at all. It "ran past
    90 s" because there was no 90. Never take a timing measurement against a bundle preflight reports
    as STALE.
    """
    if refresh_type not in REFRESH_TYPES:
        raise ValueError(f"unsupported refresh type {refresh_type!r}; expected one of {sorted(REFRESH_TYPES)}")

    result: dict[str, tuple[bool, str] | BaseException] = {}
    total_timeout = timeout_sec + REFRESH_WALL_CLOCK_GRACE_SECONDS
    if desktop_pid is not None:
        state = initial_state or _credential_state(desktop_pid)
        _raise_if_blocked(desktop_pid, state, source_hint)
        initial_state = state
        if state.unknown_reason:
            print_refresh_unknown_banner(
                desktop_pid, timeout_sec, REFRESH_WALL_CLOCK_GRACE_SECONDS, state.unknown_reason
            )
        else:
            print_refresh_banner(
                desktop_pid,
                timeout_sec,
                REFRESH_WALL_CLOCK_GRACE_SECONDS,
                operation=refresh_type,
            )

    def _run() -> None:
        conn = None
        try:
            adomd_connection = _load_adomd()
            conn = adomd_connection(f"Data Source=localhost:{port}")
            conn.Open()
            catalog = _catalog_id(conn)
            if tables:
                objects = [{"database": catalog, "table": t} for t in tables]
            else:
                objects = [{"database": catalog}]
            tmsl = json.dumps({"refresh": {"type": refresh_type, "objects": objects}})
            cmd = conn.CreateCommand()
            cmd.CommandText = tmsl
            cmd.CommandTimeout = timeout_sec
            cmd.ExecuteNonQuery()
            target = "/".join(tables) if tables else "entire database"
            verb = "calculated" if refresh_type == REFRESH_TYPE_CALCULATE else "refreshed"
            result["ok"] = (True, f"{verb} {target} (catalog {catalog})")
        except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            # Everything the worker can raise has to be handed back, not left to die on the thread:
            # `main` classifies on the exception text, and an unhandled thread exception would reach
            # it as "worker returned no result" - a generic failure masking a specific, actionable one.
            result["ok"] = exc
        finally:
            if conn is not None:
                try:
                    conn.Close()
                except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                    pass

    worker = threading.Thread(target=_run, name="xmla-refresh", daemon=True)
    worker.start()
    if desktop_pid is None:
        worker.join(total_timeout)
    else:
        join_with_credential_poll(
            worker,
            pid=desktop_pid,
            total_timeout=total_timeout,
            heartbeat_seconds=REFRESH_HEARTBEAT_SECONDS,
            poll_seconds=REFRESH_CREDENTIAL_POLL_SECONDS,
            source_hint=source_hint,
            detector=_credential_state,
            initial_state=initial_state,
        )
    if worker.is_alive():
        if desktop_pid is not None:
            _raise_if_blocked(desktop_pid, _credential_state(desktop_pid), source_hint)
        raise TimeoutError(
            f"refresh did not return within {total_timeout}s "
            f"(XMLA CommandTimeout was {timeout_sec}s and did not fire, which is the signature of a "
            f"mashup engine parked on a sign-in modal rather than a slow query)"
        )

    outcome = result.get("ok")
    if isinstance(outcome, BaseException):
        raise outcome
    if outcome is None:  # pragma: no cover - defensive; the worker always records something
        raise RuntimeError("refresh worker returned no result")
    return outcome


# pylint: enable=too-many-arguments,too-many-statements


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


def amo_dll_glob() -> str:
    """Glob pattern that can locate AMO/TOM in the active NuGet global-packages cache."""
    return str(nuget_packages_root() / "**" / "netcoreapp*" / "Microsoft.AnalysisServices*.dll")


def _load_amo():
    """Load AMO (Microsoft.AnalysisServices.Tabular) for the ImageSave path.

    Same CoreCLR-hosting constraint as `_load_adomd`: pythonnet must host CoreCLR before `import clr`.
    """
    # pylint: disable=import-outside-toplevel,import-error
    import glob

    from pythonnet import load

    load("coreclr")
    import clr

    for dll in glob.glob(amo_dll_glob(), recursive=True):
        if "resources" in dll.lower():
            continue
        try:
            # pylint: disable-next=no-member  # clr's members are generated at runtime by pythonnet
            clr.AddReference(dll)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            continue
    from Microsoft.AnalysisServices.Tabular import Server  # noqa: PLC0415

    return Server


_COMPAT_RE = re.compile(r"^(?P<indent>\s*)compatibilityLevel:\s*(?P<level>\d+)\s*$", re.M)


def sha256_file(path: Path) -> str:
    """Hash a file for generated-edit declaration evidence."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_root_for(path: Path) -> Path | None:
    """Nearest ancestor carrying the engine-run manifest."""
    for parent in (path, *path.parents):
        if (parent / "input_manifest.json").is_file():
            return parent
    return None


def _generated_artifact_run(bundle: Path) -> dict | None:
    """The run identity generated by ``run_estate.py``, if present."""
    try:
        manifest = json.loads((bundle / "input_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    generated = manifest.get(GENERATED_ARTIFACTS_KEY) if isinstance(manifest, dict) else None
    if not isinstance(generated, dict) or generated.get("version") != 1 or not generated.get("run_id"):
        return None
    return generated


def _append_generated_edit_declaration(
    bundle: Path,
    target: Path,
    baseline_sha256: str,
    expected_sha256: str,
    reason: str,
) -> None:
    """Declare this tool's intentional generated-artifact rewrite for ``--tamper``."""
    generated = _generated_artifact_run(bundle)
    if generated is None:
        return
    rel_target = target.relative_to(bundle).as_posix()
    declaration_path = bundle / GENERATED_EDIT_DECLARATIONS
    if declaration_path.is_file():
        payload = json.loads(declaration_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            payload = {"version": 1, "declarations": []}
    else:
        payload = {"version": 1, "declarations": []}
    if not isinstance(payload.get("declarations"), list):
        payload["declarations"] = []
    payload.setdefault("declarations", []).append(
        {
            "version": 1,
            "run_id": generated["run_id"],
            "kind": "changed",
            "target": rel_target,
            "baseline_sha256": baseline_sha256,
            "expected_sha256": expected_sha256,
            "script_identity": "pbip-model-refresh/refresh_pbip_model.py",
            "script_sha256": sha256_file(Path(__file__).resolve()),
            "reason": reason,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    declaration_path.parent.mkdir(parents=True, exist_ok=True)
    declaration_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_declared_compatibility(model_dir: Path) -> tuple[int | None, Path | None]:
    """The ``compatibilityLevel`` declared in ``definition/database.tmdl``, or ``(None, None)``."""
    path = model_dir / "definition" / "database.tmdl"
    if not path.exists():
        return None, None
    match = _COMPAT_RE.search(path.read_text(encoding="utf-8"))
    return (int(match.group("level")), path) if match else (None, path)


def align_declared_compatibility(path: Path, level: int) -> None:
    """Rewrite ``database.tmdl``'s declared level. **Never** writes a BOM.

    Desktop's project reader hard-rejects a UTF-8 BOM (`UTF8EncodingThrowOnBOM.CheckBom` ->
    "Only text with UTF8 encoding without BOM is supported"), and the file simply does not open --
    so this uses plain ``utf-8`` and the caller re-asserts BOM-free afterwards.
    """
    text = path.read_text(encoding="utf-8")
    new_text = _COMPAT_RE.sub(lambda m: f"{m.group('indent')}compatibilityLevel: {level}", text, count=1)
    path.write_text(new_text, encoding="utf-8", newline="")


def _align_compatibility(model_dir: Path | None, live_level: int) -> str | None:
    """Make ``database.tmdl`` declare the level the live database is actually running at.

    Returns a note describing the change, or ``None`` when nothing needed doing.

    **This is not optional and there is no flag for it**, because there is no useful other
    behaviour: a cache is only loadable when its level matches the project's, so refusing to align
    would just be a `--save` that cannot save. It is also exactly what Desktop's own Save does
    (measured: `1604 -> 1606` written at the same timestamp as `cache.abf`). An earlier version made
    it opt-in behind `--align-compat`; that was ceremony rather than safety, since the only response
    to the refusal is to re-run with the flag.

    The edit is written eagerly, but the caller (:func:`_persist_image`) treats it as **provisional**:
    it snapshots the files this can touch first and rolls them back if the ImageSave that follows
    fails, so a mid-failure never leaves ``database.tmdl`` bumped for a cache that was not written.
    """
    if model_dir is None:
        return None
    declared, declared_path = read_declared_compatibility(model_dir)
    if declared is None or declared == live_level:
        return None
    before_hash = sha256_file(declared_path)
    align_declared_compatibility(declared_path, live_level)
    after_hash = sha256_file(declared_path)
    bundle = _bundle_root_for(model_dir)
    if bundle is not None:
        _append_generated_edit_declaration(
            bundle,
            declared_path,
            before_hash,
            after_hash,
            f"Desktop raised compatibilityLevel {declared} -> {live_level} during ImageSave",
        )
    return f"aligned {declared_path.name} {declared} -> {live_level} (Desktop runs the model at {live_level})"


def _compat_rollback_paths(model_dir: Path | None) -> list[Path]:
    """The files an alignment can touch, so a failed persist can restore them exactly.

    Two files: ``database.tmdl`` (its declared level) and the generated-edit declaration ledger
    (``_build/generated-edit-declarations.json``). Rolling the level back without also dropping the
    declaration would leave a record of a change that did not stick.
    """
    if model_dir is None:
        return []
    paths = [model_dir / "definition" / "database.tmdl"]
    bundle = _bundle_root_for(model_dir)
    if bundle is not None:
        paths.append(bundle / GENERATED_EDIT_DECLARATIONS)
    return paths


def _is_benign_imagesave_response_error(exc: BaseException) -> bool:
    """AMO's ImageSave raises even after a fully correct write - only THAT error is benign.

    Measured: the client throws "The server sent an unrecognizable response" while the backup is
    written correctly, so a save cannot be judged by the absence of an exception. But every OTHER
    failure (disk full, permission denied, the stream closing mid-write) MUST propagate, or a partial
    write is mistaken for success and destroys the existing cache - the precise #113 defect. Matching
    only the known message fails SAFE if the wording ever changes: a real write is treated as failed
    (the pipeline falls back to the UI save), never a partial write treated as a success.
    """
    return "unrecognizable response" in str(exc).lower()


def _persist_image(
    cache_path: Path,
    model_dir: Path | None,
    live_level: int,
    write_image,
    lock_timeout: float = PERSIST_LOCK_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Align compat, stage the cache write, swap atomically, and roll compat back UNLESS it committed.

    Pure-Python (no .NET), so the guarantees are unit-testable without a live AS instance.
    ``write_image(staging_path)`` performs the actual engine write. Commit is decided by the
    FILESYSTEM (:func:`_cache_committed`), so an ``os.replace`` that moved the file yet still raised is
    recognised as committed and the alignment KEPT - never rolled back under a freshly installed cache.
    When the write did NOT land, the alignment is restored atomically from the snapshot, every path is
    attempted, and a residual failure raises :class:`CompatRollbackError` (round-3 blocker 2).

    The ENTIRE transaction (snapshot -> align -> write -> commit-or-rollback) runs under a per-model
    interprocess lock (issue #114). Two runs against one model share BOTH the staging file (now given
    a private per-run name) AND the provisional compatibility edit on ``database.tmdl``; a hostile
    interleaving of the compat edit alone leaves the project declaring a level no present cache
    matches. The lock serialises the whole transaction so no interleaving is possible; a dead holder's
    lock is reclaimed rather than blocking forever, and a live holder that overruns ``lock_timeout``
    surfaces as :class:`ModelLockTimeout` rather than an unbounded wait.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_name(cache_path.name + ".lock")
    with model_lock(lock_path, timeout=lock_timeout):
        return _persist_image_locked(cache_path, model_dir, live_level, write_image)


def _persist_image_locked(cache_path: Path, model_dir: Path | None, live_level: int, write_image) -> tuple[bool, str]:
    """The persist transaction itself, run while the per-model lock is held (see :func:`_persist_image`).

    ``KeyboardInterrupt`` handling is the subtle part (issue #113 route 2). The commit-detection and
    rollback below MUST run even when the write is interrupted, or a Ctrl+C during alignment/ImageSave
    leaves ``database.tmdl`` bumped for a cache that was never written - the same brick, different
    route. So the write is guarded with ``except BaseException`` (``KeyboardInterrupt`` is NOT an
    ``Exception``), the interrupt is stashed in ``write_error``, and it is re-raised only AFTER a clean
    rollback. If the rollback itself fails, :class:`CompatRollbackError` is raised INSTEAD (chained
    from the interrupt): a bricked project matters more than a tidy Ctrl+C.
    """
    rollback_paths = _compat_rollback_paths(model_dir)
    snapshot = _snapshot_rollback_paths(rollback_paths)
    staging = _staging_path(cache_path)
    before = _cache_fingerprint(cache_path)
    swapped = False
    write_error: BaseException | None = None
    try:
        aligned = _align_compatibility(model_dir, live_level)
        if aligned:
            print(f"  save   : {aligned}")
        swapped = _staged_image_write(cache_path, write_image, staging)
    except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught  # KeyboardInterrupt must NOT bypass rollback; commit judged below by the filesystem
        write_error = exc

    if swapped or _cache_committed(cache_path, staging, before):
        _cleanup_staging(staging)
        try:
            size_note = f"{cache_path.stat().st_size / 1024:.1f} KB, "
        except OSError:
            size_note = ""
        return True, f"persisted via AMO ImageSave ({size_note}compatibilityLevel {live_level})"

    # The cache was NOT installed, so the provisional alignment must come back exactly.
    _cleanup_staging(staging)
    failures = _restore_rollback_snapshot(snapshot)
    if failures:
        raise CompatRollbackError(
            "compatibility rollback FAILED for "
            + ", ".join(str(path) for path in failures)
            + " - database.tmdl no longer matches the cache; restore it from source control before retrying"
        ) from write_error
    if write_error is not None:
        # A real write/replace failure OR an interrupt, WITH a clean rollback: the project is
        # consistent again, so re-raise. For an interrupt this honours the Ctrl+C; for an ordinary
        # ImageSave failure the caller falls back to the UI save.
        raise write_error
    return False, "ImageSave did not produce a complete cache file (compatibility alignment rolled back)"


def image_save(port: int, cache_path: Path, model_dir: Path | None = None):
    """Persist the in-memory model to ``<Name>.SemanticModel/.pbi/cache.abf`` via AMO ``ImageSave``.

    ⚠️ **A cache is only loadable if its compatibility level MATCHES the project's**, so this
    ALIGNS ``database.tmdl`` to the live level as part of saving -- exactly what Desktop's own Save
    does. Root-caused 2026-08-07: Desktop silently runs the database ABOVE the declared level (we
    measured live ``1606`` against a ``database.tmdl`` of ``1604``, server default ``1700``).
    ``ImageSave`` serialises the *live* database, so without the alignment the cache is a 1606 image
    in a 1604 project and the reopen hits Desktop's own **"Tabular databases do not support
    CompatibilityLevel downgrade"** -- which surfaces with NO visible message, only an
    ``Untitled - Power BI Desktop`` window and a bridge ``Host is not ready to accept operations``.

    ⚠️ **Saving therefore EDITS the deployable artifact.** ``database.tmdl`` is part of what gets
    deployed, and this raises its declared level. If a downstream target cannot accept the higher
    level, pass ``--no-save`` -- refresh-in-memory-persist-nothing exists exactly for the
    validate-then-deploy path, and leaves the project byte-identical.

    The write is **staged and swapped atomically** and the compatibility alignment is **rolled back
    on failure** (see :func:`_persist_image` / :func:`_staged_image_write`), so a mid-failure can
    neither destroy an existing good cache nor leave ``database.tmdl`` bumped for a cache that was
    never written. Note the client throws "The server sent an unrecognizable response" while writing
    correctly, so success is judged by the FILE, never by the absence of an exception.
    """
    server_type = _load_amo()
    from System.IO import FileAccess, FileMode, FileStream  # noqa: PLC0415  # pylint: disable=import-outside-toplevel,import-error

    server = server_type()
    server.Connect(f"Data Source=localhost:{port}")
    try:
        database = server.Databases[0]
        live_level = int(database.CompatibilityLevel)

        def write_image(staging: Path) -> None:
            stream = FileStream(str(staging), FileMode.Create, FileAccess.Write)
            try:
                server.ImageSave(database.ID, stream)
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                # AMO's response parser trips even on a fully correct write, so ONLY that specific
                # error is swallowed; the staged-file check in _staged_image_write is the real
                # evidence. Every other failure (disk full, permission, the stream dying mid-write)
                # is re-raised so a partial write cannot be mistaken for a success and overwrite the
                # existing good cache (#113).
                if not _is_benign_imagesave_response_error(exc):
                    raise
            finally:
                stream.Close()

        return _persist_image(cache_path, model_dir, live_level, write_image)
    finally:
        server.Disconnect()


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


def _looks_like_semantic_model(path: Path) -> bool:
    """A resolved `byPath` target that is really an on-disk `.SemanticModel` folder.

    Requires the `.SemanticModel` suffix AND an existing `definition/` dir, so a binding pointing at a
    deleted/renamed model is rejected here and the caller then fails closed.
    """
    return path.is_dir() and path.name.endswith(".SemanticModel") and (path / "definition").is_dir()


def _resolve_pbip_binding(pbip: Path) -> tuple[str, Path | None]:
    """Resolve the `.pbip` -> report -> `definition.pbir` `byPath` binding to a model, TRI-STATE.

    Returns exactly one of:
      ("resolved", model_dir) - one binding target that is a real `.SemanticModel` (possibly OUTSIDE
                                this `.pbip`'s folder). Use it.
      ("unresolvable", None)  - a binding EXISTS but names no single real model (missing/renamed, or
                                several reports disagree). FAIL CLOSED.
      ("absent", None)        - no `byPath` anywhere; a name/count heuristic MAY now apply.

    "Absent" and "present-but-unresolvable" are deliberately DIFFERENT results: the first permits a
    heuristic, the second forbids it. Collapsing them (round-2) let a dangling or outside-the-folder
    binding fall through to `models[0]` - a wrong-project write reachable even with a single adjacent
    model. The target is resolved DIRECTLY from `byPath`, NOT intersected with the pbip folder's
    siblings, so an outside-the-folder binding still resolves and a lone decoy cannot shadow it
    (round-3 mandate, points 2-3).
    """
    try:
        pbip_data = json.loads(pbip.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        pbip_data = None
    report_dirs: list[Path] = []
    if isinstance(pbip_data, dict):
        for artifact in pbip_data.get("artifacts", []) or []:
            report = artifact.get("report") if isinstance(artifact, dict) else None
            rel = report.get("path") if isinstance(report, dict) else None
            if rel:
                report_dirs.append(pbip.parent / rel)
    if not report_dirs:
        report_dirs = [pbir.parent for pbir in pbip.parent.glob("*.Report")]

    saw_binding = False
    targets: set[Path] = set()
    for report_dir in report_dirs:
        pbir = report_dir / "definition.pbir"
        try:
            data = json.loads(pbir.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        by_path = (((data.get("datasetReference") or {}).get("byPath")) or {}).get("path")
        if not by_path:
            continue
        saw_binding = True
        target = (pbir.parent / by_path).resolve()
        if _looks_like_semantic_model(target):
            targets.add(target)
    if not saw_binding:
        return "absent", None
    if len(targets) == 1:
        return "resolved", next(iter(targets))
    return "unresolvable", None


def _cache_from_absent_binding(pbip: Path) -> Path | None:
    """Name/count heuristic for the owning model - reached ONLY when no `byPath` binding exists.

    Kept in its own function so no branch of it can precede the authoritative binding lookup in
    :func:`cache_file` (the early-return shape that failed review twice). One model -> it; otherwise
    the single sibling whose stem matches the `.pbip`; else None (ambiguous, fail closed).
    """
    models = sorted(pbip.parent.glob("*.SemanticModel"))
    if not models:
        return None
    if len(models) == 1:
        return models[0] / ".pbi" / "cache.abf"
    stem = pbip.stem.lower()
    by_name = [model for model in models if model.stem.lower() == stem]
    if len(by_name) == 1:
        return by_name[0] / ".pbi" / "cache.abf"
    return None


def cache_file(pid: int) -> Path | None:
    """Where `<Name>.SemanticModel/.pbi/cache.abf` belongs for the model this instance has open.

    Resolves the DESTINATION, not an existing file (on a first build there is no cache yet). There is
    exactly ONE resolution order, with the authoritative binding consulted FIRST and UNCONDITIONALLY -
    no early return can precede it. This failed review twice with the same shape (round-1 a stem
    heuristic ran first; round-2 a `len(models)==1` shortcut), so the binding is now tri-state
    (round-3 mandate): resolved -> use it (even OUTSIDE this folder); unresolvable -> None (fail
    closed); absent -> and only then a name/count heuristic may run.
    """
    inst = _instance(pid)
    if inst is None or not inst.get("currentFilePath"):
        return None
    pbip = Path(inst["currentFilePath"])
    state, bound = _resolve_pbip_binding(pbip)
    if state == "resolved":
        return bound / ".pbi" / "cache.abf"
    if state == "unresolvable":
        return None
    return _cache_from_absent_binding(pbip)


def tmdl_tables(model_dir: Path) -> set[str]:
    """Table names declared by the TMDL on disk - the fingerprint of the model that owns the cache.

    `utf-8-sig` and the auto date-table filter both matter to the *gate*, not just to tidiness:
    Desktop writes TMDL with a BOM, and a BOM immediately followed by the declaration would make
    `^table` miss every file - leaving an empty fingerprint, which `same_model` treats as
    unverifiable and (now) REFUSES. Auto date tables are serialized into `tables/` when auto
    date/time is (or ever was) on, and they are filtered out of the live side, so leaving them here
    would make every such model look like a stranger.
    """
    definition = model_dir / "definition"
    names: set[str] = set()
    for tmdl in sorted(definition.glob("tables/*.tmdl")):
        text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
        names.update(quoted or bare for quoted, bare in TABLE_DECL_RE.findall(text))
    return {name for name in names if not is_auto_date_table_name(name)}


def _live_tables(port: int) -> set[str]:
    """Table names in the model currently served on `port` (hidden included, auto date tables not)."""
    adomd_connection = _load_adomd()
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        return set(table_names(conn, include_hidden=True))
    finally:
        conn.Close()


def tmdl_columns_measures(model_dir: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """`(table, column)` and `(table, measure)` pairs declared by the TMDL on disk.

    The finer half of the identity fingerprint: two models can share table names but differ in their
    columns or measures, and a table-only match would confirm the wrong one (#114, round-2 blocker
    3). Each `tables/<Name>.tmdl` declares exactly one table, so its name scopes every column/measure
    in the file; auto date-table files are skipped to mirror the live-side filter.
    """
    definition = model_dir / "definition"
    columns: set[tuple[str, str]] = set()
    measures: set[tuple[str, str]] = set()
    for tmdl in sorted(definition.glob("tables/*.tmdl")):
        text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
        decl = TABLE_DECL_RE.search(text)
        if not decl:
            continue
        table = decl.group(1) or decl.group(2)
        if is_auto_date_table_name(table):
            continue
        columns.update((table, quoted or bare) for quoted, bare in COLUMN_DECL_RE.findall(text))
        measures.update((table, quoted or bare) for quoted, bare in MEASURE_DECL_RE.findall(text))
    return columns, measures


def _live_columns_measures(port: int) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """`(table, column)` and `(table, measure)` pairs in the model currently served on `port`.

    Auto-generated columns (RowNumber, and the auto date/time scaffolding) are filtered inside
    `column_names`/`measure_names` so the live side compares like-for-like with the TMDL on disk.
    """
    adomd_connection = _load_adomd()
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        return column_names(conn), measure_names(conn)
    finally:
        conn.Close()


def _first_schema_difference(kind: str, on_disk: set[tuple[str, str]], live: set[tuple[str, str]]) -> str | None:
    """Compare two `(table, name)` sets case-insensitively; return a message if they differ, else None.

    Pure and .NET-free so the exact-match rule is unit-testable. Names are compared case-folded (the
    engine and TMDL can disagree only on case), and the message names a few offenders from each side
    so a refusal is diagnosable rather than a bare mismatch.
    """
    disk_fold = {(table.casefold(), name.casefold()) for table, name in on_disk}
    live_fold = {(table.casefold(), name.casefold()) for table, name in live}
    missing = sorted(
        f"{table}[{name}]" for table, name in on_disk if (table.casefold(), name.casefold()) not in live_fold
    )
    extra = sorted(f"{table}[{name}]" for table, name in live if (table.casefold(), name.casefold()) not in disk_fold)
    if not missing and not extra:
        return None
    return (
        f"{len(missing)} TMDL {kind}(s) absent from the connected model (e.g. {missing[:3]}), "
        f"{len(extra)} connected {kind}(s) not in the TMDL (e.g. {extra[:3]})"
    )


def _first_table_difference(on_disk: set[str], live: set[str]) -> str | None:
    """Compare table-name sets case-insensitively; return the WRONG_MODEL reason, or None if equal."""
    disk_fold = {name.casefold() for name in on_disk}
    live_fold = {name.casefold() for name in live}
    missing = sorted(name for name in on_disk if name.casefold() not in live_fold)
    extra = sorted(name for name in live if name.casefold() not in disk_fold)
    if not missing and not extra:
        return None
    return (
        f"{len(missing)} TMDL table(s) absent from the connected model (e.g. {missing[:3]}), "
        f"{len(extra)} connected table(s) not in the TMDL (e.g. {extra[:3]}) - "
        "an exact table-for-table match is required"
    )


def same_model(port: int, cache_path: Path | None) -> tuple[bool, str]:
    """Is the model served on `port` the one that owns `cache_path`? Returns (ok, message).

    The only check that can catch a wrong-instance bind. The destination path is resolved from the
    pid so it is always right; the DATA comes from the port, so a widened/mistyped port silently
    writes a sibling migration's model into this migration's cache.abf - and every other signal
    (file exists, non-empty, mtime advanced, row count) agrees with it, because they all read the
    same wrong source. Comparing the model's own tables is what breaks that self-consistency.

    **Fails CLOSED.** Whenever identity cannot be established - no model folder resolved, or no TMDL
    tables to fingerprint - this returns False, because "I could not verify" and "it is fine" are
    not the same answer, and this gate guards a write into another migration's project. It used to
    return True ("unverified") in both cases and let the write through.

    **The fingerprint is EXACT, not a subset, and reaches columns and measures.** The connected
    model's tables must equal the TMDL's table-for-table, AND its columns and measures must match too.
    A table-only check confirmed a sibling that shared table names but differed in columns or measures
    - and once `cache_file`'s binding is authoritative (round-2 blocker 3a) that sibling is reachable
    on a widened/wrong port, so tables alone are not enough (round-2 blocker 3). Auto-generated
    columns (the RowNumber index, the auto date/time scaffolding) are FILTERED, not compared, so a
    legitimate model never fails its own gate over engine-only artifacts. The previous check also only
    required the TMDL tables to be PRESENT in the engine, so a SUPERSET sibling passed; an exact match
    now refuses it.
    """
    if cache_path is None:
        return False, "identity UNVERIFIED (no model folder resolved for this pid) - refusing (fail closed)"
    model_dir = cache_path.parent.parent
    on_disk = tmdl_tables(model_dir)
    if not on_disk:
        return False, (
            f"identity UNVERIFIED (no TMDL tables under {model_dir / 'definition'}) - refusing (fail closed)"
        )
    table_diff = _first_table_difference(on_disk, _live_tables(port))
    if table_diff is not None:
        return False, f"port {port} does NOT serve {model_dir.name}: {table_diff}"
    disk_columns, disk_measures = tmdl_columns_measures(model_dir)
    live_columns, live_measures = _live_columns_measures(port)
    for kind, on_disk_set, live_set in (
        ("column", disk_columns, live_columns),
        ("measure", disk_measures, live_measures),
    ):
        difference = _first_schema_difference(kind, on_disk_set, live_set)
        if difference is not None:
            return False, f"port {port} does NOT serve {model_dir.name}: {difference} - an exact match is required"
    return True, (
        f"{model_dir.name} confirmed on port {port} "
        f"({len(on_disk)} table(s), {len(disk_columns)} column(s), {len(disk_measures)} measure(s), exact match)"
    )


def row_counts(port: int, tables: list[str] | None) -> tuple[list[tuple[str, int]], bool]:
    """Row counts per canary table - the gate of record. Returns (results, implicit).

    A refresh that "succeeded" but returns no rows is not a refresh; only data proves the source was
    reachable, the credential worked and the M was valid. With an explicit canary set (one per
    distinct source), every source is asserted. With none, the first queryable table is probed and
    ``implicit`` is True - that is a single-table probe, NOT a model-level guarantee: a static
    parameter/CSV table can return rows while a live source never loaded. Whether an all-non-zero
    result earns a MODEL-level verdict is not decided here: it also depends on whether the refresh
    covered the whole database (see :func:`_emit_data_verdict`).
    """
    adomd_connection = _load_adomd()
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        implicit = not tables
        targets = list(tables) if tables else [first_table(conn)]
        results: list[tuple[str, int]] = []
        for table in targets:
            cmd = conn.CreateCommand()
            cmd.CommandText = f"EVALUATE ROW(\"n\", COUNTROWS('{table}'))"
            reader = cmd.ExecuteReader()
            rows = 0
            while reader.Read():
                value = reader.GetValue(0)
                rows = int(value) if value is not None else 0
            reader.Close()
            results.append((table, rows))
        return results, implicit
    finally:
        conn.Close()


def _refresh_and_save(  # pylint: disable=too-many-return-statements,too-many-branches
    pid: int,
    port: int,
    cache: Path | None,
    args: argparse.Namespace,
    initial_state: CredentialDetection | None = None,
) -> int | None:
    """Run the refresh and (unless suppressed) persist it. Returns an exit code, or None to continue.

    `cache` is passed in rather than re-derived: `cache_file` is another Desktop Bridge round trip,
    and the bridge returning nothing (or something else) after the identity gate has run would mean
    writing to a destination that was never verified.
    """
    source_hint = source_hint_from_model(cache.parent.parent if cache else None)
    try:
        parameters = inspect.signature(refresh).parameters
        refresh_kwargs = {}
        if "refresh_type" in parameters:
            refresh_kwargs["refresh_type"] = args.refresh_type
        if "desktop_pid" in parameters:
            refresh_kwargs.update({"desktop_pid": pid, "source_hint": source_hint})
            if "initial_state" in parameters:
                refresh_kwargs["initial_state"] = initial_state
        ok, message = refresh(port, args.tables, REFRESH_TIMEOUT_SECONDS, **refresh_kwargs)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        if isinstance(exc, CredentialMissingError):
            _emit_credential_missing(exc.pid, exc.modal, exc.source_hint)
            return 1
        if isinstance(exc, DialogBlockedError):
            _emit_blocked_by_dialog(exc.pid, exc.dialog)
            return 1
        if isinstance(exc, CredentialUnknownError):
            _emit_credential_unknown(exc.pid, exc.reason)
            return 3
        if isinstance(exc, DesktopGoneError):
            _emit_desktop_gone(exc.pid, exc.reason)
            return 2
        if isinstance(exc, DesktopUnreadyError):
            _emit_desktop_unready(exc.pid, exc.reason)
            return 2
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
            print(
                f"REFRESH: TIMEOUT - no result within "
                f"{REFRESH_TIMEOUT_SECONDS + REFRESH_WALL_CLOCK_GRACE_SECONDS}s ({text})"
            )
            print(
                "  CAUSE UNKNOWN - this script cannot distinguish these two, and they need\n"
                "  opposite responses:\n"
                "    (a) SLOW: a very large model. Refresh only what you need with --tables;\n"
                "        do NOT simply wait longer.\n"
                "    (b) BLOCKED: Desktop is showing a data-source sign-in modal no automation\n"
                "        can fill. Retrying cannot dismiss it; a human must sign in once.\n"
                "  SETTLE IT - run the arbiter that ships beside this script, do not guess:\n"
                f'    powershell -File "{CREDENTIAL_PROBE}" -DesktopPid {pid}\n'
                "  A one-row probe bundle narrows this, but does NOT settle it: a probe limited to a\n"
                "  single row is fast once the source is WARM, yet measured 2026-08-05 a 1-row probe\n"
                "  against a SUSPENDED Snowflake warehouse took 167 s (vs 21 s against an already\n"
                "  running Databricks warehouse) - the auto-resume dominates, not the row count. So a\n"
                "  slow probe is evidence of a cold source at least as often as a blocked one; use the\n"
                "  arbiter above, and check whether the compute was suspended, before concluding (b)."
            )
            return 3
        print(f"REFRESH: ERROR {text}")
        return 2
    print(f"  refresh: {message}" if ok else f"  refresh FAILED: {message}")

    # Persisting is the DEFAULT, because it is this script's stated purpose: "so the next agent (and
    # the next Desktop open) sees real data instead of an empty model". It was off for a while
    # because a persisted cache was believed to break the PBIP; root-caused 2026-08-07, that was a
    # compatibility-level mismatch, and `image_save` now aligns `database.tmdl` the way Desktop's own
    # Save does. With the hazard gone, the default that matches the purpose wins.
    #
    # Which default is safer is decided by the failure modes, not by taste. Forgetting `--no-save`
    # bumps a declared compat level and leaves a cache: bounded, visible, reversible. Forgetting a
    # `--save` opt-in hands the NEXT agent an empty model - measured this session, a probe came back
    # NO_DATA - and an agent that queries it reports findings about nothing. The second is worse and
    # more likely, since persisting is the reason to run this at all.
    #
    # `--no-save` is for read-only work (the validator is read-only BY CONTRACT and must pass it) and
    # for validate-then-deploy, where the project must stay byte-identical.
    if not args.no_save:
        # Preferred: a real API call. Falls back to driving the UI only if it fails, so a change in the
        # engine can never leave the pipeline with no way to persist.
        saved, save_message = (False, "no cache path resolved")
        if cache is not None and not args.ui_save:
            try:
                saved, save_message = image_save(port, cache, model_dir=cache.parent.parent)
            except CompatRollbackError as exc:
                # FATAL: the cache write failed AND the compatibility alignment could not be rolled back,
                # so database.tmdl declares a level that was never written to a cache. Driving the UI Save
                # on top of that inconsistent state would persist the mismatch, so do NOT fall back to it.
                print(f"  save   : {exc}")
                print(
                    "REFRESH: ERROR compatibility rollback failed - database.tmdl left inconsistent; "
                    "do NOT save. Restore database.tmdl from source control before retrying."
                )
                return 2
            except ModelLockTimeout as exc:
                print(f"  save   : {exc}")
                print(
                    "REFRESH: NOT_PERSISTED (a concurrent persist of this model holds the lock; "
                    "data is in memory only). Wait for the other run to finish and retry."
                )
                return 1
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


def _build_arg_parser() -> argparse.ArgumentParser:
    """The CLI parser, built in one place so a test can assert the documented default matches it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pid",
        type=int,
        help="Power BI Desktop process id - required when several instances are open "
        "(`powerbi-desktop status` maps pid -> open file)",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Local AS port. Derived from --pid; if given it MUST equal the pid-discovered port "
        "(a mismatch aborts - it cannot silently point the write at another instance)",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        help="Tables to REFRESH (default: the whole database). This narrows the refresh, so it can "
        "never earn a model-level DATA_OK - name canaries with --canaries instead",
    )
    parser.add_argument(
        "--canaries",
        nargs="*",
        help="Tables to VERIFY after the refresh, one per distinct live source. A whole-database "
        "refresh whose canaries all return rows earns the model-level DATA_OK. Defaults to --tables "
        "when only that is given; with neither, the first queryable table is probed and the verdict "
        "is downgraded to name that single table",
    )
    parser.set_defaults(refresh_type=REFRESH_TYPE_FULL)
    parser.add_argument(
        "--calculate-only",
        "--measures-only",
        dest="refresh_type",
        action="store_const",
        const=REFRESH_TYPE_CALCULATE,
        help=(
            "Opt-in DAX-only refresh: send TMSL refresh type 'calculate' instead of 'full'. "
            "This recalculates formulas without re-reading source rows; use only after measure-only "
            "edits, because data-affecting changes need the default full refresh"
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Accepted no-op: persisting is now the DEFAULT. Kept so existing callers still work",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help=(
            "Refresh in memory but persist NOTHING, leaving the project byte-identical. Use for "
            "read-only work (validation, auditing) and for the validate-then-deploy path, because "
            "saving raises database.tmdl's declared compatibilityLevel to the level Desktop runs at"
        ),
    )
    parser.add_argument("--verify-only", action="store_true", help="Skip refresh/save; just report the state")
    parser.add_argument(
        "--ui-save",
        action="store_true",
        help="Force the legacy UI-Automation save instead of AMO ImageSave (fallback/diagnostic)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-return-statements
    """CLI entry point: refresh, save, and prove data is really there."""
    args = _build_arg_parser().parse_args(argv)

    pid = _resolve_pid(args.pid)
    if pid is None:
        return 2

    # Resolve the destination before touching XMLA so a t=0 credential modal can be reported with the
    # source hint from the model files, and before any query is issued against a blocked Desktop.
    cache = cache_file(pid)
    source_hint = source_hint_from_model(cache.parent.parent if cache else None)
    credential_state = _credential_state(pid)
    if credential_state.modal is not None:
        _emit_credential_missing(pid, credential_state.modal, source_hint)
        return 1
    if credential_state.blocking_dialog is not None:
        _emit_blocked_by_dialog(pid, credential_state.blocking_dialog)
        return 1
    if credential_state.process_gone is not None:
        _emit_desktop_gone(pid, credential_state.process_gone)
        return 2
    if credential_state.desktop_unready is not None:
        _emit_desktop_unready(pid, credential_state.desktop_unready)
        return 2
    if credential_state.unknown_reason:
        print(f"  credential-check: UNKNOWN ({credential_state.unknown_reason})")

    # The port is DERIVED from the pid. A stray --port must never bypass that on this mutating path:
    # the destination cache is resolved from the pid, so a --port pointing at another instance would
    # read (and, via ImageSave, persist) a sibling's model into this project's own correct cache.abf.
    discovered = discover_port(pid)
    if args.port is not None and args.port != discovered:
        print(
            f"REFRESH: ERROR --port {args.port} does not match the port {discovered} discovered from "
            f"pid {pid} - refusing to read or write across instances (drop --port; it is derived from --pid)"
        )
        return 2
    port = discovered

    before_stamp = cache.stat().st_mtime if cache and cache.exists() else 0.0

    # Gate everything on identity: refreshing, row-counting or persisting a sibling's model is a
    # fully self-consistent false positive, so it has to be caught BEFORE any of the three.
    if not _identity_gate(port, cache):
        return 2

    if not args.verify_only:
        outcome = _refresh_and_save(pid, port, cache, args, credential_state)
        if outcome is not None:
            return outcome

    results, implicit = row_counts(port, _canary_tables(args))
    return _emit_data_verdict(cache, before_stamp, args, results, implicit)


if __name__ == "__main__":
    sys.exit(main())
