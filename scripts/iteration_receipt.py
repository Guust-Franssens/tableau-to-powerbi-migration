"""
purpose: Produce and finalize package-local, revision-bound Phase-2 comparison receipts.
usage:   capture_powerbi_pages.py iterate|finalize; this module has no CLI.

The receipt is NOT a reviewer-editable document. Iterate returns its byte checksum; finalize takes
that producer-returned checksum and separate judgement input. A later allocation likewise takes
the previous final receipt's returned checksum. These are caller-held compare-and-swap tokens,
not signatures: computing a replacement checksum from edited disk is not a trusted invocation.
No signing service, secondary registry, package gate, promotion or completion aggregator is added.
read_chain requires the successful producer's final checksum and independently revalidates current
artifacts. read_history verifies retained evidence only, never current completion authority.

Filesystem identities, inventory, images and Tableau admission are rebuilt at finalization.
Capture timing and A1 observations are immutable inputs pinned by the caller's capture checksum,
not measurements a reader can recollect. V3 final means retained, sealed evidence, never successful
measurement; it has no top-level outcome. Numeric evidence is always unestablished. Literal v2
semantics remain available only for reading/finalizing existing captures.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import posixpath
import re
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema

import current_artifact_revision as rev
import host_paths
import object_identity as oid
import package_filesystem as filesystem
import reference_evidence as evidence
from tableau_env import contains_credential

SCHEMA_VERSION = 3
TOOL_NAME = "capture_powerbi_pages"
TOOL_VERSION = "3.0.0"
RECEIPT_NAME = "iteration.json"
PENDING_BACKUP_NAME = ".iteration.pending"
PAGES_DIRNAME = "pages"
MODES = ("sign_off", "triage")
MODE_SIGN_OFF, MODE_TRIAGE = MODES
STATE_PENDING, STATE_FINAL = "pending", "final"
OUTCOME_INCOMPLETE, OUTCOME_COMPLETE = "incomplete", "complete"
STATUS_PENDING, STATUS_PASS, STATUS_UNVERIFIED = "pending", "pass", "unverified"
FINDING_OPEN, FINDING_RESOLVED, FINDING_ACCEPTED = "still_open", "resolved", "accepted_limitation"
DATA_STATUS_PENDING = "pending"
DATA_PENDING_REASON = (
    "probe_desktop_query and refresh_pbip_model do not expose trusted structured data evidence; "
    "data and numeric fidelity remain unverified"
)
OBSERVED, REFUSED, UNESTABLISHED = "observed", "refused", "unestablished"
NUMERIC_ABSENCE = "numeric measurement is not supported by this receipt version"
CSV_ABSENCE = "certified CSV operands are not collected by this receipt version"
TYPED_ABSENCE = "typed DAX envelopes are not collected by this receipt version"
MEASUREMENT_REFUSALS = (
    "CANARIES_REQUIRED",
    "CREDENTIAL_MISSING",
    "CREDENTIAL_UNKNOWN",
    "DESKTOP_UNREADY",
    "DIALOG_NEEDS_HUMAN",
    "DIALOG_UNREADABLE",
    "DIALOG_UNRECOGNIZED",
    "TIMEOUT",
    "TOOL_UNAVAILABLE",
    "MODEL_LOCK_TIMEOUT",
)
MAX_SECONDS = 3600
MAX_FRAMES = 1_000_000
MAX_COUNT = (1 << 53) - 1
SHA_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", re.ASCII)
ITERATION_RE = re.compile(r"[0-9]{3}", re.ASCII)
FINDING_RE = re.compile(r"F-[0-9]{3,8}", re.ASCII)
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
StatusReader = Callable[[int], dict[str, Any]]


class ReceiptError(RuntimeError):
    """A named, ASCII-safe refusal without reflected input or exception text."""

    def __init__(self, code: str, detail: str = "iteration input refused") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@contextmanager
def _named_refusals() -> Iterator[None]:
    try:
        yield
    except rev.RevisionError as error:
        raise ReceiptError(error.code, error.detail) from error
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ReceiptError("INPUT_UNREADABLE", "iteration input could not be read safely") from error


def read_strict_json(path: Path) -> dict[str, Any]:
    """One strict reader for receipts, reviewer input, manifests, PBIR and provenance."""
    with _named_refusals():
        return rev.read_json(path)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def assert_shareable(payload: Any) -> None:
    """Apply central credential and host-location containment to EVERY key and string value."""
    for text in _strings(payload):
        if (
            len(text) > 500
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in text)
            or host_paths.discloses_host_location(text)
            or URL_RE.search(text)
            or contains_credential(text)
        ):
            raise ReceiptError("PRIVACY", "a string is not safe for a shared receipt")


def _object(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _nullable(inner: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [inner, {"type": "null"}]}


TEXT = {"type": "string", "minLength": 1, "maxLength": 500}
IDENTITY = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"}
SHA = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
REVISION = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
COUNT = {"type": "integer", "minimum": 0, "maximum": MAX_COUNT}
SECONDS = {"type": "number", "minimum": 0, "maximum": MAX_SECONDS}
POSITIVE_SECONDS = {**SECONDS, "exclusiveMinimum": 0}
STATUSES = {"enum": ["pending", "pass", "layout_match", "mismatch", "unverified"]}
TIME = {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"}
CAPTURE_SCHEMA = _object(
    converged={"type": "boolean"},
    frames={"type": "integer", "minimum": 1, "maximum": MAX_FRAMES},
    poll_seconds=POSITIVE_SECONDS,
    stable_seconds=POSITIVE_SECONDS,
    max_wait_seconds=POSITIVE_SECONDS,
    settled_seconds=SECONDS,
    stable_elapsed_seconds=SECONDS,
)
PAGE_SCHEMA = _object(
    page_id=TEXT,
    display_name=TEXT,
    expected_visual_ids=_array(TEXT),
    tableau=_nullable(_object(manifest_sha256=SHA, path=TEXT, sha256=SHA, grade=TEXT)),
    tableau_reason=_nullable(TEXT),
    powerbi=_object(path=TEXT, sha256=SHA, byte_count={**COUNT, "minimum": 1}, capture=CAPTURE_SCHEMA),
)
FINDING_SCHEMA = _object(
    id={"type": "string", "pattern": r"^F-[0-9]{3,8}$"},
    page_id=_nullable(TEXT),
    visual_id=_nullable(TEXT),
    kind={"enum": ["visual", "numeric", "data", "other"]},
    severity={"enum": ["low", "medium", "high"]},
    status={"enum": [FINDING_OPEN, FINDING_RESOLVED, FINDING_ACCEPTED]},
    detail=TEXT,
    limitation_ref=_nullable(_object(pointer=TEXT, sha256=SHA)),
)
JUDGEMENT_SCHEMA = _object(
    completed_at=_nullable(TIME),
    pages=_array(
        _object(
            page_id=TEXT,
            whole_page_status=STATUSES,
            visual_results=_array(_object(visual_id=TEXT, status=STATUSES, finding_ids=_array(TEXT))),
            numeric_results=_array(
                _object(
                    visual_id=TEXT,
                    status=STATUSES,
                    tableau_evidence_sha256=_nullable(SHA),
                    powerbi_query_sha256=_nullable(SHA),
                    powerbi_result_sha256=_nullable(SHA),
                    finding_ids=_array(TEXT),
                )
            ),
        )
    ),
    findings=_array(FINDING_SCHEMA),
)
GENERATED_SCHEMA = _object(
    generated_at=TIME,
    scope={"enum": ["all_pages", "subset"]},
    artifact=_object(
        unit=TEXT,
        kind={"enum": ["workbook", "datasource"]},
        report_path=TEXT,
        model_path=TEXT,
        pbip_path=TEXT,
        package_revision=REVISION,
        report_revision=REVISION,
        model_revision=REVISION,
        cache_sha256=_nullable(SHA),
        cache_byte_count=_nullable(COUNT),
    ),
    review=_object(
        reviewer=IDENTITY,
        session_id=_nullable(IDENTITY),
        tool={"const": TOOL_NAME},
        tool_version={"const": "2.0.0"},
        desktop_pid={"type": "integer", "minimum": 1, "maximum": 2**32 - 1},
        desktop_binding_matches={"const": True},
        reload_confirmed={"const": True},
    ),
    previous=_nullable(_object(iteration=TEXT, receipt_sha256=SHA)),
    limitations=_object(spec_path=_nullable(TEXT), entry_count=COUNT),
    data_evidence=_object(status={"const": DATA_STATUS_PENDING}, reason={"const": DATA_PENDING_REASON}),
    pages=_array(PAGE_SCHEMA),
    changes_from_previous=_array(_object(page_id=TEXT, before_sha256=_nullable(SHA), after_sha256=_nullable(SHA))),
)
RECEIPT_SCHEMA = _object(
    schema_version={"type": "integer", "const": 2},
    iteration={"type": "string", "pattern": r"^[0-9]{3}$"},
    mode={"enum": list(MODES)},
    state={"enum": [STATE_PENDING, STATE_FINAL]},
    outcome={"enum": [None, OUTCOME_INCOMPLETE]},
    generated=GENERATED_SCHEMA,
    judgement=JUDGEMENT_SCHEMA,
)

IDENTITY_SCHEMA = _object(
    pid={"type": "integer", "minimum": 1, "maximum": 2**32 - 1},
    process_start={"type": "string", "pattern": r"^[1-9][0-9]{0,19}$"},
    as_pid={"type": "integer", "minimum": 1, "maximum": 2**32 - 1},
    as_process_start={"type": "string", "pattern": r"^[1-9][0-9]{0,19}$"},
    port={"type": "integer", "minimum": 1, "maximum": 65535},
)
CATALOGUE = {"type": "string", "pattern": r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"}
BINDING_SCHEMA = _object(identity=IDENTITY_SCHEMA, catalogue=CATALOGUE)
PREPARATION_REQUEST_SCHEMA = _object(
    refresh={"type": "boolean"}, persist={"type": "boolean"}, canaries={**_array(TEXT), "uniqueItems": True}
)


def _fact_schema(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "oneOf": [
            _object(status={"const": OBSERVED}, reason={"type": "null"}, observation=observation),
            _object(
                status={"const": REFUSED},
                reason={"enum": list(MEASUREMENT_REFUSALS)},
                observation={"type": "null"},
            ),
            _object(
                status={"const": UNESTABLISHED},
                reason={"enum": ["not_requested", "observation_unavailable"]},
                observation={"type": "null"},
            ),
        ]
    }


DATA_EVIDENCE_SCHEMA = _object(
    binding=_fact_schema(BINDING_SCHEMA),
    refresh=_fact_schema(
        _object(
            **BINDING_SCHEMA["properties"],
            refresh_type={"const": "full"},
            scope={"const": "database"},
            tables={"const": []},
        )
    ),
    canaries=_fact_schema(
        _array(
            _object(
                **BINDING_SCHEMA["properties"],
                table=TEXT,
                query=TEXT,
                query_sha256=SHA,
                returned_rows=COUNT,
            )
        )
    ),
    persistence=_fact_schema(
        _object(
            **BINDING_SCHEMA["properties"],
            compatibility_level={"type": "integer", "minimum": 1, "maximum": MAX_COUNT},
            method={"const": "AMO_ImageSave"},
            image=_object(
                intended_sha256=SHA,
                intended_size={**COUNT, "minimum": 1},
                installed_sha256=SHA,
                installed_size={**COUNT, "minimum": 1},
                commitment={"const": "UNESTABLISHED"},
            ),
        )
    ),
)
NUMERIC_FACT_SCHEMA = _object(status={"const": UNESTABLISHED}, reason={"const": NUMERIC_ABSENCE})
V3_JUDGEMENT_SCHEMA = _object(
    **{
        **JUDGEMENT_SCHEMA["properties"],
        "pages": _array(
            _object(
                **{
                    **JUDGEMENT_SCHEMA["properties"]["pages"]["items"]["properties"],
                    "numeric_results": _array(_object(visual_id=TEXT, finding_ids=_array(TEXT))),
                }
            )
        ),
    }
)
V3_GENERATED_SCHEMA = _object(
    **{
        **GENERATED_SCHEMA["properties"],
        "review": _object(
            **{**GENERATED_SCHEMA["properties"]["review"]["properties"], "tool_version": {"const": TOOL_VERSION}}
        ),
        "preparation": _object(
            requested=PREPARATION_REQUEST_SCHEMA, artifact_before=GENERATED_SCHEMA["properties"]["artifact"]
        ),
        "data_evidence": DATA_EVIDENCE_SCHEMA,
        "numeric_evidence": _array(
            _object(
                page_id=TEXT,
                whole_page=NUMERIC_FACT_SCHEMA,
                visuals=_array(_object(visual_id=TEXT, **NUMERIC_FACT_SCHEMA["properties"])),
            )
        ),
        "retained_roles": _object(
            receipt_json=_object(count={"const": 1}),
            powerbi_png=_object(count=COUNT),
            certified_tableau_csv=_object(count={"const": 0}, reason={"const": CSV_ABSENCE}),
            typed_dax_envelope=_object(count={"const": 0}, reason={"const": TYPED_ABSENCE}),
        ),
    }
)
V3_RECEIPT_SCHEMA = _object(
    **{
        **{key: value for key, value in RECEIPT_SCHEMA["properties"].items() if key != "outcome"},
        "schema_version": {"type": "integer", "const": 3},
        "generated": V3_GENERATED_SCHEMA,
        "judgement": V3_JUDGEMENT_SCHEMA,
    }
)


def _validate(schema: dict[str, Any], payload: Any) -> None:
    assert_shareable(payload)
    try:
        jsonschema.Draft202012Validator(schema).validate(payload)
    except (jsonschema.ValidationError, TypeError, ValueError) as error:
        raise ReceiptError("SCHEMA", "the document does not satisfy the closed receipt schema") from error

    # In-memory callers do not pass through the strict JSON reader.
    def finite(value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ReceiptError("NONFINITE_NUMBER", "numeric observations must be finite")
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        if isinstance(value, list):
            for item in value:
                finite(item)

    finite(payload)


def _validate_state(payload: dict[str, Any]) -> None:
    pending = payload["state"] == STATE_PENDING
    if payload["schema_version"] == 2 and payload["outcome"] != (None if pending else OUTCOME_INCOMPLETE):
        raise ReceiptError("STATE_INVALID", "data pending can never produce a complete outcome")
    if (payload["judgement"]["completed_at"] is None) != pending:
        raise ReceiptError("STATE_INVALID", "completion time must agree with producer state")
    if payload["mode"] == MODE_SIGN_OFF and payload["generated"]["scope"] != "all_pages":
        raise ReceiptError("STATE_INVALID", "a subset cannot be sign-off")


def validate_receipt(payload: Any) -> dict[str, Any]:
    """Closed shape AND semantic combinations; statuses alone never confer evidence."""
    version = payload.get("schema_version") if isinstance(payload, dict) else None
    if type(version) is not int or version not in (2, 3):  # pylint: disable=unidiomatic-typecheck
        raise ReceiptError("SCHEMA_VERSION", "only literal v2 and v3 receipts are supported")
    _validate(RECEIPT_SCHEMA if version == 2 else V3_RECEIPT_SCHEMA, payload)
    _validate_state(payload)
    generated = payload["generated"]
    artifact = generated["artifact"]
    if (artifact["cache_sha256"] is None) != (artifact["cache_byte_count"] is None):
        raise ReceiptError("STATE_INVALID", "cache hash and size must be present together")
    ids = [row["page_id"] for row in generated["pages"]]
    if not ids or len(set(ids)) != len(ids):
        raise ReceiptError("INVENTORY_INVALID", "captured page identifiers must be unique and nonempty")
    for row in generated["pages"]:
        if (row["tableau"] is None) == (row["tableau_reason"] is None):
            raise ReceiptError("STATE_INVALID", "a page needs either admitted evidence or a reason")
        capture = row["powerbi"]["capture"]
        if capture["stable_elapsed_seconds"] > capture["settled_seconds"]:
            raise ReceiptError("CAPTURE_INVALID", "stable dwell exceeds elapsed capture time")
        if capture["converged"] and (
            capture["frames"] < 2 or capture["stable_elapsed_seconds"] < capture["stable_seconds"]
        ):
            raise ReceiptError("CAPTURE_INVALID", "convergence requires repeated frames and elapsed stable dwell")
        visuals = row["expected_visual_ids"]
        if len(set(visuals)) != len(visuals):
            raise ReceiptError("INVENTORY_INVALID", "visual identifiers must be unique within a page")
    if version == 3:
        _validate_v3_facts(generated)
    return payload


def unestablished_fact(reason: str = "not_requested") -> dict[str, Any]:
    """An explicit absence, never a zero measurement."""
    return {"status": UNESTABLISHED, "reason": reason, "observation": None}


def _validate_v3_facts(generated: dict[str, Any]) -> None:
    requested = generated["preparation"]["requested"]
    canaries = requested["canaries"]
    if any(not name.strip() for name in canaries) or len({name.casefold() for name in canaries}) != len(canaries):
        raise ReceiptError("CANARIES_REQUIRED", "canary names must be explicit, nonempty and unique")
    facts = generated["data_evidence"]
    binding = facts["binding"]["observation"]
    if requested["refresh"] or requested["persist"] or canaries:
        if facts["binding"]["status"] != OBSERVED:
            raise ReceiptError("A1_BINDING_UNESTABLISHED", "requested observations need the held A1 binding")
        identity = binding["identity"]
        if (
            identity["pid"] != generated["review"]["desktop_pid"]
            or identity["as_pid"] == identity["pid"]
            or int(identity["as_process_start"]) < int(identity["process_start"])
        ):
            raise ReceiptError("A1_BINDING_MISMATCH", "A1 process identity does not match this capture")
    elif facts["binding"] != unestablished_fact():
        raise ReceiptError("A1_REQUEST_MISMATCH", "unrequested binding evidence cannot be supplied")
    _validate_operation_facts(generated)
    before, after = generated["preparation"]["artifact_before"], generated["artifact"]
    if any(before[key] != after[key] for key in ("unit", "kind", "report_path", "model_path", "pbip_path")):
        raise ReceiptError("GENERATED_CHANGED", "preparation cannot change the package binding")
    if generated["retained_roles"]["powerbi_png"]["count"] != len(generated["pages"]):
        raise ReceiptError("RETAINED_ROLES", "retained PNG cardinality must match the captured pages")


def _validate_operation_facts(generated: dict[str, Any]) -> None:
    requested, facts = generated["preparation"]["requested"], generated["data_evidence"]
    binding, canaries = facts["binding"]["observation"], requested["canaries"]
    for key, enabled in (
        ("refresh", requested["refresh"]),
        ("canaries", bool(canaries)),
        ("persistence", requested["persist"]),
    ):
        fact = facts[key]
        if not enabled and fact != unestablished_fact():
            raise ReceiptError("A1_REQUEST_MISMATCH", "unrequested operation evidence cannot be supplied")
        if enabled and fact == unestablished_fact():
            raise ReceiptError("A1_REQUEST_MISMATCH", "a requested operation cannot be marked not requested")
        observations = fact["observation"] if key == "canaries" else [fact["observation"]]
        for observed in observations or []:
            if observed is not None and any(observed[field] != binding[field] for field in ("identity", "catalogue")):
                raise ReceiptError("A1_BINDING_MISMATCH", "all observations must belong to the same held binding")
    rows = facts["canaries"]["observation"]
    if rows is not None:
        if [row["table"] for row in rows] != canaries:
            raise ReceiptError("A1_CANARY_SET", "returned canaries must match the complete explicit request")
        if any(hashlib.sha256(row["query"].encode("utf-8")).hexdigest() != row["query_sha256"] for row in rows):
            raise ReceiptError("A1_QUERY_CHANGED", "the retained canary query and its digest disagree")
    persisted = facts["persistence"]["observation"]
    if persisted is not None:
        image, artifact = persisted["image"], generated["artifact"]
        if not (
            image["intended_sha256"] == image["installed_sha256"] == artifact["cache_sha256"]
            and image["intended_size"] == image["installed_size"] == artifact["cache_byte_count"]
        ):
            raise ReceiptError("A1_CACHE_MISMATCH", "ImageSave readback must agree with the current cache bytes")


@dataclass(frozen=True)
class PackageTarget:
    """One package's coherent, walked report/model/PBIP identity."""

    root: Path
    unit: str
    kind: str
    report_path: str
    model_path: str
    pbip_path: str
    asset: Path | None

    @property
    def report_dir(self) -> Path:
        """The walked report location."""
        return self.root.joinpath(*self.report_path.split("/"))

    @property
    def model_dir(self) -> Path:
        """The walked model location."""
        return self.root.joinpath(*self.model_path.split("/"))

    @property
    def pbip(self) -> Path:
        """The unique PBIP referencing that report."""
        return self.root.joinpath(*self.pbip_path.split("/"))


