"""Real, isolated local installs of the singleton facade; no native/customer sources."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from types import ModuleType, SimpleNamespace
from typing import Iterator
import uuid

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")
PROJECT = "tableau-to-pbi-migration"
STAGES = {
    "preflight": "preflight.ps1",
    "survey": "run_engine_survey.py",
    "assessment": "assess_estate.py",
    "harvest": "harvest_estate_assets.py",
    "reference": "capture_tableau_oracle.py",
    "bundle": "run_estate.py",
}
UNESTABLISHED = [
    "credential_validity",
    "source_reachability",
    "path_safety",
    "process_liveness",
    "START_READY",
    "fidelity",
    "COMPLETE",
    "deployability",
]
SYNTAX = [
    ["next"],
    ["start"],
    ["start", "--project", " "],
    ["start", "--proj", "fixture"],
    ["start", "--project", "fixture", "--workbook", "PRIVATE_ARGV_CANARY"],
    ["doctor", "--show-values"],
    ["status"],
    ["status", "--run", "relative", "--repair"],
]
IMPORT_BLOCKER = {
    "id": "python_lxml",
    "source": None,
    "reason": "import_failed",
    "remedy": "sync_python_dependencies",
}

WITNESS = r"""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def state():
    return json.loads(Path(os.environ["T2P_TEST_STATE"]).read_text(encoding="utf-8"))

def note(owner, **fields):
    record = dict(owner=owner, root=str(ROOT), cwd=os.getcwd(), python=sys.executable,
                  no_bytecode=sys.dont_write_bytecode, path=os.environ.get("PATH"), **fields)
    with Path(os.environ["T2P_TEST_LOG"]).open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=True) + "\n")

real_popen = subprocess.Popen

def synthetic_child(command, *args, **options):
    allowed = Path(command[0]) == Path(sys.executable) or Path(command[0]).name.lower() in {"python", "python.exe"}
    note("child_process" if allowed else "forbidden_process", argv=[str(value) for value in command])
    assert allowed, "synthetic controls must never launch updater/login/source/native commands"
    return real_popen(command, *args, **options)

subprocess.Popen = synthetic_child
"""

ENV_OWNER = r"""
from pathlib import Path
import shutil
import subprocess
import sys
from fixture_witness import note, state
note("import:tableau_env")

real_run = subprocess.run

def preflight(command, **options):
    # PowerShell uses PATH; bare CreateProcess can prefer Windows' application directory.
    python = shutil.which("python", path=options["env"]["PATH"])
    note("preflight_transport", argv=command, child_cwd=str(options["cwd"]),
         child_no_bytecode=options["env"].get("PYTHONDONTWRITEBYTECODE"), child_python=python)
    if state().get("launch_failure"):
        raise OSError("PRIVATE_LAUNCH_CANARY")
    assert command == ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                       str(Path(__file__).parent / "preflight.ps1"), "-DoctorDiagnostics"]
    return real_run([python, "-c", "import sys; sys.path.insert(0, 'scripts'); import child_witness"], **options)

subprocess.run = preflight

def diagnose_env(path, *, explicit=False):
    note("diagnose_env", env=str(path), explicit=explicit,
         resolved_env=str(Path(path).resolve()))
    return {}, state()["configuration"]
"""

RUNS_OWNER = r"""
from pathlib import Path
from fixture_witness import ROOT, note
note("import:work_dirs")

def runs_root(parent=None):
    note("runs_root", parent=str(parent) if parent is not None else None)
    return (Path(parent) if parent is not None else ROOT) / "_runs"
"""

STATUS_OWNER = r"""
from fixture_witness import note, state
note("import:run_status")

def build_status(run):
    note("build_status", run=run)
    current = state()
    return current["observed"], current["status_code"]
"""

START_OWNER = r"""
import os
import subprocess
import sys
import time
from fixture_witness import ROOT, note, state
note("import:start_migration")

def main(argv, *, observer=None, diagnostic_env=False):
    options = dict(zip(argv[::2], argv[1::2]))
    selected_env = options.get("--env", str(ROOT / ".env"))
    parent = options.get("--runs-parent")
    note("start", argv=argv, diagnostic_env=diagnostic_env, env=selected_env,
         resolved_env=str(__import__("pathlib").Path(selected_env).resolve()),
         default_runs=str((__import__("pathlib").Path(parent).resolve()
                           if parent is not None else ROOT) / "_runs"))
    current = state()
    if "--storage-decision" in options:
        subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "storage_witness.py"),
                        options["--storage-decision"]], check=True, cwd=ROOT)
    if current.get("before_allocation"):
        return current["start_code"]
    observer({"event": "allocated", "run": str(ROOT / "_runs" / "001-fixture")})
    if current.get("streams"):
        os.write(1, b"LIVE_FD1\n")
        os.write(2, b"LIVE_FD2\n")
        subprocess.run([sys.executable, "-c",
            "import os; os.write(1,b'LIVE_CHILD_FD1\\n'); os.write(2,b'LIVE_CHILD_FD2\\n')"],
            check=True, cwd=ROOT)
        while not __import__("pathlib").Path(os.environ["T2P_TEST_RELEASE"]).exists():
            time.sleep(0.02)
    for stage, native in current["stages"]:
        observer({"event": "stage", "stage": stage, "native_exit_code": native,
                  "state": "ok" if native == 0 else "failed", "artifact": None})
    if current.get("interrupt"):
        raise KeyboardInterrupt
    return current["start_code"]
