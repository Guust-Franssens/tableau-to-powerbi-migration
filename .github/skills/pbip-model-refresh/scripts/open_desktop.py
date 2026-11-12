"""
purpose: open a .pbip in the installed Power BI Desktop, print its PID, and wait until the Desktop
         bridge reports exactly that PID ready. --check starts nothing: it finds Desktop and loads
         the ADOMD client that ships beside it.
usage:   python .github/skills/pbip-model-refresh/scripts/open_desktop.py <path-to.pbip>
         python .github/skills/pbip-model-refresh/scripts/open_desktop.py --check
         (ships inside the `pbip-model-refresh` skill; `scripts/open_desktop.py` in this repo is a
          forwarding shim.)

Desktop is looked up at every run, so an update needs no re-pin and no config change:
PBI_DESKTOP_PATH when that file exists (set but missing: ignored with a notice), else the Store
install (Get-AppxPackage), else the classic install under %ProgramFiles%.

The PID is printed on its own line as soon as Desktop starts, so a caller can always close exactly
that process, even when a later step fails. Readiness is bounded at 180 seconds. A dialog is a human
stop: this script never clicks or closes Desktop.
Exit codes: 0 ready (or check passed); 2 refused or failed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from _desktop_actions import DesktopActionError
from _desktop_frame import fit_launched_frame
from _desktop_observer import dialog_stop_detail
from probe_desktop_query import ADOMD_CLIENT_DLL, AdomdUnavailable, _load_adomd_from
from refresh_pbip_model import (
    _bridge_readiness_result,
    _credential_state,
    _ui_bridge_command,
    _ui_bridge_status,
    _ui_desktop_alive,
)

READY_TIMEOUT_SECONDS = 180
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

    Assumed to be the started process itself (not yet measured on a real Desktop); if that proves to
    be a short-lived stub, only this function changes.
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
    script = f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | Select-Object CreationDate | ConvertTo-Json -Compress"
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


def _wait_ready(pid: int, deadline: float) -> None:
    """Return once the bridge reports exactly `pid` ready; raise TimeoutError, DesktopActionError or OSError."""
    command = _ui_bridge_command()
    while time.monotonic() < deadline:
        if not _ui_desktop_alive(pid):
            raise DesktopActionError("Desktop process gone")
        instance, delay = _bridge_readiness_result(_ui_bridge_status(command, pid, deadline), pid)
        if instance is not None:
            return
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


def launch(pbip: Path) -> int:
    """Open `pbip`, print the PID at once, wait for readiness, fit the frame; 0 ready, 2 otherwise."""
    if pbip.suffix.lower() != ".pbip" or not pbip.is_file():
        print("OPEN_DESKTOP: ERROR the argument is not an existing .pbip file; nothing started")
        return 2
    found = find_desktop()
    if found is None:
        print(NOT_FOUND)
        return 2
    exe, source = found
    try:
        pid = _start(exe, pbip.resolve())
    except OSError as exc:
        print(f"OPEN_DESKTOP: ERROR Power BI Desktop could not be started ({type(exc).__name__})")
        return 2
    print(pid, flush=True)
    record = _launch_record(pid)
    print(
        f"OPEN_DESKTOP: waiting for bridge readiness of PID {pid} (at most {READY_TIMEOUT_SECONDS} seconds)", flush=True
    )
    started = time.monotonic()
    try:
        _wait_ready(pid, started + READY_TIMEOUT_SECONDS)
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
        print(json.dumps({"pid": pid, "exe_source": source, "ready_seconds": seconds}))
        return 0
    seconds = round(time.monotonic() - started)
    _diagnose(pid)
    print(f"OPEN_DESKTOP: ERROR PID {pid} not ready after {seconds} seconds ({failure}); Desktop left open")
    return 2


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
    parser.add_argument("pbip", nargs="?", help="the .pbip file to open")
    parser.add_argument("--check", action="store_true", help="load the bundled ADOMD client and print versions")
    args = parser.parse_args(argv)
    if args.check == bool(args.pbip):
        parser.error("give either a .pbip path or --check")
    return check() if args.check else launch(Path(args.pbip))


if __name__ == "__main__":
    sys.exit(main())