def resolve_package(package: Path) -> PackageTarget:  # pylint: disable=too-many-locals
    """Cross-check exact role references over one strict no-follow filesystem snapshot.

    The refresh tool's resolver permits absent-reference heuristics and multiple report artifacts;
    those are not authorities for this single-report producer. Here references are compared with
    canonical relative names of already-walked roles, never used to open an untrusted target.
    """
    with _named_refusals():
        files, directories = rev.tree_files(package)
    if "package-manifest.json" not in files:
        raise ReceiptError("NOT_A_PACKAGE", "the supplied directory has no package manifest")
    manifest = read_strict_json(files["package-manifest.json"])
    roles = manifest.get("artifacts")
    if not isinstance(roles, dict):
        raise ReceiptError("PACKAGE_MANIFEST", "the manifest must declare artifact roles")
    report, model, asset = (roles.get(key) for key in ("report", "model", "asset"))
    for role, suffix in ((report, ".Report"), (model, ".SemanticModel")):
        if not isinstance(role, str) or not filesystem.is_canonical_key(role) or not role.endswith(suffix):
            raise ReceiptError("UNSAFE_PATH", "artifact roles must be canonical package-relative paths")
        if role not in directories:
            raise ReceiptError("PACKAGE_MANIFEST", "a declared artifact directory is missing")
    pbips = [key for key in files if key.lower().endswith(".pbip")]
    if len(pbips) != 1:
        raise ReceiptError("PBIP_IDENTITY", "the package must hold exactly one PBIP, without decoys")
    pbip = pbips[0]
    project = read_strict_json(files[pbip])
    expected_report = posixpath.relpath(report, posixpath.dirname(pbip))
    if project.get("artifacts") != [{"report": {"path": expected_report}}]:
        raise ReceiptError("PBIP_IDENTITY", "the unique PBIP must reference exactly the declared report")
    binding = files.get(report + "/definition.pbir")
    if binding is None or model + "/definition/model.tmdl" not in files:
        raise ReceiptError("MODEL_BINDING", "the declared report or model definition is missing")
    expected_model = posixpath.relpath(model, report)
    if read_strict_json(binding).get("datasetReference") != {"byPath": {"path": expected_model}}:
        raise ReceiptError("MODEL_BINDING", "definition.pbir must bind exactly the declared model")
    unit, kind = manifest.get("unit"), manifest.get("kind")
    if not isinstance(unit, str) or not unit.strip() or kind not in {"workbook", "datasource"}:
        raise ReceiptError("PACKAGE_MANIFEST", "unit and kind must be explicitly declared")
    if asset is not None and (not isinstance(asset, str) or asset not in files):
        raise ReceiptError("PACKAGE_MANIFEST", "the source asset must be a walked package file")
    assert_shareable({"unit": unit, "report": report, "model": model, "pbip": pbip})
    return PackageTarget(package, unit, kind, report, model, pbip, files.get(asset))