"""

CHILD_WITNESS = r"""
import json
import child_cache_witness
from fixture_witness import note, state
note("preflight_child")
current = state()
print(json.dumps(current["preflight"]))
raise SystemExit(current["preflight_code"])
"""

STORAGE_WITNESS = r"""
from pathlib import Path
import sys
from fixture_witness import note
note("storage_child", argv=sys.argv[1:], resolved_storage=str(Path(sys.argv[1]).resolve()))
"""


def _environment(root: Path) -> dict[str, str]:
    """Allowlisted build/runtime transport; never inherit workstation credentials or profiles."""
    env = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE")
        if key in os.environ
    }
    for key, leaf in (
        ("TEMP", "temp"),
        ("TMP", "temp"),
        ("TMPDIR", "temp"),
        ("HOME", "profile"),
        ("USERPROFILE", "profile"),
        ("APPDATA", "profile/roaming"),
        ("LOCALAPPDATA", "profile/local"),
        ("UV_CACHE_DIR", "uv-cache"),
        ("PIP_CACHE_DIR", "pip-cache"),
    ):
        path = root / leaf
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update(
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_DEFAULT_TIMEOUT="30",
        PIP_RETRIES="0",
        UV_HTTP_TIMEOUT="30",
        UV_HTTP_RETRIES="0",
        UV_NO_CONFIG="true",
    )
    return env


def _checked(command: list[str | Path], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """A failed installer is a setup failure, never a launcher refusal or a skip."""
    result = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"installer/control setup failed (exit {result.returncode}), not a product refusal:\n"
        + result.stdout.decode("utf-8", errors="replace")
        + result.stderr.decode("utf-8", errors="replace")
    )
    return result


def _toolkit(root: Path) -> None:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    shutil.copyfile(REPO_ROOT / "scripts" / "t2p.py", scripts / "t2p.py")
    for name, text in (
        ("fixture_witness.py", WITNESS),
        ("tableau_env.py", ENV_OWNER),
        ("work_dirs.py", RUNS_OWNER),
        ("run_status.py", STATUS_OWNER),
        ("start_migration.py", START_OWNER),
        ("child_witness.py", CHILD_WITNESS),
        ("storage_witness.py", STORAGE_WITNESS),
        ("child_cache_witness.py", "VALUE = 'fresh synthetic child import'\n"),
    ):
        (scripts / name).write_text(text, encoding="utf-8")
    (scripts / "preflight.ps1").write_text(
        "param([switch]$DoctorDiagnostics)\n"
        "& python -c \"import sys; sys.path.insert(0, 'scripts'); import child_witness\"\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("# synthetic configuration, no credentials\n", encoding="utf-8")
    (root / "not_packaged.txt").write_text("fixture asset, not a package\n", encoding="utf-8")
    for leaf in ("data", "migrations", "customer-fixture"):
        (root / leaf).mkdir()
        (root / leaf / "not_packaged.txt").write_text("public synthetic sentinel\n", encoding="utf-8")


def _write_state(lab: SimpleNamespace, **overrides) -> None:
    state = {
        "configuration": {
            "status": "READY_TO_ATTEMPT",
            "checks": [],
            "reason": "presence_only",
            "configuration_aliases": [],
        },
        "preflight": {"critical_missing": 0, "recommended_warnings": 0, "blockers": []},
        "preflight_code": 0,
        "start_code": 0,
        "stages": [["preflight", 0], ["bundle", 0]],
        "status_code": 0,
        "observed": {
            "run_location": {"state": "intact"},
            "findings": [],
            "units": [],
            "unscoped_packages": [],
            "recorded_phases": [],
            "run_recorded_status": "active",
            "inventory_scope": "established",
        },
    }
    state.update(overrides)
    lab.state.write_text(json.dumps(state), encoding="utf-8")
    lab.log.write_text("", encoding="utf-8")


def _lab(root: Path, toolkit_name: str = "toolkit A") -> SimpleNamespace:
    root.mkdir(parents=True)
    _checked(["git", "check-ignore", "--no-index", "--quiet", root], cwd=REPO_ROOT, env=os.environ.copy())
    lab = SimpleNamespace(
        root=root,
        toolkit=root / toolkit_name,
        caller=root / "caller B",
        python=Path(sys.executable),
        state=root / "state.json",
        log=root / "calls.jsonl",
        release=root / "release",
        env=_environment(root),
    )
    _toolkit(lab.toolkit)
    _toolkit(lab.caller)
    lab.env.update(T2P_TEST_STATE=str(lab.state), T2P_TEST_LOG=str(lab.log), T2P_TEST_RELEASE=str(lab.release))
    _write_state(lab)
    return lab


@pytest.fixture(name="source_control")
def source_lab() -> Iterator[SimpleNamespace]:
    """Source-only controls need no installer and are not installed-command evidence."""
    root = REPO_ROOT / "_t2p_install_tests" / ("source-" + uuid.uuid4().hex[:8])
    try:
        yield _lab(root)
    finally:
        shutil.rmtree(root)


def _metadata(lab: SimpleNamespace) -> dict:
    return json.loads(
        _checked(
            [
                lab.python,
                "-B",
                "-c",
                "import json,sysconfig; from importlib import metadata; "
                "sites=list(dict.fromkeys(sysconfig.get_path(k) for k in ('purelib','platlib'))); "
                "d=next(d for d in metadata.distributions(path=sites) "
                "if d.metadata['Name']=='tableau-to-pbi-migration'); "
                "print(json.dumps(dict(site=sysconfig.get_path('purelib'),"
                "dist_info=str(d.locate_file(next(f for f in d.files if f.name=='METADATA')).parent),"
                "origin=json.loads(d.read_text('direct_url.json')),"
                "entries=[(e.group,e.name,e.value) for e in d.entry_points],"
                "requires=d.requires,extras=d.metadata.get_all('Provides-Extra'),"
                "version=d.version,files=[str(f) for f in d.files])))",
            ],
            cwd=lab.caller,
            env=lab.env,
        ).stdout
    )


def _assert_regular_origin(origin: dict, toolkit: Path) -> None:
    assert set(origin) == {"url", "dir_info"} and origin["url"] == toolkit.as_uri()
    assert set(origin["dir_info"]) <= {"editable"} and origin["dir_info"].get("editable", False) is False


def _install(lab: SimpleNamespace, source: Path, *, editable: bool = False) -> None:
    if lab.installer == "uv-sync" and source.is_dir():
        # A separate selected environment keeps the full protected snapshots unchanged.
        _checked(
            [
                UV,
                "sync",
                "--all-extras",
                *([] if editable else ["--no-editable"]),
                "--python",
                lab.python,
                "--reinstall-package",
                PROJECT,
            ],
            cwd=source,
            env={**lab.env, "UV_PROJECT_ENVIRONMENT": str(lab.venv)},
        )
    else:
        # Archive input is the unsupported-wheel control, never a sync fallback.
        command = (
            [lab.python, "-B", "-m", "pip", "install", "--force-reinstall"]
            if lab.installer == "pip"
            else [UV, "pip", "install", "--python", lab.python, "--reinstall-package", PROJECT]
        )
        _checked([*command, *(["--editable"] if editable else []), source], cwd=lab.caller, env=lab.env)
    lab.metadata = _metadata(lab)
    lab.site = Path(lab.metadata["site"])
    lab.dist_info = Path(lab.metadata["dist_info"])


@pytest.fixture(
    name="installation",
    scope="module",
    # Only raw uv-pip's combined-punctuation cell is deferred; sync gets the full matrix.
    params=[
        ("pip", "toolkit A"),
        ("uv", "toolkit A"),
        ("pip", "toolkit café # %"),
        ("uv-sync", "toolkit A"),
        ("uv-sync", "toolkit café # %"),
    ],
    ids=["pip", "uv", "pip-encoded-unicode", "uv-sync", "uv-sync-encoded-unicode"],
)
def installed_toolkit(request) -> Iterator[SimpleNamespace]:
    """Actual ordinary installers and wrappers, with every write under owned repo scratch."""
    assert UV is not None, "uv is required for installation controls; missing setup is not a skip"
    root = REPO_ROOT / "_t2p_install_tests" / uuid.uuid4().hex[:8]
    try:
        installer, toolkit_name = request.param
        lab = _lab(root, toolkit_name)
        lab.installer = installer
        lab.venv = root / "environment"
        _checked([UV, "venv", "--seed", "--python", sys.executable, lab.venv], cwd=root, env=lab.env)
        scripts = lab.venv / ("Scripts" if os.name == "nt" else "bin")
        lab.python = scripts / ("python.exe" if os.name == "nt" else "python")
        lab.console = scripts / ("t2p.exe" if os.name == "nt" else "t2p")
        _install(lab, lab.toolkit)
        _read_only_case(lab)
        lab.first_launch_verified = True
        _write_state(lab)
        yield lab
    finally:
        shutil.rmtree(root)


def _invoke(
    lab: SimpleNamespace,
    *args: str,
    module: bool = False,
    direct: bool = False,
    env: dict | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    command = (
        [str(lab.python), "-B", str(lab.toolkit / "scripts" / "t2p.py")]
        if direct
        else [str(lab.python), "-B", "-m", "t2p"]
        if module
        else [str(lab.console)]
    )
    return subprocess.run(
        [*command, *args],
        cwd=cwd or lab.caller,
        env=env or lab.env,
        capture_output=True,
        check=False,
        timeout=45,
    )


def _calls(lab: SimpleNamespace, owner: str | None = None) -> list[dict]:
    calls = [json.loads(line) for line in lab.log.read_text(encoding="utf-8").splitlines()]
    return [row for row in calls if row["owner"] == owner] if owner else calls


def test_regular_install_metadata_dependencies_and_payload(installation: SimpleNamespace) -> None:
    """Check the ordinary installer, actual payload and root-independent generated help."""
    lab = installation
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert lab.console.is_file(), "the real generated console executable must exist"
    _assert_regular_origin(lab.metadata["origin"], lab.toolkit)
    assert lab.metadata["entries"] == [["console_scripts", "t2p", "t2p:console_main"]]
    assert manifest["project"]["dependencies"] == ["lxml>=5.2", "jsonschema>=4.22"]
    assert {"lxml>=5.2", "jsonschema>=4.22"} <= set(lab.metadata["requires"])
    assert lab.metadata["version"] == manifest["project"]["version"] == "0.1.0"
    assert set(lab.metadata["extras"]) == set(manifest["project"]["optional-dependencies"])
    for extra, requirements in manifest["project"]["optional-dependencies"].items():
        actual = {row.split(";")[0].strip() for row in lab.metadata["requires"] if f'extra == "{extra}"' in row}
        assert actual == set(requirements), "the declared extras must survive ordinary installation"
    assert (lab.site / "t2p.py").read_bytes() == (REPO_ROOT / "scripts" / "t2p.py").read_bytes()
    payload = [
        name
        for name in lab.metadata["files"]
        if ".dist-info/" not in name and not name.startswith(("../", "__pycache__/"))
    ]
    assert payload == ["t2p.py"], "only the facade, never toolkit backends/assets, is installed"
    _checked([lab.python, "-B", "-m", "pip", "check"], cwd=lab.caller, env=lab.env)
    help_result = _invoke(lab, "--help")
    assert help_result.returncode == 0, help_result.stderr
    assert b"usage: t2p " in help_result.stdout
    assert not _calls(lab), "help must not import or join backend owners"


def _json_result(child: subprocess.CompletedProcess, code: int) -> dict:
    assert child.returncode == code, (child.stdout, child.stderr)
    assert b"Traceback" not in child.stderr, "a setup/import error is not an accepted product verdict"
    assert len(child.stdout.splitlines()) == 1, "JSON stdout must contain exactly one envelope"
    result = json.loads(child.stdout)
    assert result["schema_version"] == "t2p/v1"
    assert result["exit_code"] == code
    return result


def _undelegated(lab: SimpleNamespace, child: subprocess.CompletedProcess, reason: str, code: int) -> dict:
    result = _json_result(child, code)
    assert result["limitations"] == [reason], "the intended binding/syntax refusal must be reached"
    assert result["status"] == (
        "CANNOT_ESTABLISH"
        if code == 3 and result["command"] == "doctor"
        else "cannot_establish"
        if code == 3
        else "error"
    )
    assert result["authority"] is result["delegated_exit_code"] is None
    assert result["stages"] == result["checks"] == result["evidence"] == result["artifacts"] == []
    assert result["proof_boundary"] == {"established": [], "not_established": UNESTABLISHED}
    assert result["run_location"] is result["runs_root"] is None
    assert child.stderr == b"", "undelegated installed paths must not emit a guessed RUN marker"
    assert not _calls(lab), "pre-binding refusal must import/call zero backend owners, including work_dirs"
    assert not (lab.toolkit / "_runs").exists() and not (lab.caller / "_runs").exists()
    return result


def _source_module() -> ModuleType:
    spec = spec_from_file_location("source_only_t2p_control", REPO_ROOT / "scripts" / "t2p.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_closed_help(stdout: bytes) -> None:
    choices = re.findall(rb"\{([^{}\r\n]+)\}", stdout)
    assert choices == [b"doctor,start,status"] * 2, (
        "closed help verb assertion: usage and positional choices must be exactly doctor/start/status"
    )


def test_source_closed_help_oracle_distinguishes_prose_and_verbs() -> None:
    """The finite command choices, not the word 'next' in prose, close the surface."""
    literal = (
        b"usage: t2p [-h] {doctor,start,status} ...\n"
        b"positional arguments:\n  {doctor,start,status}\n"
        b"    status  Read-only inventory; no next action or readiness.\n"
    )
    _assert_closed_help(literal)
    _assert_closed_help(_source_module().build_parser("t2p").format_help().encode("ascii"))
    _assert_closed_help(literal.replace(b"no next action", b"no continuation"))
    for choices in (b"doctor,start,status,next", b"doctor,start,next", b"doctor,start"):
        with pytest.raises(AssertionError, match="closed help verb assertion"):
            _assert_closed_help(literal.replace(b"doctor,start,status", choices, 1))
    with pytest.raises(AssertionError, match="closed help verb assertion"):
        _assert_closed_help(b"doctor start status; no next action\n")


@pytest.mark.parametrize("native", [0, 1, 7])
@pytest.mark.parametrize("installed", [False, True])
def test_source_unavailable_preflight_environment_guidance(native: int, installed: bool, monkeypatch, capsys) -> None:
    """Pure fallback rendering retains the installed environment, never running a remedy."""
    module = _source_module()
    result = _expected_doctor(native, {"status": "READY_TO_ATTEMPT", "checks": [], "reason": "presence_only"})
    result["doctor_details"] = {"preflight": {"availability": "unavailable"}}
    before = _wire(result)
    real_import = builtins.__import__

    def forbidden(*_args, **_kwargs) -> None:
        pytest.fail("fallback renderer must not import/call an owner or launch a child")

    def guarded_import(name: str, *args, **kwargs) -> ModuleType:
        if name.split(".", 1)[0] in {"tableau_env", "start_migration", "run_status", "work_dirs"}:
            forbidden()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(module, "doctor", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    module._doctor_human(  # pylint: disable=protected-access
        result, SimpleNamespace(env=Path("relative.env")), installed=installed
    )
    captured = capsys.readouterr()
    assert captured.err == "" and _wire(result) == before, "fallback rendering must not change records or channels"
    if installed:
        assert "activate the SAME environment that supplies <selected-python>" in captured.out, (
            "installed fallback environment assertion: retain the selected Python"
        )
        assert "verify PATH python is <selected-python>" in captured.out
        assert "Invoking <selected-t2p> does not activate the parent shell." in captured.out
        assert "powershell -ExecutionPolicy Bypass -File <selected-toolkit>\\scripts\\preflight.ps1" in captured.out
        assert "Rerun doctor: <selected-t2p> doctor --env <same-selected-env>" in captured.out
    else:
        assert "<selected-python>" not in captured.out and "<selected-toolkit>" not in captured.out, (
            "direct fallback assertion: installed-only context must not leak into legacy output"
        )
        assert "Before manual preflight" not in captured.out
        assert "powershell -ExecutionPolicy Bypass -File scripts\\preflight.ps1" in captured.out
        assert "Rerun doctor: python -B scripts\\t2p.py doctor --env <same-selected-env>" in captured.out


def test_source_readme_fallback_environment_context() -> None:
    """The absolute-executable journey must not imply activation of its parent shell."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    fallback = text.split("Session-start preflight remains separate", 1)[1].split("### Start one approved", 1)[0]
    assert "**activate the same selected environment**" in fallback
    assert "`(Get-Command python).Source` points to its selected Python" in fallback
    assert "does **not** activate the parent shell; preflight uses `python` from PATH" in fallback


