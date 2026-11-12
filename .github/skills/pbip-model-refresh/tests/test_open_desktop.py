"""`open_desktop.py`: find the installed Desktop, start it, print its PID, wait for exact-PID readiness.

Nothing here starts Power BI Desktop or loads a CLR: the start call, the bridge helpers and the
pythonnet/clr modules are all fakes. The one subprocess test runs `--check` against a dummy exe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
import open_desktop  # noqa: E402  # pylint: disable=wrong-import-position,import-error
import probe_desktop_query as pdq  # noqa: E402  # pylint: disable=wrong-import-position,import-error

PID = 4242
NOTICE = "OPEN_DESKTOP: PBI_DESKTOP_PATH is set but its file does not exist; ignored"


def _exe(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / "PBIDesktop.exe"
    exe.write_bytes(b"")
    return exe


@pytest.fixture(name="installs")
def _installs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A pin, a Store install and a classic install, each switchable; nothing stored between runs."""
    pin = _exe(tmp_path / "pinned")
    store = tmp_path / "WindowsApps" / "Microsoft.MicrosoftPowerBIDesktop_2.160_x64"
    appx = _exe(store / "bin")
    program_files = tmp_path / "Program Files"
    classic = _exe(program_files / "Microsoft Power BI Desktop" / "bin")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("PBI_DESKTOP_PATH", raising=False)
    monkeypatch.setattr(open_desktop, "_store_location", lambda: store)
    return types.SimpleNamespace(pin=pin, store=store, appx=appx, classic=classic)


def test_lookup_order_is_pin_then_store_then_classic(monkeypatch, installs) -> None:
    monkeypatch.setenv("PBI_DESKTOP_PATH", str(installs.pin))
    assert open_desktop.find_desktop() == (installs.pin, "pin")

    monkeypatch.delenv("PBI_DESKTOP_PATH")
    assert open_desktop.find_desktop() == (installs.appx, "appx")

    installs.appx.unlink()
    assert open_desktop.find_desktop() == (installs.classic, "classic")

    monkeypatch.setattr(open_desktop, "_store_location", lambda: None)
    installs.classic.unlink()
    assert open_desktop.find_desktop() is None


def test_a_dead_pin_is_ignored_with_a_path_free_notice(monkeypatch, capsys, installs, tmp_path) -> None:
    """Every Desktop update deletes the versioned folder a pin names; that must not stop a launch."""
    dead = tmp_path / "Microsoft.MicrosoftPowerBIDesktop_2.159_x64" / "bin" / "PBIDesktop.exe"
    monkeypatch.setenv("PBI_DESKTOP_PATH", str(dead))

    assert open_desktop.find_desktop() == (installs.appx, "appx")
    out = capsys.readouterr().out
    assert out == f"{NOTICE}\n"
    assert str(tmp_path) not in out


@pytest.mark.parametrize("name", ["model.txt", "missing.pbip"])
def test_a_bad_argument_is_refused_before_anything_starts(monkeypatch, capsys, installs, tmp_path, name) -> None:
    (tmp_path / "model.txt").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(open_desktop, "_start", lambda *_a: pytest.fail("Desktop started"))

    assert open_desktop.main([str(tmp_path / name)]) == 2
    assert (
        capsys.readouterr().out == "OPEN_DESKTOP: ERROR the argument is not an existing .pbip file; nothing started\n"
    )


def test_no_installed_desktop_is_refused_before_anything_starts(monkeypatch, capsys, tmp_path) -> None:
    pbip = tmp_path / "model.pbip"
    pbip.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(open_desktop, "find_desktop", lambda: None)
    monkeypatch.setattr(open_desktop, "_start", lambda *_a: pytest.fail("Desktop started"))

    assert open_desktop.main([str(pbip)]) == 2
    assert capsys.readouterr().out == f"{open_desktop.NOT_FOUND}\n"


@pytest.mark.parametrize("argv", [[], ["model.pbip", "--check"]], ids=["neither", "both"])
def test_exactly_one_of_a_pbip_or_check(argv) -> None:
    with pytest.raises(SystemExit) as caught:
        open_desktop.main(argv)
    assert caught.value.code == 2