def bridge_json(command: str, pid: int) -> dict[str, Any]:
    """Read the installed bridge's structured result; never persist stdout/stderr or error text."""
    try:
        result = subprocess.run(
            ["powerbi-desktop", command, "--pid", str(pid), "--wait-seconds", "30"],
            capture_output=True,
            shell=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise ReceiptError("DESKTOP_UNVERIFIED", "the PID-scoped bridge operation failed")
        return filesystem.parse_manifest_text(result.stdout.decode("utf-8"))
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError, filesystem._ManifestError) as error:  # pylint: disable=protected-access
        raise ReceiptError("DESKTOP_UNVERIFIED", "the PID-scoped bridge result was unavailable or invalid") from error


def bridge_status(pid: int) -> dict[str, Any]:
    """Trusted default runtime status, not a caller-supplied path."""
    return bridge_json("status", pid)


def bridge_reload(pid: int) -> bool:
    """Reload only the selected instance; a fresh status check follows this operation."""
    # Bridge CLI 0.1.2 emits {status: "ok", pid, result: {success: true}}, not "succeeded".
    response = bridge_json("reload", pid)
    result = response.get("result")
    return (
        response.get("status") == "ok"
        and isinstance(response.get("pid"), int)
        and not isinstance(response["pid"], bool)
        and response["pid"] == pid
        and isinstance(result, dict)
        and result.get("success") is True
    )