def test_source_qualified_installation_guidance(capsys) -> None:
    """Installed hints retain the invocation-specific Q1 matrix; direct advice is unchanged."""
    module = _source_module()
    installed = "\n".join(module._doctor_blocker_lines(IMPORT_BLOCKER, installed=True))  # pylint: disable=protected-access
    direct = module._doctor_blocker_lines(IMPORT_BLOCKER)  # pylint: disable=protected-access
    assert direct[-1] == "Action: activate the intended Python environment and run uv sync manually."
    module._install_human({"limitations": ["install_origin_unavailable"], "exit_code": 3})  # pylint: disable=protected-access
    failure = capsys.readouterr()
    assert failure.err == ""
    for text in (installed, failure.out):
        assert "uv sync --all-extras --no-editable" in text
        assert "<selected-python> -m pip install <selected-toolkit>" in text
        assert "pip must already be available" in text
        assert "plain/spaced" in text
        assert "Raw native uv-pip combined Unicode/#/% compatibility is deferred" in text, (
            "qualified guidance assertion: defer only the raw uv-pip combined-path cell"
        )
        assert "canonical uv sync and pip retain both path-class qualification targets" in text
        assert "Do not switch checkouts/environments to pass." in text


@pytest.mark.parametrize("argv", [[], ["doctor"], ["start"], ["status"]])
def test_source_root_independent_help(argv: list[str], monkeypatch, capsys) -> None:
    """Offline source parsing controls; no installation is simulated."""
    module = _source_module()

    def forbidden(*_args) -> None:
        pytest.fail("source pre-binding help must not bind or join a backend root")

    monkeypatch.setattr(module, "_install_binding", forbidden)
    monkeypatch.setattr(module, "_run_line", forbidden)
    with pytest.raises(SystemExit) as raised:
        module.main([*argv, "--help"], installed=True)
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert output.startswith("usage: t2p ")
    if not argv:
        _assert_closed_help(output.encode("ascii"))


@pytest.mark.parametrize("argv", SYNTAX)
def test_source_root_independent_syntax(argv: list[str], monkeypatch, capsys) -> None:
    """A2 includes a parsed blank project, not only argparse's early failures."""
    module = _source_module()

    def forbidden(*_args) -> None:
        pytest.fail("source pre-binding syntax must not bind or join a backend root")

    monkeypatch.setattr(module, "_install_binding", forbidden)
    monkeypatch.setattr(module, "_run_line", forbidden)
    assert module.main([*argv, "--json"], installed=True) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["limitations"] == ["invalid_arguments_use_help"]
    assert result["authority"] is result["delegated_exit_code"] is None
    assert result["proof_boundary"]["established"] == []
    assert result["stages"] == result["checks"] == []
    assert "PRIVATE_ARGV_CANARY" not in captured.out


def test_source_physical_origin_and_direct_compatibility(source_control: SimpleNamespace) -> None:
    """Finite origin/path checks and direct -B help can be qualified without an installer."""
    lab = source_control
    module = _source_module()
    origin = {"url": lab.toolkit.as_uri(), "dir_info": {}}
    assert module._toolkit_origin(origin) == (lab.toolkit, "", 0)  # pylint: disable=protected-access
    assert module._toolkit_origin({**origin, "dir_info": {"editable": True}}) == (  # pylint: disable=protected-access
        None,
        "install_form_unsupported",
        1,
    )
    for args in (["--help"], ["doctor", "--help"], ["start", "--help"], ["status", "--help"]):
        child = _invoke(lab, *args, direct=True)
        assert child.returncode == 0 and child.stderr == b""
        assert child.stdout.startswith(b"usage: python -B scripts/t2p.py ")
    child = _invoke(lab, "start", "--project", " ", "--json", direct=True)
    _json_result(child, 2)
    assert child.stderr.startswith(b"RUN NOT_ALLOCATED; runs_root="), "retain the direct blank-project marker"


@pytest.mark.parametrize("command", ["doctor", "start", "status"])
def test_source_synthetic_owner_transports(source_control: SimpleNamespace, command: str) -> None:
    """Exercise the independent synthetic transports offline, without pretending to install."""
    lab = source_control
    lab.env["PATH"] = str(lab.python.parent) + os.pathsep + lab.env["PATH"]
    argv = {
        "doctor": ["doctor", "--env", "relative.env", "--json"],
        "start": ["start", "--project", "Exact Project", "--storage-decision", "relative.policy", "--json"],
        "status": ["status", "--run", str(lab.root / "explicit-run"), "--json"],
    }[command]
    result = _json_result(_invoke(lab, *argv, direct=True), 0)
    assert (
        result["authority"]
        == {"doctor": "preflight.ps1+tableau_env.py", "start": "start_migration.py", "status": "run_status.py"}[command]
    )
    assert all(row["root"] == str(lab.toolkit) for row in _calls(lab))
    if command == "doctor":
        assert len(_calls(lab, "preflight_child")) == 1
        detail = result.pop("doctor_details")
        assert detail["preflight"]["availability"] == "available"
        assert result == _expected_doctor(0, json.loads(lab.state.read_text(encoding="utf-8"))["configuration"])
    if command == "start":
        assert _calls(lab, "storage_child")[0]["argv"] == ["relative.policy"]
        assert _calls(lab, "storage_child")[0]["cwd"] == str(lab.toolkit)


def test_source_synthetic_read_only_transports(source_control: SimpleNamespace) -> None:
    """Calibrate the source-side snapshot/owner fixture independently of all installer setup."""
    source_control.env["PATH"] = str(source_control.python.parent) + os.pathsep + source_control.env["PATH"]
    _read_only_case(source_control, direct=True)


def test_source_synthetic_live_streams(source_control: SimpleNamespace) -> None:
    """The fixture's real Python grandchild and release barrier work without installation."""
    _live_streams(source_control, json_mode=True, direct=True)


