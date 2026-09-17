"""
purpose: Validate recorded migration feedback and build a private, offline evidence bundle.
usage:   python -B scripts/build_migration_feedback.py --input <request.json>
         [--run <absolute-run>] [--out <absolute-dir>]
internal: true
internal-reason: implementation helper for the migration-feedback skill, not a diagnostics or readiness exporter.

The request is authored by the session, not extracted from source text. Evidence is pinned by
size and digest, read once, and never executed. Recorded observations are not signed attestations:
this validates their consistency, not the honesty of their producer or historical filesystem state.
Only issue-payload.json is a public-safe projection. All other outputs remain private.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import engine_source
from harvest_estate_assets import OutputPathNotIgnoredError, unignored_output_paths
from object_identity import REVISION_ALGO_ARCHIVE, REVISION_ALGO_XML, RevisionKey, revision_key
from work_dirs import check_run_location

LOG = logging.getLogger("migration-feedback")
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "migration-feedback/1"
OUTPUT_FILES = ("feedback.json", "evidence-index.json", "reproduction.md", "issue-payload.json")
REQUEST_FIELDS = {
    "schema_version",
    "flow",
    "source_mode",
    "claim_scope",
    "owner",
    "engine_involved",
    "contrast",
    "reproducer",
    "evidence",
    "private_notes",
}
EVIDENCE_ROLES = {
    "predicate",
    "owner",
    "oracle",
    "positive_input",
    "positive_output",
    "positive_record",
    "negative_input",
    "negative_output",
    "negative_record",
    "candidate_input",
    "candidate_output",
    "candidate_record",
    "candidate_negative_input",
    "candidate_negative_output",
    "candidate_negative_record",
    "engine_receipt",
    "input_manifest",
    "engine_report",
    "fresh_output",
    "baseline_output",
    "source_provenance",
    "migration_spec",
    "run_status",
    "package_manifest",
    "gate_results",
    "parse_sweep",
    "engine_gap_report",
    "external_evidence",
    "external_confirmation",
}
CONTEXT_ROLES = {"migration_spec", "run_status", "package_manifest", "gate_results", "parse_sweep", "engine_gap_report"}
RECORD_FIELDS = {
    "schema_version",
    "input_sha256",
    "output_sha256",
    "owner_sha256",
    "oracle_sha256",
    "predicate_sha256",
    "command",
    "cwd",
    "started_at",
    "finished_at",
    "exit_code",
    "setup",
}
FAILURE_CLASSES = {"incorrect_output", "missing_output", "unexpected_refusal", "runtime_failure", "external_block"}
ROUTES = {
    "engine": "ENGINE_UPSTREAM",
    "repository": "AGENTIC_REPOSITORY",
    "external": "EXTERNAL_OR_CONFIGURATION",
}
REPOSITORIES = {
    "ENGINE_UPSTREAM": "Yarbrdab000/tableau-fabric-skills",
    "AGENTIC_REPOSITORY": "Guust-Franssens/tableau-to-powerbi-migration",
}
REPRO_SUFFIXES = {".twb", ".tds", ".json", ".csv", ".txt"}
REPRODUCER_FIELDS = {"authorship", "redistributable", "reviewed_sha256"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}\Z")
LUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


class FeedbackError(Exception):
    """An explicit refusal (1), usage error (2), or missing proof (3)."""

    def __init__(self, reason: str, exit_code: int = 3) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


def _require(condition: bool, reason: str, exit_code: int = 3) -> None:
    if not condition:
        raise FeedbackError(reason, exit_code)


def _choice(value: Any, choices: set[str], reason: str) -> None:
    _require(isinstance(value, str) and value in choices, reason, 1)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _object(value: Any, allowed: set[str], required: set[str], label: str) -> dict:
    _require(isinstance(value, dict), f"{label}:object_required", 1)
    _require(not set(value) - allowed, f"{label}:unknown_field", 1)
    _require(required <= set(value), f"{label}:missing_field")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, "json:duplicate_key", 1)
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise FeedbackError("json:nonfinite_number", 1)


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FeedbackError("json:invalid_document", 1) from exc


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _timestamp(value: Any, label: str) -> datetime:
    _require(isinstance(value, str), f"{label}:timestamp_missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackError(f"{label}:timestamp_invalid") from exc
    _require(result.tzinfo is not None, f"{label}:timezone_missing")
    return result


def _path(value: Any, base: Path | None = None) -> Path:
    _require(isinstance(value, str) and bool(value), "path:missing", 1)
    _require(not value.startswith(("\\\\", "//")), "path:network_or_device", 1)
    path = Path(value)
    _require(".." not in path.parts and "~" not in path.parts, "path:unsafe_component", 1)
    _require(all(ord(char) >= 32 for char in value), "path:control_character", 1)
    _require(not any(":" in part for part in path.parts[1:]), "path:alternate_stream", 1)
    if not path.is_absolute() and base is not None:
        path = base / path
    _require(path.is_absolute(), "path:absolute_required", 2)
    for entry in [*reversed(path.parents), path]:
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        _require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "path:link_or_reparse",
            1,
        )
        _require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "path:not_regular", 1)
    return path


@dataclass(frozen=True)
class Evidence:
    """One immutable byte snapshot; paths and original digests stay private."""

    role: str
    path: Path
    raw: bytes

    @property
    def sha256(self) -> str:
        """Digest of the bytes actually inspected, never a caller-supplied claim."""
        return _sha(self.raw)

    def document(self) -> Any:
        """Decode structured evidence without treating any value as instructions."""
        return _json(self.raw)

    def index_entry(self) -> dict:
        """Private source locator, without copying the original."""
        return {"role": self.role, "path": str(self.path), "size_bytes": len(self.raw), "sha256": self.sha256}


@dataclass
class Assessment:
    """Private result and the independently projected public facts."""

    route: str = "CANNOT_ESTABLISH"
    reasons: list[str] = field(default_factory=list)
    source: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)
    controls: dict = field(default_factory=dict)
    repro_files: dict[str, bytes] = field(default_factory=dict)
    exit_code: int = 3

    @property
    def reproducer_status(self) -> str:
        """A consistent status derived from the result, not a second writable verdict."""
        if self.exit_code == 0 and self.route == "EXTERNAL_OR_CONFIGURATION":
            return "not_applicable"
        return "established" if self.exit_code == 0 and self.repro_files else "reproducer_not_established"


def _read_evidence(request: dict, base: Path) -> dict[str, Evidence]:
    declarations = _object(request.get("evidence"), EVIDENCE_ROLES, set(), "evidence")
    evidence = {}
    snapshots: dict[Path, bytes] = {}
    for role, declaration in declarations.items():
        _object(declaration, {"path", "size_bytes", "sha256"}, {"path", "size_bytes", "sha256"}, role)
        _require(_integer(declaration["size_bytes"]) and declaration["size_bytes"] >= 0, f"{role}:size_invalid", 1)
        _require(
            isinstance(declaration["sha256"], str) and HEX64.fullmatch(declaration["sha256"]) is not None,
            f"{role}:digest_invalid",
            1,
        )
        path = _path(declaration["path"], base)
        try:
            if path not in snapshots:
                snapshots[path] = path.read_bytes()
        except OSError as exc:
            raise FeedbackError(f"{role}:unreadable") from exc
        raw = snapshots[path]
        _require(
            len(raw) == declaration["size_bytes"] and _sha(raw) == declaration["sha256"], f"{role}:changed_bytes", 1
        )
        evidence[role] = Evidence(role, path, raw)
    return evidence


def _get(evidence: dict[str, Evidence], role: str) -> Evidence:
    _require(role in evidence, f"missing_evidence:{role}")
    return evidence[role]


def _predicate(evidence: dict[str, Evidence]) -> dict:
    predicate = _get(evidence, "predicate").document()
    _object(
        predicate,
        {"schema_version", "defined_at", "kind", "pointer", "expected", "failure_class"},
        {"schema_version", "defined_at", "kind", "expected", "failure_class"},
        "predicate",
    )
    _require(
        _integer(predicate["schema_version"]) and predicate["schema_version"] == 1,
        "predicate:unsupported_schema",
        1,
    )
    _choice(predicate["kind"], {"json_equals", "json_missing", "text_contains"}, "predicate:unsupported_kind")
    _choice(predicate["failure_class"], FAILURE_CLASSES, "predicate:unknown_failure_class")
    _timestamp(predicate["defined_at"], "predicate")
    if predicate["kind"] == "text_contains":
        _require(
            isinstance(predicate["expected"], str) and bool(predicate["expected"].strip()), "predicate:empty_signature"
        )
    else:
        _require(
            isinstance(predicate.get("pointer"), str) and predicate["pointer"].startswith("/"),
            "predicate:json_pointer_required",
        )
        if predicate["kind"] == "json_missing":
            _require(predicate["expected"] is True, "predicate:presence_expected")
    return predicate


def _failed(predicate: dict, output: Evidence) -> bool:
    if predicate["kind"] == "text_contains":
        try:
            return predicate["expected"] in output.raw.decode("utf-8")
        except UnicodeError as exc:
            raise FeedbackError("predicate:output_not_text") from exc
    value = output.document()
    found = True
    for token in predicate["pointer"].split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdecimal() and int(token) < len(value):
            value = value[int(token)]
        else:
            found = False
            break
    if predicate["kind"] == "json_missing":
        return not found
    _require(found, "predicate:assertion_not_reached")
    # JSON booleans must not compare equal to numeric 0/1.
    return json.dumps(value, sort_keys=True) != json.dumps(predicate["expected"], sort_keys=True)


def _mentions_path(command: list[str], path: Path, cwd: Path | None) -> bool:
    for argument in command:
        candidate = Path(argument)
        if not candidate.is_absolute() and cwd is not None:
            candidate = cwd / candidate
        if candidate == path:
            return True
    return False


def _record(evidence: dict[str, Evidence], prefix: str, predicate: dict) -> dict:
    record = _get(evidence, f"{prefix}_record").document()
    _object(record, RECORD_FIELDS, RECORD_FIELDS, f"{prefix}_record")
    _require(_integer(record["schema_version"]) and record["schema_version"] == 1, f"{prefix}:unsupported_schema", 1)
    _require(record["setup"] == "ready", f"{prefix}:setup_not_ready")
    _require(_integer(record["exit_code"]), f"{prefix}:exit_not_recorded")
    command = record["command"]
    _require(
        isinstance(command, list) and len(command) >= 2 and all(isinstance(arg, str) and arg for arg in command),
        f"{prefix}:command_not_recorded",
    )
    cwd = _path(record["cwd"])
    _require(_mentions_path(command, _get(evidence, "owner").path, cwd), f"{prefix}:owner_not_in_invocation")
    start = _timestamp(record["started_at"], prefix)
    end = _timestamp(record["finished_at"], prefix)
    _require(_timestamp(predicate["defined_at"], "predicate") <= start <= end, f"{prefix}:predicate_not_defined_first")
    for field_name, role in (
        ("input_sha256", f"{prefix}_input"),
        ("output_sha256", f"{prefix}_output"),
        ("owner_sha256", "owner"),
        ("oracle_sha256", "oracle"),
        ("predicate_sha256", "predicate"),
    ):
        _require(record[field_name] == _get(evidence, role).sha256, f"{prefix}:{field_name}_mismatch")
    return record


def _controls(evidence: dict[str, Evidence], predicate: dict, positive: str, negative: str) -> dict:
    records = [_record(evidence, prefix, predicate) for prefix in (positive, negative)]
    _require(records[0]["command"][:2] == records[1]["command"][:2], "controls:different_invocation")
    _require(
        _get(evidence, f"{positive}_input").sha256 != _get(evidence, f"{negative}_input").sha256,
        "controls:no_input_contrast",
    )
    _require(_failed(predicate, _get(evidence, f"{positive}_output")), f"{positive}:predicate_not_reproduced")
    _require(not _failed(predicate, _get(evidence, f"{negative}_output")), f"{negative}:negative_control_failed")
    owner, oracle = _get(evidence, "owner"), _get(evidence, "oracle")
    _require(owner.raw != oracle.raw and bool(oracle.raw.strip()), "controls:oracle_not_independent")
    _require(
        oracle.path
        not in {
            _get(evidence, f"{prefix}_{part}").path for prefix in (positive, negative) for part in ("input", "output")
        },
        "controls:oracle_is_test_data",
    )
    return {"positive": "same_predicate_failed", "negative": "same_predicate_passed", "oracle": "independent_recorded"}


def _origin(source: Evidence, key: RevisionKey, provenance: Evidence | None) -> dict:
    if provenance is None:
        return {"status": "not_provided"}
    document = provenance.document()
    _require(
        isinstance(document, dict) and document.get("schema") == "tableau-source-provenance/1",
        "provenance:unsupported_schema",
    )
    rows = document.get("inputs")
    _require(isinstance(rows, list), "provenance:inputs_missing")
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("input"), dict) and row["input"].get("sha256") == source.sha256
    ]
    _require(len(matches) == 1, "provenance:input_identity_ambiguous_or_missing")
    local = matches[0]["input"]
    _require(local.get("size_bytes") == len(source.raw), "provenance:input_size_mismatch")
    recorded_key = RevisionKey.from_json(local.get("revision_key"))
    _require(recorded_key is not None and key.agrees_with(recorded_key) is True, "provenance:revision_key_mismatch")
    origin = matches[0].get("origin")
    if not isinstance(origin, dict):
        return {"status": "origin_unavailable"}
    fields = {
        "server",
        "site",
        "workbook_luid",
        "datasource_luid",
        "project",
        "updated_at",
        "tableau_product_version",
        "rest_api_version",
        "remote_revision_key",
        "revision_match",
    }
    result = {name: origin[name] for name in sorted(fields) if name in origin}
    agreement = key.agrees_with(RevisionKey.from_json(origin.get("remote_revision_key")))
    result["status"] = (
        "confirmed" if agreement is True and origin.get("revision_match") == "same" else "origin_unavailable"
    )
    return result


def _source(request: dict, evidence: dict[str, Evidence]) -> dict:
    source = _get(evidence, "positive_input")
    result = {"mode": request["source_mode"], "engine_input": source.index_entry()}
    if request["flow"] == "script":
        _require(request["source_mode"] == "not_applicable", "source:script_mode_conflict")
        _require(request["claim_scope"] == "local_artifact", "source:script_remote_claim")
        result["origin"] = {"status": "not_provided"}
        return result
    _require(request["source_mode"] in {"local_download", "remote_capture"}, "source:mode_missing")
    suffixes = {".twb", ".twbx"} if request["flow"] == "workbook" else {".tds", ".tdsx"}
    _require(source.path.suffix.lower() in suffixes, "source:kind_mismatch")
    key = revision_key(source.raw)
    _require(
        key is not None and key.algo in {REVISION_ALGO_ARCHIVE, REVISION_ALGO_XML}, "source:revision_key_unavailable"
    )
    result["revision_key"] = key.as_json()
    result["origin"] = _origin(source, key, evidence.get("source_provenance"))
    spec = _get(evidence, "migration_spec").document()
    _require(
        isinstance(spec, dict)
        and isinstance(spec.get("source"), dict)
        and spec["source"].get("file_name") == source.path.name,
        "migration_spec:source_mismatch",
    )
    return result


def _remote_claim(request: dict, source: dict, evidence: dict[str, Evidence]) -> None:
    if request["claim_scope"] == "remote_state":
        origin = source["origin"]
        luid = origin.get(f"{request['flow']}_luid")
        provenance = evidence.get("source_provenance")
        phase = provenance.document().get("phase") if provenance is not None else None
        _require(
            origin["status"] == "confirmed"
            and isinstance(luid, str)
            and LUID.fullmatch(luid) is not None
            and all(isinstance(origin.get(key), str) and bool(origin[key]) for key in ("site", "server"))
            and isinstance(phase, dict)
            and phase.get("status") == "success",
            "source:remote_revision_unconfirmed",
        )


def _manifest_input(evidence: dict[str, Evidence], flow: str) -> None:
    source = _get(evidence, "positive_input")
    manifest = _get(evidence, "input_manifest").document()
    _require(isinstance(manifest, dict) and isinstance(manifest.get("assets"), list), "engine:manifest_inputs_missing")
    matches = [
        row
        for row in manifest["assets"]
        if isinstance(row, dict) and row.get("staged_input_path") == str(source.path) and row.get("kind") == flow
    ]
    _require(len(matches) == 1, "engine:consumed_identity_ambiguous_or_missing")
    _require(
        matches[0].get("sha256") == source.sha256 and matches[0].get("size_bytes") == len(source.raw),
        "engine:consumed_bytes_mismatch",
    )


def _fresh_output(evidence: dict[str, Evidence], root: Path, version: str) -> None:
    receipt = _get(evidence, "engine_receipt")
    proof = _get(evidence, "fresh_output").document()
    fields = {
        "output_dir",
        "observed_absent_at",
        "started_at",
        "finished_at",
        "before_state",
        "scope",
        "receipt_sha256",
        "input_sha256",
        "engine_root",
        "engine_version",
        "command",
        "exit_code",
    }
    _object(proof, fields | {"cwd"}, fields, "fresh_output")
    _require(proof["before_state"] == "absent" and proof["scope"] == "full", "engine:not_a_fresh_full_output")
    _require(_path(proof["output_dir"]) == receipt.path.parent, "engine:fresh_output_path_mismatch")
    _require(
        proof["receipt_sha256"] == receipt.sha256 and proof["input_sha256"] == _get(evidence, "positive_input").sha256,
        "engine:fresh_output_identity_mismatch",
    )
    _require(_path(proof["engine_root"]) == root and proof["engine_version"] == version, "engine:fresh_engine_mismatch")
    _require(
        _timestamp(proof["observed_absent_at"], "fresh_output")
        <= _timestamp(proof["started_at"], "fresh_output")
        <= _timestamp(proof["finished_at"], "fresh_output"),
        "engine:fresh_output_time_conflict",
    )
    command = proof["command"]
    _require(isinstance(command, list) and all(isinstance(arg, str) for arg in command), "engine:command_missing")
    _require(_integer(proof["exit_code"]), "engine:exit_missing")
    _require("--output" in command and "--input" in command, "engine:command_scope_missing")
    cwd = _path(proof["cwd"]) if "cwd" in proof else None
    try:
        destination = _path(command[command.index("--output") + 1], cwd)
        input_dir = _path(command[command.index("--input") + 1], cwd)
    except IndexError as exc:
        raise FeedbackError("engine:command_scope_missing") from exc
    _require(destination == receipt.path.parent, "engine:command_output_mismatch")
    _require(_get(evidence, "positive_input").path.is_relative_to(input_dir), "engine:command_input_mismatch")
    _require(
        "--allow-noncanonical-engine" not in command and "--force" not in command,
        "engine:unsafe_rerun_flags",
    )
    entrypoints = {
        REPO_ROOT / "scripts" / "run_estate.py",
        engine_source.engine_scripts_dir(root) / "migrate_estate.py",
    }
    _require(any(_mentions_path(command, path, cwd) for path in entrypoints), "engine:canonical_entrypoint_missing")
    if "--engine" in command:
        index = command.index("--engine") + 1
        _require(index < len(command) and _path(command[index], cwd) == root, "engine:command_engine_mismatch")


def _engine(request: dict, evidence: dict[str, Evidence]) -> dict:
    receipt_file = _get(evidence, "engine_receipt")
    receipt = receipt_file.document()
    _require(isinstance(receipt, dict) and receipt.get("version") == 1, "engine:receipt_invalid")
    recorded = receipt.get("engine")
    _require(isinstance(recorded, dict), "engine:receipt_identity_missing")
    try:
        root = engine_source.engine_root()
    except engine_source.EngineNotFoundError as exc:
        raise FeedbackError("engine:canonical_plugin_unavailable") from exc
    version = engine_source.engine_version(root)
    _require(isinstance(version, str) and VERSION.fullmatch(version) is not None, "engine:version_unavailable")
    _require(
        recorded.get("canonical") is True
        and recorded.get("source") == "plugin"
        and _path(recorded.get("root")) == root
        and _path(recorded.get("plugin_root")) == root
        and recorded.get("version") == version,
        "engine:canonical_receipt_mismatch",
    )
    for name, role in (("report_sha256", "engine_report"), ("input_manifest_sha256", "input_manifest")):
        _require(receipt.get(name) == _get(evidence, role).sha256, f"engine:{role}_mismatch")
        _require(_get(evidence, role).path.parent == receipt_file.path.parent, f"engine:{role}_location_mismatch")
    _manifest_input(evidence, request["flow"])
    _fresh_output(evidence, root, version)
    output = _get(evidence, "positive_output" if request["owner"] == "engine" else "baseline_output")
    _require(output.path.is_relative_to(receipt_file.path.parent), "engine:baseline_outside_fresh_output")
    relative = output.path.relative_to(receipt_file.path.parent).as_posix()
    artifacts = receipt.get("artifacts")
    _require(isinstance(artifacts, list), "engine:artifact_receipt_missing")
    matches = [row for row in artifacts if isinstance(row, dict) and row.get("path") == relative]
    _require(
        len(matches) == 1 and matches[0].get("sha256") == output.sha256 and matches[0].get("size") == len(output.raw),
        "engine:baseline_not_receipt_backed",
    )
    return {"root": str(root), "version": version, "canonical": True, "fresh_output": str(receipt_file.path.parent)}


def _external(evidence: dict[str, Evidence]) -> None:
    evidence_file = _get(evidence, "external_evidence")
    finding = evidence_file.document()
    fields = {"system", "condition", "record_sha256", "confirmation_sha256"}
    _object(finding, fields, fields, "external")
    _choice(
        finding["system"], {"tableau", "powerbi", "credentials", "network", "environment"}, "external:system_unknown"
    )
    _choice(
        finding["condition"],
        {"credential_modal", "permission_denied", "service_unavailable", "configuration_mismatch"},
        "external:condition_unknown",
    )
    confirmation = _get(evidence, "external_confirmation")
    _require(finding["record_sha256"] == _get(evidence, "positive_record").sha256, "external:observation_mismatch")
    _require(finding["confirmation_sha256"] == confirmation.sha256, "external:confirmation_mismatch")
    observed = confirmation.document()
    fields = {"system", "condition", "input_sha256", "observed", "observed_at"}
    _object(observed, fields, fields, "external_confirmation")
    _require(
        observed["system"] == finding["system"]
        and observed["condition"] == finding["condition"]
        and observed["observed"] is True
        and observed["input_sha256"] == _get(evidence, "positive_input").sha256,
        "external:positive_confirmation_missing",
    )
    _timestamp(observed["observed_at"], "external_confirmation")
    _require(
        confirmation.sha256 != _get(evidence, "positive_output").sha256,
        "external:confirmation_not_independent",
    )


def _route(request: dict, evidence: dict[str, Evidence], predicate: dict, result: Assessment) -> None:
    owner = _get(evidence, "owner")
    _require(bool(owner.raw.strip()), "owner:empty_code_evidence")
    layer = request["owner"]
    if layer == "external":
        _external(evidence)
    elif layer == "engine":
        _require(request["engine_involved"] is True, "owner:engine_involvement_missing")
        _require(owner.path.is_relative_to(Path(result.engine["root"])), "owner:outside_canonical_plugin")
    elif layer == "repository":
        _require(
            any(owner.path.is_relative_to(REPO_ROOT / part) for part in ("scripts", ".github", "docs")),
            "owner:outside_agentic_repository",
        )
        if request["engine_involved"]:
            _require(not _failed(predicate, _get(evidence, "baseline_output")), "owner:baseline_also_fails")
    else:
        raise FeedbackError("owner:cannot_establish_code_owner")
    result.route = ROUTES[layer]


def _reproducer(request: dict, evidence: dict[str, Evidence], predicate: dict) -> dict[str, bytes]:
    declaration = request.get("reproducer")
    _object(declaration, REPRODUCER_FIELDS, REPRODUCER_FIELDS, "reproducer")
    _require(
        declaration["authorship"] == "fictitious_from_scratch" and declaration["redistributable"] is True,
        "reproducer:privacy_not_established",
        1,
    )
    reviewed = _object(
        declaration["reviewed_sha256"],
        {"candidate_input", "candidate_negative_input"},
        {"candidate_input", "candidate_negative_input"},
        "reproducer_review",
    )
    original_end = max(
        _timestamp(_get(evidence, f"{prefix}_record").document()["finished_at"], prefix)
        for prefix in ("positive", "negative")
    )
    for prefix in ("candidate", "candidate_negative"):
        record = _get(evidence, f"{prefix}_record").document()
        _require(isinstance(record, dict), f"{prefix}:record_missing")
        _require(_timestamp(record.get("started_at"), prefix) >= original_end, "reproducer:original_controls_not_first")
    _controls(evidence, predicate, "candidate", "candidate_negative")
    _require(
        _get(evidence, "candidate_record").document()["command"][:2]
        == _get(evidence, "positive_record").document()["command"][:2],
        "reproducer:different_invocation",
    )
    original_hashes = {
        item.sha256 for role, item in evidence.items() if role not in {"candidate_input", "candidate_negative_input"}
    }
    files = {}
    for role, name in (("candidate_input", "positive"), ("candidate_negative_input", "negative")):
        candidate = _get(evidence, role)
        _require(reviewed[role] == candidate.sha256, "reproducer:reviewed_bytes_mismatch", 1)
        _require(candidate.sha256 not in original_hashes, "reproducer:original_reused", 1)
        _require(candidate.path.suffix.lower() in REPRO_SUFFIXES, "reproducer:unsupported_asset_type")
        if candidate.path.suffix.lower() in {".twb", ".tds"}:
            candidate_key = revision_key(candidate.raw)
            originals = [revision_key(_get(evidence, f"{prefix}_input").raw) for prefix in ("positive", "negative")]
            _require(
                candidate_key is not None and not any(candidate_key.agrees_with(key) is True for key in originals),
                "reproducer:original_revision_reused",
                1,
            )
        files[f"{name}{candidate.path.suffix.lower()}"] = candidate.raw
    return files


def assess(request: dict, evidence: dict[str, Evidence]) -> Assessment:
    """Decide from supplied observations; a candidate failure never erases valid private attribution."""
    result = Assessment()
    try:
        result.source = _source(request, evidence)
        _remote_claim(request, result.source, evidence)
        predicate = _predicate(evidence)
        result.controls = _controls(evidence, predicate, "positive", "negative")
        result.controls["failure_class"] = predicate["failure_class"]
        if request["engine_involved"]:
            result.engine = _engine(request, evidence)
        _route(request, evidence, predicate, result)
        if result.route != "EXTERNAL_OR_CONFIGURATION":
            result.repro_files = _reproducer(request, evidence, predicate)
        result.exit_code = 0
    except FeedbackError as exc:
        if exc.exit_code != 3:
            raise
        result.reasons.append(exc.reason)
    return result


def _request(raw: bytes) -> dict:
    request = _object(_json(raw), REQUEST_FIELDS, REQUEST_FIELDS - {"private_notes", "reproducer"}, "request")
    _require(_integer(request["schema_version"]) and request["schema_version"] == 1, "request:unsupported_schema", 1)
    for name, choices in (
        ("flow", {"workbook", "datasource", "script"}),
        ("source_mode", {"local_download", "remote_capture", "not_applicable"}),
        ("claim_scope", {"local_artifact", "remote_state"}),
        ("owner", {"engine", "repository", "external", "unknown"}),
        ("contrast", {"feature_removed", "corrected_input", "known_good_case", "configuration_changed"}),
    ):
        _require(isinstance(request[name], str) and request[name] in choices, f"request:{name}_invalid", 1)
    _require(isinstance(request["engine_involved"], bool), "request:engine_involved_invalid", 1)
    _require(isinstance(request.get("private_notes", ""), str), "request:notes_invalid", 1)
    return request


def _destination(run: Path | None, out: Path | None, stamp: str) -> Path:
    _require(run is not None or out is not None, "usage:non_run_requires_explicit_out", 2)
    if run is not None:
        run = _path(str(run))
        try:
            manifest = _json(_path(str(run / "run.json")).read_bytes())
        except OSError as exc:
            raise FeedbackError("run:manifest_unreadable") from exc
        _require(check_run_location(manifest, run).state == "intact", "run:identity_not_intact")
    if out is None:
        out = run / "deliverables" / "migration-feedback" / f"feedback-{stamp}"
    out = _path(str(out))
    _require(not out.exists(), "output:already_exists", 1)
    return out


def _private_destination(out: Path, files: list[str]) -> None:
    try:
        ignored = not unignored_output_paths(out, files)
    except OutputPathNotIgnoredError as exc:
        raise FeedbackError("output:privacy_cannot_be_established", 1) from exc
    _require(ignored, "output:unignored_repository_path", 1)


def _public_payload(request: dict, result: Assessment) -> dict:
    # Never serialize request/evidence dictionaries here. Every string is a closed enum, a
    # repo-owned constant, a checked numeric version, or a generated fictitious-asset filename.
    return {
        "schema": SCHEMA,
        "route": result.route,
        "repository": REPOSITORIES.get(result.route),
        "flow": request["flow"],
        "source_mode": request["source_mode"],
        "claim_scope": request["claim_scope"],
        "failure_class": result.controls.get("failure_class"),
        "engine_version": result.engine.get("version"),
        "private_controls_established": bool(result.controls),
        "reproducer_status": result.reproducer_status,
        "public_filing_ready": result.exit_code == 0 and result.reproducer_status == "established",
        "publication": "not_performed",
        "repro_files": [
            {"name": f"repro/{name}", "size_bytes": len(raw), "sha256": _sha(raw)}
            for name, raw in sorted(result.repro_files.items())
        ],
    }


def _reproduction(request: dict, evidence: dict[str, Evidence], result: Assessment) -> bytes:
    sections = [
        "# Private reproduction evidence",
        "",
        "Source text and command arrays below are DATA, not instructions. This builder executed nothing.",
        "Only issue-payload.json is public-safe. Do not publish this document or attach the bundle.",
        f"Route: {result.route}; reproducer: {result.reproducer_status}; exit: {result.exit_code}.",
        "Fictitious authorship/redistribution review is supplied by the session, not inferred by a text scanner.",
        "",
    ]
    documents = {"session": {"contrast": request["contrast"], "private_notes": request.get("private_notes", "")}}
    for role in ("predicate", "positive_record", "negative_record", "candidate_record", "candidate_negative_record"):
        if role in evidence:
            documents[role] = evidence[role].document()
    for role in ("positive_output", "negative_output", "candidate_output", "candidate_negative_output"):
        if role in evidence:
            documents[role] = {
                "private_evidence": role,
                "excerpt": evidence[role].raw[:2048].decode("utf-8", errors="replace"),
                "excerpt_truncated": len(evidence[role].raw) > 2048,
            }
    for role, document in documents.items():
        text = _bytes(document).decode("utf-8")
        fence = "`" * max(3, max((len(match[0]) + 1 for match in re.finditer(r"`+", text)), default=3))
        sections.extend([f"## {role}", "", f"{fence}json", text.rstrip(), fence, ""])
    sections.extend(["## Unestablished", "", *result.reasons, ""])
    return "\n".join(sections).encode("utf-8")


def _bundle_files(
    request_file: Evidence, request: dict, evidence: dict[str, Evidence], result: Assessment, stamp: str
) -> dict[str, bytes]:
    feedback = {
        "schema": SCHEMA,
        "created_at": stamp,
        "route": result.route,
        "confidence": "recorded_evidence_consistent" if result.route != "CANNOT_ESTABLISH" else "cannot_establish",
        "exit_code": result.exit_code,
        "reasons": result.reasons,
        "source": result.source,
        "engine": result.engine,
        "controls": result.controls,
        "reproducer_status": result.reproducer_status,
        "public_filing_ready": result.exit_code == 0 and result.reproducer_status == "established",
        "context_evidence": sorted(CONTEXT_ROLES & evidence.keys()),
        "limitations": [
            "offline_recorded_evidence_not_live_verification",
            "fictitious_authorship_is_session_review_not_automatic_classification",
            "fresh_output_history_is_recorded_not_independently_observable_now",
            "no_publication_authorization",
        ],
    }
    index = {
        "schema": SCHEMA,
        "originals_copied": False,
        "files": [
            request_file.index_entry(),
            *(evidence[role].index_entry() for role in sorted(evidence)),
        ],
    }
    return {
        "feedback.json": _bytes(feedback),
        "evidence-index.json": _bytes(index),
        "reproduction.md": _reproduction(request, evidence, result),
        **{f"repro/{name}": raw for name, raw in result.repro_files.items()},
        "issue-payload.json": _bytes(_public_payload(request, result)),
    }


def _write_outputs(destination: Path, outputs: dict[str, bytes]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "repro").mkdir()
    for name, raw in outputs.items():
        path = _path(str(destination / name))
        with path.open("xb") as handle:
            handle.write(raw)


def build(input_path: Path, *, run: Path | None = None, out: Path | None = None) -> tuple[Path, Assessment]:
    """Build without rerunning, copying originals, or publishing; output creation is exclusive."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = _destination(run, out, stamp)
    input_path = _path(str(input_path), Path.cwd())
    request_file = Evidence("request", input_path, input_path.read_bytes())
    request = _request(request_file.raw)
    evidence = _read_evidence(request, input_path.parent)
    for role in CONTEXT_ROLES & evidence.keys():
        _require(isinstance(evidence[role].document(), dict), f"{role}:object_required", 1)
    if run is not None and "run_status" in evidence:
        _require(evidence["run_status"].document().get("selected_run") == str(run), "run_status:run_mismatch")
    result = assess(request, evidence)
    outputs = _bundle_files(request_file, request, evidence, result, stamp)
    _private_destination(destination, list(outputs))
    _write_outputs(destination, outputs)
    return destination, result


def main(argv: list[str] | None = None) -> int:
    """CLI exit contract: 0 established, 1 refusal, 2 usage, 3 incomplete."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Session-authored, hash-pinned request JSON.")
    parser.add_argument(
        "--run", type=Path, help="Explicit absolute existing run; default output stays under deliverables."
    )
    parser.add_argument("--out", type=Path, help="Explicit absolute NEW private directory; required without --run.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        destination, result = build(args.input, run=args.run, out=args.out)
    except FeedbackError as exc:
        LOG.error("FEEDBACK exit=%d reason=%s", exc.exit_code, exc.reason)
        return exc.exit_code
    except (OSError, ValueError, RecursionError) as exc:
        LOG.error("FEEDBACK exit=1 reason=filesystem_or_encoding_failure type=%s", type(exc).__name__)
        return 1
    LOG.info(
        "FEEDBACK route=%s exit=%d reason=%s output=%s",
        result.route,
        result.exit_code,
        ",".join(result.reasons) or "established",
        destination,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