def assert_desktop_binding(target: PackageTarget, pid: int, reader: StatusReader = bridge_status) -> None:
    """Require exactly one PID-scoped currentFilePath equal to the coherent PBIP identity."""
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2**32 - 1:
        raise ReceiptError("DESKTOP_UNVERIFIED", "a valid Desktop process identifier is required")
    with _named_refusals():
        payload = reader(pid)
        instances = payload.get("instances") if isinstance(payload, dict) else None
        if not isinstance(instances, list) or payload.get("status") != "ready":
            raise ReceiptError("DESKTOP_UNVERIFIED", "the bridge supplied no instance list")
        matching = [
            item
            for item in instances
            if isinstance(item, dict)
            and isinstance(item.get("pid"), int)
            and not isinstance(item["pid"], bool)
            and item["pid"] == pid
        ]
        if len(matching) != 1:
            raise ReceiptError("DESKTOP_UNVERIFIED", "the bridge did not identify exactly one requested PID")
        current = matching[0].get("currentFilePath")
        if not isinstance(current, str) or not Path(current).is_absolute() or Path(current) != target.pbip.absolute():
            raise ReceiptError("DESKTOP_BINDING_MISMATCH", "the requested PID is not showing the package PBIP")
        if matching[0].get("bridgeStatus") != "connected" or matching[0].get("hasUnsavedChanges") is not False:
            raise ReceiptError("DESKTOP_UNVERIFIED", "Desktop must confirm there are no unsaved changes")


def report_inventory(report_dir: Path) -> list[rev.PageInventory]:
    """Read the single current PBIR denominator."""
    with _named_refusals():
        return rev.report_inventory(report_dir)


def artifact_facts(target: PackageTarget) -> dict[str, Any]:
    """Re-derive the entire artifact identity, including non-cache .pbi bytes."""
    with _named_refusals():
        cache = rev.cache_facts(target.model_dir)
        return {
            "unit": target.unit,
            "kind": target.kind,
            "report_path": target.report_path,
            "model_path": target.model_path,
            "pbip_path": target.pbip_path,
            "package_revision": rev.package_working_revision(target.root, target.model_dir),
            "report_revision": rev.report_revision(target.report_dir),
            "model_revision": rev.model_revision(target.model_dir),
            "cache_sha256": cache.sha256 if cache else None,
            "cache_byte_count": cache.byte_count if cache else None,
        }


def _admitted_evidence(target: PackageTarget) -> list[tuple[evidence.Evidence, str]]:
    files, _ = rev.tree_files(target.root)
    admitted = []
    for folder, manifest, loader in (
        ("reference", "manifest.json", evidence.reference_evidence),
        ("oracle", "oracle-manifest.json", evidence.oracle_evidence),
    ):
        key = f"{folder}/{manifest}"
        if key not in files:
            continue
        document = read_strict_json(files[key])
        # Check all declared render roles before the existing admission helper may open them.
        for text in _render_paths(document):
            if not filesystem.is_canonical_key(text) or f"{folder}/{text}" not in files:
                raise ReceiptError("TABLEAU_PATH", "a Tableau render role is not a walked package file")
        try:
            renders, _ = loader([target.root / folder])
        except (oid.AmbiguousIdentity, TypeError, AttributeError) as error:
            raise ReceiptError("TABLEAU_INVALID", "Tableau evidence identity is ambiguous or malformed") from error
        admitted.extend((item, rev.sha256_of_file(files[key])) for item in renders)
    return admitted


def _render_paths(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"image", "path"} and isinstance(item, str):
                yield item
            yield from _render_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _render_paths(item)


