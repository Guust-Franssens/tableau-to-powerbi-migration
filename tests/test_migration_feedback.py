"""Recorded, independently exercised controls for the Phase-1 offline feedback boundary."""

from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name

import ast
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "migration-feedback"
SCRIPT = ROOT / "scripts" / "build_migration_feedback.py"
SKILL = ROOT / ".github" / "skills" / "migration-feedback" / "SKILL.md"
sys.path.insert(0, str(ROOT / "scripts"))

import build_migration_feedback as feedback  # noqa: E402  # pylint: disable=wrong-import-position
import engine_source  # noqa: E402  # pylint: disable=wrong-import-position
from migration_bundle import write_engine_receipt  # noqa: E402  # pylint: disable=wrong-import-position
from object_identity import revision_key  # noqa: E402  # pylint: disable=wrong-import-position


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Case:
    """Fictitious producer-shaped inputs and their independently pinned observations."""

    root: Path
    run: Path
    kind: str
    request: dict
    paths: dict[str, Path] = field(default_factory=dict)

    @property
    def request_path(self) -> Path:
        return self.root / "request.json"

    def pin(self, role: str, path: Path) -> None:
        self.paths[role] = path
        self.request["evidence"][role] = {"path": str(path), "size_bytes": path.stat().st_size, "sha256": _hash(path)}

    def replace(self, role: str, value: Any) -> None:
        self.pin(role, _write(self.paths[role], value))

    def save(self) -> Path:
        return _write(self.request_path, self.request)


def _observe(case: Case, prefix: str, *, relative: bool = False, output_label: str | None = None) -> None:
    source = case.paths[f"{prefix}_input"]
    owner = case.paths["owner"]
    runtime = str(case.paths["runtime"])
    output_dir = case.run / "scratch" / (output_label or f"emitted-{prefix}")
    if case.kind == "engine":
        assert not output_dir.exists(), "the positive engine control really starts with no output"
        command = [runtime, "-B", str(owner), "--input", str(source.parent), "--output", str(output_dir)]
        artifact = output_dir / "reports" / "Fixture.Report" / "definition" / "report.json"
    else:
        command = [runtime, "-B", str(owner), str(source)]
        artifact = case.root / f"{prefix}-output.json"
    if relative:
        for index in (2, 4, 6) if case.kind == "engine" else (2, 3):
            command[index] = os.path.relpath(command[index], case.root)
    start = _now()
    launched = [runtime, "-B", str(case.paths["wrapper"]), *command[3:]] if "wrapper" in case.paths else command
    execution = subprocess.run(launched, capture_output=True, check=False, cwd=case.root, timeout=15)
    end = _now()
    assert execution.returncode == 0, execution.stderr
    # Independent code, not the classifier or the intentionally mutated producer, earns the expectation.
    oracle_command = [runtime, "-B", str(case.paths["oracle"]), str(source), str(case.paths["predicate"])]
    oracle_start = _now()
    oracle = subprocess.run(
        oracle_command,
        capture_output=True,
        check=False,
        cwd=case.root,
        timeout=15,
    )
    oracle_end = _now()
    assert oracle.returncode == 0, oracle.stderr
    expected = _json(case.paths["predicate"])["expected"]
    assert json.loads(oracle.stdout)["expected"] == expected
    if case.kind != "engine":
        artifact.write_bytes(execution.stdout)
    else:
        assert _json(artifact) == json.loads(execution.stdout)
    case.pin(f"{prefix}_output", artifact)
    witness = case.root / f"{prefix}-witness.json"
    witness.write_bytes(execution.stderr)
    case.pin(f"{prefix}_witness", witness)
    oracle_result = case.root / f"{prefix}-oracle-result.json"
    oracle_result.write_bytes(oracle.stdout)
    case.pin(f"{prefix}_oracle_result", oracle_result)
    binding = {
        "role": f"{prefix}_input",
        "kind": "directory" if case.kind == "engine" else "file",
        "argument_index": 4 if case.kind == "engine" else 3,
    }
    case.pin(
        f"{prefix}_oracle_record",
        _write(
            case.root / f"{prefix}-oracle-record.json",
            {
                "schema_version": 1,
                "input_sha256": _hash(source),
                "output_sha256": _hash(oracle_result),
                "oracle_sha256": _hash(case.paths["oracle"]),
                "predicate_sha256": _hash(case.paths["predicate"]),
                "runtime_sha256": _hash(case.paths["runtime"]),
                "command": oracle_command,
                "cwd": str(case.root),
                "started_at": oracle_start,
                "finished_at": oracle_end,
                "exit_code": oracle.returncode,
                "setup": "ready",
                "input_binding": {"role": f"{prefix}_input", "kind": "file", "argument_index": 3},
            },
        ),
    )
    record = {
        "schema_version": 2,
        "input_sha256": _hash(source),
        "output_sha256": _hash(artifact),
        "owner_sha256": _hash(owner),
        "oracle_sha256": _hash(case.paths["oracle"]),
        "predicate_sha256": _hash(case.paths["predicate"]),
        "command": command,
        "cwd": str(case.root),
        "started_at": start,
        "finished_at": end,
        "exit_code": execution.returncode,
        "setup": "ready",
        "runtime_sha256": _hash(case.paths["runtime"]),
        "input_binding": binding,
        "witness_sha256": _hash(witness),
        "oracle_record_sha256": _hash(case.paths[f"{prefix}_oracle_record"]),
    }
    case.pin(f"{prefix}_record", _write(case.root / f"{prefix}-record.json", record))
    if "wrapper" in case.paths:
        parent = {key: record[key] for key in feedback.PROCESS_FIELDS}
        parent.update(
            schema_version=1,
            command=launched,
            wrapper_sha256=_hash(case.paths["wrapper"]),
            child_record_sha256=_hash(case.paths[f"{prefix}_record"]),
        )
        case.pin(f"{prefix}_wrapper_record", _write(case.root / f"{prefix}-wrapper-record.json", parent))


def _engine_evidence(case: Case) -> None:
    output_dir = case.paths["positive_output"].parents[3]
    assert output_dir.name == "emitted-positive"
    source = case.paths["positive_input"]
    source_kind = case.request["flow"]
    manifest = {
        "source_kind": "LocalFilesSource",
        "verifier": "migrate_estate/input_identity/1",
        "assets": [
            {
                "kind": source_kind,
                "name": "Sample",
                "staged_input_path": str(source),
                "size_bytes": source.stat().st_size,
                "sha256": _hash(source),
            }
        ],
    }
    case.pin("input_manifest", _write(output_dir / "input_manifest.json", manifest))
    case.pin("engine_report", _write(output_dir / "report.json", {"workbooks": [{"name": "Sample"}]}))
    # Use the existing receipt producer, independent of the feedback code under test.
    case.pin("engine_receipt", write_engine_receipt(output_dir))
    record = _json(case.paths.get("positive_wrapper_record", case.paths["positive_record"]))
    case.pin(
        "fresh_output",
        _write(
            case.root / "fresh-output.json",
            {
                "output_dir": str(output_dir),
                "observed_absent_at": record["started_at"],
                "started_at": record["started_at"],
                "finished_at": _now(),
                "before_state": "absent",
                "scope": "full",
                "receipt_sha256": _hash(case.paths["engine_receipt"]),
                "input_sha256": _hash(source),
                "engine_root": str(engine_source.engine_root()),
                "engine_version": engine_source.engine_version(),
                "command": record["command"],
                "exit_code": record["exit_code"],
            },
        ),
    )