@pytest.fixture(name="launch")
def _launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A launch whose start, launch record and bridge helpers are fakes; returns the .pbip and a log."""
    pbip = tmp_path / "model.pbip"
    pbip.write_text("{}", encoding="utf-8")
    log: dict[str, list] = {"start": [], "frame": [], "status": []}
    monkeypatch.setattr(open_desktop, "find_desktop", lambda: (tmp_path / "bin" / "PBIDesktop.exe", "appx"))
    monkeypatch.setattr(open_desktop, "_start", lambda exe, model: log["start"].append((exe, model)) or PID)
    monkeypatch.setattr(open_desktop, "_launch_record", lambda pid: "/Date(1767225600000)/")
    monkeypatch.setattr(open_desktop, "_ui_bridge_command", lambda: ["powerbi-desktop"])
    monkeypatch.setattr(open_desktop, "_ui_desktop_alive", lambda pid: True)
    monkeypatch.setattr(open_desktop, "time", types.SimpleNamespace(monotonic=lambda: 100.0, sleep=lambda _s: None))
    monkeypatch.setattr(
        open_desktop,
        "_credential_state",
        lambda pid: types.SimpleNamespace(dialog=None, state="READY"),
    )
    return types.SimpleNamespace(pbip=pbip, log=log)


def test_success_prints_the_pid_first_fits_the_frame_and_ends_with_json(monkeypatch, capsys, launch) -> None:
    payloads = iter([{"pending": True}, {"ready": True}])
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda command, pid, deadline: next(payloads))
    monkeypatch.setattr(
        open_desktop,
        "_bridge_readiness_result",
        lambda payload, pid: ({"pid": pid} if payload.get("ready") else None, 0.0),
    )

    def fit(pid, record):
        launch.log["frame"].append((pid, record))
        return types.SimpleNamespace(describe=lambda: "frame: fitted")

    monkeypatch.setattr(open_desktop, "fit_launched_frame", fit)

    assert open_desktop.main([str(launch.pbip)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == str(PID)
    assert "frame: fitted" in lines
    assert json.loads(lines[-1]) == {"pid": PID, "exe_source": "appx", "ready_seconds": 0}
    assert launch.log["start"] == [(launch.pbip.parent / "bin" / "PBIDesktop.exe", launch.pbip.resolve())]
    assert launch.log["frame"] == [(PID, "/Date(1767225600000)/")]


def test_a_frame_fit_failure_is_never_fatal(monkeypatch, capsys, launch) -> None:
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda *_a: {})
    monkeypatch.setattr(open_desktop, "_bridge_readiness_result", lambda payload, pid: ({"pid": pid}, 0.0))
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: (_ for _ in ()).throw(OSError("no frame")))

    assert open_desktop.main([str(launch.pbip)]) == 0
    assert "frame: not fitted (OSError)" in capsys.readouterr().out


def _raise(exc: Exception):
    def boom(*_args, **_kwargs):
        raise exc

    return boom


@pytest.mark.parametrize(
    ("patches", "reason"),
    [
        ({"_ui_bridge_status": _raise(TimeoutError())}, "timed out"),
        ({"_ui_desktop_alive": lambda pid: False}, "Desktop process gone"),
        ({"_ui_bridge_status": _raise(open_desktop.DesktopActionError("bridge refused"))}, "bridge refused"),
        ({"_ui_bridge_status": _raise(PermissionError("C:\\Users\\someone"))}, "PermissionError"),
    ],
    ids=["timeout", "gone", "bridge-error", "bridge-oserror"],
)
def test_the_pid_is_printed_when_readiness_fails(monkeypatch, capsys, launch, patches, reason) -> None:
    for name, value in patches.items():
        monkeypatch.setattr(open_desktop, name, value)
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: pytest.fail("frame fitted"))

    assert open_desktop.main([str(launch.pbip)]) == 2
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == str(PID)
    assert "OPEN_DESKTOP: diagnostic: READY; inspect that Desktop locally" in lines
    assert lines[-1] == f"OPEN_DESKTOP: ERROR PID {PID} not ready after 0 seconds ({reason}); Desktop left open"


def test_a_dialog_is_reported_for_a_human_and_left_alone(monkeypatch, capsys, launch) -> None:
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", _raise(TimeoutError()))
    monkeypatch.setattr(
        open_desktop, "_credential_state", lambda pid: types.SimpleNamespace(dialog=object(), state="DIALOG")
    )
    monkeypatch.setattr(open_desktop, "dialog_stop_detail", lambda _finding: "A dialog is open. A human must act.")

    assert open_desktop.main([str(launch.pbip)]) == 2
    out = capsys.readouterr().out
    assert "OPEN_DESKTOP: diagnostic: A dialog is open. A human must act." in out
    assert out.splitlines()[0] == str(PID)


@pytest.fixture(name="fake_netfx")
def _fake_netfx(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A fresh process whose (fake) CLR records references and reports file versions."""
    monkeypatch.setattr(pdq, "_ADOMD_BY_PORT", {})
    monkeypatch.setattr(pdq, "_ADOMD_LOADED", {})
    references: list[str] = []
    versions = {"PBIDesktop.exe": "2.160.579.0", pdq.ADOMD_CLIENT_DLL: "17.0.97.16"}
    pythonnet = types.ModuleType("pythonnet")
    pythonnet.load = lambda _runtime: None
    clr = types.ModuleType("clr")
    clr.AddReference = references.append
    adomd = types.ModuleType("Microsoft.AnalysisServices.AdomdClient")
    adomd.AdomdConnection = object()
    diagnostics = types.ModuleType("System.Diagnostics")
    diagnostics.FileVersionInfo = types.SimpleNamespace(
        GetVersionInfo=lambda path: types.SimpleNamespace(FileVersion=versions[Path(path).name])
    )
    modules = {
        "pythonnet": pythonnet,
        "clr": clr,
        "Microsoft": types.ModuleType("Microsoft"),
        "Microsoft.AnalysisServices": types.ModuleType("Microsoft.AnalysisServices"),
        "Microsoft.AnalysisServices.AdomdClient": adomd,
        "System": types.ModuleType("System"),
        "System.Diagnostics": diagnostics,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return references


def test_check_loads_the_bundled_client_and_prints_versions(monkeypatch, capsys, installs, fake_netfx) -> None:
    (installs.appx.parent / pdq.ADOMD_CLIENT_DLL).write_bytes(b"")
    monkeypatch.setattr(open_desktop, "_start", lambda *_a: pytest.fail("Desktop started"))

    assert open_desktop.main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "exe_source": "appx",
        "desktop_version": "2.160.579.0",
        "adomd_version": "17.0.97.16",
    }
    assert fake_netfx == [str(installs.appx.parent / pdq.ADOMD_CLIENT_DLL)]