def tableau_matches(  # pylint: disable=too-many-locals
    target: PackageTarget, pages: list[rev.PageInventory]
) -> dict[str, dict[str, Any] | None]:
    """Reuse grade, revision attribution and exact Tableau object matching; never inflate oracle grade."""
    result: dict[str, dict[str, Any] | None] = {page.page_id: None for page in pages}
    if target.asset is None:
        return result
    source_sha = rev.sha256_of_file(target.asset)
    provenance = target.root / "source-provenance.json"
    if provenance.is_file():
        read_strict_json(provenance)
    try:
        luid, revision = evidence.provenance_origin(target.root, source_sha, target.asset)
    except (oid.AmbiguousIdentity, TypeError, AttributeError) as error:
        raise ReceiptError("TABLEAU_INVALID", "source provenance cannot establish one workbook") from error
    identity = evidence.UnitIdentity(target.unit, target.asset, source_sha, luid, revision)
    index: oid.CandidateIndex[tuple[evidence.Evidence, str]] = oid.CandidateIndex()
    for item, manifest_sha in _admitted_evidence(target):
        index.add(item.candidate(), (item, manifest_sha))
    for page in pages:
        hits = []
        ambiguous = False
        for kind in (oid.KIND_DASHBOARD, oid.KIND_WORKSHEET):
            key = oid.ObjectIdentity.from_engine(kind, page.display_name)
            if key is None:
                continue
            resolution = index.resolve(key)
            ambiguous |= resolution.outcome == oid.AMBIGUOUS
            if resolution.outcome == oid.UNIQUE:
                hits.append(resolution.value())
        if ambiguous or len(hits) != 1:
            continue
        item, manifest_sha = hits[0]
        if item.attribution(identity).admitted:
            result[page.page_id] = {
                "manifest_sha256": manifest_sha,
                "path": Path(item.path).relative_to(target.root).as_posix(),
                "sha256": item.render_digest,
                "grade": item.grade,
            }
    digests = [row["sha256"] for row in result.values() if row]
    return {key: row if row and digests.count(row["sha256"]) == 1 else None for key, row in result.items()}


def page_image_name(page_id: str) -> str:
    """One generated mapping, never a reviewer path or a raw page identifier."""
    return "page-" + hashlib.sha256(page_id.encode("utf-8")).hexdigest() + ".png"


def screenshot_role(page_id: str, relative: str) -> str:
    """Validate an exact generated role BEFORE a filesystem path may be constructed."""
    canonical = f"{PAGES_DIRNAME}/{page_image_name(page_id)}"
    if relative != canonical or not filesystem.is_canonical_key(relative):
        raise ReceiptError("SCREENSHOT_PATH", "the screenshot role is not the generated canonical page mapping")
    return canonical


def image_facts(directory: Path, page_id: str, relative: str) -> dict[str, Any]:
    """Exact role, no-follow containment, no hardlink aliases, valid PNG structure, hash and size."""
    canonical = screenshot_role(page_id, relative)
    files, _ = rev.tree_files(directory)
    image = files.get(canonical)
    if image is None:
        raise ReceiptError("SCREENSHOT_MISSING", "a captured page has no retained canonical screenshot")
    if image.lstat().st_nlink != 1:
        raise ReceiptError("SCREENSHOT_ALIAS", "a retained screenshot must not be a hardlink alias")
    blob = image.read_bytes()
    assert_png(blob)
    return {"path": canonical, "sha256": hashlib.sha256(blob).hexdigest(), "byte_count": len(blob)}


def assert_png(blob: bytes) -> None:
    """Use the existing Pillow extra for a PNG structure check AND compressed-pixel decode."""
    if evidence._png_size(blob) is None:  # pylint: disable=protected-access
        raise ReceiptError("SCREENSHOT_NOT_PNG", "the PNG chunk stream is not structurally complete")
    try:
        from PIL import Image  # pylint: disable=import-outside-toplevel
    except ImportError as error:
        raise ReceiptError("PNG_VERIFIER_UNAVAILABLE", "PNG verification requires the existing Pillow extra") from error
    try:
        with Image.open(io.BytesIO(blob)) as image:
            if image.format != "PNG":
                raise ReceiptError("SCREENSHOT_NOT_PNG", "the retained bytes do not declare PNG format")
            image.verify()
        with Image.open(io.BytesIO(blob)) as image:
            image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise ReceiptError("SCREENSHOT_NOT_PNG", "the retained bytes are not a complete decodable PNG") from error


def limitation_facts(package: Path) -> dict[str, Any]:
    """Strictly read the current limitations list if supplied."""
    path = package / "migration-spec.json"
    if not path.is_file():
        return {"spec_path": None, "entry_count": 0}
    entries = read_strict_json(path).get("limitations_encountered")
    if not isinstance(entries, list):
        raise ReceiptError("LIMITATIONS_INVALID", "the current spec must declare a limitations list")
    return {"spec_path": "migration-spec.json", "entry_count": len(entries)}


def pending_judgement(pages: list[rev.PageInventory], *, schema_version: int = 3) -> dict[str, Any]:
    """Visual slots begin pending; v3 numeric entries only reference generated slots/findings."""
    judgement = {
        "completed_at": None,
        "pages": [
            {
                "page_id": page.page_id,
                "whole_page_status": STATUS_PENDING,
                "visual_results": [
                    {"visual_id": key, "status": STATUS_PENDING, "finding_ids": []} for key in page.visual_ids
                ],
                "numeric_results": [
                    {
                        "visual_id": key,
                        "status": STATUS_PENDING,
                        "tableau_evidence_sha256": None,
                        "powerbi_query_sha256": None,
                        "powerbi_result_sha256": None,
                        "finding_ids": [],
                    }
                    for key in page.visual_ids
                ],
            }
            for page in pages
        ],
        "findings": [],
    }
    if schema_version == 3:
        for page in judgement["pages"]:
            page["numeric_results"] = [
                {"visual_id": row["visual_id"], "finding_ids": []} for row in page["numeric_results"]
            ]
    return judgement


def now_rfc3339() -> str:
    """UTC producer timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def generated_facts(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    target: PackageTarget,
    directory: Path,
    captured: dict[str, dict[str, Any]],
    review: dict[str, Any],
    generated_at: str,
    previous: Iteration | None,
    *,
    schema_version: int = 2,
    preparation: dict[str, Any] | None = None,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build every generated field from current artifacts and checksum-pinned capture observations."""
    inventory = report_inventory(target.report_dir)
    selected = [page for page in inventory if page.page_id in captured]
    if {page.page_id for page in selected} != set(captured):
        raise ReceiptError("INVENTORY_CHANGED", "a captured page is no longer in the current inventory")
    matches = tableau_matches(target, selected)
    pages = [
        {
            "page_id": page.page_id,
            "display_name": page.display_name,
            "expected_visual_ids": list(page.visual_ids),
            "tableau": matches[page.page_id],
            "tableau_reason": None if matches[page.page_id] else "no unique admitted Tableau render for this page",
            "powerbi": {
                **image_facts(directory, page.page_id, f"{PAGES_DIRNAME}/{page_image_name(page.page_id)}"),
                "capture": captured[page.page_id],
            },
        }
        for page in selected
    ]
    before = (
        {row["page_id"]: row["powerbi"]["sha256"] for row in previous.payload["generated"]["pages"]} if previous else {}
    )
    after = {row["page_id"]: row["powerbi"]["sha256"] for row in pages}
    generated = {
        "generated_at": generated_at,
        "scope": "all_pages" if len(selected) == len(inventory) else "subset",
        "artifact": artifact_facts(target),
        "review": review,
        "previous": {"iteration": previous.name, "receipt_sha256": previous.receipt_sha256} if previous else None,
        "limitations": limitation_facts(target.root),
        "data_evidence": {"status": DATA_STATUS_PENDING, "reason": DATA_PENDING_REASON},
        "pages": pages,
        "changes_from_previous": [
            {"page_id": key, "before_sha256": before.get(key), "after_sha256": after.get(key)}
            for key in sorted(set(before) | set(after))
        ]
        if previous
        else [],
    }
    if schema_version == 3:
        generated.update(
            preparation=preparation,
            data_evidence=observations,
            numeric_evidence=[
                {
                    "page_id": page.page_id,
                    "whole_page": {"status": UNESTABLISHED, "reason": NUMERIC_ABSENCE},
                    "visuals": [
                        {"visual_id": key, "status": UNESTABLISHED, "reason": NUMERIC_ABSENCE}
                        for key in page.visual_ids
                    ],
                }
                for page in inventory
            ],
            retained_roles={
                "receipt_json": {"count": 1},
                "powerbi_png": {"count": len(pages)},
                "certified_tableau_csv": {"count": 0, "reason": CSV_ABSENCE},
                "typed_dax_envelope": {"count": 0, "reason": TYPED_ABSENCE},
            },
        )
    return generated