@pytest.mark.parametrize("module", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
def test_installed_help_and_syntax_before_binding(
    installation: SimpleNamespace, module: bool, unavailable: bool
) -> None:
    """Real wrappers/modules parse without owner imports even when the bound target is gone."""
    lab = installation
    moved = lab.root / "temporarily moved"
    if unavailable:
        lab.toolkit.rename(moved)
    try:
        _write_state(lab)
        for argv in ([], ["doctor"], ["start"], ["status"]):
            env = {**lab.env, "PYTHONIOENCODING": "cp1252"}
            child = _invoke(lab, *argv, "--help", module=module, env=env)
            assert child.returncode == 0 and child.stderr == b""
            assert child.stdout.startswith(b"usage: t2p ")
            assert b"--json" in child.stdout or not argv
            if not argv:
                _assert_closed_help(child.stdout)
            flags = {b"--help", *([] if not argv else [b"--json"])}
            if argv and argv[0] in {"doctor", "start"}:
                flags.add(b"--env")
            if argv == ["start"]:
                flags.update({b"--project", b"--runs-parent", b"--storage-decision", b"--session-id"})
            if argv == ["status"]:
                flags.add(b"--run")
            assert set(re.findall(rb"--[\w-]+", child.stdout)) == flags, "the installed surface is exactly three verbs"
            if not unavailable:
                direct = _invoke(lab, *argv, "--help", direct=True, env=env)
                assert direct.returncode == 0 and direct.stderr == b""
                assert set(re.findall(rb"--[\w-]+", child.stdout)) == set(re.findall(rb"--[\w-]+", direct.stdout))
        assert not _calls(lab), "all installed helps must be root-independent"
        for argv in SYNTAX:
            _write_state(lab)
            child = _invoke(lab, *argv, "--json", module=module)
            _undelegated(lab, child, "invalid_arguments_use_help", 2)
            assert b"PRIVATE_ARGV_CANARY" not in child.stdout + child.stderr
        if unavailable:
            for argv in (["doctor"], ["start", "--project", "fixture"], ["status", "--run", "relative"]):
                _write_state(lab)
                _undelegated(lab, _invoke(lab, *argv, "--json", module=module), "install_target_unavailable", 3)
    finally:
        if unavailable:
            moved.rename(lab.toolkit)


@pytest.mark.parametrize("module", [False, True])
def test_installed_checkout_and_selected_python(installation: SimpleNamespace, module: bool) -> None:
    """Literal A/B witnesses, not the facade's reported root, establish selection."""
    lab = installation
    decoy = lab.caller / "tableau_to_pbi_migration.egg-info"
    decoy.mkdir(exist_ok=True)
    (decoy / "PKG-INFO").write_text(f"Name: {PROJECT}\nVersion: 99\n", encoding="utf-8")
    alien = lab.root / "conflicting-python"
    alien.mkdir(exist_ok=True)
    leaf = "python.cmd" if os.name == "nt" else "python"
    (alien / leaf).write_text("@exit /b 91\n" if os.name == "nt" else "#!/bin/sh\nexit 91\n", encoding="utf-8")
    (alien / leaf).chmod(0o755)
    unrelated = lab.root / "unrelated"
    unrelated.mkdir(exist_ok=True)
    inherited = {**lab.env, "PATH": str(alien) + os.pathsep + lab.env["PATH"]}
    for caller in (lab.caller, unrelated):
        _write_state(lab)
        child = _invoke(lab, "doctor", "--json", module=module, cwd=caller, env=inherited)
        result = _json_result(child, 0)
        assert result["status"] == "READY_TO_ATTEMPT"
        config, transport, grandchild = (
            _calls(lab, "diagnose_env"),
            _calls(lab, "preflight_transport"),
            _calls(lab, "preflight_child"),
        )
        assert len(config) == len(transport) == len(grandchild) == 1
        assert config[0]["root"] == str(lab.toolkit) and config[0]["cwd"] == str(caller)
        assert config[0]["env"] == str(lab.toolkit / ".env") and config[0]["explicit"] is False
        assert all(row["root"] == str(lab.toolkit) for row in _calls(lab)), "caller B must never supply an owner"
        assert all(Path(row["python"]) == lab.python for row in _calls(lab)), "select the installed environment Python"
        assert all(row["no_bytecode"] for row in _calls(lab)), "suppress toolkit imports and the doctor grandchild"
        assert grandchild[0]["cwd"] == str(lab.toolkit)
        assert Path(config[0]["path"].split(os.pathsep)[0]) == lab.python.parent
        assert transport[0]["child_no_bytecode"] == "1"
        assert Path(transport[0]["child_python"]) == lab.python, "PowerShell PATH must select the same interpreter"
        assert lab.env.get("PYTHONDONTWRITEBYTECODE") is None, "selected transport changes are child-process-only"
        assert not (caller / "_runs").exists(), "doctor must not allocate"


@contextmanager
def _changed_file(path: Path, content: bytes | None) -> Iterator[None]:
    original = path.read_bytes() if path.exists() else None
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(content)
    try:
        yield
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)


@pytest.mark.parametrize(
    "damage,reason,code",
    [
        ("missing-origin", "install_origin_unavailable", 3),
        ("malformed-origin", "install_origin_invalid", 3),
        ("nonobject-origin", "install_origin_invalid", 3),
        ("no-provider", "install_provider_unavailable", 3),
        ("wrong-provider", "install_provider_mismatch", 1),
        ("facade-record", "install_facade_mismatch", 1),
        ("foreign-project", "install_target_identity_mismatch", 1),
        ("malformed-project", "install_metadata_unavailable", 3),
        ("missing-owner", "install_target_incomplete", 3),
    ],
)
def test_installed_damaged_binding_never_borrows_caller(
    installation: SimpleNamespace, damage: str, reason: str, code: int
) -> None:
    """Damage one standard metadata/required-owner input and pin the intended refusal."""
    lab = installation
    path = lab.dist_info / "direct_url.json"
    content = None
    if damage == "malformed-origin":
        content = b"{PRIVATE_METADATA_CANARY"
    elif damage == "nonobject-origin":
        content = b'["PRIVATE_METADATA_CANARY"]'
    elif damage in {"no-provider", "wrong-provider"}:
        path = lab.dist_info / "entry_points.txt"
        content = b"" if damage == "no-provider" else b"[console_scripts]\nt2p = other:main\n"
    elif damage == "facade-record":
        path = lab.dist_info / "RECORD"
        content = b"\n".join(row for row in path.read_bytes().splitlines() if not row.startswith(b"t2p.py,"))
    elif damage in {"foreign-project", "malformed-project"}:
        path = lab.toolkit / "pyproject.toml"
        content = b'[project]\nname="foreign-project"\n' if damage == "foreign-project" else b"not = [valid"
    elif damage == "missing-owner":
        path = lab.toolkit / "scripts" / "start_migration.py"
    _write_state(lab)
    with _changed_file(path, content):
        child = _invoke(lab, "start", "--project", "fixture", "--json")
        _undelegated(lab, child, reason, code)
        assert b"PRIVATE_METADATA_CANARY" not in child.stdout + child.stderr


def test_installed_unreadable_origin(installation: SimpleNamespace) -> None:
    """An undecodable metadata read must remain unavailable, not owner dispatch."""
    lab = installation
    with _changed_file(lab.dist_info / "direct_url.json", b"\xff unreadable UTF-8 metadata"):
        _write_state(lab)
        _undelegated(lab, _invoke(lab, "doctor", "--json"), "install_metadata_unavailable", 3)


@pytest.mark.parametrize(
    "origin",
    [
        {"url": "https://example.invalid/toolkit", "dir_info": {}},
        {"url": "file://server/share/toolkit", "dir_info": {}},
        {"url": "file:relative", "dir_info": {}},
        {"url": "file:////server/share/toolkit", "dir_info": {}},
        {"url": "file:///toolkit?PRIVATE_QUERY_CANARY", "dir_info": {}},
        {"url": "file:///toolkit#PRIVATE_FRAGMENT_CANARY", "dir_info": {}},
        {"url": "file:///toolkit", "dir_info": {"editable": True}},
        {"url": "file:///toolkit", "dir_info": {"editable": "false"}},
        {"url": "file:///toolkit", "dir_info": {}, "subdirectory": "scripts"},
        {"url": "file:///toolkit", "archive_info": {}},
        {"url": "file:///toolkit", "vcs_info": {"vcs": "git", "commit_id": "synthetic"}},
    ],
)
def test_installed_unsupported_origins(installation: SimpleNamespace, origin: dict) -> None:
    """Reject forms outside the approved local-directory installation contract."""
    lab = installation
    _write_state(lab)
    with _changed_file(lab.dist_info / "direct_url.json", json.dumps(origin).encode()):
        child = _invoke(lab, "doctor", "--json")
        _undelegated(lab, child, "install_form_unsupported", 1)
        assert b"PRIVATE_" not in child.stdout + child.stderr


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("missing-distribution", "install_distribution_unavailable"),
        ("duplicate-distribution", "install_distribution_ambiguous"),
        ("duplicate-provider", "install_provider_ambiguous"),
        ("foreign-provider", "install_provider_mismatch"),
    ],
)
def test_installed_unique_distribution_and_provider(installation: SimpleNamespace, damage: str, reason: str) -> None:
    """Never choose the first/latest provider or borrow a second distribution."""
    lab = installation
    extra = lab.site / "other_provider-9.9.dist-info"
    relocated = lab.root / "temporarily-removed-dist-info"
    original_entry = (lab.dist_info / "entry_points.txt").read_bytes()
    if damage == "missing-distribution":
        lab.dist_info.rename(relocated)
    elif damage == "duplicate-distribution":
        shutil.copytree(lab.dist_info, extra)
    else:
        extra.mkdir()
        (extra / "METADATA").write_text("Name: other-provider\nVersion: 9.9\n", encoding="utf-8")
        (extra / "entry_points.txt").write_text("[console_scripts]\nt2p = other:main\n", encoding="utf-8")
        if damage == "foreign-provider":
            (lab.dist_info / "entry_points.txt").write_text("", encoding="utf-8")
    try:
        _write_state(lab)
        code = 1 if damage == "foreign-provider" else 3
        _undelegated(lab, _invoke(lab, "start", "--project", "fixture", "--json"), reason, code)
    finally:
        if damage == "missing-distribution":
            relocated.rename(lab.dist_info)
        else:
            shutil.rmtree(extra)
            (lab.dist_info / "entry_points.txt").write_bytes(original_entry)