def _code(case: Case, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = FIXTURES / case.kind
    fake_repo = case.root / "toolkit"
    monkeypatch.setattr(feedback, "REPO_ROOT", fake_repo)
    if case.kind == "engine":
        plugin = case.root / "home" / ".copilot" / "installed-plugins" / "tableau-collection" / "tableau-fabric-skills"
        monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)
        engine_dir = plugin / "skills" / "tableau-migration"
        engine_dir.mkdir(parents=True)
        (engine_dir / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        owner = engine_dir / "scripts" / "migrate_estate.py"
        source_owner = fixture / "migrate_estate.py"
    else:
        owner = fake_repo / "scripts" / "fixture_cli.py"
        source_owner = fixture / ("probe.py" if case.kind == "external" else "cli.py")
    owner.parent.mkdir(parents=True)
    shutil.copyfile(source_owner, owner)
    case.pin("owner", owner)
    case.pin("runtime", Path(sys.executable).resolve())
    for role in ("oracle", "predicate"):
        path = case.root / (f"{role}.py" if role == "oracle" else f"{role}.json")
        shutil.copyfile(fixture / path.name, path)
        case.pin(role, path)


def _inputs(case: Case, *, privacy: bool, packed: bool, datasource: bool) -> None:
    for prefix, name in (
        ("positive", "positive"),
        ("negative", "negative"),
        ("candidate", "candidate"),
        ("candidate_negative", "candidate-negative"),
    ):
        if case.kind == "external" and prefix.startswith("candidate"):
            continue
        extension = ".twb" if case.kind == "engine" else ".json"
        source_fixture = FIXTURES / case.kind / f"{name}{extension}"
        if privacy and prefix == "positive":
            source_fixture = FIXTURES / "privacy" / "private-input.twb"
        if datasource:
            extension = ".tds"
        if packed and prefix == "positive":
            extension += "x"
        source = case.root / "inputs" / prefix / f"sample{extension}"
        source.parent.mkdir(parents=True)
        raw = source_fixture.read_bytes()
        if datasource:
            feature = "enabled" if prefix in {"positive", "candidate"} else "disabled"
            caption = "Fixture" if prefix.startswith("candidate") else "Sample"
            raw = (
                f'<datasource version="18.1" feature="{feature}">'
                f'<column name="[Value]" caption="{caption}" datatype="integer" role="measure" type="quantitative"/>'
                "</datasource>"
            ).encode("utf-8")
        if packed and prefix == "positive":
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("fixture.tds" if datasource else "fixture.twb", raw)
        else:
            source.write_bytes(raw)
        case.pin(f"{prefix}_input", source)
        _observe(case, prefix)


def _case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str = "engine",
    *,
    privacy: bool = False,
    shape: str = ".twb",
    wrapper: bool = False,
) -> Case:
    root = tmp_path / "case"
    run = root / "_runs" / "001-fixture"
    run.mkdir(parents=True)
    _write(
        run / "run.json",
        {
            "run": 1,
            "unit_key": "fixture",
            "allocated_dir_name": run.name,
            "allocated_abs_path": str(run),
        },
    )
    request = {
        "schema_version": 1,
        "flow": ("datasource" if shape.startswith(".tds") else "workbook") if kind == "engine" else "script",
        "source_mode": "local_download" if kind == "engine" else "not_applicable",
        "claim_scope": "local_artifact",
        "owner": "engine" if kind == "engine" else ("external" if kind == "external" else "repository"),
        "engine_involved": kind == "engine",
        "contrast": "configuration_changed" if kind == "external" else "feature_removed",
        "evidence": {},
    }
    case = Case(root, run, kind, request)
    _code(case, monkeypatch)
    if wrapper:
        path = feedback.REPO_ROOT / "scripts" / "run_estate.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import subprocess, sys\n"
            f"child = [sys.executable, '-B', {str(case.paths['owner'])!r}, *sys.argv[1:]]\n"
            "result = subprocess.run(child, capture_output=True, check=False)\n"
            "sys.stdout.buffer.write(result.stdout)\nsys.stderr.buffer.write(result.stderr)\n"
            "raise SystemExit(result.returncode)\n",
            encoding="utf-8",
        )
        case.pin("wrapper", path)
    _inputs(case, privacy=privacy, packed=shape.endswith("x"), datasource=shape.startswith(".tds"))
    if kind == "engine":
        case.pin(
            "migration_spec",
            _write(
                root / "migration-spec.json",
                {
                    "migration_spec_version": "1.0",
                    "source": {"file_name": case.paths["positive_input"].name},
                    "data_sources": [],
                    "worksheets": [],
                    "dashboards": [],
                },
            ),
        )
        _engine_evidence(case)
    if kind == "external":
        confirmation = _json(FIXTURES / "external" / "confirmation.json")
        confirmation["input_sha256"] = _hash(case.paths["positive_input"])
        case.pin("external_confirmation", _write(root / "confirmation.json", confirmation))
        case.pin(
            "external_evidence",
            _write(
                root / "external-evidence.json",
                {
                    "system": "credentials",
                    "condition": "credential_modal",
                    "record_sha256": _hash(case.paths["positive_record"]),
                    "confirmation_sha256": _hash(case.paths["external_confirmation"]),
                },
            ),
        )
    else:
        request["reproducer"] = {
            "authorship": "fictitious_from_scratch",
            "redistributable": True,
            "reviewed_sha256": {
                role: _hash(case.paths[role]) for role in ("candidate_input", "candidate_negative_input")
            },
        }
    if privacy:
        request["private_notes"] = _json(FIXTURES / "privacy" / "sentinels.json")["private_notes"]
    case.save()
    return case


def _build(case: Case, expected: int = 0) -> tuple[Path, dict, dict]:
    destination = case.root / "feedback-output"
    code = feedback.main(["--input", str(case.save()), "--run", str(case.run), "--out", str(destination)])
    assert code == expected
    return destination, _json(destination / "feedback.json"), _json(destination / "issue-payload.json")


def _repin_record(case: Case, prefix: str, **changes: Any) -> None:
    record = _json(case.paths[f"{prefix}_record"])
    record.update(changes)
    case.replace(f"{prefix}_record", record)