def _current_generated(target: PackageTarget, selected: Iteration, previous: Iteration | None) -> dict[str, Any]:
    """Re-read mutable artifact facts, carrying only checksum-pinned, non-recollectable observations."""
    generated = selected.payload["generated"]
    return generated_facts(
        target,
        selected.directory,
        {row["page_id"]: row["powerbi"]["capture"] for row in generated["pages"]},
        generated["review"],
        generated["generated_at"],
        previous,
        schema_version=selected.payload["schema_version"],
        preparation=generated["preparation"] if selected.payload["schema_version"] == 3 else None,
        observations=generated["data_evidence"],
    )


@dataclass(frozen=True)
class Iteration:
    """One exact receipt and its verified retained screenshots."""

    name: str
    directory: Path
    receipt_bytes: bytes
    payload: dict[str, Any]

    @property
    def receipt_sha256(self) -> str:
        """The checksum of the same immutable bytes that were parsed and validated."""
        return hashlib.sha256(self.receipt_bytes).hexdigest()


def iterations_root(package: Path) -> Path:
    """The sole self-referential evidence subtree."""
    return package / "validation" / "iterations"


def _iteration_files(directory: Path, payload: dict[str, Any], pending_backup: bytes | None = None) -> None:
    files, directories = rev.tree_files(directory)
    named = set()
    for page in payload["generated"]["pages"]:
        recorded = page["powerbi"]
        facts = image_facts(directory, page["page_id"], recorded["path"])
        if any(value != recorded[key] for key, value in facts.items()):
            raise ReceiptError("SCREENSHOT_CHANGED", "retained screenshot bytes differ from their receipt")
        if facts["path"] in named:
            raise ReceiptError("SCREENSHOT_ALIAS", "two pages cannot claim one screenshot role")
        named.add(facts["path"])
    if pending_backup is not None:
        backup = files.get(PENDING_BACKUP_NAME)
        if backup is None or backup.read_bytes() != pending_backup:
            raise ReceiptError("CAPTURE_CHANGED", "the displaced pending receipt no longer matches the capture")
        named.add(PENDING_BACKUP_NAME)
    if set(files) != {RECEIPT_NAME, *named} or directories != {PAGES_DIRNAME}:
        raise ReceiptError("EXTRA_FILE", "an iteration must contain exactly its receipt and canonical page PNGs")


def _require_pin(actual: str, expected: str | None, code: str) -> None:
    if not isinstance(expected, str) or not SHA_RE.fullmatch(expected) or actual != expected:
        raise ReceiptError(code, "the producer-returned receipt checksum is required and must still match")


def read_chain(package: Path, expected_sha256: str) -> list[Iteration]:
    """Read current final authority, pinned to the successful producer's returned final checksum.

    A final file and absent rollback markers are insufficient. Rebuild current artifact identity
    independently of publication/rollback writes; do not derive the expected checksum from disk.
    """
    with _named_refusals():
        chain = read_history(package)
        if not chain or chain[-1].payload["state"] != STATE_FINAL:
            raise ReceiptError("NO_FINAL_ITERATION", "the latest iteration must be final")
        _require_pin(chain[-1].receipt_sha256, expected_sha256, "FINAL_RECEIPT_MISMATCH")
        _assert_snapshot(package, chain)
        return chain


def read_history(package: Path) -> list[Iteration]:
    """Parse retained receipt/PNG history, including pending captures; NEVER confer current authority.

    Previous artifacts may legitimately differ while building the next iteration. Historical reads
    still verify every exact predecessor link, retained PNG and allowed file set.
    """
    return _read_history(package)


def _read_history(package: Path, pending_backup: Iteration | None = None) -> list[Iteration]:
    """Only the active finalizer may admit its exact displaced pending bytes; other readers refuse."""
    root = iterations_root(package)
    with _named_refusals():
        package_files, package_dirs = rev.tree_files(package)
        if "validation/iterations" in package_files:
            raise ReceiptError("EXTRA_FILE", "the iterations root is not a directory")
        if "validation/iterations" not in package_dirs:
            return []
        _, directories = rev.tree_files(root)
        names = sorted(key for key in directories if "/" not in key)
        if names != [f"{index:03d}" for index in range(1, len(names) + 1)] or len(names) > 999:
            raise ReceiptError(
                "ITERATION_GAP", "iteration directories must be contiguous canonical three-digit numbers"
            )
        if any(path.is_file() for path in root.iterdir()):
            raise ReceiptError("EXTRA_FILE", "the iterations root cannot contain loose files")
        chain: list[Iteration] = []
        for name in names:
            directory = root / name
            blob = (directory / RECEIPT_NAME).read_bytes()
            payload = validate_receipt(rev.parse_json_bytes(blob))
            if payload["iteration"] != name:
                raise ReceiptError("ITERATION_MISLABELLED", "the receipt name disagrees with its directory")
            previous = payload["generated"]["previous"]
            expected = {"iteration": chain[-1].name, "receipt_sha256": chain[-1].receipt_sha256} if chain else None
            if previous != expected:
                raise ReceiptError("PREVIOUS_RECEIPT_MISMATCH", "the receipt no longer pins its exact predecessor")
            _iteration_files(
                directory,
                payload,
                pending_backup.receipt_bytes if pending_backup and directory == pending_backup.directory else None,
            )
            if chain and chain[-1].payload["state"] != STATE_FINAL:
                raise ReceiptError("PREVIOUS_NOT_FINAL", "only a final receipt may have a successor")
            assert_iteration_progress(
                payload["generated"]["artifact"], chain[-1] if chain else None, version=payload["schema_version"]
            )
            if payload["state"] == STATE_FINAL:
                _assert_judgement(payload, chain[-1] if chain else None)
            chain.append(Iteration(name, directory, blob, payload))
        return chain


def checked_history(package: Path, previous_sha256: str | None) -> list[Iteration]:
    """Validate the predecessor token before preparation as well as before allocation."""
    chain = read_history(package)
    if chain:
        _require_pin(chain[-1].receipt_sha256, previous_sha256, "PREVIOUS_RECEIPT_MISMATCH")
        if chain[-1].payload["state"] != STATE_FINAL:
            raise ReceiptError("PREVIOUS_NOT_FINAL", "finalize the current iteration before allocating another")
    elif previous_sha256 is not None:
        raise ReceiptError("PREVIOUS_RECEIPT_MISMATCH", "the first iteration cannot name a predecessor")
    return chain