def test_installed_missing_and_moved_target(installation: SimpleNamespace) -> None:
    """A still-launchable environment refuses its unavailable target without using B."""
    lab = installation
    relocated = lab.root / "moved toolkit"
    lab.toolkit.rename(relocated)
    try:
        _write_state(lab)
        _undelegated(lab, _invoke(lab, "start", "--project", "fixture", "--json"), "install_target_unavailable", 3)
        # Even a second otherwise valid origin must not be guessed from the caller directory.
        assert (lab.caller / "scripts" / "start_migration.py").is_file()
    finally:
        relocated.rename(lab.toolkit)
    with _changed_file(
        lab.dist_info / "direct_url.json",
        json.dumps({"url": (lab.root / "deleted target").as_uri(), "dir_info": {}}).encode(),
    ):
        _write_state(lab)
        _undelegated(lab, _invoke(lab, "doctor", "--json"), "install_target_unavailable", 3)


def _directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        _checked(
            [os.environ["COMSPEC"], "/c", "mklink", "/J", link, target],
            cwd=link.parent,
            env=os.environ.copy(),
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        link.rmdir()


def test_installed_redirected_root_and_required_owner(installation: SimpleNamespace) -> None:
    """A finite required-path join must not dispatch an owner redirected to B."""
    lab = installation
    alias = lab.root / "toolkit alias"
    _directory_link(alias, lab.toolkit)
    try:
        with _changed_file(
            lab.dist_info / "direct_url.json", json.dumps({"url": alias.as_uri(), "dir_info": {}}).encode()
        ):
            _write_state(lab)
            _undelegated(lab, _invoke(lab, "doctor", "--json"), "install_target_redirected", 1)
    finally:
        _remove_link(alias)
    scripts = lab.toolkit / "scripts"
    saved = lab.toolkit / "saved scripts"
    scripts.rename(saved)
    _directory_link(scripts, lab.caller / "scripts")
    try:
        _write_state(lab)
        _undelegated(lab, _invoke(lab, "start", "--project", "fixture", "--json"), "install_target_redirected", 1)
    finally:
        _remove_link(scripts)
        saved.rename(scripts)


def test_installed_module_shadow_cannot_dispatch(installation: SimpleNamespace) -> None:
    """An independently imported cwd facade cannot claim the installed module's binding."""
    lab = installation
    with _changed_file(lab.caller / "t2p.py", (lab.caller / "scripts" / "t2p.py").read_bytes()):
        _write_state(lab)
        _undelegated(
            lab, _invoke(lab, "start", "--project", "fixture", "--json", module=True), "install_facade_mismatch", 1
        )


def _expected_base(command: str) -> dict:
    """Independent fixed v1 fields, not imported from the implementation under test."""
    result = {
        "schema_version": "t2p/v1",
        "command": command,
        "status": "error",
        "exit_code": 1,
        "authority": None,
        "delegated_exit_code": None,
        "stages": [],
        "checks": [],
        "evidence": [],
        "artifacts": [],
        "run_location": None,
        "runs_root": None,
        "run_location_reason": "not_applicable_no_run" if command == "doctor" else "not_allocated_path_withheld",
        "path_report": None,
        "limitations": [],
        "proof_boundary": {"established": [], "not_established": UNESTABLISHED},
    }
    if command != "doctor":
        reason = "unknown_until_harvest" if command == "start" else "not_recorded"
        result["path_report"] = {
            "owner": "run_estate.py",
            "initial_decision": "cannot_establish",
            "initial_reason": "unknown_until_harvest",
            "file_ceiling_utf16": 259,
            "directory_ceiling_utf16": 247,
            "projection_verdict": None,
            "projection_reason": reason,
            **{
                field: {"value": None, "reason": reason}
                for field in (
                    "longest_file_locator",
                    "longest_directory_locator",
                    "longest_file_utf16",
                    "longest_directory_utf16",
                    "remaining_file_utf16",
                    "remaining_directory_utf16",
                )
            },
            "rerun": {"argv": None, "authoritative": False, "reason": "arguments_not_recorded"},
            "subst": {"value": None, "reason": "not_derivable_read_only"},
        }
    return result


def _wire(result: dict) -> bytes:
    return (json.dumps(result, ensure_ascii=True, sort_keys=True) + os.linesep).encode("ascii")


def _assert_json_start_markers(child: subprocess.CompletedProcess, first: str, last: str) -> None:
    assert not any(line.startswith(b"RUN ") for line in child.stdout.splitlines()), (
        "start marker channel assertion: JSON progress belongs on stderr"
    )
    lines = child.stderr.splitlines()
    assert lines and lines[0] == first.encode("ascii", errors="backslashreplace"), (
        "start initial marker assertion: ASCII-escaped first progress"
    )
    assert len(lines) >= 2 and lines[-1] == last.encode("utf-8"), "start final marker assertion: UTF-8 last progress"


@pytest.mark.parametrize("early", [False, True])
def test_source_unicode_start_marker_oracle_controls(early: bool) -> None:
    """Independent literal bytes discriminate paths, encodings, presence and channels."""
    first = "RUN NOT_ALLOCATED; runs_root=C:\\fixture\\café # %\\_runs"
    first_bytes = rb"RUN NOT_ALLOCATED; runs_root=C:\fixture\caf\xe9 # %\_runs"
    last = first if early else "RUN C:\\fixture\\café # %\\_runs\\001-fixture"
    last_bytes = (
        b"RUN NOT_ALLOCATED; runs_root=C:\\fixture\\caf\xc3\xa9 # %\\_runs"
        if early
        else b"RUN C:\\fixture\\caf\xc3\xa9 # %\\_runs\\001-fixture"
    )
    stdout = b'{"schema_version":"t2p/v1"}\n'
    stderr = first_bytes + b"\n" + last_bytes + b"\n"
    child = subprocess.CompletedProcess([], 1 if early else 0, stdout, stderr)
    _assert_json_start_markers(child, first, last)
    for bad_stderr, label in (
        (stderr.replace(b"C:\\fixture", b"C:\\other", 1), "start initial marker assertion"),
        (
            first_bytes + b"\n" + last_bytes.replace(b"C:\\fixture", b"C:\\other") + b"\n",
            "start final marker assertion",
        ),
        (first_bytes + b"\n", "start final marker assertion"),
        (first_bytes + b"\n" + last.encode("ascii", errors="backslashreplace") + b"\n", "start final marker assertion"),
    ):
        with pytest.raises(AssertionError, match=label):
            _assert_json_start_markers(
                subprocess.CompletedProcess([], child.returncode, stdout, bad_stderr), first, last
            )
    with pytest.raises(AssertionError, match="start marker channel assertion"):
        _assert_json_start_markers(subprocess.CompletedProcess([], child.returncode, stdout + stderr, b""), first, last)


@pytest.mark.parametrize("early", [False, True])
def test_source_unicode_start_markers(tmp_path: Path, early: bool) -> None:
    """Real direct synthetic owners retain ASCII first/UTF-8 last stderr markers."""
    lab = _lab(tmp_path / "markers", "toolkit café # %")
    _write_state(lab, before_allocation=early, start_code=1 if early else 0)
    child = _invoke(lab, "start", "--project", "fixture", "--json", direct=True)
    first = "RUN NOT_ALLOCATED; runs_root=" + str(lab.toolkit / "_runs")
    last = first if early else "RUN " + str(lab.toolkit / "_runs" / "001-fixture")
    _assert_json_start_markers(child, first, last)
    result = _json_result(child, 1 if early else 0)
    assert result["authority"] == "start_migration.py"
    assert len(_calls(lab, "start")) == 1, "the direct marker control must reach its synthetic owner"
    assert (result["run_location"] is None) is early


def _expected_doctor(native: int | None, configuration: dict) -> dict:
    expected = _expected_base("doctor")
    unknown = native not in {0, 1} or configuration["status"] == "CANNOT_ESTABLISH"
    blocked = native == 1 or configuration["status"] == "BLOCKED_CONFIG"
    expected.update(
        status="CANNOT_ESTABLISH" if unknown else "BLOCKED_CONFIG" if blocked else "READY_TO_ATTEMPT",
        exit_code=3 if unknown else 1 if blocked else 0,
        authority="preflight.ps1+tableau_env.py",
        checks=configuration["checks"],
        limitations=[configuration["reason"], "presence_only_not_authentication"],
        stages=[
            {
                "stage": "preflight",
                "owner": "preflight.ps1",
                "native_exit_code": native,
                "state": "ok" if native == 0 else "cannot_establish" if native is None else "failed",
                "artifact": None,
            }
        ],
    )
    expected["proof_boundary"]["established"] = ["configuration_presence", "plain_preflight_result"]
    return expected


@pytest.mark.parametrize("native", [0, 1, 7, None])
@pytest.mark.parametrize("configuration_status", ["READY_TO_ATTEMPT", "BLOCKED_CONFIG", "CANNOT_ESTABLISH"])
def test_installed_doctor_native_and_json_parity(
    installation: SimpleNamespace, native: int | None, configuration_status: str
) -> None:
    """The existing two-owner doctor policy and complete JSON remain unchanged."""
    lab = installation
    configuration = {
        "status": configuration_status,
        "reason": "presence_only",
        "checks": [],
        "configuration_aliases": [],
    }
    _write_state(lab, configuration=configuration, preflight_code=native, launch_failure=native is None)
    expected = _expected_doctor(native, configuration)
    outputs = []
    for direct, module in ((False, False), (False, True), (True, False)):
        lab.log.write_text("", encoding="utf-8")
        child = _invoke(lab, "doctor", "--env", "relative.env", "--json", direct=direct, module=module)
        result = _json_result(child, expected["exit_code"])
        assert child.stderr == b"Checking existing preflight; no upgrades or Tableau login." + os.linesep.encode()
        detail = result.pop("doctor_details")
        assert result == expected, "installed doctor must retain every fixed native/JSON v1 field"
        assert detail["configuration_aliases"] == []
        assert detail["preflight"]["availability"] == ("available" if native == 0 else "unavailable")
        assert _calls(lab, "diagnose_env")[0]["env"] == "relative.env"
        assert _calls(lab, "diagnose_env")[0]["resolved_env"] == str(lab.caller / "relative.env")
        assert len(_calls(lab, "preflight_transport")) == 1, "exactly one plain preflight child"
        assert len(_calls(lab, "preflight_child")) == (0 if native is None else 1)
        outputs.append(child.stdout)
    assert outputs[0] == outputs[1] == outputs[2], (
        "complete doctor JSON bytes must remain direct/module/console equivalent"
    )


@pytest.mark.parametrize(
    "code,stages,interrupt",
    [
        (0, [["preflight", 0], ["bundle", 0]], False),
        (1, [["preflight", 0], ["survey", 7]], False),
        (1, [["preflight", 0], ["reference", 4], ["bundle", 0]], False),
        (3, [["preflight", 0], ["reference", None], ["bundle", 0]], False),
        (1, [["preflight", 0], ["bundle", 10]], False),
        (130, [["preflight", 0]], True),
    ],
)
def test_installed_start_argv_native_and_json_parity(
    installation: SimpleNamespace, code: int, stages: list, interrupt: bool
) -> None:
    """Keep literal owner transport, stage results and wrapper exits independently pinned."""
    lab = installation
    _write_state(lab, start_code=code, stages=stages, interrupt=interrupt)
    args = [
        "start",
        "--project",
        "Exact Project é",
        "--env",
        "relative.env",
        "--runs-parent",
        "relative-parent",
        "--storage-decision",
        "relative.policy",
        "--session-id",
        "5f556e3c-8062-49bd-a93e-c8d37a9a58bf",
        "--json",
    ]
    expected = _expected_base("start")
    run = str(lab.toolkit / "_runs" / "001-fixture")
    expected.update(
        exit_code=code,
        delegated_exit_code=code,
        authority="start_migration.py",
        status="ok" if code == 0 else "interrupted" if code == 130 else "cannot_establish" if code == 3 else "error",
        run_location={
            "recorded_run": run,
            "selected_run": run,
            "physical_run": run,
            "relationship": "repo_local",
            "git_visibility": "unknown",
            "git_visibility_reason": "not_recorded",
            "commit_safety": "not_for_git",
            "commit_safety_reason": "existing_output_guard_passed",
        },
        run_location_reason=None,
        limitations=["capture_only_not_completion"],
        stages=[
            {"stage": stage, "owner": owner, "native_exit_code": None, "state": "not_launched", "artifact": None}
            for stage, owner in STAGES.items()
        ],
    )
    for stage, native in stages:
        expected["stages"][list(STAGES).index(stage)].update(
            native_exit_code=native, state="ok" if native == 0 else "failed"
        )
    if any(stage == "bundle" for stage, _ in stages):
        expected["path_report"].update(projection_reason="not_recorded")
    expected["proof_boundary"]["established"] = ["delegated_wrapper_result"]
    for direct, module in ((False, False), (False, True), (True, False)):
        lab.log.write_text("", encoding="utf-8")
        child = _invoke(lab, *args, direct=direct, module=module)
        _assert_json_start_markers(
            child,
            "RUN NOT_ALLOCATED; runs_root=" + str(lab.caller / "relative-parent" / "_runs"),
            "RUN " + run,
        )
        assert _json_result(child, code) == expected, "native stage exits must remain distinct from the wrapper"
        assert child.stdout == _wire(expected), "fixed complete start v1 bytes"
        calls = _calls(lab, "start")
        assert len(calls) == 1 and calls[0]["argv"] == args[1:-1], "literal argv must reach the same owner once"
        assert calls[0]["cwd"] == str(lab.caller) and calls[0]["root"] == str(lab.toolkit)
        assert calls[0]["diagnostic_env"] is True
        assert calls[0]["resolved_env"] == str(lab.caller / "relative.env")
        assert calls[0]["default_runs"] == str(lab.caller / "relative-parent" / "_runs")
        storage = _calls(lab, "storage_child")
        assert len(storage) == 1 and storage[0]["argv"] == ["relative.policy"]
        assert storage[0]["cwd"] == str(lab.toolkit)
        assert storage[0]["resolved_storage"] == str(lab.toolkit / "relative.policy"), (
            "do not silently rebase storage policy"
        )


def test_installed_start_default_roots_and_preallocation(installation: SimpleNamespace) -> None:
    """Default paths are rooted at A and delegated preallocation markers survive."""
    lab = installation
    for early in (False, True):
        _write_state(lab, before_allocation=early, start_code=1 if early else 0)
        child = _invoke(lab, "start", "--project", "fixture", "--json")
        first = "RUN NOT_ALLOCATED; runs_root=" + str(lab.toolkit / "_runs")
        _assert_json_start_markers(
            child, first, first if early else "RUN " + str(lab.toolkit / "_runs" / "001-fixture")
        )
        result = _json_result(child, 1 if early else 0)
        calls = _calls(lab, "start")
        assert len(calls) == 1 and calls[0]["argv"] == ["--project", "fixture"]
        assert calls[0]["env"] == str(lab.toolkit / ".env")
        assert calls[0]["default_runs"] == str(lab.toolkit / "_runs")
        if early:
            assert result["run_location"] is result["runs_root"] is None


@pytest.mark.parametrize("case", ["intact", "findings", "missing", "moved", "relative"])
def test_installed_status_single_owner_json_and_human_parity(installation: SimpleNamespace, case: str) -> None:
    """Status owns run identity/diagnostic exits; the installed form adds no policy or reads."""
    lab = installation
    intact = case in {"intact", "findings"}
    code = 2 if case == "relative" else 0 if intact else 1
    observed = {
        "run_location": {"state": "intact" if intact else case},
        "findings": [
            {
                "code": "UNASSESSABLE_EVIDENCE",
                "reason": "run_location_unestablished",
                "unassessable": not intact,
                "unit": "PRIVATE_LABEL_CANARY",
            }
        ]
        if case != "intact"
        else [],
        "units": [],
        "unscoped_packages": [],
        "recorded_phases": [],
        "run_recorded_status": "active" if intact else None,
        "inventory_scope": "established" if intact else None,
    }
    _write_state(lab, observed=observed, status_code=code)
    run = str(lab.root / "explicit-run") if case != "relative" else "relative-run"
    expected = _expected_base("status")
    expected.update(
        status="cannot_establish" if not intact else "findings" if observed["findings"] else "ok",
        exit_code=code,
        delegated_exit_code=code,
        authority="run_status.py",
        limitations=["last_observed_not_liveness", "customer_labels_withheld", "no_routing"],
        run_location_reason=None if intact else "identity_unestablished",
        observations={
            "run_recorded_status": observed["run_recorded_status"],
            "inventory_scope": observed["inventory_scope"],
            "canonical_subdirs": None,
            "child_evidence": None,
            "findings": [{key: value for key, value in row.items() if key != "unit"} for row in observed["findings"]],
            "units": [],
            "unscoped_package_count": 0,
            "recorded_phases": [],
        },
    )
    if intact:
        expected["run_location"] = {
            "recorded_run": run,
            "selected_run": run,
            "physical_run": run,
            **{key: None for key in ("relationship", "git_visibility", "commit_safety")},
            **{
                key + "_reason": "not_derivable_read_only"
                for key in ("relationship", "git_visibility", "commit_safety")
            },
        }
    expected["proof_boundary"]["established"] = ["read_only_inventory"]
    for json_mode in (False, True):
        outputs = []
        for direct, module in ((False, False), (False, True), (True, False)):
            lab.log.write_text("", encoding="utf-8")
            child = _invoke(
                lab, "status", "--run", run, *(["--json"] if json_mode else []), direct=direct, module=module
            )
            assert child.returncode == code and child.stderr == b""
            assert b"PRIVATE_LABEL_CANARY" not in child.stdout
            assert len(_calls(lab, "build_status")) == 1, "one existing owner, not a second diagnostic path"
            assert _calls(lab, "build_status")[0]["run"] == run, "absolute/relative spelling belongs to the owner"
            if json_mode:
                assert _json_result(child, code) == expected
                assert child.stdout == _wire(expected), "fixed complete status v1 bytes"
            outputs.append(child.stdout)
        assert outputs[0] == outputs[1] == outputs[2], "installed status presentation must not redesign direct output"


def test_installed_only_environment_safe_doctor_guidance(installation: SimpleNamespace) -> None:
    """A1 changes only installed human usage while preserving direct advice and JSON."""
    lab = installation
    blocker = {
        "id": "engine_source",
        "source": None,
        "reason": "verdict_unavailable",
        "remedy": "inspect_engine_source",
    }
    _write_state(
        lab,
        preflight_code=1,
        preflight={"critical_missing": 2, "recommended_warnings": 0, "blockers": [IMPORT_BLOCKER, blocker]},
    )
    installed = _invoke(lab, "doctor", "--env", "relative.env")
    direct = _invoke(lab, "doctor", "--env", "relative.env", direct=True)
    assert installed.returncode == direct.returncode == 1
    text = installed.stdout.decode("ascii")
    assert (
        "uv sync --all-extras --no-editable --reinstall-package tableau-to-pbi-migration from <selected-toolkit>"
        in text
    )
    assert (
        "uv pip install --reinstall-package tableau-to-pbi-migration"
        " --python <selected-python> <selected-toolkit> only for plain/spaced paths, without --editable"
    ) in text
    assert "Raw native uv-pip combined Unicode/#/% compatibility is deferred" in text
    assert "canonical uv sync and pip retain both path-class qualification targets" in text
    assert "<selected-python> -m pip install <selected-toolkit> (pip must already be available)" in text
    assert "In <selected-toolkit>, using <selected-python> for Python commands:" in text
    assert "Rerun doctor: <selected-t2p> doctor --env <same-selected-env>" in text
    assert str(lab.toolkit) not in text and str(lab.venv) not in text, (
        "installed repair strings use only safe placeholders"
    )
    assert b"activate the intended Python environment and run uv sync manually." in direct.stdout
    assert b"Rerun doctor: python -B scripts\\t2p.py doctor --env <same-selected-env>" in direct.stdout
    assert _invoke(lab, "doctor", "--json").stdout == _invoke(lab, "doctor", "--json", direct=True).stdout
    with _changed_file(lab.dist_info / "direct_url.json", None):
        _write_state(lab)
        failure = _invoke(lab, "start", "--project", "PRIVATE_ARGV_CANARY")
        assert failure.returncode == 3 and failure.stderr == b""
        assert b"No owner was entered" in failure.stdout and b"without --editable" in failure.stdout
        assert b"pip must already be available" in failure.stdout
        assert b"PRIVATE_ARGV_CANARY" not in failure.stdout and str(lab.toolkit).encode() not in failure.stdout


def _snapshot(root: Path) -> dict:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else ("directory",)
        for path in root.rglob("*")
    }


