"""Offline controls for the frozen doctor/project-start/read-only-status pilot."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import t2p  # noqa: E402
import tableau_env  # noqa: E402
import work_dirs  # noqa: E402
from test_start_migration import ENV, _Pipeline, _captured  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_tableau_environment(monkeypatch):
    """Never inherit workstation credentials in any offline control."""
    for key in tableau_env.ACCEPTED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("COPILOT_AGENT_SESSION_ID", raising=False)


def _config(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


def _snapshot(root):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _json(capsys):
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
    return result, captured


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("preflight_code,expected", [(0, 0), (1, 1), (7, 3), (None, 3)])
def test_doctor_private_presence_only(monkeypatch, tmp_path, capsys, json_mode, preflight_code, expected):
    env_file = tmp_path / "selected.env"
    values = {key: f"SYNTHETIC_PRIVATE_{key}_a9" for key in tableau_env.ACCEPTED_ENV_KEYS}
    values["TABLEAU_SERVER"] = values["TABLEAU_SERVER_URL"]
    values["TABLEAU_PAT_VALUE"] = values["TABLEAU_PAT_SECRET"]
    env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    raw = "RAW_PREFLIGHT_PRIVATE_CANARY"
    calls = []

    def child(command, **options):
        calls.append((command, options))
        assert options["env"]["PYTHONDONTWRITEBYTECODE"] == "1", "doctor child bytecode must be disabled"
        if preflight_code is None:
            raise OSError(raw)
        return subprocess.CompletedProcess(command, preflight_code, raw.encode(), raw.encode())

    monkeypatch.setattr(t2p.subprocess, "run", child)
    before = _snapshot(tmp_path)
    code = t2p.main(["doctor", "--env", str(env_file), *(["--json"] if json_mode else [])])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == expected
    for value in [*values.values(), raw, str(env_file), str(t2p.ROOT)]:
        assert value not in output, "doctor must not disclose values or host paths"
    for value in values.values():
        assert hashlib.sha256(value.encode()).hexdigest() not in output, "doctor must not disclose value digests"
    assert _snapshot(tmp_path) == before, "doctor must not mutate its configuration/tree"
    assert len(calls) == 1
    command, options = calls[0]
    assert command[:4] == ["powershell", "-ExecutionPolicy", "Bypass", "-File"], "doctor must delegate plain preflight"
    assert len(command) == 5, "doctor must delegate plain preflight without update/upstream flags"
    assert options["stdout"] == options["stderr"] == subprocess.PIPE
    presence = json.loads(captured.out)
    assert presence["limitations"] == ["presence_only", "presence_only_not_authentication"], (
        "doctor value/length/digest leak assertion"
    )
    assert all(
        set(row) == {"key", "state", "source", "required", "secret", "reason", "remedy"} for row in presence["checks"]
    ), "doctor value/length/digest leak assertion"
    if json_mode:
        result = json.loads(captured.out)
        jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
        assert result["delegated_exit_code"] is None
        assert result["run_location"] is result["path_report"] is None
        assert result["stages"][0]["native_exit_code"] == preflight_code
        assert len(result["checks"]) == len(tableau_env.CANONICAL_ENV_KEYS) + len(
            tableau_env.DATASOURCE_CREDENTIAL_KEYS
        )


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("good", 0),
        ("missing", 1),
        ("blank", 1),
        ("conflict", 1),
        ("malformed", 3),
        ("missing_file", 3),
    ],
)
def test_doctor_configuration_results(monkeypatch, tmp_path, capsys, mode, expected):
    _config(monkeypatch)
    path = tmp_path / "selected.env"
    path.write_text("# empty\n", encoding="utf-8")
    if mode == "missing":
        monkeypatch.delenv("TABLEAU_PAT_NAME")
    if mode == "blank":
        monkeypatch.setenv("TABLEAU_PAT_SECRET", " \t ")
    if mode == "conflict":
        monkeypatch.setenv("TABLEAU_PAT_VALUE", "different-secret")
    if mode == "malformed":
        path.write_text("PRIVATE_INVALID_LINE", encoding="utf-8")
    if mode == "missing_file":
        path.unlink()
    monkeypatch.setattr(t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    assert t2p.main(["doctor", "--env", str(path), "--json"]) == expected
    result, _ = _json(capsys)
    assert result["status"] == {0: "READY_TO_ATTEMPT", 1: "BLOCKED_CONFIG", 3: "CANNOT_ESTABLISH"}[expected]


def test_doctor_child_cannot_create_pycache(monkeypatch, tmp_path, capsys):
    """Exercise a real Python grandchild through the plain-preflight invocation boundary."""
    _config(monkeypatch)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    root = tmp_path / "clean"
    root.mkdir()
    (root / "witness.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_run = subprocess.run
    calls = []

    def preflight(command, **options):
        calls.append(command)
        return real_run([sys.executable, "-c", "import witness"], **{**options, "cwd": root})

    monkeypatch.setattr(t2p.subprocess, "run", preflight)
    monkeypatch.setattr(t2p, "ROOT", root)
    before = _snapshot(root)
    assert t2p.main(["doctor", "--json"]) == 0
    _json(capsys)
    assert calls[0][0] == "powershell"
    assert _snapshot(root) == before, "doctor clean-tree/no-pyc assertion"
    assert not list(root.rglob("*.pyc")), "doctor clean-tree/no-pyc assertion"


@pytest.mark.parametrize(
    "argv",
    [
        ["next"],
        ["start"],
        ["start", "--project", " "],
        ["start", "--proj", "name"],
        ["start", "--project", "name", "--workbook", "PRIVATE_ARGV_CANARY"],
        ["doctor", "--show-values"],
        ["status"],
        ["status", "--run", "x", "--repair"],
    ],
)
def test_closed_surface_and_safe_syntax(argv, capsys):
    assert t2p.main([*argv, "--json"]) == 2
    result, captured = _json(capsys)
    assert result["status"] == "error"
    assert result["delegated_exit_code"] is None
    assert "PRIVATE_ARGV_CANARY" not in captured.out + captured.err


@pytest.mark.parametrize("name", [None, "doctor", "start", "status"])
def test_help_cp1252(name):
    result = subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "scripts" / "t2p.py"), *([name] if name else []), "--help"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert b"--json" in result.stdout or name is None
    assert b"--next" not in result.stdout


@pytest.mark.parametrize(
    "native,wrapper,options",
    [
        ({}, 0, {}),
        ({"run_engine_survey.py": 7}, 1, {}),
        ({"capture_tableau_oracle.py": 4}, 1, {}),
        ({}, 3, {"missing_oracle": True}),
        ({"run_estate.py": 10}, 1, {}),
        ({}, 130, {"interrupt": True}),
    ],
)
def test_start_owner_parity_and_native_stages(monkeypatch, tmp_path, capsys, native, wrapper, options):
    import start_migration

    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, exit_codes=native, **options)
    monkeypatch.setattr(
        start_migration,
        "diagnose_env",
        lambda *a, **k: (
            dict(ENV),
            {"status": "READY_TO_ATTEMPT", "checks": [], "reason": "presence_only"},
        ),
    )
    assert t2p.main(["start", "--project", "Finance", "--json"]) == wrapper
    out, err = _captured(capsys, pipeline)
    result = json.loads(out)
    jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
    assert result["delegated_exit_code"] == wrapper, "preserve wrapper exit, not first native failure"
    stages = {row["stage"]: row for row in result["stages"]}
    expected_names = [
        "run_engine_survey.py",
        "assess_estate.py",
        "harvest_estate_assets.py",
        "capture_tableau_oracle.py",
        "run_estate.py",
    ]
    if "run_engine_survey.py" in native:
        expected_names = expected_names[:1]
    if options.get("interrupt"):
        expected_names = expected_names[:2]
    actual = [next(Path(part).name for part in command if part.endswith(".py")) for command in pipeline.commands]
    assert actual == expected_names, "reference failure must still attempt bundle; earlier failure stops"
    for name, code in native.items():
        stage_name = next(stage for stage, owner in t2p.STAGE_OWNERS.items() if owner == name)
        assert stages[stage_name]["native_exit_code"] == code, "retain per-stage native results"
    assert result["path_report"]["initial_reason"] == "unknown_until_harvest"
    assert result["path_report"]["projection_verdict"] is None, "never invent projection PASS from exit 0"
    assert result["run_location"]["physical_run"] in err
    assert err.splitlines()[0].startswith("RUN NOT_ALLOCATED; runs_root=")
    assert err.splitlines()[-1] == "RUN " + result["run_location"]["physical_run"]
    assert all(
        "--reference-best" in command
        for command in pipeline.commands
        if "capture_tableau_oracle.py" in " ".join(command)
    )
    assert all(options["stdout"] is None and options["stderr"] is None for options in pipeline.options), (
        "stage output must stream without buffering"
    )


def test_start_preallocation_block_has_no_path(monkeypatch, capsys):
    import start_migration

    monkeypatch.setattr(start_migration.harvest, "refuse_unignored_output", lambda *a, **k: True)
    monkeypatch.setattr(work_dirs, "allocate_run", lambda *a, **k: pytest.fail("must not allocate"))
    assert t2p.main(["start", "--project", "Finance", "--json"]) == 1
    result, captured = _json(capsys)
    assert result["run_location"] is result["runs_root"] is None
    assert result["run_location_reason"] == "not_allocated_path_withheld"
    assert captured.err.splitlines()[0] == captured.err.splitlines()[-1]


def test_native_descriptor_output_is_live_and_json_stdout_single(tmp_path):
    """A real subprocess emits on fd1/fd2 while still alive, without source/network access."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(REPO_ROOT / "scripts" / "t2p.py", scripts / "t2p.py")
    (scripts / "work_dirs.py").write_text(
        "from pathlib import Path\ndef runs_root(parent=None): return Path(__file__).parent / '_runs'\n",
        encoding="utf-8",
    )
    (scripts / "start_migration.py").write_text(
        "import os,time,subprocess,sys\n"
        "def main(argv, *, observer=None, diagnostic_env=False):\n"
        "    observer({'event':'allocated','run':os.path.dirname(__file__),'output_guard':'passed'})\n"
        "    os.write(1,b'LIVE_FD1\\n'); os.write(2,b'LIVE_FD2\\n')\n"
        "    subprocess.run([sys.executable,'-B','-c',\"import os; os.write(1,b'LIVE_CHILD_FD1\\\\n'); os.write(2,b'LIVE_CHILD_FD2\\\\n')\"],check=True)\n"
        "    time.sleep(2)\n"
        "    observer({'event':'stage','stage':'bundle','native_exit_code':10,'state':'failed','artifact':'bundle/report.json'})\n"
        "    return 1\n",
        encoding="utf-8",
    )
    with subprocess.Popen(
        [sys.executable, "-B", str(scripts / "t2p.py"), "start", "--project", "Fixture", "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stderr], daemon=True)
        reader.start()
        progress = []
        while not any("LIVE_CHILD_FD2" in line for line in progress):
            progress.append(lines.get(timeout=10))
        assert process.poll() is None, "progress must stream before delegated completion"
        assert any("LIVE_FD1" in line for line in progress), "OS stdout must be routed to stderr"
        assert any("LIVE_CHILD_FD1" in line for line in progress), "inherited child stdout must be routed to stderr"
        output = process.stdout.read()
        assert process.wait(timeout=15) == 1
        reader.join(timeout=5)
        assert not reader.is_alive()
        while not lines.empty():
            progress.append(lines.get_nowait())
    result = json.loads(output)
    assert result["delegated_exit_code"] == 1
    assert len(output.splitlines()) == 1
    assert progress[0].startswith("RUN NOT_ALLOCATED")
    assert progress[-1].strip() == "RUN " + str(scripts)


@pytest.mark.parametrize("damage", [None, "missing", "malformed", "moved"])
def test_status_readonly_and_owner_parity(monkeypatch, tmp_path, capsys, damage):
    import run_status

    run = work_dirs.allocate_run("synthetic", repo_root=tmp_path)
    path = run.root
    if damage == "missing":
        (path / "run.json").unlink()
    elif damage == "malformed":
        (path / "run.json").write_text("not-json", encoding="utf-8")
    elif damage == "moved":
        moved = path.with_name("009-moved")
        path.rename(moved)
        path = moved
    direct, expected = run_status.build_status(str(path))
    before = _snapshot(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("status must not launch subprocess/git"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("status must not launch subprocess"))
    for _ in range(2):
        assert t2p.main(["status", "--run", str(path), "--json"]) == expected
        result, _captured_output = _json(capsys)
        assert result["delegated_exit_code"] == expected
        assert len(result["observations"]["findings"]) == len(direct["findings"])
        assert "next_action" not in json.dumps(result), "status must not route or suggest"
        assert result["path_report"]["projection_verdict"] is None
        assert _snapshot(tmp_path) == before, "status byte/mtime/tree read-only assertion"
        if damage:
            assert result["status"] == "cannot_establish"
            assert result["run_location"] is None


def test_status_rejects_relative_without_scan(capsys):
    assert t2p.main(["status", "--run", "relative", "--json"]) == 2
    result, _ = _json(capsys)
    assert result["status"] == "cannot_establish"
    assert result["run_location"] is None


@pytest.mark.parametrize("command", ["doctor", "start", "status"])
def test_explicit_branch_reaches_owner(monkeypatch, capsys, command):
    calls = []

    def owner(_args, result):
        calls.append(command)
        result["status"] = "error"
        return 1

    monkeypatch.setattr(t2p, command, owner)
    arguments = {"doctor": [], "start": ["--project", "Fixture"], "status": ["--run", "relative"]}
    t2p.main([command, *arguments[command], "--json"])
    _json(capsys)
    assert calls == [command], "explicit command branch must reach its owning operation"


@pytest.mark.parametrize("root_kind", ["repo", "external"])
@pytest.mark.parametrize("gate", ["ok", "over_ceiling", "cannot_establish"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_start_reaches_existing_postharvest_gate(monkeypatch, tmp_path, capsys, root_kind, gate, json_mode):
    """Join the facade to the real conversion entry point; intercept only external work."""
    import run_estate
    import start_migration

    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    monkeypatch.setattr(
        start_migration,
        "diagnose_env",
        lambda *a, **k: (
            dict(ENV),
            {"status": "READY_TO_ATTEMPT", "checks": [], "reason": "presence_only"},
        ),
    )
    original = start_migration._run_child
    gate_calls = []
    monkeypatch.setattr(run_estate, "resolve_engine", lambda *a, **k: pipeline.root)
    monkeypatch.setattr(
        run_estate,
        "engine_provenance",
        lambda *_a: {
            "root": str(pipeline.root),
            "version": "synthetic",
            "canonical": True,
        },
    )

    original_projection = run_estate.project_estate_path_ceiling

    def projection(*args, **kwargs):
        gate_calls.append("post_harvest")
        assert any("harvest_estate_assets.py" in " ".join(command) for command in pipeline.commands)
        return original_projection(*args, **kwargs)

    monkeypatch.setattr(run_estate, "project_estate_path_ceiling", projection)
    monkeypatch.setattr(run_estate, "_input_candidates", lambda *_a: [Path("synthetic.twb")])
    monkeypatch.setattr(
        run_estate,
        "_engine_unit_names",
        lambda *_a: {
            "ok": ["W"],
            "over_ceiling": ["W" * 300],
            "cannot_establish": None,
        }[gate],
    )
    conversion_calls = []

    def engine(*_args, **_kwargs):
        assert gate == "ok", "non-pass path gate must prevent conversion"
        conversion_calls.append("permitted")
        return 7, "synthetic later engine failure"

    monkeypatch.setattr(run_estate, "run_engine", engine)

    def child(name, index, command, env, output, *, observer=None):
        if name != "bundle":
            return original(name, index, command, env, output)
        with start_migration._observe_path_gate(run_estate, observer):
            code = run_estate.main(command[command.index("--input") :])
        return start_migration.StageResult(name, code, output)

    monkeypatch.setattr(start_migration, "_run_child", child)
    arguments = ["start", "--project", "Finance", *(["--json"] if json_mode else [])]
    if root_kind == "external":
        arguments.extend(["--runs-parent", str(tmp_path / "external")])
    assert t2p.main(arguments) == 1
    out, _err = _captured(capsys, pipeline)
    result = json.loads(out) if json_mode else json.JSONDecoder().raw_decode(out, out.index('{\n  "artifacts"'))[0]
    assert gate_calls == ["post_harvest"], "existing path gate must be reached after harvest"
    expected_native = run_estate.EXIT_ENGINE_FAILED if gate == "ok" else 10
    assert result["stages"][-1]["native_exit_code"] == expected_native, "non-pass path gate must retain native 10"
    assert result["path_report"]["initial_decision"] == "cannot_establish", "unknown must not become PASS"
    report = result["path_report"]
    assert (
        report["projection_verdict"]
        == {
            "ok": "PASS",
            "over_ceiling": "OVER_CEILING",
            "cannot_establish": "CANNOT_ESTABLISH",
        }[gate]
    ), "authoritative owner path verdict must be exposed"
    assert any(row["locator"] == "bundle#pre_conversion_path_projection" for row in result["evidence"])
    assert conversion_calls == (["permitted"] if gate == "ok" else [])
    if gate != "cannot_establish":
        assert isinstance(report["longest_file_utf16"]["value"], int)
        assert report["remaining_file_utf16"]["value"] == 259 - report["longest_file_utf16"]["value"]
    else:
        assert report["longest_file_utf16"] == {"value": None, "reason": "not_recorded"}


@pytest.mark.parametrize("source", ["environment", "selected_env"])
@pytest.mark.parametrize("site", [None, "", " \t "])
def test_doctor_valid_default_and_optional_absence(monkeypatch, tmp_path, capsys, source, site):
    values = {key: value for key, value in ENV.items() if key != "TABLEAU_SITE"}
    if site is not None:
        values["TABLEAU_SITE"] = site
    selected = tmp_path / "selected.env"
    selected.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) if source == "selected_env" else "# empty\n",
        encoding="utf-8",
    )
    if source == "environment":
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    assert t2p.main(["doctor", "--env", str(selected), "--json"]) == 0
    result, _ = _json(capsys)
    row = next(row for row in result["checks"] if row["key"] == "TABLEAU_SITE")
    assert (row["state"], row["reason"], row["remedy"]) == ("set", "default_site", "none"), (
        "valid Default site must not prescribe repair"
    )
    assert row["source"] == ("default" if site is None else source)
    for row in result["checks"]:
        if not row["required"] and row["state"] == "missing":
            assert row["remedy"] == "none", "optional missing keys must not prescribe repair"


@pytest.mark.parametrize("json_mode", [False, True])
def test_status_customer_labels_are_withheld(monkeypatch, tmp_path, capsys, json_mode):
    import run_status

    labels = ["PRIVATE_UNIT_CANARY_73", "PRIVATE_FINDING_CANARY_84", "PRIVATE_PACKAGE_CANARY_95"]
    unit, finding, package = labels
    observed = {
        "run_location": {"state": "intact"},
        "findings": [
            {
                "code": "UNSCOPED_PACKAGE",
                "reason": "preserve_and_inspect_association",
                "unassessable": False,
                "unit": finding,
                "source": f"packages/{finding}",
            }
        ],
        "units": [
            {
                "unit": unit,
                "kind": "workbook",
                "scope": "scoped",
                "generated_working_copy": "present",
                "handover": "present",
                "occurrences": [{"unit": unit, "source": f"bundle/{unit}", "kind": "workbook", "status": "observed"}],
                "package": {"unit": package, "relative_path": f"packages/{package}"},
                "current_certification": "NOT_CHECKED",
            }
        ],
        "unscoped_packages": [{"unit": package, "relative_path": f"packages/{package}"}],
    }
    monkeypatch.setattr(run_status, "build_status", lambda _run: (observed, 0))
    assert t2p.main(["status", "--run", str(tmp_path), *(["--json"] if json_mode else [])]) == 0
    captured = capsys.readouterr()
    for label in labels:
        assert label not in captured.out + captured.err, f"status leaked customer label: {label}"
    result = json.loads(captured.out)
    jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
    assert len(result["observations"]["units"]) == len(result["observations"]["findings"]) == 1
    assert result["observations"]["unscoped_package_count"] == 1


@pytest.mark.parametrize(
    "file,before,after,selector,assertion",
    [
        (
            "t2p.py",
            "code = doctor(args, result)",
            "code = 0",
            "test_explicit_branch_reaches_owner[doctor]",
            "explicit command branch must reach",
        ),
        (
            "t2p.py",
            "code = start(args, result)",
            "code = 0",
            "test_explicit_branch_reaches_owner[start]",
            "explicit command branch must reach",
        ),
        (
            "t2p.py",
            "code = status(args, result)",
            "code = 0",
            "test_explicit_branch_reaches_owner[status]",
            "explicit command branch must reach",
        ),
        (
            "t2p.py",
            '"PYTHONDONTWRITEBYTECODE": "1"',
            '"MUTANT_BYTECODE": "1"',
            "test_doctor_child_cannot_create_pycache",
            "doctor clean-tree/no-pyc assertion",
        ),
        (
            "t2p.py",
            '"Bypass",',
            '"Bypass", "-Update",',
            "test_doctor_private_presence_only",
            "doctor must delegate plain preflight",
        ),
        (
            "t2p.py",
            'result["checks"] = configuration["checks"]',
            'result["checks"] = configuration["checks"]\n    result["checks"][0]["reason"] = _values.get("TABLEAU_PAT_SECRET")',
            "test_doctor_private_presence_only",
            "doctor must not disclose values",
        ),
        (
            "t2p.py",
            '"presence_only_not_authentication"]',
            '"presence_only_not_authentication", len(_values.get("TABLEAU_PAT_SECRET", ""))]',
            "test_doctor_private_presence_only",
            "doctor value/length/digest leak assertion",
        ),
        (
            "t2p.py",
            '"presence_only_not_authentication"]',
            '"presence_only_not_authentication", __import__("hashlib").sha256(_values["TABLEAU_PAT_SECRET"].encode()).hexdigest()]',
            "test_doctor_private_presence_only",
            "doctor must not disclose value digests",
        ),
        (
            "tableau_env.py",
            "source, values = label, candidate",
            'source, values = "absent", candidate',
            "test_doctor_provenance_control",
            "winning source must be environment",
        ),
        (
            "t2p.py",
            '"initial_decision": "cannot_establish"',
            '"initial_decision": "PASS"',
            "test_start_reaches_existing_postharvest_gate",
            "unknown must not become PASS",
        ),
        (
            "run_estate.py",
            "if args.input:\n        path_ok, path_detail = preflight_estate_path_ceiling",
            "if False:\n        path_ok, path_detail = preflight_estate_path_ceiling",
            "test_start_reaches_existing_postharvest_gate",
            "non-pass path gate must prevent conversion",
        ),
        (
            "t2p.py",
            'record["native_exit_code"],',
            "None,",
            "test_start_owner_parity_and_native_stages",
            "retain per-stage native results",
        ),
        (
            "start_migration.py",
            "        bundle_command = [",
            "        if failed:\n            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)\n        bundle_command = [",
            "test_start_owner_parity_and_native_stages",
            "reference failure must still attempt bundle",
        ),
        (
            "t2p.py",
            'result["delegated_exit_code"] = code',
            'result["delegated_exit_code"] = 0',
            "test_start_owner_parity_and_native_stages",
            "preserve wrapper exit",
        ),
        (
            "t2p.py",
            "os.dup2(2, 1)",
            "os.dup2(saved, 1)",
            "test_native_descriptor_output_is_live_and_json_stdout_single",
            "OS stdout must be routed to stderr",
        ),
        (
            "start_migration.py",
            '"stdout": None, "stderr": None',
            '"stdout": subprocess.PIPE, "stderr": subprocess.PIPE',
            "test_start_owner_parity_and_native_stages",
            "stage output must stream without buffering",
        ),
        (
            "t2p.py",
            "observed, code = run_status.build_status(args.run)",
            'observed, code = run_status.build_status(args.run)\n    (Path(args.run) / "mutant-write").write_text("changed")',
            "test_status_readonly_and_owner_parity",
            "status byte/mtime/tree read-only assertion",
        ),
        (
            "t2p.py",
            'result["authority"] = "run_status.py"',
            'result["authority"] = "run_status.py"\n    result["next_action"] = "mutant"',
            "test_status_readonly_and_owner_parity",
            "status must not route or suggest",
        ),
        (
            "start_migration.py",
            "observer(_path_gate_record(projection, allowed))",
            "pass",
            "test_start_reaches_existing_postharvest_gate",
            "authoritative owner path verdict must be exposed",
        ),
        (
            "tableau_env.py",
            "if default_site:",
            "if default_site and not present:",
            "test_doctor_valid_default_and_optional_absence",
            "valid Default site must not prescribe repair",
        ),
        (
            "tableau_env.py",
            "key in required and state in",
            "state in",
            "test_doctor_valid_default_and_optional_absence",
            "optional missing keys must not prescribe repair",
        ),
        (
            "t2p.py",
            '"ordinal": index,',
            '"ordinal": index, "unit": row["unit"],',
            "test_status_customer_labels_are_withheld",
            "status leaked customer label: PRIVATE_UNIT_CANARY_73",
        ),
    ],
)
def test_isolated_mutations_fail_intended_assertion(tmp_path, file, before, after, selector, assertion):
    """Approved mutation controls fail on the intended assertion, never an import/exit alone."""
    isolated = tmp_path / "copy"
    scripts = isolated / "scripts"
    scripts.mkdir(parents=True)
    for source in (REPO_ROOT / "scripts").glob("*.py"):
        shutil.copyfile(source, scripts / source.name)
    tests = isolated / "tests"
    tests.mkdir()
    for name in ("test_t2p.py", "test_start_migration.py"):
        shutil.copyfile(REPO_ROOT / "tests" / name, tests / name)
    target = scripts / file
    source = target.read_text(encoding="utf-8")
    assert before in source, "mutation anchor must exist"
    target.write_text(source.replace(before, after, 1), encoding="utf-8")
    outcome = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            str(tests / "test_t2p.py") + "::" + selector,
            "--basetemp",
            str(isolated / "_test"),
        ],
        cwd=isolated,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
    )
    assert outcome.returncode == 1, outcome.stdout + outcome.stderr
    assert assertion in outcome.stdout, outcome.stdout + outcome.stderr
    assert "ERROR collecting" not in outcome.stdout + outcome.stderr


def test_doctor_provenance_control(monkeypatch, tmp_path, capsys):
    _config(monkeypatch)
    path = tmp_path / "selected.env"
    path.write_text("TABLEAU_PAT_SECRET=losing-file-secret\n", encoding="utf-8")
    monkeypatch.setattr(t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    assert t2p.main(["doctor", "--env", str(path), "--json"]) == 0
    result, _ = _json(capsys)
    row = next(row for row in result["checks"] if row["key"] == "TABLEAU_PAT_SECRET")
    assert row["source"] == "environment", "winning source must be environment"


@pytest.mark.parametrize("json_mode", [False, True])
def test_doctor_interrupt_is_safe_in_both_modes(monkeypatch, capsys, json_mode):
    _config(monkeypatch)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt("PRIVATE_INTERRUPT_CANARY")

    monkeypatch.setattr(t2p.subprocess, "run", interrupt)
    assert t2p.main(["doctor", *(["--json"] if json_mode else [])]) == 130
    result, captured = _json(capsys)
    assert result["status"] == "interrupted"
    assert result["delegated_exit_code"] is None
    assert "PRIVATE_INTERRUPT_CANARY" not in captured.out + captured.err


def test_offline_help_output_journey(monkeypatch, tmp_path, capsys):
    """Help/output-only mechanics rehearsal, explicitly not live blind-run acceptance."""
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    monkeypatch.setattr(t2p, "ROOT", pipeline.root)
    _config(monkeypatch)
    with pytest.raises(SystemExit) as help_result:
        t2p.main(["--help"])
    assert help_result.value.code == 0
    help_text = capsys.readouterr().out
    assert "{doctor,start,status}" in help_text
    assert t2p.main(["doctor", "--json"]) == 0
    doctor_result, _ = _json(capsys)
    assert doctor_result["status"] == "READY_TO_ATTEMPT"
    assert t2p.main(["start", "--project", "Finance", "--json"]) == 0
    output, progress = _captured(capsys, pipeline)
    started = json.loads(output)
    selected = started["run_location"]["physical_run"]
    assert progress.splitlines()[-1] == "RUN " + selected
    assert t2p.main(["status", "--run", selected, "--json"]) in {0, 1}
    observed, _ = _json(capsys)
    assert observed["run_location"]["physical_run"] == selected
    for result in (doctor_result, started, observed):
        assert {"START_READY", "COMPLETE", "fidelity", "process_liveness"} <= set(
            result["proof_boundary"]["not_established"]
        )
    assert len(pipeline.commands) == 5, "offline harness must exercise the existing five-stage owner"