def assert_iteration_progress(artifact: dict[str, Any], previous: Iteration | None, *, version: int = 3) -> None:
    """V3 successors require a finding/revision association, not a claim that one caused the other."""
    if previous is None:
        return
    prior = previous.payload
    if version == 2:
        if prior["schema_version"] != 2:
            raise ReceiptError("SCHEMA_TRANSITION", "a v3 chain cannot return to v2")
        return
    changed = artifact != prior["generated"]["artifact"]
    if not changed and prior["schema_version"] == 2:
        return  # The single unchanged evidence-only upgrade; no downgrade can repeat it.
    if not changed:
        raise ReceiptError("ITERATION_UNCHANGED", "another v3 iteration requires an observable artifact revision")
    if not any(row["status"] == FINDING_OPEN for row in prior["judgement"]["findings"]):
        raise ReceiptError("ITERATION_WITHOUT_OPEN_FINDING", "a revised successor requires a predecessor open finding")


def allocate_iteration(package: Path, previous_sha256: str | None = None) -> tuple[Path, Iteration | None]:
    """Validate the caller-pinned entire chain before an exclusive allocation."""
    with _named_refusals():
        chain = checked_history(package, previous_sha256)
        if len(chain) >= 999:
            raise ReceiptError("ITERATION_LIMIT", "the three-digit iteration range is exhausted")
        root = iterations_root(package)
        root.mkdir(parents=True, exist_ok=True)
        directory = root / f"{len(chain) + 1:03d}"
        try:
            directory.mkdir(exist_ok=False)
        except FileExistsError as error:
            raise ReceiptError("ITERATION_NUMBER_TAKEN", "another producer allocated this number") from error
        return directory, chain[-1] if chain else None


def _assert_visual_status(status: str, page: dict[str, Any]) -> None:
    if status not in {"pass", "layout_match"}:
        return
    admitted = page["tableau"]
    if not page["powerbi"]["capture"]["converged"] or admitted is None:
        raise ReceiptError(
            "COMPARISON_EVIDENCE_MISSING", "a positive comparison needs admitted Tableau and stable PBI evidence"
        )
    if status == "pass" and admitted["grade"] != evidence.GRADE_VALIDATION:
        raise ReceiptError("COMPARISON_GRADE", "layout or text evidence cannot confer a full visual pass")
    if status == "layout_match" and admitted["grade"] not in {evidence.GRADE_VALIDATION, evidence.GRADE_ORACLE}:
        raise ReceiptError("COMPARISON_GRADE", "the admitted evidence does not support this comparison grade")


def _assert_findings(payload: dict[str, Any], previous: Iteration | None) -> None:
    findings = payload["judgement"]["findings"]
    current = {finding["id"]: finding for finding in findings}
    if len(current) != len(findings):
        raise ReceiptError("FINDING_ID_DUPLICATE", "finding identifiers must be unique")
    prior = {row["id"]: row for row in previous.payload["judgement"]["findings"]} if previous else {}
    if set(prior) - set(current):
        raise ReceiptError("FINDING_DISAPPEARED", "every prior finding must remain in the next iteration")
    inventory = {page["page_id"]: page["expected_visual_ids"] for page in payload["generated"]["pages"]}
    transitions = {
        FINDING_OPEN: {FINDING_OPEN, FINDING_RESOLVED, FINDING_ACCEPTED},
        FINDING_RESOLVED: {FINDING_RESOLVED},
        FINDING_ACCEPTED: {FINDING_ACCEPTED},
    }
    for key, row in current.items():
        if key in prior:
            if {field: value for field, value in row.items() if field != "status"} != {
                field: value for field, value in prior[key].items() if field != "status"
            }:
                raise ReceiptError(
                    "FINDING_IDENTITY_CHANGED", "a reused finding ID must preserve its complete identity"
                )
            if row["status"] not in transitions[prior[key]["status"]]:
                raise ReceiptError("FINDING_TRANSITION", "the finding lifecycle transition is not legal")
        elif row["status"] == FINDING_RESOLVED:
            raise ReceiptError("FINDING_TRANSITION", "a new finding cannot arrive already resolved")
        page, visual = row["page_id"], row["visual_id"]
        if key not in prior and (
            (page is not None and page not in inventory)
            or (visual is not None and visual not in inventory.get(page, []))
        ):
            raise ReceiptError("FINDING_TARGET", "a new finding must refer to the captured page and visual inventory")
        if row["status"] == FINDING_ACCEPTED and row["limitation_ref"] is None:
            raise ReceiptError(
                "ACCEPTED_LIMITATION_UNBOUND", "accepted limitations need their immutable spec reference"
            )


def _assert_result_findings(page_id: str, row: dict[str, Any], findings: dict[str, Any]) -> None:
    if len(set(row["finding_ids"])) != len(row["finding_ids"]):
        raise ReceiptError("FINDING_REFERENCE", "finding references must be unique")
    for key in row["finding_ids"]:
        finding = findings.get(key)
        if finding is None or finding["page_id"] != page_id or finding["visual_id"] not in (None, row["visual_id"]):
            raise ReceiptError("FINDING_REFERENCE", "a referenced finding must describe this page and visual")


def _assert_judgement(payload: dict[str, Any], previous: Iteration | None) -> None:
    generated, judgement = payload["generated"], payload["judgement"]
    if [row["page_id"] for row in judgement["pages"]] != [row["page_id"] for row in generated["pages"]]:
        raise ReceiptError("JUDGEMENT_PAGE_SET", "judgement must cover exactly the captured pages")
    _assert_findings(payload, previous)
    findings = {row["id"]: row for row in judgement["findings"]}
    for measured, judged in zip(generated["pages"], judgement["pages"]):
        if judged["whole_page_status"] == STATUS_PENDING:
            raise ReceiptError("PENDING_JUDGEMENT", "whole-page judgement remains pending")
        _assert_visual_status(judged["whole_page_status"], measured)
        for section in ("visual_results", "numeric_results"):
            if [row["visual_id"] for row in judged[section]] != measured["expected_visual_ids"]:
                raise ReceiptError("JUDGEMENT_VISUAL_SET", "judgement must cover exactly the captured visual inventory")
            for row in judged[section]:
                if section == "numeric_results" and payload["schema_version"] == 3:
                    _validate(_object(visual_id=TEXT, finding_ids=_array(TEXT)), row)
                    _assert_result_findings(measured["page_id"], row, findings)
                    continue
                if row["status"] == STATUS_PENDING:
                    raise ReceiptError("PENDING_JUDGEMENT", "a visual or numeric judgement remains pending")
                if section == "numeric_results":
                    if row["status"] != STATUS_UNVERIFIED or any(
                        row[key] is not None
                        for key in ("tableau_evidence_sha256", "powerbi_query_sha256", "powerbi_result_sha256")
                    ):
                        raise ReceiptError(
                            "NUMERIC_EVIDENCE_UNAVAILABLE",
                            "reviewer-authored hashes are not structured numeric producer evidence",
                        )
                else:
                    _assert_visual_status(row["status"], measured)
                _assert_result_findings(measured["page_id"], row, findings)