def _remove_source_caches(lab: SimpleNamespace) -> None:
    for root in (lab.toolkit, lab.caller):
        for path in root.rglob("__pycache__"):
            shutil.rmtree(path)


def _monitored_roots(lab: SimpleNamespace) -> tuple[Path, ...]:
    run = lab.root / "monitored-run"
    run.mkdir(exist_ok=True)
    (run / "run.json").write_text('{"synthetic":true}\n', encoding="utf-8")
    return lab.toolkit, lab.caller, lab.root / "profile", run


def _assert_unchanged(roots: tuple[Path, ...], before: list[dict], *, label: str) -> None:
    assert [_snapshot(root) for root in roots] == before, label


def _read_only_case(
    lab: SimpleNamespace,
    *,
    prefix: Path | None = None,
    label: str = "installed toolkit/config/run byte/mtime/tree/no-pyc assertion",
    direct: bool = False,
) -> None:
    _remove_source_caches(lab)
    roots = _monitored_roots(lab)
    _write_state(lab)
    before = [_snapshot(root) for root in roots]
    env = {
        key: value for key, value in lab.env.items() if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}
    }
    if prefix is not None:
        prefix.mkdir()
        env["PYTHONPYCACHEPREFIX"] = str(prefix)
    for _ in range(2):
        for command in (["doctor", "--json"], ["status", "--run", str(roots[-1]), "--json"]):
            lab.log.write_text("", encoding="utf-8")
            _json_result(_invoke(lab, *command, env=env, direct=direct), 0)
            _assert_unchanged(roots, before, label=label)
            if prefix is not None:
                toolkit_caches = [
                    path
                    for path in prefix.rglob("*.pyc")
                    if path.name.startswith(
                        ("fixture_witness.", "tableau_env.", "run_status.", "child_witness.", "child_cache_witness.")
                    )
                ]
                assert not toolkit_caches, label
            if command[0] == "doctor":
                assert len(_calls(lab, "preflight_child")) == 1, "doctor grandchild must actually run"
            else:
                assert len(_calls(lab, "build_status")) == 1, "read-only status must reach its single owner"
            assert all(row["no_bytecode"] for row in _calls(lab)), "selected backend/grandchild suppression witness"
            assert not _calls(lab, "forbidden_process"), "forbidden updater/login/source command log must stay empty"