def test_engine_fixture_earns_canonical_fresh_route_with_independent_controls(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    out, private, public = _build(case)
    assert private["route"] == public["route"] == "ENGINE_UPSTREAM"
    assert public["repository"] == "Yarbrdab000/tableau-fabric-skills"
    assert public["engine_version"] == "1.2.3"
    assert public["public_filing_ready"] is True
    assert public["failure_class"] == "missing_output"
    assert private["engine"]["canonical"] is True
    assert _json(case.paths["positive_record"])["exit_code"] == 0, "a nonzero exit is not our failure predicate"
    assert _json(case.paths["positive_output"])["count"] == 0
    assert _json(case.paths["negative_output"])["count"] == 1
    assert sorted(path.name for path in out.iterdir()) == sorted([*feedback.OUTPUT_FILES, "repro"])
    assert (out / "repro" / "positive.twb").read_bytes() == case.paths["candidate_input"].read_bytes()
    assert not (out / "issue-draft.md").exists()


def test_local_script_default_routes_without_any_tableau_file(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    _, private, public = _build(case)
    assert private["route"] == public["route"] == "AGENTIC_REPOSITORY"
    assert private["engine"] == {}
    assert public["engine_version"] is None
    assert public["flow"] == "script"
    assert public["public_filing_ready"] is True
    assert not any(role in case.paths for role in ("migration_spec", "source_provenance", "engine_receipt"))


def test_external_requires_positive_confirmation_and_is_nonfileable(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "external")
    _, private, public = _build(case)
    assert private["route"] == "EXTERNAL_OR_CONFIGURATION"
    assert public["repository"] is None
    assert public["public_filing_ready"] is False
    assert public["reproducer_status"] == "not_applicable"


@pytest.mark.parametrize("role", ["external_evidence", "external_confirmation"])
def test_external_is_never_an_elimination_guess(tmp_path, monkeypatch, role) -> None:
    case = _case(tmp_path, monkeypatch, "external")
    del case.request["evidence"][role]
    _, private, public = _build(case, 3)
    assert private["route"] == public["route"] == "CANNOT_ESTABLISH"
    assert private["reasons"] == [f"missing_evidence:{role}"]


@pytest.mark.parametrize("role", _json(FIXTURES / "cannot" / "cases.json")["remove"])
def test_missing_identity_receipt_or_control_does_not_guess(tmp_path, monkeypatch, role) -> None:
    case = _case(tmp_path, monkeypatch)
    del case.request["evidence"][role]
    _, private, public = _build(case, 3)
    assert private["route"] == public["route"] == "CANNOT_ESTABLISH"
    assert private["reasons"] == [f"missing_evidence:{role}"]
    assert public["public_filing_ready"] is False


def test_local_download_has_raw_and_normalized_identity_without_remote_fields(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    _, private, public = _build(case)
    source = private["source"]
    assert source["mode"] == "local_download"
    assert source["engine_input"]["path"] == str(case.paths["positive_input"])
    assert source["engine_input"]["sha256"] == _hash(case.paths["positive_input"])
    assert source["engine_input"]["size_bytes"] == case.paths["positive_input"].stat().st_size
    assert source["revision_key"] == revision_key(case.paths["positive_input"].read_bytes()).as_json()
    assert source["origin"] == {"status": "not_provided"}
    assert not any(word in json.dumps(public) for word in ("luid", "updated_at", "server", "project"))


def _provenance(case: Case, origin: Any = None) -> None:
    source = case.paths["positive_input"]
    case.pin(
        "source_provenance",
        _write(
            case.root / "source-provenance.json",
            {
                "schema": "tableau-source-provenance/1",
                "inputs": [
                    {
                        "input": {
                            "file": source.name,
                            "sha256": _hash(source),
                            "size_bytes": source.stat().st_size,
                            "revision_key": revision_key(source.read_bytes()).as_json(),
                        },
                        "origin": origin,
                    }
                ],
                "input_count": 1,
                "phase": {"status": "success" if origin is not None else "local_only", "errors": []},
            },
        ),
    )


@pytest.mark.parametrize("remote_claim", [False, True])
def test_attempted_unavailable_origin_only_blocks_remote_state_claims(tmp_path, monkeypatch, remote_claim) -> None:
    case = _case(tmp_path, monkeypatch)
    _provenance(case)
    if remote_claim:
        case.request["claim_scope"] = "remote_state"
    _, private, public = _build(case, 3 if remote_claim else 0)
    if remote_claim:
        assert private["reasons"] == ["source:remote_revision_unconfirmed"]
        assert public["route"] == "CANNOT_ESTABLISH"
        assert private["source"]["engine_input"]["sha256"] == _hash(case.paths["positive_input"])
        assert private["source"]["origin"] == {"status": "origin_unavailable"}
    else:
        assert private["source"]["origin"] == {"status": "origin_unavailable"}
        assert public["route"] == "ENGINE_UPSTREAM"


def test_remote_revision_uses_normalized_key_not_raw_archive_hash_or_timestamp(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    key = revision_key(case.paths["positive_input"].read_bytes()).as_json()
    _provenance(
        case,
        {
            "server": "https://tableau.example.invalid",
            "site": "fixture",
            "workbook_luid": "00000000-0000-0000-0000-000000000001",
            "updated_at": "2026-01-01T00:00:00Z",
            "tableau_product_version": "2026.1",
            "rest_api_version": "3.28",
            "remote_revision_key": key,
            "revision_match": "same",
            "remote_sha256": "f" * 64,
        },
    )
    case.request["claim_scope"] = "remote_state"
    _, private, public = _build(case)
    assert private["source"]["origin"]["status"] == "confirmed"
    assert public["claim_scope"] == "remote_state"
    assert "00000000-0000" not in json.dumps(public)
    assert "2026-01-01" not in json.dumps(public)


def test_repacked_downloads_keep_existing_revision_key(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, shape=".twbx")
    source = case.paths["positive_input"]
    archives = []
    for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=compression) as archive:
            archive.writestr("fixture.twb", (FIXTURES / "engine" / "positive.twb").read_bytes())
        archives.append(stream.getvalue())
    assert hashlib.sha256(archives[0]).digest() != hashlib.sha256(archives[1]).digest()
    assert revision_key(archives[0]) == revision_key(archives[1])
    _, private, _ = _build(case)
    assert private["source"]["revision_key"]["algo"] == "twbx-content-v3"
    assert private["source"]["revision_key"] == revision_key(source.read_bytes()).as_json()


@pytest.mark.parametrize("packed", [False, True])
def test_datasource_download_is_a_first_class_offline_source(tmp_path, monkeypatch, packed) -> None:
    case = _case(tmp_path, monkeypatch, shape=".tdsx" if packed else ".tds")
    _, private, public = _build(case)
    assert public["flow"] == "datasource"
    assert private["source"]["origin"] == {"status": "not_provided"}
    assert private["route"] == "ENGINE_UPSTREAM"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("before_state", "populated", "engine:not_a_fresh_full_output"),
        ("scope", "partial", "engine:not_a_fresh_full_output"),
        ("input_sha256", "0" * 64, "engine:fresh_output_identity_mismatch"),
        ("engine_version", "9.9.9", "engine:fresh_engine_mismatch"),
    ],
)
def test_fresh_output_proof_is_required_and_consistent(tmp_path, monkeypatch, field, value, reason) -> None:
    case = _case(tmp_path, monkeypatch)
    proof = _json(case.paths["fresh_output"])
    proof[field] = value
    case.replace("fresh_output", proof)
    _, private, public = _build(case, 3)
    assert private["reasons"] == [reason]
    assert public["route"] == "CANNOT_ESTABLISH"


@pytest.mark.parametrize("field,value", [("canonical", False), ("source", "override"), ("version", "9.9.9")])
def test_canonical_claim_alone_does_not_cover_another_engine(tmp_path, monkeypatch, field, value) -> None:
    case = _case(tmp_path, monkeypatch)
    receipt = _json(case.paths["engine_receipt"])
    receipt["engine"][field] = value
    case.replace("engine_receipt", receipt)
    _, private, _ = _build(case, 3)
    assert private["reasons"] == ["engine:canonical_receipt_mismatch"]


def test_wrong_consumed_source_cannot_borrow_receipt(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    manifest = _json(case.paths["input_manifest"])
    manifest["assets"][0]["sha256"] = "0" * 64
    case.replace("input_manifest", manifest)
    receipt = _json(case.paths["engine_receipt"])
    receipt["input_manifest_sha256"] = _hash(case.paths["input_manifest"])
    case.replace("engine_receipt", receipt)
    _, private, _ = _build(case, 3)
    assert private["reasons"] == ["engine:consumed_bytes_mismatch"]


@pytest.mark.parametrize("prefix,route", [("positive", "CANNOT_ESTABLISH"), ("candidate", "ENGINE_UPSTREAM")])
def test_setup_error_nonzero_is_not_reproduction(tmp_path, monkeypatch, prefix, route) -> None:
    case = _case(tmp_path, monkeypatch)
    _repin_record(case, prefix, setup="error", exit_code=1)
    _, private, public = _build(case, 3)
    assert private["route"] == route
    assert private["reasons"] == [f"{prefix}:setup_not_ready"]
    assert public["public_filing_ready"] is False


def test_candidate_with_a_different_predicate_is_not_credited(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    _repin_record(case, "candidate", predicate_sha256=hashlib.sha256(b"different predicate").hexdigest())
    out, private, public = _build(case, 3)
    assert private["route"] == "ENGINE_UPSTREAM", "private attribution remains, not public reproducer readiness"
    assert private["reproducer_status"] == "reproducer_not_established"
    assert private["reasons"] == ["candidate:predicate_sha256_mismatch"]
    assert public["public_filing_ready"] is False
    assert not list((out / "repro").iterdir())


def test_minimization_that_removes_failure_is_rejected(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    case.replace("candidate_output", {"count": 1})
    _repin_record(case, "candidate", output_sha256=_hash(case.paths["candidate_output"]))
    _repin_witness(case, "candidate", output_sha256=_hash(case.paths["candidate_output"]))
    _, private, _ = _build(case, 3)
    assert private["reasons"] == ["candidate:predicate_not_reproduced"]


def test_meaningful_negative_must_pass_the_identical_predicate(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    case.replace("negative_output", {"count": 0})
    _repin_record(case, "negative", output_sha256=_hash(case.paths["negative_output"]))
    _repin_witness(case, "negative", output_sha256=_hash(case.paths["negative_output"]))
    _, private, _ = _build(case, 3)
    assert private["route"] == "CANNOT_ESTABLISH"
    assert private["reasons"] == ["negative:negative_control_failed"]


def test_predicate_is_defined_before_original_controls_and_candidate(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    _repin_record(case, "positive", started_at="2025-01-01T00:00:00Z")
    _, private, _ = _build(case, 3)
    assert private["reasons"] == ["positive:predicate_not_defined_first"]


def test_private_paths_sizes_hashes_and_excerpts_never_leak_into_payload(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, privacy=True)
    source_before = case.paths["positive_input"].read_bytes()
    # A source-shaped command string must remain inert even when recorded as an argument.
    marker = case.root / "MUST_NOT_EXIST"
    _repin_record(
        case,
        "positive",
        command=[
            *_json(case.paths["positive_record"])["command"],
            f"; write PRIVATE_SENTINEL_684 to {marker}",
        ],
    )
    out, private, public = _build(case, 3)
    assert private["route"] == "CANNOT_ESTABLISH"
    assert private["reasons"] == ["positive:directory_invocation_unsupported"]
    raw = (out / "issue-payload.json").read_text(encoding="utf-8")
    for forbidden in _json(FIXTURES / "privacy" / "sentinels.json")["forbidden"]:
        assert forbidden not in raw
    assert str(case.root) not in raw
    assert _hash(case.paths["positive_input"]) not in raw
    assert not marker.exists()
    assert case.paths["positive_input"].read_bytes() == source_before
    index = _json(out / "evidence-index.json")
    assert index["originals_copied"] is False
    for item in index["files"]:
        path = Path(item["path"])
        assert item["size_bytes"] == path.stat().st_size
        assert item["sha256"] == _hash(path)
    assert "PRIVATE_SENTINEL_684" in (out / "reproduction.md").read_text(encoding="utf-8")
    assert all(source_before != path.read_bytes() for path in out.rglob("*") if path.is_file())
    assert set(public) == {
        "schema",
        "route",
        "repository",
        "flow",
        "source_mode",
        "claim_scope",
        "failure_class",
        "engine_version",
        "private_controls_established",
        "reproducer_status",
        "public_filing_ready",
        "publication",
        "repro_files",
    }


@pytest.mark.parametrize("where", ["request", "evidence", "predicate", "record", "reproducer"])
def test_unknown_structured_fields_refuse_instead_of_spilling_public_text(tmp_path, monkeypatch, where) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    if where == "request":
        case.request["issue_title"] = "PRIVATE_SENTINEL_684"
    elif where == "evidence":
        case.request["evidence"]["customer_dump"] = case.request["evidence"]["positive_input"]
    elif where == "reproducer":
        case.request["reproducer"]["public_description"] = "PRIVATE_SENTINEL_684"
    else:
        role = "predicate" if where == "predicate" else "positive_record"
        value = _json(case.paths[role])
        value["private_extension"] = "PRIVATE_SENTINEL_684"
        case.replace(role, value)
        if where == "predicate":
            _repin_record(case, "positive", predicate_sha256=_hash(case.paths["predicate"]))
    out = case.root / "refused-output"
    assert feedback.main(["--input", str(case.save()), "--out", str(out)]) == 1
    assert not out.exists()


def test_changed_bytes_refuse_before_any_output(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    case.paths["positive_input"].write_text("PRIVATE_SENTINEL_684", encoding="utf-8")
    out = case.root / "refused-output"
    assert feedback.main(["--input", str(case.save()), "--out", str(out)]) == 1
    assert not out.exists()


def test_customer_original_cannot_be_relabelled_as_a_fictitious_candidate(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, privacy=True)
    candidate = case.paths["candidate_input"]
    shutil.copyfile(case.paths["positive_input"], candidate)
    case.pin("candidate_input", candidate)
    _repin_record(case, "candidate", input_sha256=_hash(candidate))
    case.request["reproducer"]["reviewed_sha256"]["candidate_input"] = _hash(candidate)
    out = case.root / "refused-output"
    assert feedback.main(["--input", str(case.save()), "--out", str(out)]) == 1
    assert not out.exists()


@pytest.mark.parametrize("field,value", [("authorship", "downloaded"), ("redistributable", False)])
def test_unreviewed_or_nonredistributable_candidate_is_privacy_refusal(tmp_path, monkeypatch, field, value) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    case.request["reproducer"][field] = value
    out = case.root / "refused-output"
    assert feedback.main(["--input", str(case.save()), "--out", str(out)]) == 1
    assert not out.exists()


def test_default_output_is_under_selected_run_deliverables(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    out, result = feedback.build(case.save(), run=case.run)
    assert result.exit_code == 0
    assert out.parent == case.run / "deliverables" / "migration-feedback"
    assert out.name.startswith("feedback-") and out.name.endswith("Z")


def test_non_run_explicit_output_is_mandatory_and_run_identity_is_not_inferred(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    assert feedback.main(["--input", str(case.save())]) == 2
    _write(case.run / "run.json", {"run": 1})
    assert feedback.main(["--input", str(case.save()), "--run", str(case.run)]) == 3
    assert not (case.run / "deliverables").exists()


def test_unignored_destination_is_refused_and_existing_output_is_never_overwritten(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    refused = ROOT / "feedback-684-MUST-NOT-BE-CREATED"
    assert not refused.exists()
    assert feedback.main(["--input", str(case.save()), "--out", str(refused)]) == 1
    assert not refused.exists()
    existing = case.root / "exists"
    existing.mkdir()
    (existing / "sentinel").write_text("keep", encoding="utf-8")
    assert feedback.main(["--input", str(case.save()), "--out", str(existing)]) == 1
    assert (existing / "sentinel").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("path", [r"\\server\share\evidence.json", r"\\?\C:\file.json"])
def test_network_and_device_paths_refuse_before_access(tmp_path, monkeypatch, path) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    case.request["evidence"]["positive_input"]["path"] = path
    assert feedback.main(["--input", str(case.save()), "--out", str(case.root / "out")]) == 1


def test_no_network_or_source_execution_during_build(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, privacy=True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the offline builder attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    real_run = subprocess.run

    def local_git_only(command, *args, **kwargs):
        assert command[0] == "git", command
        assert not kwargs.get("shell")
        assert not any(word in command for word in ("push", "fetch", "clone", "pull"))
        assert kwargs.get("check") is False
        return real_run(command, *args, check=kwargs.pop("check"), **kwargs)

    monkeypatch.setattr(subprocess, "run", local_git_only)
    _build(case)


def test_real_cli_entrypoint_engine_route_and_usage_exits(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    home = engine_source.PLUGIN_ENGINE_ROOT.parents[3]
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS": ""}
    out = case.root / "cli-output"
    command = [sys.executable, "-B", str(SCRIPT), "--input", str(case.save()), "--out", str(out)]
    execution = subprocess.run(command, capture_output=True, text=True, check=False, env=env, timeout=30)
    assert execution.returncode == 0, execution.stderr
    assert _json(out / "issue-payload.json")["route"] == "ENGINE_UPSTREAM"
    usage = subprocess.run([sys.executable, "-B", str(SCRIPT)], capture_output=True, text=True, check=False, timeout=15)
    assert usage.returncode == 2


def test_script_and_skill_have_no_publication_or_network_action_surface() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    forbidden_imports = {
        "subprocess",
        "requests",
        "urllib",
        "http",
        "httpx",
        "socket",
        "aiohttp",
        "webbrowser",
        "importlib",
        "runpy",
        "multiprocessing",
    }
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add((node.module or "").split(".")[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "compile", "__import__"}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            if node.func.value.id == "os":
                assert node.func.attr in {
                    "fstat",
                    "open",
                    "close",
                    "mkdir",
                    "fdopen",
                    "dup",
                    "fsync",
                    "lseek",
                    "fsencode",
                    "unlink",
                    "rmdir",
                    "fchmod",
                }
    assert not imports & forbidden_imports
    text = SCRIPT.read_text(encoding="utf-8") + SKILL.read_text(encoding="utf-8")
    for action in ("gh issue create", "gh issue comment", "gh pr create", "gh api", "curl -X", "Invoke-RestMethod"):
        assert action not in text
    assert "explicit user approval" in SKILL.read_text(encoding="utf-8")


def test_skill_contract_and_size() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 350
    front = text.split("---", 2)[1]
    description = next(line.split(": ", 1)[1] for line in front.splitlines() if line.startswith("description:"))
    assert len(description) <= 1024
    assert "Use this skill whenever" in description
    for trigger in ("broken", "send feedback", "script", "workbook", "datasource"):
        assert trigger in description
    for section in (
        "## Prerequisites",
        "## Workflow",
        "## Error handling",
        "## Output",
        "## Post-Run Reflection",
        "## References",
    ):
        assert section in text
    assert not (SKILL.parent / "references").exists()
    assert "ai-sales-kit" not in text and "../core/SKILL.md" not in text


@pytest.mark.parametrize(
    "kind,pointer,expected,actual,failed",
    [
        ("text_contains", None, "precise failure 684", b"prefix precise failure 684 suffix", True),
        ("text_contains", None, "precise failure 684", b"an unrelated setup error", False),
        ("json_missing", "/missing", True, b'{"present":1}', True),
        ("json_missing", "/present", True, b'{"present":1}', False),
        ("json_equals", "/value", False, b'{"value":0}', True),
        ("json_equals", "/value", {"a": 1, "b": 2}, b'{"value":{"b":2,"a":1}}', False),
        ("json_equals", "/a~1b/0", 2, b'{"a/b":[2]}', False),
    ],
)
def test_supported_predicates_are_data_not_code(kind, pointer, expected, actual, failed) -> None:
    predicate = {"kind": kind, "pointer": pointer, "expected": expected}
    output = feedback.Evidence("positive_output", ROOT / "not-read", actual)
    assert feedback._failed(predicate, output) is failed


def test_missing_assertion_is_not_a_json_value_failure() -> None:
    output = feedback.Evidence("positive_output", ROOT / "not-read", b'{"setup":"failed"}')
    with pytest.raises(feedback.FeedbackError, match="predicate:assertion_not_reached") as refusal:
        feedback._failed({"kind": "json_equals", "pointer": "/value", "expected": 1}, output)
    assert refusal.value.exit_code == 3


def test_duplicate_keys_and_nonfinite_json_are_integrity_refusals() -> None:
    for raw in (b'{"owner":"engine","owner":"repository"}', b'{"value":NaN}'):
        with pytest.raises(feedback.FeedbackError) as refusal:
            feedback._json(raw)
        assert refusal.value.exit_code == 1


def test_recorded_relative_cli_paths_resolve_only_against_recorded_cwd(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    relative_owner = os.path.relpath(case.paths["owner"], case.root)
    for prefix in ("positive", "negative", "candidate", "candidate_negative"):
        record = _json(case.paths[f"{prefix}_record"])
        record["command"][2] = relative_owner
        replay = subprocess.run(
            record["command"],
            cwd=case.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert replay.returncode == 0
        assert json.loads(replay.stdout) == _json(case.paths[f"{prefix}_output"])
        case.replace(f"{prefix}_witness", json.loads(replay.stderr))
        record["witness_sha256"] = _hash(case.paths[f"{prefix}_witness"])
        case.replace(f"{prefix}_record", record)
    _, _, public = _build(case)
    assert public["route"] == "AGENTIC_REPOSITORY"


def test_fresh_proof_can_record_the_actual_relative_invocation(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch)
    for prefix in feedback.PREFIXES:
        shutil.rmtree(case.run / "scratch" / f"emitted-{prefix}")
        _observe(case, prefix, relative=True)
    _engine_evidence(case)
    _, _, public = _build(case)
    assert public["route"] == "ENGINE_UPSTREAM"


def _repin_witness(case: Case, prefix: str, **changes: Any) -> None:
    witness = _json(case.paths[f"{prefix}_witness"])
    witness.update(changes)
    case.replace(f"{prefix}_witness", witness)
    _repin_record(case, prefix, witness_sha256=_hash(case.paths[f"{prefix}_witness"]))


def test_positive_command_consumes_negative_not_declared_positive(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    record = _json(case.paths["positive_record"])
    record["command"][-1] = str(case.paths["negative_input"])
    actual = subprocess.run(record["command"], cwd=case.root, capture_output=True, check=False, timeout=15)
    assert actual.returncode == 0
    assert json.loads(actual.stdout) == _json(case.paths["negative_output"])
    case.replace("positive_record", record)
    _, private, public = _build(case, 3)
    assert private["reasons"] == ["positive:input_not_in_invocation"]
    assert public["route"] == "CANNOT_ESTABLISH" and not public["public_filing_ready"]


def test_all_commands_execute_oracle_with_owner_inert(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    for prefix in feedback.PREFIXES:
        command = [
            str(case.paths["runtime"]),
            "-B",
            str(case.paths["oracle"]),
            str(case.paths[f"{prefix}_input"]),
            str(case.paths["predicate"]),
            str(case.paths["owner"]),
        ]
        actual = subprocess.run(command, cwd=case.root, capture_output=True, check=False, timeout=15)
        assert actual.returncode == 0
        assert json.loads(actual.stdout)["oracle_sha256"] == _hash(case.paths["oracle"])
        _repin_record(case, prefix, command=command)
    _, private, public = _build(case, 3)
    assert private["reasons"] == ["positive:entrypoint_not_invoked"]
    assert not public["public_filing_ready"]


@pytest.mark.parametrize(
    "tail",
    [
        ["--input", "other"],
        ["--input=other"],
        ["--output", "other"],
        ["--output=other"],
        ["--force"],
        ["--", "other"],
    ],
)
def test_duplicate_or_ambiguous_option_semantics_are_not_guessed(tmp_path, monkeypatch, tail) -> None:
    case = _case(tmp_path, monkeypatch)
    record = _json(case.paths["positive_record"])
    _repin_record(case, "positive", command=[*record["command"], *tail])
    _, private, public = _build(case, 3)
    assert private["reasons"] == ["positive:directory_invocation_unsupported"]
    assert not public["public_filing_ready"]


def test_unrelated_oracle_prose_repinned_cannot_borrow_an_observation(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    # Even valid Python prose is not the code whose invocation produced the separately pinned result.
    case.paths["oracle"].write_text('"Unrelated oracle prose, not an executed expectation."\n', encoding="utf-8")
    case.pin("oracle", case.paths["oracle"])
    for prefix in feedback.PREFIXES:
        oracle_record = _json(case.paths[f"{prefix}_oracle_record"])
        oracle_record["oracle_sha256"] = _hash(case.paths["oracle"])
        case.replace(f"{prefix}_oracle_record", oracle_record)
        _repin_record(
            case,
            prefix,
            oracle_sha256=_hash(case.paths["oracle"]),
            oracle_record_sha256=_hash(case.paths[f"{prefix}_oracle_record"]),
        )
    _, private, public = _build(case, 3)
    assert private["reasons"] == ["positive:oracle_oracle_sha256_mismatch"]
    assert not public["public_filing_ready"]


def test_oracle_result_must_independently_earn_predicate_expectation(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    result = _json(case.paths["positive_oracle_result"])
    result["expected"] = True
    case.replace("positive_oracle_result", result)
    record = _json(case.paths["positive_oracle_record"])
    record["output_sha256"] = _hash(case.paths["positive_oracle_result"])
    case.replace("positive_oracle_record", record)
    _repin_record(case, "positive", oracle_record_sha256=_hash(case.paths["positive_oracle_record"]))
    _, private, public = _build(case, 3)
    assert private["reasons"] == ["positive:oracle_expectation_mismatch"]
    assert not public["public_filing_ready"]


def test_consumed_bytes_witness_is_not_an_invocation_claim(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    _repin_witness(case, "positive", input_sha256=_hash(case.paths["negative_input"]))
    _, private, public = _build(case, 3)
    assert private["reasons"] == ["positive:witness_input_sha256_mismatch"]
    assert not public["public_filing_ready"]


@pytest.mark.parametrize("role", ["positive_witness", "positive_oracle_record", "positive_oracle_result", "runtime"])
def test_missing_participation_evidence_cannot_route(tmp_path, monkeypatch, role) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    del case.request["evidence"][role]
    _, private, public = _build(case, 3)
    assert private["reasons"] == [f"missing_evidence:{role}"]
    assert public["route"] == "CANNOT_ESTABLISH"


@pytest.mark.parametrize("declaration", [None, [], {}, {"authorship": "fictitious_from_scratch"}])
def test_present_malformed_reproducer_refuses(tmp_path, monkeypatch, declaration) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    case.request["reproducer"] = declaration
    destination = case.root / "out"
    assert feedback.main(["--input", str(case.save()), "--out", str(destination)]) == 1
    assert not destination.exists()


def test_omitted_reproducer_retains_private_attribution(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    del case.request["reproducer"]
    out, private, public = _build(case, 3)
    assert private["route"] == "AGENTIC_REPOSITORY"
    assert private["reasons"] == ["reproducer:not_provided"]
    assert public["reproducer_status"] == "reproducer_not_established"
    assert not public["public_filing_ready"]
    assert not list((out / "repro").iterdir())


@pytest.mark.parametrize("remote", [False, True])
def test_failed_empty_provenance_does_not_erase_local_identity(tmp_path, monkeypatch, remote) -> None:
    case = _case(tmp_path, monkeypatch)
    case.pin(
        "source_provenance",
        _write(
            case.root / "provenance.json",
            {
                "schema": "tableau-source-provenance/1",
                "inputs": [],
                "input_count": 0,
                "phase": {"status": "failed", "errors": ["origin unavailable"]},
            },
        ),
    )
    case.request["claim_scope"] = "remote_state" if remote else "local_artifact"
    _, private, public = _build(case, 3 if remote else 0)
    assert private["source"]["engine_input"]["sha256"] == _hash(case.paths["positive_input"])
    assert private["source"]["origin"] == {"status": "origin_unavailable"}
    assert public["route"] == ("CANNOT_ESTABLISH" if remote else "ENGINE_UPSTREAM")
    assert public["public_filing_ready"] is not remote


@pytest.mark.parametrize("missing", ["tableau_product_version", "rest_api_version"])
def test_remote_versions_are_required_never_inferred(tmp_path, monkeypatch, missing) -> None:
    case = _case(tmp_path, monkeypatch)
    origin = {
        "server": "https://tableau.example.invalid",
        "site": "fixture",
        "workbook_luid": "00000000-0000-0000-0000-000000000001",
        "tableau_product_version": "2026.1",
        "rest_api_version": "3.28",
        "remote_revision_key": revision_key(case.paths["positive_input"].read_bytes()).as_json(),
        "revision_match": "same",
    }
    del origin[missing]
    _provenance(case, origin)
    case.request["claim_scope"] = "remote_state"
    _, private, public = _build(case, 3)
    assert private["reasons"] == ["source:remote_revision_unconfirmed"]
    assert not public["public_filing_ready"]


@pytest.mark.parametrize(
    "suffix,raw",
    [
        (".twb", b"MZ\x00opaque"),
        (".tds", b"MZ\x00opaque"),
        (".twb", b"opaque"),
        (".twb", b"<datasource/>"),
        (".tds", b"<workbook/>"),
        (".twb", b'<!DOCTYPE workbook [<!ENTITY x "opaque">]><workbook>&x;</workbook>'),
        (".json", b"{broken"),
        (".json", b'{"a":1,"a":2}'),
        (".json", b'{"a":1e999}'),
        (".txt", b"\x00binary"),
        (".txt", b"\xff\xfeopaque"),
        (".txt", b"bad\x1bcontrol"),
        (".csv", b"a,b\nx,\x00"),
        (".csv", b"a,b\nx"),
        (".csv", b'a,b\n"broken,x'),
    ],
)
def test_reproducer_content_not_suffix_is_allowlisted(tmp_path, monkeypatch, suffix, raw) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    candidate = case.paths["candidate_input"].with_suffix(suffix)
    candidate.write_bytes(raw)
    case.pin("candidate_input", candidate)
    case.request["reproducer"]["reviewed_sha256"]["candidate_input"] = _hash(candidate)
    out, private, public = _build(case, 3)
    assert private["route"] == "AGENTIC_REPOSITORY"
    assert private["reasons"] == ["reproducer:format_not_established"]
    assert not public["public_filing_ready"] and not list((out / "repro").iterdir())


@pytest.mark.parametrize(
    "suffix,raw",
    [
        (".twb", b"<workbook/>"),
        (".tds", b"<datasource/>"),
        (".json", b'{"a":1}'),
        (".txt", "fictitious caf\u00e9\n".encode("utf-8")),
        (".txt", b"\xef\xbb\xbffictitious\n"),
        (".csv", b'a,b\n"fictitious, cell",1\n'),
    ],
)
def test_supported_content_formats_have_positive_controls(suffix, raw) -> None:
    feedback._reproducer_format(feedback.Evidence("candidate_input", ROOT / f"not-read{suffix}", raw))


@pytest.mark.parametrize(
    "boundary",
    [
        "request",
        "predicate",
        "record",
        "oracle_record",
        "witness",
        "oracle_result",
        "receipt",
        "evidence_size",
        "manifest_size",
        "artifact_size",
        "provenance_size",
        "provenance_count",
        "input_index",
        "run_number",
    ],
)
def test_boolean_integer_boundaries_refuse(tmp_path, monkeypatch, boundary) -> None:
    case = _case(tmp_path, monkeypatch)
    if boundary == "request":
        case.request["schema_version"] = True
    elif boundary == "evidence_size":
        case.request["evidence"]["positive_input"]["size_bytes"] = True
    elif boundary == "run_number":
        run = _json(case.run / "run.json")
        run["run"] = True
        _write(case.run / "run.json", run)
    elif boundary.startswith("provenance"):
        _provenance(case)
        provenance = _json(case.paths["source_provenance"])
        if boundary == "provenance_count":
            provenance["input_count"] = True
        else:
            provenance["inputs"][0]["input"]["size_bytes"] = True
        case.replace("source_provenance", provenance)
    elif boundary in {"receipt", "manifest_size", "artifact_size"}:
        receipt = _json(case.paths["engine_receipt"])
        if boundary == "receipt":
            receipt["version"] = True
        elif boundary == "artifact_size":
            receipt["artifacts"][0]["size"] = True
        else:
            manifest = _json(case.paths["input_manifest"])
            manifest["assets"][0]["size_bytes"] = True
            case.replace("input_manifest", manifest)
            receipt["input_manifest_sha256"] = _hash(case.paths["input_manifest"])
        case.replace("engine_receipt", receipt)
        proof = _json(case.paths["fresh_output"])
        proof["receipt_sha256"] = _hash(case.paths["engine_receipt"])
        case.replace("fresh_output", proof)
    else:
        role = {
            "predicate": "predicate",
            "record": "positive_record",
            "oracle_record": "positive_oracle_record",
            "witness": "positive_witness",
            "oracle_result": "positive_oracle_result",
            "input_index": "positive_record",
        }[boundary]
        document = _json(case.paths[role])
        if boundary == "input_index":
            document["input_binding"]["argument_index"] = True
        else:
            document["schema_version"] = True
        case.replace(role, document)
        if boundary in {"witness", "oracle_record"}:
            _repin_record(case, "positive", **{f"{boundary}_sha256": _hash(case.paths[role])})
        elif boundary == "oracle_result":
            oracle_record = _json(case.paths["positive_oracle_record"])
            oracle_record["output_sha256"] = _hash(case.paths[role])
            case.replace("positive_oracle_record", oracle_record)
            _repin_record(case, "positive", oracle_record_sha256=_hash(case.paths["positive_oracle_record"]))
    out = case.root / "refused"
    assert feedback.main(["--input", str(case.save()), "--run", str(case.run), "--out", str(out)]) == 1
    assert not out.exists(), "a Boolean must not satisfy any integer boundary"


def _junction(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null",
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_junction(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def test_destination_junction_before_first_write_never_receives_bytes(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    destination, escaped = case.root / "out", case.root / "escaped"
    escaped.mkdir()
    original = feedback._Filesystem.read
    attempts = []

    def swapped(store, path, *, create=None):
        if create is not None and not attempts:
            assert not destination.exists(), "no success-shaped final directory may precede staging"
            _junction(destination, escaped)
            attempts.append(True)
        return original(store, path, create=create)

    monkeypatch.setattr(feedback._Filesystem, "read", swapped)
    try:
        assert feedback.main(["--input", str(case.save()), "--out", str(destination)]) == 1
        assert attempts == [True]
        assert not list(escaped.iterdir()), "a destination swap must not redirect even the first private write"
        assert not list(case.root.glob(".migration-feedback-*.staging"))
    finally:
        _remove_junction(destination)
    assert not destination.exists()


@pytest.mark.parametrize("target_kind", ["parent", "stage", "repro"])
def test_parent_and_staging_reparse_swaps_cannot_redirect_writes(tmp_path, monkeypatch, target_kind) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    destination = case.root / "out"
    escaped = case.root.parent / "escaped"
    escaped.mkdir()
    original = feedback._Filesystem.read
    attempts = []
    renamed = []

    def swapped(store, path, *, create=None):
        if create is not None and not attempts:
            stage = path.parent
            target = case.root if target_kind == "parent" else stage / "repro" if target_kind == "repro" else stage
            moved = target.with_name(target.name + "-moved")
            try:
                target.rename(moved)
            except PermissionError:
                attempts.append("denied")
            else:
                _junction(target, escaped)
                renamed.append((target, moved))
                attempts.append("swapped")
        return original(store, path, create=create)

    monkeypatch.setattr(feedback._Filesystem, "read", swapped)
    code = feedback.main(["--input", str(case.save()), "--out", str(destination)])
    assert attempts in (["denied"], ["swapped"])
    assert code == (0 if attempts == ["denied"] else 1)
    assert not list(escaped.iterdir()), "held ancestors, not lexical names, must own every staged write"
    for target, moved in reversed(renamed):
        _remove_junction(target)
        moved.rename(target)
    if code == 1:
        assert not destination.exists()


def test_final_payload_failure_leaves_no_final_or_staging_tree(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    destination = case.root / "out"
    original = feedback._Filesystem.read
    written = []

    def failed(store, path, *, create=None):
        if create is not None:
            assert not destination.exists(), "final directory became visible before the complete transaction"
            written.append(path.name)
            if path.name == "issue-payload.json":
                raise OSError("controlled final payload write failure")
        return original(store, path, create=create)

    monkeypatch.setattr(feedback._Filesystem, "read", failed)
    assert feedback.main(["--input", str(case.save()), "--out", str(destination)]) == 1
    assert written[-1] == "issue-payload.json" and "positive.json" in written and "feedback.json" in written
    assert not destination.exists(), "failed transactions must not expose established feedback or candidate bytes"
    assert not list(case.root.glob(".migration-feedback-*.staging")), "failed stage must be cleaned"


def test_hardlink_alias_and_changed_inode_cannot_copy_private_bytes(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    source, candidate = case.paths["positive_input"], case.paths["candidate_input"]
    candidate.unlink()
    os.link(source, candidate)
    sentinel = b'{"value":null,"private":"PRIVATE_SENTINEL_684"}'
    digest = hashlib.sha256(sentinel).hexdigest()
    case.request["evidence"]["candidate_input"].update(size_bytes=len(sentinel), sha256=digest)
    case.request["reproducer"]["reviewed_sha256"]["candidate_input"] = digest
    original = feedback._Filesystem.read
    attempted = []

    def swapped(store, path, *, create=None):
        if path == candidate:
            attempted.append(True)
            source.unlink()
            source.write_bytes(b'{"replacement":true}')
            candidate.write_bytes(sentinel)
        return original(store, path, create=create)

    monkeypatch.setattr(feedback._Filesystem, "read", swapped)
    out = case.root / "refused"
    assert feedback.main(["--input", str(case.save()), "--out", str(out)]) == 1
    assert not attempted, "aliased roles must be refused before either read can borrow an old snapshot"
    assert not out.exists()


def test_undeclared_hardlink_is_also_an_unsafe_source_alias(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    os.link(case.paths["candidate_input"], case.root / "private-alias.json")
    out = case.root / "refused"
    assert feedback.main(["--input", str(case.save()), "--out", str(out)]) == 1
    assert not out.exists(), "a candidate alias outside the declared role set is still unsafe"


def test_same_path_private_candidate_roles_are_refused_before_reads(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    case.pin("candidate_input", case.paths["positive_input"])
    out = case.root / "refused"
    assert feedback.main(["--input", str(case.save()), "--out", str(out)]) == 1
    assert not out.exists()


def test_read_binds_preopen_identity_not_only_equal_bytes(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"fictitious identity control")
    before = source.lstat()
    if os.name == "nt":
        real_open = feedback._windows_open

        def changed(path, **kwargs):
            if path == source:
                replacement = tmp_path / "replacement.txt"
                replacement.write_bytes(source.read_bytes())
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                os.replace(replacement, source)
            return real_open(path, **kwargs)

        monkeypatch.setattr(feedback, "_windows_open", changed)
    else:
        real_open = os.open

        def changed(path, flags, *args, **kwargs):
            if path == source.name:
                replacement = tmp_path / "replacement.txt"
                replacement.write_bytes(source.read_bytes())
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                os.replace(replacement, source)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", changed)
    with feedback._Filesystem() as store:
        with pytest.raises(feedback.FeedbackError, match="filesystem:file_changed"):
            store.read(source)
    assert source.lstat().st_ino != before.st_ino


def test_retained_snapshot_bytes_are_rechecked_through_held_handle(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"fictitious")
    with feedback._Filesystem() as store:
        store.read(source)
        store.files[source].raw = b"PRIVATE_SENTINEL_684"
        with pytest.raises(feedback.FeedbackError, match="filesystem:bytes_changed"):
            store.verify()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing modes are a native Windows boundary")
def test_windows_held_input_denies_write_and_inode_replacement(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"fictitious")
    with feedback._Filesystem() as store:
        store.read(source)
        with pytest.raises(PermissionError):
            source.write_bytes(b"PRIVATE_SENTINEL_684")
        with pytest.raises(PermissionError):
            source.rename(tmp_path / "renamed.txt")
        store.verify()


@pytest.mark.parametrize(
    "suffix,raw",
    [(".twb", b"MZ\x00"), (".tds", b"opaque"), (".json", b"{bad"), (".txt", b"text\x00"), (".csv", b"a,b\nc")],
)
def test_format_guard_has_a_direct_negative_control(suffix, raw) -> None:
    with pytest.raises(feedback.FeedbackError, match="reproducer:format_not_established"):
        feedback._reproducer_format(feedback.Evidence("candidate_input", ROOT / f"not-read{suffix}", raw))


@pytest.mark.skipif(os.name != "nt", reason="Windows close/rename sharing window requires its native ACL control")
def test_sealed_stage_blocks_repro_swap_and_byte_change_after_handles_close(tmp_path, monkeypatch) -> None:
    case = _case(tmp_path, monkeypatch, "local")
    real_move = feedback._windows_move
    observed = []

    def attempt(descriptor, destination):
        if destination is not None:
            stage = next(destination.parent.glob(".migration-feedback-*.staging"))
            try:
                (stage / "repro").rename(stage / "changed-repro")
            except PermissionError:
                blocked = True
            else:
                (stage / "changed-repro").rename(stage / "repro")
                blocked = False
            assert blocked, "stage seal must deny a child-directory replacement at commit"
            before = (stage / "feedback.json").read_bytes()
            try:
                (stage / "feedback.json").write_bytes(b"PRIVATE_SENTINEL_684")
            except PermissionError:
                blocked = True
            else:
                (stage / "feedback.json").write_bytes(before)
                blocked = False
            assert blocked, "stage seal must deny changing verified bytes at commit"
            observed.append(True)
        real_move(descriptor, destination)

    monkeypatch.setattr(feedback, "_windows_move", attempt)
    _, _, public = _build(case)
    assert observed == [True] and public["public_filing_ready"]


@pytest.fixture(autouse=True)
def restore_private_bundle_access(tmp_path):
    """Only dispose of this test's sealed outputs; the production artifact stays read-only."""
    yield
    for marker in tmp_path.rglob("feedback.json"):
        root = marker.parent
        if (root / "repro").is_dir():
            files = [path for path in root.rglob("*") if path.is_file()]
            with feedback._Filesystem() as filesystem:
                filesystem.directory(root)
                feedback._stage_permissions(filesystem, root, files, writable=True)


@pytest.mark.parametrize("missing", [False, True])
def test_real_wrapper_records_the_engine_child_not_just_its_own_success(tmp_path, monkeypatch, missing) -> None:
    case = _case(tmp_path, monkeypatch, wrapper=True)
    child = _json(case.paths["positive_record"])
    wrapper = _json(case.paths["positive_wrapper_record"])
    assert wrapper["command"][2] != child["command"][2]
    assert _json(case.paths["positive_witness"])["command"] == child["command"]
    if missing:
        del case.request["evidence"]["positive_wrapper_record"]
    _, private, public = _build(case, 3 if missing else 0)
    assert public["route"] == ("CANNOT_ESTABLISH" if missing else "ENGINE_UPSTREAM")
    if missing:
        assert private["reasons"] == ["engine:fresh_invocation_mismatch"]


@pytest.mark.parametrize("correct_baseline", [False, True])
def test_repository_regression_needs_its_separate_consumed_engine_baseline(
    tmp_path, monkeypatch, correct_baseline
) -> None:
    case = _case(tmp_path, monkeypatch)
    engine_owner = case.paths["owner"]
    shutil.rmtree(case.run / "scratch")
    if correct_baseline:
        for prefix in feedback.PREFIXES:
            source = case.paths[f"{prefix}_input"]
            raw = source.read_bytes()
            raw = raw.replace(b"enabled", b"SWAP").replace(b"disabled", b"enabled").replace(b"SWAP", b"disabled")
            source.write_bytes(raw)
            case.pin(f"{prefix}_input", source)
    _observe(case, "positive")
    _engine_evidence(case)
    case.pin("baseline_owner", engine_owner)
    case.pin("baseline_output", case.paths["positive_output"])
    baseline = _json(case.paths["positive_record"])
    baseline = {key: baseline[key] for key in feedback.PROCESS_FIELDS | {"owner_sha256", "witness_sha256"}}
    baseline["schema_version"] = 1
    witness = case.root / "baseline-witness.json"
    witness.write_bytes(case.paths["positive_witness"].read_bytes())
    case.pin("baseline_witness", witness)
    case.pin("baseline_record", _write(case.root / "baseline-record.json", baseline))
    owner = feedback.REPO_ROOT / "scripts" / "local_step.py"
    owner.parent.mkdir(parents=True, exist_ok=True)
    code = engine_owner.read_text(encoding="utf-8")
    if correct_baseline:
        code = code.replace('== "enabled"', '== "disabled"')
    owner.write_text(code, encoding="utf-8")
    case.pin("owner", owner)
    case.request["owner"] = "repository"
    for prefix in feedback.PREFIXES:
        _observe(case, prefix, output_label=f"local-{prefix}")
    case.request["reproducer"]["reviewed_sha256"] = {
        role: _hash(case.paths[role]) for role in ("candidate_input", "candidate_negative_input")
    }
    _, private, public = _build(case, 0 if correct_baseline else 3)
    assert public["route"] == ("AGENTIC_REPOSITORY" if correct_baseline else "CANNOT_ESTABLISH")
    if not correct_baseline:
        assert private["reasons"] == ["owner:baseline_also_fails"]


@pytest.mark.parametrize("value", [True, False])
def test_integer_schema_does_not_borrow_boolean_numeric_equality(value) -> None:
    assert not feedback._integer(value)
    assert feedback._integer(int(value))


def test_boolean_size_refuses_even_when_bytes_really_have_length_one(tmp_path) -> None:
    source = tmp_path / "one-byte.json"
    source.write_bytes(b"0")
    request = {"evidence": {"positive_input": {"path": str(source), "size_bytes": True, "sha256": _hash(source)}}}
    with feedback._Filesystem() as filesystem:
        with pytest.raises(feedback.FeedbackError, match="positive_input:size_invalid"):
            feedback._read_evidence(request, tmp_path, filesystem)
    request["evidence"]["positive_input"]["size_bytes"] = 1
    with feedback._Filesystem() as filesystem:
        assert feedback._read_evidence(request, tmp_path, filesystem)["positive_input"].raw == b"0"


def test_role_identity_guard_precedes_any_evidence_read(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(b"0")
    declaration = {"path": str(source), "size_bytes": 1, "sha256": _hash(source)}
    request = {"evidence": {"positive_input": declaration, "candidate_input": dict(declaration)}}
    with feedback._Filesystem() as filesystem:
        with pytest.raises(feedback.FeedbackError, match="evidence:aliased_roles"):
            feedback._read_evidence(request, tmp_path, filesystem)
        assert not filesystem.files, "ambiguous roles must not acquire a borrowed byte snapshot"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows OPEN_REPARSE_POINT control")
def test_native_directory_handle_does_not_follow_a_junction(tmp_path) -> None:
    target, link = tmp_path / "target", tmp_path / "link"
    target.mkdir()
    _junction(link, target)
    try:
        descriptor = feedback._windows_open(link, directory=True, create=False, movable=False)
        try:
            assert os.fstat(descriptor).st_file_attributes & 0x400, "native open followed the reparse target"
            assert os.fstat(descriptor).st_ino != target.lstat().st_ino
        finally:
            os.close(descriptor)
    finally:
        _remove_junction(link)
