"""
purpose: open a .pbip in the installed Power BI Desktop, print its PID, and wait until the Desktop
         bridge reports exactly that PID ready on that .pbip. --refresh then refreshes it, Saves and
         closes it in one step. --check starts nothing: it finds Desktop and loads the ADOMD client
         that ships beside it.
usage:   python .github/skills/pbip-model-refresh/scripts/open_desktop.py <path-to.pbip>
         python .github/skills/pbip-model-refresh/scripts/open_desktop.py <path-to.pbip-or-its-folder>
             --refresh [--canaries "A" "B"] [--no-save]
         python .github/skills/pbip-model-refresh/scripts/open_desktop.py --check
         python .github/skills/pbip-model-refresh/scripts/open_desktop.py --observe PID
             [--process-start TOKEN] [--json]
         python .github/skills/pbip-model-refresh/scripts/open_desktop.py --collect PID
             --process-start TOKEN --out FILE
         (ships inside the `pbip-model-refresh` skill; `scripts/open_desktop.py` in this repo is a
          forwarding shim. `t2p refresh` runs the --refresh form, `t2p desktop observe` the --observe one
          and `t2p desktop collect` the --collect one.)

Desktop is looked up at every run, so an update needs no re-pin and no config change:
PBI_DESKTOP_PATH when that file exists (set but missing: ignored with a notice), else the Store
install (Get-AppxPackage), else the classic install under %ProgramFiles%.

The PID is printed on its own line as soon as Desktop starts, so a caller can always close exactly
that process, even when a later step fails; the next line is `OPEN_DESKTOP: process_start=<token>`
(or `unknown`). Readiness is bounded at 180 seconds; until then any bridge reply other than "ready on
this .pbip" means Desktop is still starting. Meanwhile the observer looks about every 2 seconds, and an
owned window whose owner is disabled (a real modal) held for 10 seconds ends the wait early with exit 2,
Desktop left open. Ready on a different file is
refused. A .pbip that another Desktop lists as open by the same resolved absolute path is refused too
(best effort, one listing; nothing is started). A dialog is a human stop: this script never clicks Desktop,
except Copy details with --collect.

--refresh runs `refresh_pbip_model.py`'s refresh on the PID it just opened. That Desktop holds no
human work, so its unsaved-changes flag is ignored. Afterwards it closes that PID with its engine
child, so an idle Desktop does not stay in RAM, but only while the process holding the PID still has
the start time read at launch (compared just before the kill). A failed run that ends on a dialog
leaves it open for a human instead. An unreadable start time, a failed close or Ctrl+C during the
launch wait also leaves that Desktop running; its PID was already printed. With --no-save the
refreshed data is discarded on close.
--observe PID takes one read-only look at a running Desktop through the existing observer and starts,
clicks and closes nothing. With --process-start the observation is bound to that exact process.
--json prints the observation (`desktop.observation.v2`) as the only stdout line.
--collect PID clicks Desktop's own "Copy details" link on the dialog the observer names (no mouse,
keyboard or focus) and writes the copied text to --out, which must be private run scratch: it holds
session and user IDs, machine paths and stack traces. The copy replaces the clipboard and enters its
history. It prints the title, the error code and the first message line.
Exit codes: 0 ready (or check passed, or refresh verdict OK); 2 refused or failed; with --refresh the
refresh verdict (1 data or persist failure, 3 needs a human, including Desktop left open on a dialog);
with --observe 0 only for READY, 3 for every other observed state; with --collect 0 collected, 3 no
dialog, 2 refused or not collected.
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

from _desktop_actions import DesktopActionError
from _desktop_frame import fit_launched_frame
from _desktop_observer import dialog_stop_detail, observe, process_start, sanitize_title
from probe_desktop_query import ADOMD_CLIENT_DLL, AdomdUnavailable, _load_adomd_from
from refresh_pbip_model import (
    _bridge_readiness_result,
    _build_arg_parser,
    _credential_state,
    _run,
    _ui_bridge_command,
    _ui_bridge_status,
    _ui_desktop_alive,
)

READY_TIMEOUT_SECONDS = 180
STARTUP_POLL_SECONDS = 2.0
# Launch-wait watch (#741): bounded bridge calls, a look every 2 s, an early stop on a held modal.
BRIDGE_SLICE_SECONDS = 5.0
LOOK_SECONDS = 2.0
DIALOG_STOP_SECONDS = 10.0
COLLECT_TIMEOUT_SECONDS = 30
DETAILS_HELPER = Path(__file__).with_name("_desktop_details.py")
ERROR_CODE_LABEL = re.compile(r"(?:InnerException\d+\.)?PowerBINonFatalError_ErrorCode:")
NOT_FOUND = "OPEN_DESKTOP: ERROR Power BI Desktop not found (PBI_DESKTOP_PATH, Store install, classic install)"


def _store_location() -> Path | None:
    """InstallLocation of the Store (MSIX) Desktop package, or None."""
    try:
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop).InstallLocation",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return Path(lines[0]) if lines else None


def find_desktop() -> tuple[Path, str] | None:
    """The installed PBIDesktop.exe and which lookup found it (pin, appx or classic); nothing is stored."""
    pin = os.environ.get("PBI_DESKTOP_PATH")
    if pin:
        if Path(pin).is_file():
            return Path(pin), "pin"
        print("OPEN_DESKTOP: PBI_DESKTOP_PATH is set but its file does not exist; ignored", flush=True)
    location = _store_location()
    if location is not None and (location / "bin" / "PBIDesktop.exe").is_file():
        return location / "bin" / "PBIDesktop.exe", "appx"
    classic = Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "Microsoft Power BI Desktop", "bin")
    if (classic / "PBIDesktop.exe").is_file():
        return classic / "PBIDesktop.exe", "classic"
    return None


def _start(exe: Path, pbip: Path) -> int:
    """Start Desktop detached on `pbip` and return the PID to hold.

    Observed on the Store build 2.160.579.0: a run reached readiness with the PID this returns, and
    readiness requires the bridge to report exactly that PID, so it is the Desktop process itself, not
    a short-lived stub. If a build ever changes that, only this function changes.
    """
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [str(exe), str(pbip)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return process.pid


def _launch_record(pid: int) -> str:
    """CIM CreationDate of `pid` as Windows PowerShell renders it (``/Date(<ms>)/``); "" when unread."""
    script = (
        f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'"
        " | Select-Object CreationDate | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        ).stdout
        return str(json.loads(out)["CreationDate"])
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return ""


def _held_modal(pid: int, token: str | None) -> int | None:
    """The handle of an OWNED window with a DISABLED owner that the observer names as the blocker, else None.

    Only a real modal counts: the observer's unowned-root fallback (the startup splash or frame) never does.
    """
    try:
        observation = observe(pid, expected_process_start=token)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None
    window = observation.dialog.window if observation.state == "HUMAN_ACTION_REQUIRED" else None
    if window is None or not window.owner_hwnd or window.owner_enabled is not False:
        return None
    return window.hwnd


def _wait_ready(pid: int, pbip: Path, deadline: float, token: str | None = None) -> None:
    """Return once the bridge reports exactly `pid` ready on `pbip`; raise TimeoutError, DesktopActionError, OSError.

    Desktop 2.160.579.0 was measured passing through several startup replies before binding the file
    (an untitled canvas, "Host is not ready", "The task was cancelled."), and they vary by version. This
    script takes no action on the model, so it waits through any of them until the deadline instead of
    listing them; refresh keeps the strict shared check. A failing status command still stops at once.
    Each status call is bounded to a short slice so the observer can look about every 2 s in between;
    the same modal held for DIALOG_STOP_SECONDS ends the wait early (measured: a popup about 15 s in).
    """
    command = _ui_bridge_command()
    next_look_at = time.monotonic()
    held, held_since = None, 0.0
    while time.monotonic() < deadline:
        if not _ui_desktop_alive(pid):
            raise DesktopActionError("Desktop process gone")
        delay = 0.0
        try:
            payload = _ui_bridge_status(command, pid, min(deadline, time.monotonic() + BRIDGE_SLICE_SECONDS))
        except TimeoutError:
            payload = None  # the slice ran out; the bridge helper already stopped its own child
        if payload is not None:
            try:
                instance, delay = _bridge_readiness_result(payload, pid)
            except DesktopActionError:
                instance, delay = None, STARTUP_POLL_SECONDS
            if instance is not None:
                bound = Path(instance["currentFilePath"]).resolve()
                if os.path.normcase(bound) != os.path.normcase(pbip.resolve()):
                    raise DesktopActionError("bridge bound to a different file")
                return
        now = time.monotonic()
        if now >= next_look_at:
            next_look_at = now + LOOK_SECONDS
            handle = _held_modal(pid, token)
            if handle != held:
                held, held_since = handle, now
            elif handle is not None and now - held_since >= DIALOG_STOP_SECONDS:
                raise DesktopActionError("a dialog stayed open")
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
    raise TimeoutError


def _diagnose(pid: int) -> None:
    """One read-only dialog diagnostic. A dialog is a human stop: nothing is clicked or closed."""
    try:
        state = _credential_state(pid)
        if state.dialog is not None:
            print(f"OPEN_DESKTOP: diagnostic: {dialog_stop_detail(state.dialog)}", flush=True)
        else:
            print(f"OPEN_DESKTOP: diagnostic: {state.state}; inspect that Desktop locally", flush=True)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print("OPEN_DESKTOP: diagnostic: CANNOT_READ; inspect that Desktop locally", flush=True)


def _open_elsewhere(model: Path) -> dict | None:
    """The bridge listing entry of a running Desktop that already has `model` open (one listing), else None."""
    payload = _ui_bridge_status(_ui_bridge_command(), None, time.monotonic() + 30)
    instances = payload.get("instances")
    if not isinstance(instances, list):
        raise DesktopActionError("malformed bridge instances")
    for instance in instances:
        path = instance.get("currentFilePath") if isinstance(instance, dict) else None
        if isinstance(path, str) and path and os.path.normcase(Path(path).resolve()) == os.path.normcase(model):
            return instance
    return None


def _start_token(pid: int) -> str | None:
    """The process-start token of `pid` while it is certainly ours; None when unreadable."""
    try:
        return process_start(pid)
    except OSError:
        return None


def open_pbip(pbip: Path) -> tuple[int | None, dict | None, str | None]:  # pylint: disable=too-many-return-statements
    """Open `pbip`, print the PID at once, wait for readiness, fit the frame.

    Returns (pid, ready record, start token); pid is None when nothing was started, the record is None
    when not ready, the token is what `_release` compares before it closes that PID.
    A .pbip another Desktop lists as open by the same resolved absolute path is refused (best effort).
    """
    if pbip.suffix.lower() != ".pbip" or not pbip.is_file():
        print("OPEN_DESKTOP: ERROR the argument is not an existing .pbip file; nothing started")
        return None, None, None
    found = find_desktop()
    if found is None:
        print(NOT_FOUND)
        return None, None, None
    exe, source = found
    model = pbip.resolve()
    try:
        holder = _open_elsewhere(model)
    except (TimeoutError, DesktopActionError, OSError) as exc:
        detail = str(exc) if isinstance(exc, DesktopActionError) else type(exc).__name__
        print(f"OPEN_DESKTOP: ERROR cannot list the open Desktops ({detail}); nothing started")
        return None, None, None
    if holder is not None:
        print(f"OPEN_DESKTOP: ERROR this .pbip is already open in Desktop PID {holder.get('pid')}; nothing started")
        return None, None, None
    try:
        pid = _start(exe, model)
    except OSError as exc:
        print(f"OPEN_DESKTOP: ERROR Power BI Desktop could not be started ({type(exc).__name__})")
        return None, None, None
    token = _start_token(pid)
    print(pid, flush=True)
    print(f"OPEN_DESKTOP: process_start={token or 'unknown'}", flush=True)
    record = _launch_record(pid)
    print(
        f"OPEN_DESKTOP: waiting for bridge readiness of PID {pid} (at most {READY_TIMEOUT_SECONDS} seconds)", flush=True
    )
    started = time.monotonic()
    try:
        _wait_ready(pid, model, started + READY_TIMEOUT_SECONDS, token)
    except TimeoutError:
        failure = "timed out"
    except DesktopActionError as exc:
        failure = str(exc)
    except OSError as exc:
        failure = type(exc).__name__
    else:
        seconds = round(time.monotonic() - started)
        print(f"OPEN_DESKTOP: ready after {seconds} seconds", flush=True)
        try:
            print(fit_launched_frame(pid, record).describe(), flush=True)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            print(f"frame: not fitted ({type(exc).__name__})", flush=True)
        return pid, {"pid": pid, "exe_source": source, "ready_seconds": seconds}, token
    seconds = round(time.monotonic() - started)
    _diagnose(pid)
    print(f"OPEN_DESKTOP: ERROR PID {pid} not ready after {seconds} seconds ({failure})", flush=True)
    return pid, None, token


def launch(pbip: Path) -> int:
    """The CLI: open `pbip` and leave Desktop open for the caller; 0 ready, 2 otherwise."""
    pid, ready, _token = open_pbip(pbip)
    if ready is not None:
        print(json.dumps(ready))
        return 0
    if pid is not None:
        print(f"OPEN_DESKTOP: Desktop PID {pid} left open")
    return 2


def _resolve_pbip(path: Path) -> Path | None:
    """`path` when it is a .pbip file, or the only .pbip directly inside the folder `path`; else None."""
    if path.is_dir():
        found = sorted(path.glob("*.pbip"))
        return found[0] if len(found) == 1 else None
    return path if path.suffix.lower() == ".pbip" and path.is_file() else None


def _release(pid: int, token: str | None, result: int) -> bool:
    """Close the Desktop this run opened, by PID with its engine child; True when left open for a dialog.

    Only while the PID still has the start `token` read at launch: a reused PID is never killed.
    """
    try:
        if not _ui_desktop_alive(pid):
            print(f"OPEN_DESKTOP: PID {pid} already exited", flush=True)
            return False
    except DesktopActionError:
        pass
    if result != 0:
        try:
            dialog = _credential_state(pid).dialog
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            dialog = None
        if dialog is not None:
            print(f"OPEN_DESKTOP: PID {pid} left open for a human: {dialog_stop_detail(dialog)}", flush=True)
            print(f"OPEN_DESKTOP: close it afterwards with Stop-Process -Id {pid} -Force", flush=True)
            return True
    live = _start_token(pid)
    if token is None or live is None:
        print(f"OPEN_DESKTOP: PID {pid} not closed: its identity is unproven; check it before stopping it", flush=True)
        return False
    if live != token:
        print(f"OPEN_DESKTOP: PID {pid} is now a different process; nothing closed", flush=True)
        return False
    try:
        command = ["taskkill", "/PID", str(pid), "/T", "/F"]
        closed = subprocess.run(command, capture_output=True, check=False, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        closed = False
    if closed:
        print(f"OPEN_DESKTOP: closed PID {pid}", flush=True)
    else:
        print(f"OPEN_DESKTOP: could not close PID {pid}; close it with Stop-Process -Id {pid} -Force", flush=True)
    return False


def refresh_and_close(path: Path, canaries: list[str] | None, no_save: bool) -> int:
    """Open the .pbip in a new Desktop, refresh and Save that PID, then close it (see `_release`)."""
    pbip = _resolve_pbip(path)
    if pbip is None:
        print("OPEN_DESKTOP: ERROR the path is not a .pbip file or a folder holding exactly one; nothing started")
        return 2
    pid, ready, token = open_pbip(pbip)
    if pid is None:
        return 2
    result = 2
    try:
        if ready is not None:
            argv = ["--pid", str(pid)]
            if canaries is not None:
                argv += ["--canaries", *canaries]
            if no_save:
                argv.append("--no-save")
            # This run started this Desktop moments ago, so an unsaved-changes flag (or Desktop's
            # "files have changed" banner) cannot be a human's work: refresh and Save regardless.
            result = _run(_build_arg_parser().parse_args(argv), pid, require_clean=False)
    finally:
        left_for_human = _release(pid, token, result)
    return 3 if left_for_human else result


def observe_command(pid: str, process_start_token: str | None, as_json: bool) -> int:
    """One read-only observation of `pid`; 0 only for READY, 3 for any other state, 2 for a bad identity."""
    try:
        observation = observe(pid, expected_process_start=process_start_token)
    except ValueError:
        print(
            "OPEN_DESKTOP: ERROR invalid --observe or --process-start identity; nothing observed",
            file=sys.stderr if as_json else sys.stdout,
        )
        return 2
    state = observation.state
    head = (
        f"OPEN_DESKTOP: observe pid={pid} state={state} reason={observation.reason}"
        f" process_started_at={observation.process_started_at or 'unknown'}"
    )
    if state == "PROCESS_GONE":
        # The shared JSON carries no process_gone field, so only this line tells an exit from a changed identity.
        head += f" gone={observation.process_gone}"
        detail = "the process exited or its identity changed; nothing was observed"
    elif state == "HUMAN_ACTION_REQUIRED":
        detail = dialog_stop_detail(observation.dialog)
    elif state == "READY":
        detail = (
            "no blocking window observed for this PID (not verified as Power BI Desktop); this does not show"
            " sign-in, source reachability, model validity or that the model opened"
        )
    elif state == "NO_WINDOW":
        detail = (
            "no Desktop window found for this PID yet; it may still be starting or be hung, or the PID is not Desktop"
        )
    else:
        detail = "the windows could not be read completely; inspect that Desktop locally"
    if as_json:
        print(observation.to_json())
    print(f"{head}\n{detail}", file=sys.stderr if as_json else sys.stdout, flush=True)
    return 0 if state == "READY" else 3


def error_code(text: str) -> str | None:
    """The first non-empty `PowerBINonFatalError_ErrorCode` value in text order, top-level or InnerException<N>.

    Desktop's copied details put each label on its own line ending in ``:`` and the value on the next
    non-empty line, so a label followed directly by another label has no value.
    """
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if ERROR_CODE_LABEL.fullmatch(line):
            value = next((later for later in lines[index + 1 :] if later), "")
            if value and not value.endswith(":"):
                return value
    return None


def collect_command(pid: str, token: str, out: Path) -> int:  # pylint: disable=too-many-return-statements
    """Click the open dialog's own Copy details and write the copied text to `out`.

    0 collected, 3 established that there is no dialog, 2 refused or could not collect. The click runs in a
    helper process bounded by COLLECT_TIMEOUT_SECONDS, so a hung Desktop UI thread cannot hang this CLI.
    """
    if not out.parent.is_dir():
        print("OPEN_DESKTOP: ERROR the --out folder does not exist; nothing collected", flush=True)
        return 2
    try:
        observation = observe(pid, expected_process_start=token)
    except ValueError:
        print("OPEN_DESKTOP: ERROR invalid --collect or --process-start identity; nothing collected", flush=True)
        return 2
    state = observation.state
    if observation.process_gone == "identity_changed":
        print("OPEN_DESKTOP: ERROR the PID is not the Desktop that token names; nothing collected", flush=True)
        return 2
    if state == "UNKNOWN":
        hint = "; restore the Desktop window and retry" if observation.reason == "minimized" else ""
        print(
            f"OPEN_DESKTOP: ERROR could not read the windows ({observation.reason}); nothing collected{hint}",
            flush=True,
        )
        return 2
    if state != "HUMAN_ACTION_REQUIRED":
        print(f"OPEN_DESKTOP: no dialog to collect (state={state})", flush=True)
        return 3
    command = [sys.executable, str(DETAILS_HELPER), pid, str(observation.dialog.window.hwnd)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False, timeout=COLLECT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print("OPEN_DESKTOP: ERROR collector timed out; Desktop left as is", flush=True)
        return 2
    try:
        result = json.loads(done.stdout)
    except ValueError:
        result = None
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        reason = result.get("error") if isinstance(result, dict) else None
        print(f"OPEN_DESKTOP: ERROR could not collect ({sanitize_title(reason) or 'collector failed'})", flush=True)
        return 2
    if result.get("sequence_changed") is not True or result.get("owner_pid") != int(pid):
        print("OPEN_DESKTOP: ERROR clipboard was not written by this Desktop; nothing written", flush=True)
        return 2
    if _start_token(int(pid)) != token:
        print("OPEN_DESKTOP: ERROR the Desktop changed during collect; nothing written", flush=True)
        return 2
    try:
        out.write_text(result["text"], encoding="utf-8", newline="")
    except OSError as exc:
        print(f"OPEN_DESKTOP: ERROR cannot write --out ({type(exc).__name__})", flush=True)
        return 2
    code = error_code(result["text"])
    print(
        f'OPEN_DESKTOP: collect pid={pid} title="{observation.dialog.title}"'
        f" error_code={sanitize_title(code) if code else 'none'} out={out}",
        flush=True,
    )
    print(f"message: {sanitize_title(result.get('message'))}", flush=True)
    return 0


def check() -> int:
    """Find Desktop and load its bundled ADOMD client; print versions; start nothing."""
    found = find_desktop()
    if found is None:
        print(NOT_FOUND)
        return 2
    exe, source = found
    try:
        _load_adomd_from(exe.parent)
    except AdomdUnavailable as exc:
        print(f"OPEN_DESKTOP: ERROR ADOMD_UNAVAILABLE {exc.detail}")
        return 2
    # .NET Framework is loaded now, so its FileVersionInfo reads both versions.
    from System.Diagnostics import FileVersionInfo  # pylint: disable=import-outside-toplevel,import-error

    versions = {
        "exe_source": source,
        "desktop_version": FileVersionInfo.GetVersionInfo(str(exe)).FileVersion,
        "adomd_version": FileVersionInfo.GetVersionInfo(str(exe.parent / ADOMD_CLIENT_DLL)).FileVersion,
    }
    print(json.dumps(versions))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pbip", nargs="?", help="the .pbip file to open (with --refresh: or its folder)")
    parser.add_argument("--check", action="store_true", help="load the bundled ADOMD client and print versions")
    parser.add_argument("--refresh", action="store_true", help="then refresh it, Save and close it")
    parser.add_argument("--canaries", nargs="*", help="with --refresh: tables to verify (see refresh_pbip_model.py)")
    parser.add_argument("--no-save", action="store_true", help="with --refresh: no Save; data discarded on close")
    parser.add_argument("--observe", metavar="PID", help="one read-only observation of that running Desktop PID")
    parser.add_argument("--collect", metavar="PID", help="click Copy details on that Desktop's dialog; see --out")
    parser.add_argument("--process-start", help="with --observe or --collect: the exact process-start token")
    parser.add_argument("--out", type=Path, help="with --collect: private file for the copied details text")
    parser.add_argument("--json", action="store_true", help="with --observe: print the observation JSON on stdout")
    args = parser.parse_args(argv)
    modes = [bool(args.pbip), args.check, args.observe is not None, args.collect is not None]
    if modes.count(True) != 1:
        parser.error("give either a .pbip path or --check, or --observe PID, or --collect PID")
    refresh_options = args.canaries is not None or args.no_save
    if (refresh_options and not args.refresh) or (args.refresh and not args.pbip):
        parser.error("--canaries and --no-save need --refresh, which needs a .pbip path")
    if args.collect is not None:
        if args.process_start is None or args.out is None or args.json:
            parser.error("--collect needs --process-start and --out, and takes no --json")
        return collect_command(args.collect, args.process_start, args.out)
    if (args.process_start is not None or args.json) and args.observe is None:
        parser.error("--process-start and --json need --observe")
    if args.out is not None:
        parser.error("--out needs --collect")
    if args.observe is not None:
        return observe_command(args.observe, args.process_start, args.json)
    if args.check:
        return check()
    if args.refresh:
        return refresh_and_close(Path(args.pbip), args.canaries, args.no_save)
    return launch(Path(args.pbip))


if __name__ == "__main__":
    sys.exit(main())