def test_check_failure_lines(monkeypatch, capsys, installs, fake_netfx) -> None:
    assert open_desktop.main(["--check"]) == 2
    assert capsys.readouterr().out == (
        f"OPEN_DESKTOP: ERROR ADOMD_UNAVAILABLE {pdq.ADOMD_CLIENT_DLL} is not beside the Power BI Desktop executable\n"
    )
    assert fake_netfx == []

    monkeypatch.setattr(open_desktop, "find_desktop", lambda: None)
    assert open_desktop.main(["--check"]) == 2
    assert capsys.readouterr().out == f"{open_desktop.NOT_FOUND}\n"


def test_check_fails_cleanly_in_a_real_process(tmp_path) -> None:
    """A pinned exe with no client beside it: exact line, exit 2, no traceback, nothing started."""
    exe = _exe(tmp_path / "bin")
    result = subprocess.run(
        [sys.executable, str(SKILL_SCRIPTS / "open_desktop.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "PBI_DESKTOP_PATH": str(exe)},
    )

    assert result.returncode == 2
    assert result.stdout == (
        f"OPEN_DESKTOP: ERROR ADOMD_UNAVAILABLE {pdq.ADOMD_CLIENT_DLL} is not beside the Power BI Desktop executable\n"
    )
    assert "Traceback" not in result.stdout + result.stderr