def limitation_entry_sha256(entry: Any) -> str:
    """Canonical spec-entry identity, unchanged over a finding's lifetime."""
    return hashlib.sha256(
        json.dumps(entry, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _assert_limitations(package: Path, payload: dict[str, Any]) -> None:
    bound = [row["limitation_ref"] for row in payload["judgement"]["findings"] if row["limitation_ref"] is not None]
    if not bound:
        return
    entries = read_strict_json(package / "migration-spec.json").get("limitations_encountered")
    for reference in bound:
        match = re.fullmatch(r"/limitations_encountered/(0|[1-9][0-9]*)", reference["pointer"])
        if not match or not isinstance(entries, list) or int(match[1]) >= len(entries):
            raise ReceiptError("ACCEPTED_LIMITATION_UNBOUND", "the finding's immutable spec reference does not resolve")
        if limitation_entry_sha256(entries[int(match[1])]) != reference["sha256"]:
            raise ReceiptError("ACCEPTED_LIMITATION_UNBOUND", "the referenced limitation entry changed")


def receipt_bytes(payload: dict[str, Any]) -> bytes:
    """The sole serialization, with finite JSON and ASCII escapes."""
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def receipt_sha256(payload: dict[str, Any]) -> str:
    """The producer-returned checksum to retain outside the mutable package."""
    return hashlib.sha256(receipt_bytes(payload)).hexdigest()


def write_receipt(directory: Path, payload: dict[str, Any]) -> str:
    """Validate and atomically persist only producer-built output."""
    validate_receipt(payload)
    with _named_refusals():
        staged = directory / ".iteration.writing"
        try:
            staged.write_bytes(receipt_bytes(payload))
            os.replace(staged, directory / RECEIPT_NAME)
        finally:
            staged.unlink(missing_ok=True)
    return receipt_sha256(payload)


def _assert_snapshot(package: Path, chain: list[Iteration], pending_backup: Iteration | None = None) -> None:
    selected, previous = chain[-1], chain[-2] if len(chain) > 1 else None
    generated = selected.payload["generated"]
    current = _current_generated(resolve_package(package), selected, previous)
    if current != generated or _read_history(package, pending_backup) != chain:
        raise ReceiptError("GENERATED_CHANGED", "the package or exact iteration chain no longer matches the receipt")


def _restore_pending(selected: Iteration) -> None:
    backup = selected.directory / PENDING_BACKUP_NAME
    destination = selected.directory / RECEIPT_NAME
    try:
        try:
            backup.read_bytes()
        except FileNotFoundError:
            # Cleanup retired the backup. Reconstruct the exact captured bytes, never the
            # reserialized payload or the now-untrusted published receipt.
            try:
                with backup.open("xb") as output:
                    output.write(selected.receipt_bytes)
            except OSError:
                if not backup.exists():
                    # If even marker creation fails, withdraw the final receipt into that
                    # deterministic non-authoritative path before reporting rollback failure.
                    os.replace(destination, backup)
                raise
        if backup.read_bytes() != selected.receipt_bytes:
            raise OSError("pending backup changed")
        os.replace(backup, destination)
    except OSError as error:
        # Retain whatever rollback evidence exists. Compound write failure can leave no marker:
        # read_chain still requires the producer's final token and an independent current snapshot.
        raise ReceiptError(
            "FINALIZATION_ROLLBACK_FAILED", "rollback failed; no authoritative final checksum was returned"
        ) from error


def _publish_final(package: Path, chain: list[Iteration], payload: dict[str, Any]) -> None:
    selected = chain[-1]
    blob = receipt_bytes(validate_receipt(payload))
    expected = [*chain[:-1], Iteration(selected.name, selected.directory, blob, payload)]
    destination = selected.directory / RECEIPT_NAME
    staged = selected.directory / ".iteration.writing"
    backup = selected.directory / PENDING_BACKUP_NAME
    staged_owned = displaced = published = False
    try:
        with staged.open("xb") as output:
            staged_owned = True
            output.write(blob)
        # Move the actual pending file, not a copy made before the swap. This retains any intervening
        # receipt replacement for comparison and leaves an ordinary reader fail-closed until commit.
        os.replace(destination, backup)
        displaced = True
        if backup.read_bytes() != selected.receipt_bytes:
            raise ReceiptError("CAPTURE_CHANGED", "the receipt changed before atomic publication")
        # Link is atomic and no-clobber on both supported hosts. A receipt recreated after displacement
        # must refuse, not be overwritten unnoticed by a second replace.
        os.link(staged, destination)
        published = True
        staged.unlink()
        staged_owned = False
        _assert_snapshot(package, expected, selected)
        backup.unlink()
        # This is the success boundary: all cleanup is over, and rollback still has the exact
        # pending bytes in selected. No filesystem or external operation may follow this snapshot.
        read_chain(package, expected[-1].receipt_sha256)
        return
    except BaseException as error:
        try:
            if displaced:
                _restore_pending(selected)
        finally:
            if staged_owned:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass  # Any residue also makes the ordinary exact-file-set reader refuse.
        if not isinstance(error, Exception):
            raise
        code = "FINALIZATION_CHANGED" if published else "FINALIZATION_WRITE_FAILED"
        raise ReceiptError(code, "final receipt not committed; pending receipt preserved") from error


def finalize(  # pylint: disable=too-many-locals
    package: Path,
    expected_sha256: str,
    judgement: dict[str, Any],
    iteration: str | None = None,
    *,
    state_reader: StatusReader = bridge_status,
) -> dict[str, Any]:
    """Finalize separate reviewer input against an immutable capture and every current identity."""
    with _named_refusals():
        target = resolve_package(package)
        root = iterations_root(package)
        if not root.is_dir():
            raise ReceiptError("NO_ITERATION", "no capture iteration exists")
        names = sorted(path.name for path in root.iterdir())
        name = iteration or (names[-1] if names else "")
        if not ITERATION_RE.fullmatch(name) or name != (names[-1] if names else None):
            raise ReceiptError("NO_ITERATION", "only the latest canonical iteration can be finalized")
        chain = read_history(package)
        if not chain or chain[-1].name != name:
            raise ReceiptError("NO_ITERATION", "the selected latest iteration changed during finalization")
        selected, previous = chain[-1], chain[-2] if len(chain) > 1 else None
        _require_pin(selected.receipt_sha256, expected_sha256, "CAPTURE_CHANGED")
        original = selected.payload
        if original["state"] != STATE_PENDING:
            raise ReceiptError("ALREADY_FINAL", "a final receipt is immutable")
        _validate(JUDGEMENT_SCHEMA if original["schema_version"] == 2 else V3_JUDGEMENT_SCHEMA, judgement)
        if judgement["completed_at"] is not None:
            raise ReceiptError("STATE_INVALID", "reviewer input cannot set producer completion time")
        generated = original["generated"]
        assert_desktop_binding(target, generated["review"]["desktop_pid"], state_reader)
        current = _current_generated(target, selected, previous)
        if current != generated:
            raise ReceiptError(
                "GENERATED_CHANGED", "the complete generated facts no longer match current package truth"
            )
        payload = {**original, "generated": current, "judgement": json.loads(json.dumps(judgement, allow_nan=False))}
        _assert_judgement(payload, previous)
        _assert_limitations(package, payload)
        assert_desktop_binding(target, generated["review"]["desktop_pid"], state_reader)
        payload["judgement"]["completed_at"] = now_rfc3339()
        payload["state"] = STATE_FINAL
        if payload["schema_version"] == 2:
            payload["outcome"] = OUTCOME_INCOMPLETE
        # No external PID/status/bridge call may follow this last pre-publication snapshot.
        _assert_snapshot(package, chain)
        _publish_final(package, chain, payload)
        return payload