def test_installed_first_import_and_read_only_cache_controls(installation: SimpleNamespace) -> None:
    """Calibrate snapshots, then distinguish toolkit writes from environment/prefix caches."""
    lab = installation
    assert lab.first_launch_verified, "fixture monitored the first actual generated launch, without -B"
    roots = _monitored_roots(lab)
    before = [_snapshot(root) for root in roots]
    known_write = lab.toolkit / "known-write-control"
    known_write.write_text("calibrate the actual snapshot\n", encoding="utf-8")
    try:
        with pytest.raises(AssertionError, match="calibrated toolkit tree detects writes"):
            _assert_unchanged(roots, before, label="calibrated toolkit tree detects writes")
    finally:
        known_write.unlink()
    _read_only_case(lab)
    _read_only_case(lab, prefix=lab.root / "fresh-prefix")


@contextmanager
def _facade_mutation(lab: SimpleNamespace, old: str, new: str) -> Iterator[None]:
    """One deliberately broken disposable installed copy, restored before the next control."""
    path = lab.site / "t2p.py"
    original = path.read_text(encoding="utf-8")
    assert original.count(old) == 1, "mutation must reach its one intended installed-source statement"
    with _changed_file(path, original.replace(old, new).encode("utf-8")):
        cache = lab.site / "__pycache__"
        for pyc in cache.glob("t2p.*.pyc"):
            pyc.unlink()
        try:
            yield
        finally:
            for pyc in cache.glob("t2p.*.pyc"):
                pyc.unlink()
            _remove_source_caches(lab)


@pytest.mark.parametrize("boundary", ["backend-import", "doctor-child"])
def test_installed_cache_reversals_fail_named_assertions(installation: SimpleNamespace, boundary: str) -> None:
    """Successful mutated launches must fail filesystem assertions, never import/setup."""
    lab = installation
    if boundary == "backend-import":
        old = 'sys.dont_write_bytecode = True\n    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"'
        new = 'sys.dont_write_bytecode = False\n    os.environ.pop("PYTHONDONTWRITEBYTECODE", None)'
        label = "installed toolkit/config/run byte/mtime/tree/no-pyc assertion"
    else:
        old, new = '"PYTHONDONTWRITEBYTECODE": "1"', '"PYTHONDONTWRITEBYTECODE": ""'
        label = "installed doctor grandchild no-pyc assertion"
    with _facade_mutation(lab, old, new):
        with pytest.raises(AssertionError, match=label):
            _read_only_case(lab, label=label)
    _read_only_case(lab)


