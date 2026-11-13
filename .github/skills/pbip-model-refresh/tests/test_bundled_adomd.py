"""The ADOMD client is the one beside the bound Power BI Desktop, and nothing else.

Every test fakes the CLR (`pythonnet`, `clr` and the AdomdClient namespace in `sys.modules`) and the
port owner, so no .NET runtime, no Desktop and no network is touched; the one subprocess test runs the
real CLI against a port nothing listens on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
import probe_desktop_query as pdq  # noqa: E402  # pylint: disable=wrong-import-position,import-error

DLL = pdq.ADOMD_CLIENT_DLL
VERSION = "2.160.579.0"


class FileLoadException(Exception):
    """Stands in for the .NET exception a failed AddReference raises."""


class _FakeClr:
    """Records what the loader asked the (fake) CLR for."""

    def __init__(self) -> None:
        self.runtimes: list[str] = []
        self.references: list[str] = []
        self.failure: Exception | None = None
        self.connection = object()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pythonnet = types.ModuleType("pythonnet")
        pythonnet.load = self.runtimes.append
        clr = types.ModuleType("clr")
        clr.AddReference = self._add_reference
        adomd = types.ModuleType("Microsoft.AnalysisServices.AdomdClient")
        adomd.AdomdConnection = self.connection
        modules = {
            "pythonnet": pythonnet,
            "clr": clr,
            "Microsoft": types.ModuleType("Microsoft"),
            "Microsoft.AnalysisServices": types.ModuleType("Microsoft.AnalysisServices"),
            "Microsoft.AnalysisServices.AdomdClient": adomd,
        }
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)

    def _add_reference(self, name: str) -> None:
        if self.failure is not None:
            raise self.failure
        self.references.append(name)


@pytest.fixture(name="fake_clr")
def _fake_clr(monkeypatch: pytest.MonkeyPatch) -> _FakeClr:
    """A fresh process: empty memo, nothing loaded, and a fake CLR."""
    monkeypatch.setattr(pdq, "_ADOMD_BY_PORT", {})
    monkeypatch.setattr(pdq, "_ADOMD_LOADED", {})
    fake = _FakeClr()
    fake.install(monkeypatch)
    return fake


def _desktop_bin(root: Path, *, with_dll: bool = True) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "PBIDesktop.exe").write_bytes(b"")
    (bin_dir / "msmdsrv.exe").write_bytes(b"")
    if with_dll:
        (bin_dir / DLL).write_bytes(b"")
    return bin_dir


def _owned_by(monkeypatch: pytest.MonkeyPatch, owners: dict[int, tuple[str, str]]) -> list[int]:
    asked: list[int] = []

    def owner(port: int) -> tuple[str, str]:
        asked.append(port)
        return owners.get(port, ("", ""))

    monkeypatch.setattr(pdq, "_adomd_port_owner", owner)
    return asked


def _unavailable(port: int) -> str:
    with pytest.raises(pdq.AdomdUnavailable) as caught:
        pdq._load_adomd(port)
    assert str(caught.value) == "ADOMD_UNAVAILABLE"
    return caught.value.detail


def test_the_bound_desktop_folder_supplies_the_client_under_netfx(monkeypatch, tmp_path, fake_clr) -> None:
    bin_dir = _desktop_bin(tmp_path / "desktop")
    _owned_by(monkeypatch, {5000: (str(bin_dir / "msmdsrv.exe"), VERSION)})

    assert pdq._load_adomd(5000) is fake_clr.connection
    assert fake_clr.runtimes == ["netfx"]
    assert fake_clr.references == [str(bin_dir / DLL)]


def test_a_client_anywhere_else_is_never_used(monkeypatch, tmp_path, fake_clr) -> None:
    """CWD, PATH, sys.path and a NuGet-style cache all hold the DLL; only the bound folder counts."""
    elsewhere = tmp_path / "elsewhere"
    nuget = tmp_path / "nuget" / "microsoft.analysisservices.adomdclient" / "19.84.1" / "lib" / "net45"
    for folder in (elsewhere, nuget):
        folder.mkdir(parents=True)
        (folder / DLL).write_bytes(b"")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", f"{elsewhere}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("NUGET_PACKAGES", str(tmp_path / "nuget"))
    monkeypatch.syspath_prepend(str(elsewhere))
    bin_dir = _desktop_bin(tmp_path / "desktop", with_dll=False)
    _owned_by(monkeypatch, {5000: (str(bin_dir / "msmdsrv.exe"), VERSION)})

    detail = _unavailable(5000)

    assert detail == f"Power BI Desktop {VERSION}: {DLL} is not beside the Power BI Desktop executable"
    assert fake_clr.references == []


def test_a_desktop_update_is_picked_up_by_a_fresh_process_with_no_configuration(
    monkeypatch, tmp_path, fake_clr
) -> None:
    monkeypatch.delenv("PBI_DESKTOP_PATH", raising=False)
    old = _desktop_bin(tmp_path / "Desktop_2.159")
    new = _desktop_bin(tmp_path / "Desktop_2.160")
    _owned_by(monkeypatch, {5000: (str(old / "PBIDesktop.exe"), "2.159.0.0")})
    pdq._load_adomd(5000)

    # The update: a new process, and the engine now runs from the new version folder.
    monkeypatch.setattr(pdq, "_ADOMD_BY_PORT", {})
    monkeypatch.setattr(pdq, "_ADOMD_LOADED", {})
    _owned_by(monkeypatch, {5000: (str(new / "msmdsrv.exe"), VERSION)})
    pdq._load_adomd(5000)

    assert fake_clr.references == [str(old / DLL), str(new / DLL)]


def test_a_load_failure_names_only_the_exception_type(monkeypatch, tmp_path, fake_clr) -> None:
    bin_dir = _desktop_bin(tmp_path / "desktop")
    _owned_by(monkeypatch, {5000: (str(bin_dir / "msmdsrv.exe"), VERSION)})
    fake_clr.failure = FileLoadException(f"could not load {bin_dir / DLL}")

    detail = _unavailable(5000)

    assert detail == f"Power BI Desktop {VERSION}: loading {DLL} failed (FileLoadException)"
    assert str(tmp_path) not in detail


def test_pythonnet_missing_is_named(monkeypatch, tmp_path, fake_clr) -> None:
    bin_dir = _desktop_bin(tmp_path / "desktop")
    _owned_by(monkeypatch, {5000: (str(bin_dir / "msmdsrv.exe"), VERSION)})
    monkeypatch.setitem(sys.modules, "pythonnet", None)

    assert _unavailable(5000) == f"Power BI Desktop {VERSION}: pythonnet is not installed in this Python"
    assert fake_clr.references == []


def test_one_client_dll_per_process(monkeypatch, tmp_path, fake_clr) -> None:
    """The CLR cannot hold two builds of one assembly: a second Desktop build needs a new process."""
    first = _desktop_bin(tmp_path / "Desktop_a")
    second = _desktop_bin(tmp_path / "Desktop_b")
    _owned_by(
        monkeypatch,
        {
            5000: (str(first / "msmdsrv.exe"), VERSION),
            5001: (str(first / "PBIDesktop.exe"), VERSION),
            5002: (str(second / "msmdsrv.exe"), "2.161.0.0"),
        },
    )
    pdq._load_adomd(5000)
    pdq._load_adomd(5001)

    detail = _unavailable(5002)

    assert detail == (
        "Power BI Desktop 2.161.0.0: another Desktop build's ADOMD client is already loaded in this process; restart it"
    )
    assert fake_clr.references == [str(first / DLL), str(first / DLL)]


def test_the_client_is_memoized_per_port(monkeypatch, tmp_path, fake_clr) -> None:
    bin_dir = _desktop_bin(tmp_path / "desktop")
    asked = _owned_by(monkeypatch, {5000: (str(bin_dir / "msmdsrv.exe"), VERSION)})

    assert pdq._load_adomd(5000) is pdq._load_adomd(5000)
    assert asked == [5000]
    assert fake_clr.references == [str(bin_dir / DLL)]


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        (("", ""), "no readable process is listening on port 5000"),
        ((r"C:\Tools\python.exe", ""), "the process on port 5000 is not a Power BI Desktop engine"),
    ],
    ids=["no-owner", "not-an-engine"],
)
def test_the_port_must_belong_to_a_desktop_engine(monkeypatch, fake_clr, owner, expected) -> None:
    _owned_by(monkeypatch, {5000: owner})

    assert _unavailable(5000) == expected
    assert fake_clr.references == []


def test_the_probe_cli_prints_the_detail_before_any_model_read(monkeypatch, capsys, fake_clr) -> None:
    _owned_by(monkeypatch, {})
    monkeypatch.setattr(pdq, "_probe_with_credential_poll", lambda *_a, **_k: pytest.fail("model read attempted"))

    assert pdq.main(["--port", "1234"]) == 2
    assert (
        capsys.readouterr().out == "PREFLIGHT: ERROR ADOMD_UNAVAILABLE no readable process is listening on port 1234\n"
    )
    assert fake_clr.references == []


@pytest.mark.skipif(os.name != "nt", reason="the port owner is read through Windows PowerShell")
def test_the_probe_cli_fails_cleanly_in_a_real_process() -> None:
    """Nothing listens on port 1: the real CLI must exit 2 with the exact line and no traceback."""
    result = subprocess.run(
        [sys.executable, str(SKILL_SCRIPTS / "probe_desktop_query.py"), "--port", "1"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 2
    assert result.stdout == "PREFLIGHT: ERROR ADOMD_UNAVAILABLE no readable process is listening on port 1\n"
    assert "Traceback" not in result.stdout + result.stderr