def _live_streams(lab: SimpleNamespace, *, json_mode: bool, direct: bool = False) -> None:
    _write_state(lab, streams=True, start_code=1, stages=[["bundle", 10]])
    lab.release.unlink(missing_ok=True)
    with subprocess.Popen(
        [
            *([str(lab.python), "-B", str(lab.toolkit / "scripts" / "t2p.py")] if direct else [str(lab.console)]),
            "start",
            "--project",
            "fixture",
            *(["--json"] if json_mode else []),
        ],
        cwd=lab.caller,
        env=lab.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        lines: queue.Queue = queue.Queue()

        def read(channel: str, stream) -> None:
            for line in iter(stream.readline, b""):
                lines.put((channel, line))

        readers = [
            threading.Thread(target=read, args=(name, stream), daemon=True)
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
        ]
        for reader in readers:
            reader.start()
        progress = []
        try:
            while not all(
                any(token == line.strip() for _, line in progress)
                for token in (b"LIVE_FD1", b"LIVE_FD2", b"LIVE_CHILD_FD1", b"LIVE_CHILD_FD2")
            ):
                try:
                    progress.append(lines.get(timeout=10))
                except queue.Empty:
                    pytest.fail("installed live-progress assertion: no progress before delegated completion")
            assert process.poll() is None, "installed live-progress assertion: owner must still be alive"
            expected = {
                b"LIVE_FD1": "stderr" if json_mode else "stdout",
                b"LIVE_FD2": "stderr",
                b"LIVE_CHILD_FD1": "stderr" if json_mode else "stdout",
                b"LIVE_CHILD_FD2": "stderr",
            }
            for token, channel in expected.items():
                assert any(source == channel and line.strip() == token for source, line in progress), (
                    "installed live channel assertion: native/grandchild fd1/fd2 must use the intended live channel"
                )
        finally:
            lab.release.write_text("release synthetic owner\n", encoding="utf-8")
            try:
                code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
            for reader in readers:
                reader.join(timeout=5)
        while not lines.empty():
            progress.append(lines.get_nowait())
        assert all(not reader.is_alive() for reader in readers)
    assert code == 1, "installed native bundle 10 must keep wrapper exit 1"
    stdout = b"".join(line for channel, line in progress if channel == "stdout")
    if json_mode:
        assert len(stdout.splitlines()) == 1, "installed JSON stdout isolation assertion"
        result = json.loads(stdout)
        assert result["delegated_exit_code"] == 1
        assert result["stages"][-1]["native_exit_code"] == 10
    else:
        assert b"LIVE_FD1" in stdout and b"LIVE_CHILD_FD1" in stdout


@pytest.mark.parametrize("json_mode", [False, True])
def test_installed_native_streams_are_live(installation: SimpleNamespace, json_mode: bool) -> None:
    """Observe all four native/grandchild channels before releasing the live owner."""
    _live_streams(installation, json_mode=json_mode)


def test_installed_stream_reversal_fails_channel_assertion(installation: SimpleNamespace) -> None:
    """An fd1 reversal must fail the live-channel assertion before JSON decoding."""
    with _facade_mutation(installation, "os.dup2(2, 1)", "os.dup2(saved, 1)"):
        with pytest.raises(AssertionError, match="installed live channel assertion"):
            _live_streams(installation, json_mode=True)
    _live_streams(installation, json_mode=True)


def test_regular_install_missing_dependency_is_not_healthy(installation: SimpleNamespace) -> None:
    """Pip's independent dependency checker must detect a truly uninstalled requirement."""
    lab = installation
    _checked([lab.python, "-B", "-m", "pip", "check"], cwd=lab.caller, env=lab.env)
    _checked([lab.python, "-B", "-m", "pip", "uninstall", "-y", "lxml"], cwd=lab.caller, env=lab.env)
    try:
        child = subprocess.run(
            [str(lab.python), "-B", "-m", "pip", "check"],
            cwd=lab.caller,
            env=lab.env,
            capture_output=True,
            check=False,
            timeout=45,
        )
        assert child.returncode == 1, "dependency negative must reach pip's actual dependency check"
        assert b"requires lxml, which is not installed" in child.stdout.lower(), (
            "pip must name the missing base dependency"
        )
    finally:
        _checked([lab.python, "-B", "-m", "pip", "install", "lxml>=5.2"], cwd=lab.caller, env=lab.env)
    _checked([lab.python, "-B", "-m", "pip", "check"], cwd=lab.caller, env=lab.env)


def test_real_wheel_install_is_explicitly_unsupported(installation: SimpleNamespace) -> None:
    """Obtain an actual archive origin, then reach its install-layer refusal."""
    lab = installation
    wheels = lab.root / "wheels"
    wheels.mkdir()
    _checked(
        [lab.python, "-B", "-m", "pip", "wheel", "--no-deps", "--wheel-dir", wheels, lab.toolkit],
        cwd=lab.caller,
        env=lab.env,
    )
    (wheel,) = wheels.glob("tableau_to_pbi_migration-*.whl")
    try:
        _install(lab, wheel)
        assert "archive_info" in lab.metadata["origin"] and "dir_info" not in lab.metadata["origin"]
        _write_state(lab)
        _undelegated(lab, _invoke(lab, "doctor", "--json"), "install_form_unsupported", 1)
    finally:
        _install(lab, lab.toolkit)


def test_real_editable_install_is_explicitly_unsupported(installation: SimpleNamespace) -> None:
    """Real editable metadata reaches refusal and exposes the earlier source cache effect."""
    lab = installation
    try:
        _install(lab, lab.toolkit, editable=True)
        assert lab.metadata["origin"]["dir_info"]["editable"] is True, "reach a real editable install, not ImportError"
        _remove_source_caches(lab)
        _write_state(lab)
        _undelegated(lab, _invoke(lab, "start", "--project", "fixture", "--json"), "install_form_unsupported", 1)
        assert list((lab.toolkit / "scripts" / "__pycache__").glob("t2p.*.pyc")), (
            "editable early module import caches before console_main;"
            " this is intentionally NOT a read-only supported form"
        )
        _write_state(lab)
        assert _json_result(_invoke(lab, "start", "--project", "fixture", "--json", direct=True), 0)["authority"] == (
            "start_migration.py"
        )
    finally:
        _install(lab, lab.toolkit)
        _remove_source_caches(lab)


def test_regular_reinstall_selects_b_and_snapshot_backends_stay_live(installation: SimpleNamespace) -> None:
    """Only reinstall rebinds the snapshot; edits to selected backend contents stay live."""
    lab = installation
    original = (lab.toolkit / "scripts" / "t2p.py").read_bytes()
    try:
        # Changing facade source is not a live editable update to the installed snapshot.
        (lab.toolkit / "scripts" / "t2p.py").write_text(
            "raise RuntimeError('source snapshot changed')\n", encoding="utf-8"
        )
        _write_state(lab)
        assert (
            _json_result(_invoke(lab, "start", "--project", "fixture", "--json"), 0)["authority"]
            == "start_migration.py"
        )
        owner = lab.toolkit / "scripts" / "start_migration.py"
        with _changed_file(owner, owner.read_bytes() + b'\nnote("live_backend_revision")\n'):
            _write_state(lab)
            _json_result(_invoke(lab, "start", "--project", "fixture", "--json"), 0)
            assert len(_calls(lab, "live_backend_revision")) == 1, "backend contents at the bound location remain live"
        (lab.toolkit / "scripts" / "t2p.py").write_bytes(original)
        revision = original + b'\nINSTALLER_TEST_REVISION = "regular-reinstall-control"\n'
        with _changed_file(lab.toolkit / "scripts" / "t2p.py", revision):
            _install(lab, lab.toolkit)
            assert (lab.site / "t2p.py").read_bytes() == revision, "regular reinstall must refresh the facade snapshot"
            _write_state(lab)
            _json_result(_invoke(lab, "start", "--project", "fixture", "--json"), 0)
        _install(lab, lab.caller)
        _assert_regular_origin(lab.metadata["origin"], lab.caller)
        _write_state(lab)
        _json_result(_invoke(lab, "start", "--project", "fixture", "--json"), 0)
        assert _calls(lab, "start")[0]["root"] == str(lab.caller), "only an intentional regular reinstall rebinds to B"
    finally:
        (lab.toolkit / "scripts" / "t2p.py").write_bytes(original)
        _install(lab, lab.toolkit)


@pytest.mark.parametrize("toolkit_name", ["toolkit A", "toolkit café # %"], ids=["plain", "encoded-unicode"])
def test_real_uv_sync_regular_nested_environment(toolkit_name: str) -> None:
    """Canonical regular uv sync also binds outside cwd, with .venv caches separated."""
    assert UV is not None, "uv is required; installer prerequisites cannot silently skip"
    root = REPO_ROOT / "_t2p_install_tests" / ("sync-" + uuid.uuid4().hex[:8])
    try:
        lab = _lab(root, toolkit_name)
        _checked(
            [UV, "sync", "--all-extras", "--no-editable", "--python", sys.executable], cwd=lab.toolkit, env=lab.env
        )
        lab.venv = lab.toolkit / ".venv"
        scripts = lab.venv / ("Scripts" if os.name == "nt" else "bin")
        lab.python = scripts / ("python.exe" if os.name == "nt" else "python")
        lab.console = scripts / ("t2p.exe" if os.name == "nt" else "t2p")
        installed = _metadata(lab)
        _assert_regular_origin(installed["origin"], lab.toolkit)
        assert installed["entries"] == [["console_scripts", "t2p", "t2p:console_main"]]
        assert (lab.venv / "pyvenv.cfg").is_file() and lab.console.is_file()
        _checked([lab.python, "-B", "-c", "import lxml,jsonschema"], cwd=lab.caller, env=lab.env)
        for path in (lab.toolkit / "scripts").rglob("__pycache__"):
            shutil.rmtree(path)
        before = {
            str(path.relative_to(lab.toolkit)): (path.read_bytes(), path.stat().st_mtime_ns)
            if path.is_file()
            else ("directory",)
            for path in lab.toolkit.rglob("*")
            if not path.is_relative_to(lab.venv)
        }
        for module in (False, True):
            _write_state(lab)
            result = _json_result(_invoke(lab, "doctor", "--json", module=module), 0)
            assert result["status"] == "READY_TO_ATTEMPT"
            assert _calls(lab, "diagnose_env")[0]["root"] == str(lab.toolkit)
            assert all(Path(row["python"]) == lab.python for row in _calls(lab))
            assert {
                str(path.relative_to(lab.toolkit)): (path.read_bytes(), path.stat().st_mtime_ns)
                if path.is_file()
                else ("directory",)
                for path in lab.toolkit.rglob("*")
                if not path.is_relative_to(lab.venv)
            } == before, "nested .venv host caches must never exempt toolkit scripts/config/run writes"
    finally:
        shutil.rmtree(root)
