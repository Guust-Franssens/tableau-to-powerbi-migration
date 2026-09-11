"""
purpose: Trusted Phase-2 tool observations and lossless, reproducible numerical comparisons.
usage:   library only; collect_completion_evidence(package, EvidenceRequest(...)).

This is Slice A, NOT a completion gate, evidence-import CLI, or status registry. The caller must
retain producer-returned bytes/pins inside its existing iteration authority. Reading a schema-valid
observation is never proof of who produced it. Payloads contain customer data; keep them run-owned.
Opaque IDs and hashes in metadata are not anonymization of the underlying low-entropy values.

Native success is unreleased pending the independent cold-reopen/source-unavailable control.
The semantic witness uses TOM's complete model serialization, not names or a second TMDL parser.
Only exact supported serialization equality qualifies; unknown metadata/normalizations refuse.
Source attribution initially supports literal two-step Sql.Database navigation, one partition per
table. Parameters, custom SQL, local-file attribution, dual/hybrid partitions and other connectors
remain CANNOT_ESTABLISH. Mixed *tables* require each Import and DirectQuery source leg.

The existing Tableau CSV capture certifies bytes but records no filter/parameter/snapshot witness.
Consequently comparisons can be retained and recomputed, but their numerical-context eligibility
remains CANNOT_ESTABLISH. An input plan cannot supply the missing authority. No image is numeric
evidence and no general PBIR-to-DAX compiler is implied by the supported single-measure card case.
"""

from __future__ import annotations

# Booleans/floats are deliberately excluded from integer evidence at every wire boundary.
# pylint: disable=unidiomatic-typecheck

import csv
import hashlib
import importlib
import io
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import jsonschema

import check_connection_fidelity as connections
import check_field_bindings as fields
import current_artifact_revision as rev
import dax_oracle_server as dax
import package_filesystem as filesystem
import preflight_source_credentials as sources
import reference_evidence as reference
from tableau_oracle_manifest import withhold_uncertified_evidence
from tableau_payload_facts import CSV_CERTIFIED, certify_csv

VERSION = 1
COMPARISON_ALGORITHM = "typed-rows-v1"
DEFINITION_ALGORITHM = "tom-json-exact-v1"
SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "skills" / "pbip-model-refresh" / "scripts"
SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
REVISION = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
OPAQUE = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
GUID = {"type": "string", "pattern": "^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"}
ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"}
TEXT = {"type": "string", "minLength": 1, "maxLength": 500}
COUNT = {"type": "integer", "minimum": 1, "maximum": 2**53 - 1}
ROLE = {
    "type": "string",
    "pattern": "^evidence/[0-9a-f]{32}/(?:plan.json|query.dax|result.json|tableau.csv|"
    "tableau.canonical.json|result.canonical.json|binding.json)$",
}
REASONS = frozenset(
    {
        "OBSERVED",
        "INPUT_INVALID",
        "INPUT_CHANGED",
        "PRIVACY",
        "TOOL_UNAVAILABLE",
        "TIMEOUT",
        "IDENTITY_UNESTABLISHED",
        "PID_REUSED",
        "WRONG_PID_PORT",
        "CATALOGUE_UNESTABLISHED",
        "CATALOGUE_CHANGED",
        "DEFINITION_UNSUPPORTED",
        "DEFINITION_MISMATCH",
        "MODEL_CHANGED",
        "SOURCE_COVERAGE_MISSING",
        "CANARIES_REQUIRED",
        "CANARY_UNKNOWN",
        "NO_DATA",
        "FULL_REFRESH_REQUIRED",
        "PERSISTENCE_REQUIRED",
        "CACHE_CHANGED",
        "PERSISTENCE_FAILED",
        "CREDENTIAL_MISSING",
        "CREDENTIAL_UNKNOWN",
        "DIALOG_NEEDS_HUMAN",
        "DIALOG_UNREADABLE",
        "DIALOG_UNRECOGNIZED",
        "REFRESH_IN_PROGRESS",
        "DESKTOP_GONE",
        "DESKTOP_UNREADY",
        "ACCESS_DENIED",
        "CSV_UNCERTIFIED",
        "CSV_INVALID",
        "CSV_EMPTY",
        "CSV_IDENTITY",
        "CSV_HASH_MISMATCH",
        "SOURCE_REVISION_UNKNOWN",
        "PLAN_INVALID",
        "PLAN_CHANGED",
        "NUMERIC_COVERAGE_MISSING",
        "NUMERIC_CONTEXT_UNESTABLISHED",
        "QUERY_HASH_MISMATCH",
        "RESULT_HASH_MISMATCH",
        "RESULT_SCHEMA",
        "RESULT_TYPE",
        "RESULT_NONFINITE",
        "RESULT_TRUNCATED",
        "RESULT_COLUMNS",
        "QUERY_INVALID",
        "COMPARISON_MISMATCH",
        "EVIDENCE_INVALID",
    }
)


class EvidenceError(RuntimeError):
    """Fixed closed code only. Never serialize native exceptions or customer payload excerpts."""

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in REASONS else "INPUT_INVALID"
        super().__init__(self.code)


def _object(**properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items: dict, *, minimum: int = 0) -> dict:
    return {"type": "array", "items": items, "minItems": minimum}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


PAYLOAD_SCHEMA = _object(role=ROLE, sha256=SHA, byte_count=COUNT)
IMAGE_SCHEMA = _object(sha256=SHA, byte_count=COUNT)
BINDING_SCHEMA = _object(
    pid={"type": "integer", "minimum": 1, "maximum": 2**32 - 1},
    process_start={"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
    as_pid={"type": "integer", "minimum": 1, "maximum": 2**32 - 1},
    as_process_start={"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
    port={"type": "integer", "minimum": 1, "maximum": 65535},
    catalogue=GUID,
)
CANARY_SCHEMA = _object(
    id=OPAQUE,
    source_key={"type": "string", "pattern": "^source-key:[0-9a-f]{16}$"},
    mode={"enum": ["import", "directQuery"]},
    binding=PAYLOAD_SCHEMA,
    query=PAYLOAD_SCHEMA,
    result=PAYLOAD_SCHEMA,
    sampled_rows=COUNT,
    total_rows={"type": "null"},
)
PERSISTENCE_SCHEMA = {
    "oneOf": [
        _object(
            status={"const": "PERSISTED"},
            method={"const": "AMO_ImageSave"},
            catalogue=GUID,
            compatibility_level={"type": "integer", "minimum": 1200},
            model_revision=REVISION,
            intended=IMAGE_SCHEMA,
            committed=IMAGE_SCHEMA,
        ),
        _object(status={"const": "NOT_APPLICABLE"}, reason={"const": "PURE_DIRECTQUERY"}),
    ]
}
REFRESH_SCHEMA = _object(
    catalogue=GUID,
    refresh_type={"const": "full"},
    scope={"const": "database"},
    tables={"type": "array", "maxItems": 0},
)
FAILURE_SCHEMA = _object(
    schema_version={"const": VERSION},
    status={"const": "CANNOT_ESTABLISH"},
    code={"enum": sorted(REASONS - {"OBSERVED"})},
)
DATA_SCHEMA = {
    "oneOf": [
        FAILURE_SCHEMA,
        _object(
            schema_version={"const": VERSION},
            status={"const": "DATA_OK"},
            code={"const": "OBSERVED"},
            binding=BINDING_SCHEMA,
            model_revision=REVISION,
            spec_sha256=SHA,
            definition=_object(algorithm={"const": DEFINITION_ALGORITHM}, sha256=SHA),
            storage_modes={
                "type": "array",
                "items": {"enum": ["import", "directQuery"]},
                "minItems": 1,
                "uniqueItems": True,
            },
            canaries=_array(CANARY_SCHEMA, minimum=1),
            refresh=_nullable(REFRESH_SCHEMA),
            persistence=PERSISTENCE_SCHEMA,
        ),
    ]
}
DECIMAL_INPUT = {"type": "string", "maxLength": 100, "pattern": "^-?[0-9]+(?:\\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?$"}
MAPPING_SCHEMA = _object(
    source=TEXT,
    target=TEXT,
    kind={"enum": sorted(dax.VALUE_KINDS - {"blank", "datetime"})},
    scale=DECIMAL_INPUT,
    blank={"enum": ["empty", "forbid"]},
)
CASE_SCHEMA = _object(
    id=ID,
    page_id=ID,
    visual_id=ID,
    projection=TEXT,
    view_luid=GUID,
    view_kind={"enum": ["worksheet", "dashboard"]},
    metric=_object(entity=TEXT, property=TEXT, kind={"const": "Measure"}),
    columns=_array(MAPPING_SCHEMA, minimum=1),
    grain=_array(TEXT),
    context=_object(
        kpi=TEXT,
        period=TEXT,
        filters=_array(_object(field=TEXT, value=TEXT)),
        parameters=_array(_object(name=TEXT, value=TEXT)),
    ),
    normalization=_object(
        row_order={"enum": ["ordered", "unordered"]}, absolute_tolerance=DECIMAL_INPUT, relative_tolerance=DECIMAL_INPUT
    ),
)
PLAN_SCHEMA = _object(schema_version={"const": VERSION}, cases=_array(CASE_SCHEMA, minimum=1))
COMPARISON_SCHEMA = _object(
    algorithm={"const": COMPARISON_ALGORITHM},
    outcome={"enum": ["match", "mismatch"]},
    compared_rows={"type": "integer", "minimum": 0},
)
CASE_RESULT_SCHEMA = _object(
    id=ID,
    page_id=ID,
    visual_id=ID,
    plan=PAYLOAD_SCHEMA,
    manifest=IMAGE_SCHEMA,
    source_revision=SHA,
    view_luid=GUID,
    view_kind={"enum": ["worksheet", "dashboard"]},
    original=PAYLOAD_SCHEMA,
    query=PAYLOAD_SCHEMA,
    result=PAYLOAD_SCHEMA,
    source_canonical=PAYLOAD_SCHEMA,
    result_canonical=PAYLOAD_SCHEMA,
    model_revision=REVISION,
    catalogue=GUID,
    comparison=COMPARISON_SCHEMA,
)
NUMERIC_SCHEMA = {
    "oneOf": [
        FAILURE_SCHEMA,
        _object(
            schema_version={"const": VERSION},
            status={"const": "CANNOT_ESTABLISH"},
            code={"const": "NUMERIC_CONTEXT_UNESTABLISHED"},
            cases=_array(CASE_RESULT_SCHEMA, minimum=1),
        ),
    ]
}
OBSERVATION_SCHEMA = _object(schema_version={"const": VERSION}, data=DATA_SCHEMA, numeric=NUMERIC_SCHEMA)


def _validate(payload: dict, schema: dict, code: str) -> None:
    try:
        jsonschema.Draft202012Validator(schema).validate(payload)
    except (jsonschema.ValidationError, TypeError, ValueError):
        raise EvidenceError(code) from None


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _facts(blob: bytes) -> dict:
    return {"sha256": hashlib.sha256(blob).hexdigest(), "byte_count": len(blob)}


def _shareable(payload: Any) -> None:
    # Lazy import keeps the future iteration -> adapter call acyclic. This is the existing
    # central path/credential policy, not a new redaction grammar.
    from iteration_receipt import ReceiptError, assert_shareable  # pylint: disable=import-outside-toplevel

    try:
        assert_shareable(payload)
    except ReceiptError:
        raise EvidenceError("PRIVACY") from None


@dataclass(frozen=True)
class Payload:
    """Immutable LOCAL bytes and a generated package-relative role suggestion. Nothing is written."""

    role: str
    blob: bytes

    def facts(self) -> dict:
        """Shareable identity, not a substitute for retaining the original bytes."""
        return {"role": self.role, **_facts(self.blob)}


@dataclass(frozen=True)
class CollectedEvidence:
    """Producer return; Slice B must retain/pin both metadata and original local payloads."""

    observation: dict
    payloads: tuple[Payload, ...]

    def to_bytes(self) -> bytes:
        """Closed shareable metadata only; payload values never appear here."""
        validate_observation(self.observation)
        return _json_bytes(self.observation)


@dataclass(frozen=True)
class EvidenceRequest:
    """Inputs only. No caller-supplied catalogue, success flag, observed value, or expected scalar."""

    pid: int
    canaries: tuple[str, ...]
    authorize_refresh: bool = False
    port: int | None = None
    plan_role: str | None = None


def _payload(kind: str, blob: bytes, group: str | None = None) -> Payload:
    return Payload(f"evidence/{group or uuid.uuid4().hex}/{kind}", blob)


def _failure(code: str) -> dict:
    return {"schema_version": VERSION, "status": "CANNOT_ESTABLISH", "code": EvidenceError(code).code}


def validate_observation(payload: dict) -> None:
    """Validate shape/consistency only. Origin requires the enclosing producer's external pin."""
    _validate(payload, OBSERVATION_SCHEMA, "EVIDENCE_INVALID")
    _shareable(payload)
    _strict_integers(payload)
    data = payload["data"]
    if data["status"] == "DATA_OK":
        bindings = data["binding"]
        if bindings["pid"] == bindings["as_pid"] or int(bindings["as_process_start"]) < int(bindings["process_start"]):
            raise EvidenceError("EVIDENCE_INVALID")
        canaries = data["canaries"]
        for values in (
            [row["id"] for row in canaries],
            [(row["source_key"], row["mode"], row["binding"]["sha256"]) for row in canaries],
        ):
            if len(values) != len(set(values)):
                raise EvidenceError("EVIDENCE_INVALID")
        if not {row["mode"] for row in canaries} <= set(data["storage_modes"]):
            raise EvidenceError("EVIDENCE_INVALID")
        has_import = "import" in data["storage_modes"]
        persistence, refresh = data["persistence"], data["refresh"]
        if has_import:
            if refresh is None or persistence["status"] != "PERSISTED":
                raise EvidenceError("PERSISTENCE_REQUIRED")
            if (
                persistence["intended"] != persistence["committed"]
                or persistence["model_revision"] != data["model_revision"]
                or persistence["catalogue"] != bindings["catalogue"]
                or refresh["catalogue"] != bindings["catalogue"]
            ):
                raise EvidenceError("EVIDENCE_INVALID")
        elif persistence["status"] != "NOT_APPLICABLE" or refresh is not None:
            raise EvidenceError("EVIDENCE_INVALID")


def _strict_integers(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key
                in {
                    "schema_version",
                    "pid",
                    "as_pid",
                    "port",
                    "byte_count",
                    "sampled_rows",
                    "compatibility_level",
                    "compared_rows",
                }
                and type(child) is not int
            ):
                raise EvidenceError("EVIDENCE_INVALID")
            suffixes = {
                "query": "query.dax",
                "result": "result.json",
                "binding": "binding.json",
                "plan": "plan.json",
                "original": "tableau.csv",
                "source_canonical": "tableau.canonical.json",
                "result_canonical": "result.canonical.json",
            }
            if key in suffixes and isinstance(child, dict) and "role" in child:
                if not child["role"].endswith("/" + suffixes[key]):
                    raise EvidenceError("EVIDENCE_INVALID")
            _strict_integers(child)
    elif isinstance(value, list):
        for child in value:
            _strict_integers(child)


def read_observation(blob: bytes) -> dict:
    """Parse and validate the SAME caller-held bytes; never a completion-authority reader."""
    try:
        payload = rev.parse_json_bytes(blob)
    except rev.RevisionError:
        raise EvidenceError("EVIDENCE_INVALID") from None
    validate_observation(payload)
    return payload


def _decimal_input(text: str, *, nonnegative: bool = False) -> Decimal:
    try:
        number = Decimal(text)
    except (ArithmeticError, ValueError):
        raise EvidenceError("PLAN_INVALID") from None
    if not number.is_finite() or abs(number.adjusted()) > 1000 or (nonnegative and number < 0):
        raise EvidenceError("PLAN_INVALID")
    return number


@dataclass(frozen=True)
class ComparisonPlan:
    """Held original bytes, not a mutable dictionary that can widen tolerance after execution."""

    blob: bytes

    @property
    def cases(self) -> tuple[dict, ...]:
        """Fresh decoded cases so callers cannot mutate the pinned input behind its hash."""
        return tuple(rev.parse_json_bytes(self.blob)["cases"])


def read_plan(blob: bytes) -> ComparisonPlan:
    """Closed plan grammar: an input may map columns/context, never supply an observed result."""
    try:
        payload = rev.parse_json_bytes(blob)
    except rev.RevisionError:
        raise EvidenceError("PLAN_INVALID") from None
    _validate(payload, PLAN_SCHEMA, "PLAN_INVALID")
    if type(payload["schema_version"]) is not int:
        raise EvidenceError("PLAN_INVALID")
    cases = payload["cases"]
    if len({case["id"] for case in cases}) != len(cases):
        raise EvidenceError("PLAN_INVALID")
    targets = [(row["page_id"], row["visual_id"], row["projection"]) for row in cases]
    if len(targets) != len(set(targets)):
        raise EvidenceError("PLAN_INVALID")
    for case in cases:
        for key in ("source", "target"):
            names = [column[key].casefold() for column in case["columns"]]
            if len(set(names)) != len(names):
                raise EvidenceError("PLAN_INVALID")
        norm = case["normalization"]
        for key in ("absolute_tolerance", "relative_tolerance"):
            _decimal_input(norm[key], nonnegative=True)
        for column in case["columns"]:
            scale = _decimal_input(column["scale"])
            if scale == 0 or (column["kind"] not in {"int32", "int64", "decimal", "double"} and scale != 1):
                raise EvidenceError("PLAN_INVALID")
        if len(case["grain"]) != len(set(case["grain"])) or not set(case["grain"]) <= {
            column["source"] for column in case["columns"]
        }:
            raise EvidenceError("PLAN_INVALID")
    return ComparisonPlan(blob)


@dataclass(frozen=True)
class CertifiedCsv:
    """Original independent producer bytes plus their strictly read manifest record."""

    blob: bytes
    manifest_blob: bytes
    record: dict
    source_sha256: str


def _held_file(root: Path, role: str) -> bytes:
    try:
        if not filesystem.is_canonical_key(role):
            raise EvidenceError("INPUT_INVALID")
        files, _ = rev.tree_files(root)
        path = files.get(role)
        if path is None:
            raise EvidenceError("INPUT_INVALID")
        return path.read_bytes()
    except (rev.RevisionError, OSError, ValueError):
        raise EvidenceError("INPUT_INVALID") from None


def _assert_source_bytes(package: Path, identity: reference.UnitIdentity) -> None:
    files, _ = rev.tree_files(package)
    source_roles = [role for role, path in files.items() if path == identity.source_path]
    if len(source_roles) != 1:
        raise EvidenceError("CSV_IDENTITY")
    if _facts(_held_file(package, source_roles[0]))["sha256"] != identity.source_sha256:
        raise EvidenceError("INPUT_CHANGED")


def read_tableau_csv(  # pylint: disable=too-many-locals
    package: Path, identity: reference.UnitIdentity, manifest_role: str, view_luid: str, view_kind: str
) -> CertifiedCsv:
    """Use only the existing capture authority's walked certified CSV role and original SHA/size."""
    manifest_blob = _held_file(package, manifest_role)
    manifest = rev.parse_json_bytes(manifest_blob)
    records = manifest.get("views")
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise EvidenceError("CSV_INVALID")
    records = withhold_uncertified_evidence(records)
    matching = [row for row in records if row.get("view_luid") == view_luid]
    if len(matching) != 1 or matching[0].get("view_type") != view_kind or view_kind not in {"worksheet", "dashboard"}:
        raise EvidenceError("CSV_IDENTITY")
    record = matching[0]
    # WorkbookIdentity is the existing identity authority; require the LUID axis, not names.
    owner = reference.WorkbookIdentity.of(luid=record.get("workbook_luid"))
    if not identity.workbook_luid or record.get("workbook_luid") != identity.workbook_luid:
        raise EvidenceError("CSV_IDENTITY")
    if identity.workbook().attribute(owner).route != reference.WB_LUID:
        raise EvidenceError("CSV_IDENTITY")
    if identity.revision != reference.REVISION_CONFIRMED:
        raise EvidenceError("SOURCE_REVISION_UNKNOWN")
    _assert_source_bytes(package, identity)
    data = record.get("data")
    if not isinstance(data, dict) or data.get("status") != "ok" or data.get("certification") != CSV_CERTIFIED:
        raise EvidenceError("CSV_UNCERTIFIED")
    if data.get("response_framing") not in {"content_length", "chunked"} or data.get("content_encoding") != "identity":
        raise EvidenceError("CSV_UNCERTIFIED")
    role = data.get("path")
    if not isinstance(role, str) or not role.endswith(".csv") or not filesystem.is_canonical_key(role):
        raise EvidenceError("CSV_UNCERTIFIED")
    prefix = manifest_role.rsplit("/", 1)[0] if "/" in manifest_role else ""
    blob = _held_file(package, f"{prefix}/{role}" if prefix else role)
    if type(data.get("bytes")) is not int or data["bytes"] != len(blob) or data.get("sha256") != _facts(blob)["sha256"]:
        raise EvidenceError("CSV_HASH_MISMATCH")
    header, rows = _csv_rows(blob)
    if type(data.get("row_count")) is not int or data["row_count"] != len(rows) or data.get("columns") != header:
        raise EvidenceError("CSV_INVALID")
    if _held_file(package, manifest_role) != manifest_blob:
        raise EvidenceError("INPUT_CHANGED")
    return CertifiedCsv(blob, manifest_blob, record, identity.source_sha256)


def _csv_rows(blob: bytes) -> tuple[list[str], list[list[str]]]:
    if certify_csv(blob, "text/csv") != CSV_CERTIFIED:
        raise EvidenceError("CSV_INVALID")
    try:
        rows = list(csv.reader(io.StringIO(blob.decode("utf-8-sig")), strict=True))
    except (UnicodeError, csv.Error):
        raise EvidenceError("CSV_INVALID") from None
    if len(rows) < 2:
        raise EvidenceError("CSV_EMPTY")
    header = rows[0]
    if (
        not header
        or any(not name for name in header)
        or len({name.casefold() for name in header}) != len(header)
        or any(len(row) != len(header) for row in rows[1:])
    ):
        raise EvidenceError("CSV_INVALID")
    _shareable({"columns": header, "rows": rows[1:]})
    return header, rows[1:]


def _source_cell(text: str, mapping: dict) -> dax.TypedValue:  # pylint: disable=too-many-return-statements,too-many-branches
    kind = mapping["kind"]
    if text == "" and mapping["blank"] == "empty":
        return dax.TypedValue("blank", None)
    try:
        if kind in {"decimal", "double", "int32", "int64"}:
            if not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?", text):
                raise EvidenceError("RESULT_TYPE")
            if kind == "double":
                if _decimal_input(mapping["scale"]) != 1:
                    raise EvidenceError("NUMERIC_CONTEXT_UNESTABLISHED")
                return dax.typed_value(float(text), "double")
            value = Decimal(text)
            if not value.is_finite():
                raise EvidenceError("RESULT_NONFINITE")
            if len(value.as_tuple().digits) > 1000 or abs(value.adjusted()) > 1000:
                raise EvidenceError("RESULT_TYPE")
            with localcontext() as context:
                context.prec = 2200
                value *= _decimal_input(mapping["scale"])
            if kind in {"int32", "int64"}:
                if value != value.to_integral_value():
                    raise EvidenceError("RESULT_TYPE")
                return dax.typed_value(int(value), kind)
            return dax.typed_value(value, kind)
        if kind == "boolean":
            if text not in {"true", "false"}:
                raise EvidenceError("RESULT_TYPE")
            return dax.typed_value(text == "true")
        if kind == "date":
            cell = dax.TypedValue("date", text)
            dax._validate_cell(cell)  # pylint: disable=protected-access
            return cell
        return dax.typed_value(text, "string")
    except (ArithmeticError, ValueError):
        raise EvidenceError("RESULT_TYPE") from None


def normalize_source(blob: bytes, case: dict, query_hash: str) -> dax.TypedResult:
    """Normalize the original CSV deterministically under the pinned plan, retaining duplicates."""
    header, rows = _csv_rows(blob)
    columns = case["columns"]
    if {column["source"] for column in columns} != set(header):
        raise EvidenceError("NUMERIC_COVERAGE_MISSING")
    indices = [header.index(column["source"]) for column in columns]
    return dax.TypedResult(
        query_hash,
        tuple((column["target"], column["kind"]) for column in columns),
        tuple(tuple(_source_cell(row[index], column) for index, column in zip(indices, columns)) for row in rows),
    )


def normalize_result(result: dax.TypedResult, case: dict) -> dax.TypedResult:
    """Projection only; no implicit cast, row omission, rounding, DISTINCT or scalar extraction."""
    expected = [(column["target"], column["kind"]) for column in case["columns"]]
    if set(expected) != set(result.columns) or len(expected) != len(result.columns):
        raise EvidenceError("RESULT_COLUMNS")
    indices = [result.columns.index(column) for column in expected]
    return dax.TypedResult(
        result.query_sha256, tuple(expected), tuple(tuple(row[i] for i in indices) for row in result.rows)
    )


def _number(cell: dax.TypedValue) -> Decimal | None:
    if cell.kind in {"int32", "int64", "decimal"}:
        return Decimal(cell.value)
    if cell.kind == "double":
        return Decimal.from_float(float.fromhex(cell.value))
    return None


def _row_equal(left: tuple, right: tuple, absolute: Decimal, relative: Decimal) -> bool:
    for first, second in zip(left, right):
        if first.kind != second.kind:
            return False
        a, b = _number(first), _number(second)
        if a is None:
            if first != second:
                return False
        else:
            # Guard precision independently of the process Decimal context (usually only 28).
            with localcontext() as context:
                context.prec = 5000
                if abs(a - b) > max(absolute, relative * max(abs(a), abs(b))):
                    return False
    return True


def compare_results(left: dax.TypedResult, right: dax.TypedResult, case: dict) -> dict:
    """Versioned complete-row comparison. Unordered rows still preserve duplicate multiplicity.

    Tolerance with unordered rows is intentionally unsupported: sorting numerics then zipping is
    not a sound bipartite tolerance match. Refuse instead of inventing a heuristic matching engine.
    """
    left.to_bytes()
    right.to_bytes()
    normalization = case["normalization"]
    absolute = _decimal_input(normalization["absolute_tolerance"], nonnegative=True)
    relative = _decimal_input(normalization["relative_tolerance"], nonnegative=True)
    first, second = left.rows, right.rows
    if normalization["row_order"] == "unordered":
        if absolute or relative:
            raise EvidenceError("NUMERIC_CONTEXT_UNESTABLISHED")

        def order(row: tuple) -> tuple:
            return tuple((cell.kind, _number(cell) if _number(cell) is not None else cell.value or "") for cell in row)

        first, second = tuple(sorted(first, key=order)), tuple(sorted(second, key=order))
    match = (
        bool(first)
        and left.columns == right.columns
        and len(first) == len(second)
        and all(_row_equal(a, b, absolute, relative) for a, b in zip(first, second))
    )
    return {
        "algorithm": COMPARISON_ALGORITHM,
        "outcome": "match" if match else "mismatch",
        "compared_rows": min(len(first), len(second)),
    }


def recompute_comparison(case: dict, original: bytes, query: bytes, result_blob: bytes) -> tuple[dict, bytes, bytes]:
    """Recompute from retained original CSV + exact query/result, never from a serialized match."""
    query_hash = _facts(query)["sha256"]
    result = dax.read_typed_result(result_blob)
    if result.query_sha256 != query_hash:
        raise EvidenceError("QUERY_HASH_MISMATCH")
    left, right = normalize_source(original, case, query_hash), normalize_result(result, case)
    return compare_results(left, right, case), left.to_bytes(), right.to_bytes()


MODEL_KEYS = frozenset(
    {
        "name",
        "culture",
        "defaultMode",
        "defaultPowerBIDataSourceVersion",
        "tables",
        "relationships",
        "expressions",
        "roles",
        "cultures",
        "annotations",
        "dataAccessOptions",
        "discourageImplicitMeasures",
        "sourceQueryCulture",
        "dataSources",
    }
)


@dataclass(frozen=True)
class DefinitionWitness:
    """Private TOM model payload, semantically bound to the current disk revision."""

    model_blob: bytes
    model_revision: str

    @property
    def model(self) -> dict:
        """Return a fresh decoded snapshot, not a mutable definition behind a retained digest."""
        return rev.parse_json_bytes(self.model_blob)

    def facts(self) -> dict:
        """No definition text or connection metadata leaves the private witness."""
        return {"algorithm": DEFINITION_ALGORITHM, "sha256": _facts(self.model_blob)["sha256"]}


def compare_definitions(disk_blob: bytes, live_blob: bytes, model_revision: str) -> DefinitionWitness:
    """Exact TOM metadata equality including DAX, M, relationships, roles and culture instructions.

    Compare the held serializer bytes, not a JSON float round-trip. The same TOM serializer/options
    produce both sides; even an ordering/format difference refuses rather than guessing a semantic
    normalization. Lists, defaults, expressions, annotations and every nested property remain
    significant. There is no name-set, regex, or subset equality.
    Unknown root features are explicitly unsupported, even if a test supplies them on both sides.
    """
    disk, live = rev.parse_json_bytes(disk_blob), rev.parse_json_bytes(live_blob)
    for model in (disk, live):
        if (
            not set(model) <= MODEL_KEYS
            or not isinstance(model.get("tables"), list)
            or not model["tables"]
            or model.get("dataSources")  # curated/restricted provider metadata is outside the initial M-only subset
        ):
            raise EvidenceError("DEFINITION_UNSUPPORTED")
        for table in model["tables"]:
            if not isinstance(table, dict) or not isinstance(table.get("name"), str):
                raise EvidenceError("DEFINITION_UNSUPPORTED")
            if any(key in table for key in ("calculationGroup", "refreshPolicy", "detailRowsDefinition")):
                raise EvidenceError("DEFINITION_UNSUPPORTED")
    if disk_blob != live_blob:
        raise EvidenceError("DEFINITION_MISMATCH")
    return DefinitionWitness(disk_blob, model_revision)


def _skill_modules() -> tuple[Any, Any]:
    sys.path.insert(0, str(SKILL_SCRIPTS))
    pdq = importlib.import_module("probe_desktop_query")
    refresh = importlib.import_module("refresh_pbip_model")

    if any(Path(module.__file__).resolve().parent != SKILL_SCRIPTS for module in (pdq, refresh)):
        raise EvidenceError("TOOL_UNAVAILABLE")
    return pdq, refresh


def _native_definition(bound, model_dir: Path) -> DefinitionWitness:  # pylint: disable=too-many-locals
    pdq, refresh = _skill_modules()
    before = rev.model_revision(model_dir)
    pdq.recheck_bound(bound)
    early, _ = refresh.same_model(bound.identity.port, model_dir / ".pbi" / "cache.abf")
    if not early:
        raise EvidenceError("DEFINITION_MISMATCH")
    server_type = refresh._load_amo()  # pylint: disable=protected-access
    from Microsoft.AnalysisServices.Tabular import (  # pylint: disable=import-outside-toplevel,import-error
        JsonSerializer,
        SerializeOptions,
        TmdlSerializer,
    )

    options = SerializeOptions()
    options.IgnoreTimestamps = True
    options.IgnoreInferredObjects = True
    options.IgnoreInferredProperties = True
    options.IgnoreChildren = False
    options.IncludeRestrictedInformation = True
    server = server_type()
    server.Connect(f"Data Source=localhost:{bound.identity.port};Connect Timeout=30")
    try:
        catalogues = list(server.Databases)
        if len(catalogues) != 1 or str(catalogues[0].ID) != bound.catalogue:
            raise EvidenceError("CATALOGUE_CHANGED")
        disk = TmdlSerializer.DeserializeDatabaseFromFolder(str(model_dir / "definition"))
        disk_blob = str(JsonSerializer.SerializeObject(disk.Model, options)).encode("utf-8")
        live_blob = str(JsonSerializer.SerializeObject(catalogues[0].Model, options)).encode("utf-8")
        witness = compare_definitions(disk_blob, live_blob, before)
    finally:
        server.Disconnect()
    pdq.recheck_bound(bound)
    if rev.model_revision(model_dir) != before:
        raise EvidenceError("MODEL_CHANGED")
    return witness


@dataclass(frozen=True)
class SourceBinding:
    """Private source-table-partition attribution; names are retained only in the binding payload."""

    source_key: str
    table: str
    partition: str
    mode: str


def _m_literal(value: str) -> str:
    # The comparable subset admits literal strings only; M escape sequences are not decoded here.
    if not isinstance(value, str) or not value or any(char in value for char in '#"\r\n'):
        raise EvidenceError("SOURCE_COVERAGE_MISSING")
    return f'"{value}"'


def _supported_sql_expression(leg: dict, table: str, expression: str) -> bool:
    """Compare a closed whole-expression template, not a connector mention or host-name match."""
    if leg.get("class") not in {"sqlserver", "azure_sqldb", "azure_sql_dw", "azuresqldw"} or leg.get("port") not in (
        None,
        "",
    ):
        return False
    server = _m_literal(leg.get("server"))
    database = _m_literal(leg.get("database") or leg.get("dbname"))
    schema = _m_literal(leg.get("schema"))
    item = _m_literal(table)
    expected = (
        f"let\nSource = Sql.Database({server}, {database}),\n"
        f"Data = Source{{[Schema={schema}, Item={item}]}}[Data]\nin\nData"
    )

    # Strip indentation only; never whitespace within a string or expression, never a comment.
    def compact(text: str) -> str:
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())

    return connections.partition_provenance(expression) == frozenset({"Sql"}) and compact(expression) == compact(
        expected
    )


def source_bindings(  # pylint: disable=too-many-locals,too-many-branches
    spec: dict, witness: DefinitionWitness, canaries: tuple[str, ...]
) -> tuple[SourceBinding, ...]:
    """Require explicit current-source coverage; unsupported attribution cannot earn DATA_OK."""
    if (
        not isinstance(canaries, tuple)
        or not canaries
        or any(not isinstance(table, str) or not table for table in canaries)
        or len({table.casefold() for table in canaries}) != len(canaries)
    ):
        raise EvidenceError("CANARIES_REQUIRED")
    model = witness.model
    parts = []
    for table in model["tables"]:
        partitions = table.get("partitions")
        if not isinstance(partitions, list) or len(partitions) != 1:
            raise EvidenceError("SOURCE_COVERAGE_MISSING")
        part = partitions[0]
        source = part.get("source", {})
        if source.get("type") == "calculated":
            if table["name"] in canaries:
                raise EvidenceError("SOURCE_COVERAGE_MISSING")
            continue
        mode = part.get("mode", model.get("defaultMode"))
        if (
            mode not in {"import", "directQuery"}
            or source.get("type") != "m"
            or not isinstance(source.get("expression"), str)
        ):
            raise EvidenceError("SOURCE_COVERAGE_MISSING")
        parts.append(
            {"table": table["name"], "partition": part.get("name"), "mode": mode, "expression": source["expression"]}
        )
    data_sources = spec.get("data_sources")
    if not isinstance(data_sources, list) or not data_sources:
        raise EvidenceError("SOURCE_COVERAGE_MISSING")
    model_stub = connections.Model("", "", tuple(parts), "")
    all_parts, keys, bindings = set(), set(), []
    for index, source in enumerate(data_sources):
        connection = source.get("connection") or {}
        legs = connection.get("connections") or [connection]
        if len(legs) != 1:
            raise EvidenceError("SOURCE_COVERAGE_MISSING")
        classifications = sources._classify_legs(source, index)  # pylint: disable=protected-access
        key = classifications[0][0] if len(classifications) == 1 else ""
        if not re.fullmatch(r"source-key:[0-9a-f]{16}", key) or key in keys:
            raise EvidenceError("SOURCE_COVERAGE_MISSING")
        keys.add(key)
        names = connections._table_names(source)  # pylint: disable=protected-access
        matched, unmatched = connections._attribute((model_stub,), set(names))  # pylint: disable=protected-access
        if unmatched or not matched:
            raise EvidenceError("SOURCE_COVERAGE_MISSING")
        source_canaries = []
        for part in matched:
            identity = (part["table"], part["partition"])
            if identity in all_parts or not isinstance(part["partition"], str) or not part["partition"]:
                raise EvidenceError("SOURCE_COVERAGE_MISSING")
            all_parts.add(identity)
            if not _supported_sql_expression(legs[0], part["table"], part["expression"]):
                raise EvidenceError("SOURCE_COVERAGE_MISSING")
            if part["table"] in canaries:
                source_canaries.append(SourceBinding(key, part["table"], part["partition"], part["mode"]))
        # A mixed source must prove both legs, never let an Import cache certify its DQ table.
        if {part["mode"] for part in matched} != {row.mode for row in source_canaries}:
            raise EvidenceError("SOURCE_COVERAGE_MISSING")
        bindings.extend(source_canaries)
    if all_parts != {(part["table"], part["partition"]) for part in parts} or {row.table for row in bindings} != set(
        canaries
    ):
        raise EvidenceError("SOURCE_COVERAGE_MISSING")
    return tuple(bindings)


def _storage_modes(model: dict) -> list[str]:
    """All partitions count for applicability, including static/calculated Import materialization."""
    modes = set()
    for table in model["tables"]:
        for partition in table.get("partitions", []):
            mode = partition.get("mode", model.get("defaultMode"))
            if mode not in {"import", "directQuery"}:
                raise EvidenceError("SOURCE_COVERAGE_MISSING")
            modes.add(mode)
    if not modes:
        raise EvidenceError("SOURCE_COVERAGE_MISSING")
    return sorted(modes)


def _bind_card_case(target, case: dict) -> str:  # pylint: disable=too-many-locals
    inventory = rev.report_inventory(target.report_dir)
    page = next((row for row in inventory if row.page_id == case["page_id"]), None)
    if page is None or case["visual_id"] not in page.visual_ids:
        raise EvidenceError("NUMERIC_COVERAGE_MISSING")
    prefix = f"definition/pages/{case['page_id']}/"
    visual = rev.parse_json_bytes(_held_file(target.report_dir, prefix + f"visuals/{case['visual_id']}/visual.json"))
    page_json = rev.parse_json_bytes(_held_file(target.report_dir, prefix + "page.json"))
    report_json = rev.parse_json_bytes(_held_file(target.report_dir, "definition/report.json"))
    metric = case["metric"]
    context = case["context"]
    if (
        case["view_kind"] != "worksheet"
        or case["grain"]
        or context != {"kpi": metric["property"], "period": "all", "filters": [], "parameters": []}
    ):
        raise EvidenceError("NUMERIC_CONTEXT_UNESTABLISHED")
    if (
        any(document.get("filterConfig") for document in (visual, page_json, report_json))
        or visual.get("visual", {}).get("visualType") != "card"
        or visual.get("visualContainerObjects")
        or visual.get("visual", {}).get("objects")
    ):
        raise EvidenceError("NUMERIC_CONTEXT_UNESTABLISHED")
    expected_field = {
        "Measure": {"Expression": {"SourceRef": {"Entity": metric["entity"]}}, "Property": metric["property"]}
    }
    states = visual["visual"].get("query", {}).get("queryState")
    if not isinstance(states, dict) or set(states) != {"Values"}:
        raise EvidenceError("NUMERIC_COVERAGE_MISSING")
    projections = states["Values"].get("projections")
    if not isinstance(projections, list) or len(projections) != 1 or len(case["columns"]) != 1:
        raise EvidenceError("NUMERIC_COVERAGE_MISSING")
    projection = projections[0]
    if (
        projection.get("field"),
        projection.get("queryRef"),
        projection.get("active") is False,
        projection.get("displayName", metric["property"]),
        case["columns"][0]["target"],
    ) != (expected_field, case["projection"], False, metric["property"], "[value]"):
        raise EvidenceError("NUMERIC_COVERAGE_MISSING")
    model = fields.parse_model(target.model_dir)
    field = fields.FieldRef(kind="Measure", entity=metric["entity"], prop=metric["property"], file=target.report_dir)
    if (
        fields.resolve_reference(model, field)["status"] != "resolved"
        or metric["property"] not in model.table(metric["entity"]).measures
    ):
        raise EvidenceError("NUMERIC_COVERAGE_MISSING")
    entity, prop = metric["entity"].replace("'", "''"), metric["property"].replace("]", "]]")
    query = f"EVALUATE ROW(\"value\", '{entity}'[{prop}])"
    _shareable(query)
    return query


def _unit_identity(target) -> reference.UnitIdentity:
    if target.asset is None:
        raise EvidenceError("SOURCE_REVISION_UNKNOWN")
    source_sha = _facts(target.asset.read_bytes())["sha256"]
    blob = _held_file(target.root, "source-provenance.json")
    provenance = rev.parse_json_bytes(blob)
    inputs = provenance.get("inputs")
    if not isinstance(inputs, list):
        raise EvidenceError("SOURCE_REVISION_UNKNOWN")
    claims = [reference._stamped_origin(row) for row in inputs]  # pylint: disable=protected-access
    origins = [origin for claim in claims if claim is not None for digest, origin in [claim] if digest == source_sha]
    ids = {reference._identity_claim(origin) for origin in origins}  # pylint: disable=protected-access
    if not origins or len(ids) != 1 or None in ids:
        raise EvidenceError("SOURCE_REVISION_UNKNOWN")
    if any(reference.revision_status(origin, inputs) != reference.REVISION_CONFIRMED for origin in origins):
        raise EvidenceError("SOURCE_REVISION_UNKNOWN")
    return reference.UnitIdentity(target.unit, target.asset, source_sha, ids.pop(), reference.REVISION_CONFIRMED)


class _Runtime:
    """Internal native seam, not caller-supplied evidence. All connections use one bound catalogue."""

    def __init__(self) -> None:
        self.pdq, self.refresh_tool = _skill_modules()

    def bind(self, request: EvidenceRequest):
        """Bind exact caller PID/start/child listener/catalogue."""
        return self.pdq.bind_desktop(request.pid, request.port)

    def guard(self, bound) -> None:
        """Recheck the actual process and catalogue."""
        self.pdq.evidence_call(bound.identity.pid, lambda: self.pdq.recheck_bound(bound))

    def witness(self, bound, model_dir: Path) -> DefinitionWitness:
        """Compare all supported semantic metadata using the installed TOM serializer."""
        return self.pdq.evidence_call(bound.identity.pid, lambda: _native_definition(bound, model_dir))

    def execute(self, bound, query: str) -> dax.TypedResult:
        """Execute the actual bound query; never accept caller results."""

        def call():
            connection = self.pdq.open_bound(bound)
            try:
                result = dax.execute_typed(connection, query)
                self.pdq.recheck_bound(bound, connection)
                return result
            finally:
                connection.Close()

        return self.pdq.evidence_call(bound.identity.pid, call)

    def probe(self, bound, canaries: tuple[str, ...]) -> tuple:
        """Explicit samples, not total counts or a legacy console verdict."""

        def call():
            connection = self.pdq.open_bound(bound)
            try:
                return self.pdq.probe_observations(
                    bound,
                    connection,
                    list(canaries),
                    lambda query: dax.execute_typed(connection, query),
                )
            finally:
                connection.Close()

        return self.pdq.evidence_call(bound.identity.pid, call)

    def refresh(self, bound):
        """Preserve the existing full-refresh absolute timer and credential routing."""
        observations = []
        ok, _ = self.refresh_tool.refresh(
            bound.identity.port,
            None,
            desktop_pid=bound.identity.pid,
            bound=bound,
            observations=observations,
        )
        if not ok or len(observations) != 1:
            raise EvidenceError("FULL_REFRESH_REQUIRED")
        return observations[0]

    def persist(self, bound, model_dir: Path, expected_revision: str):
        """No UI fallback. A timeout revokes authorization for a later staged-file commit."""
        observations = []
        deadline = time.monotonic() + self.pdq.EVIDENCE_TIMEOUT_SECONDS
        if rev.model_revision(model_dir) != expected_revision:
            raise EvidenceError("MODEL_CHANGED")
        baseline = _model_bytes(model_dir)
        if rev.model_revision(model_dir) != expected_revision:
            raise EvidenceError("MODEL_CHANGED")

        def permitted() -> None:
            if time.monotonic() >= deadline:
                raise EvidenceError("TIMEOUT")
            self.pdq.recheck_bound(bound)
            _native_definition(bound, model_dir)
            if time.monotonic() >= deadline:
                raise EvidenceError("TIMEOUT")

        def revision() -> str:
            _assert_alignment_only(baseline, model_dir, self.refresh_tool)
            return _native_definition(bound, model_dir).model_revision

        def call():
            result = self.refresh_tool.image_save(
                bound.identity.port,
                model_dir / ".pbi" / "cache.abf",
                model_dir,
                bound=bound,
                on_persist=observations.append,
                revision_reader=revision,
                before_write=permitted,
            )
            if result[0] is not True or len(observations) != 1:
                raise EvidenceError("PERSISTENCE_FAILED")
            return observations[0]

        try:
            return self.pdq.evidence_call(bound.identity.pid, call)
        finally:
            deadline = 0  # a late ImageSave may finish writing staging, but must never commit after refusal


def _model_bytes(model_dir: Path) -> dict[str, bytes]:
    files, _ = rev.tree_files(model_dir)
    return {role: path.read_bytes() for role, path in files.items() if role != ".pbi/cache.abf"}


def _assert_alignment_only(before: dict[str, bytes], model_dir: Path, refresh_tool) -> None:
    after = _model_bytes(model_dir)
    role = "definition/database.tmdl"
    if set(before) != set(after) or any(before[key] != after[key] for key in before if key != role):
        raise EvidenceError("MODEL_CHANGED")
    declared, _ = refresh_tool.read_declared_compatibility(model_dir)
    if declared is None or role not in before:
        raise EvidenceError("DEFINITION_UNSUPPORTED")
    expected = refresh_tool._COMPAT_RE.sub(  # pylint: disable=protected-access
        lambda match: f"{match.group('indent')}compatibilityLevel: {declared}", before[role].decode("utf-8"), count=1
    ).encode("utf-8")
    if after[role] != expected:
        raise EvidenceError("MODEL_CHANGED")


def _bound_metadata(bound) -> dict:
    return {
        "pid": bound.identity.pid,
        "process_start": bound.identity.process_start,
        "as_pid": bound.identity.as_pid,
        "as_process_start": bound.identity.as_process_start,
        "port": bound.identity.port,
        "catalogue": bound.catalogue,
    }


def _canary_payloads(binding: SourceBinding, observation) -> tuple[dict, tuple[Payload, ...]]:
    query = observation.query.encode("utf-8")
    if observation.result.query_sha256 != _facts(query)["sha256"]:
        raise EvidenceError("QUERY_HASH_MISMATCH")
    expected_query = f"EVALUATE TOPN(1, '{binding.table.replace(chr(39), chr(39) * 2)}')"
    if observation.query != expected_query or observation.table != binding.table or observation.total_rows is not None:
        raise EvidenceError("SOURCE_COVERAGE_MISSING")
    result = observation.result.to_bytes()
    if (
        type(observation.sampled_rows) is not int
        or observation.sampled_rows != observation.result.row_count
        or observation.sampled_rows <= 0
    ):
        raise EvidenceError("NO_DATA")
    _shareable(rev.parse_json_bytes(result))
    _shareable(observation.query)
    group = uuid.uuid4().hex
    payloads = (
        _payload(
            "binding.json",
            _json_bytes(
                {
                    "table": binding.table,
                    "partition": binding.partition,
                    "source_key": binding.source_key,
                    "mode": binding.mode,
                }
            ),
            group,
        ),
        _payload("query.dax", query, group),
        _payload("result.json", result, group),
    )
    return {
        "id": group,
        "source_key": binding.source_key,
        "mode": binding.mode,
        "binding": payloads[0].facts(),
        "query": payloads[1].facts(),
        "result": payloads[2].facts(),
        "sampled_rows": observation.sampled_rows,
        "total_rows": None,
    }, payloads


def _data_observation(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    target, request: EvidenceRequest, runtime
) -> tuple[dict, tuple[Payload, ...], Any]:
    spec_blob = _held_file(target.root, "migration-spec.json")
    spec = rev.parse_json_bytes(spec_blob)
    bound = runtime.bind(request)
    if bound.identity.pid != request.pid or (request.port is not None and bound.identity.port != request.port):
        raise EvidenceError("WRONG_PID_PORT")
    witness = runtime.witness(bound, target.model_dir)
    if witness.model_revision != rev.model_revision(target.model_dir):
        raise EvidenceError("MODEL_CHANGED")
    bindings = source_bindings(spec, witness, request.canaries)
    storage = _storage_modes(witness.model)
    has_import = "import" in storage
    refreshed, persisted = None, None
    if has_import:
        if request.authorize_refresh is not True:
            raise EvidenceError("FULL_REFRESH_REQUIRED")
        refreshed = runtime.refresh(bound)
        if (refreshed.catalogue, refreshed.refresh_type, refreshed.scope, refreshed.tables) != (
            bound.catalogue,
            "full",
            "database",
            (),
        ):
            raise EvidenceError("FULL_REFRESH_REQUIRED")
        current = runtime.witness(bound, target.model_dir)
        if current != witness:
            raise EvidenceError("MODEL_CHANGED")
    observations = runtime.probe(bound, request.canaries)
    if len(observations) != len(bindings) or {row.table for row in observations} != set(request.canaries):
        raise EvidenceError("SOURCE_COVERAGE_MISSING")
    canaries, payloads = [], []
    for binding in bindings:
        row = next(observation for observation in observations if observation.table == binding.table)
        metadata, local = _canary_payloads(binding, row)
        canaries.append(metadata)
        payloads.extend(local)
    if has_import:
        persisted = runtime.persist(bound, target.model_dir, witness.model_revision)
        if persisted.catalogue != bound.catalogue or persisted.method != "AMO_ImageSave":
            raise EvidenceError("CATALOGUE_CHANGED")
    final = runtime.witness(bound, target.model_dir)
    if final.model_blob != witness.model_blob:
        raise EvidenceError("DEFINITION_MISMATCH")
    if not has_import and final != witness:
        raise EvidenceError("MODEL_CHANGED")
    pdq, refresh_tool = _skill_modules()
    del pdq
    verdict = refresh_tool.derive_data_verdict(
        [(row.table, row.sampled_rows) for row in observations],
        False,
        wanted_save=has_import,
        commit=persisted.image if persisted else None,
    )
    if verdict.code != "DATA_OK":
        raise EvidenceError("NO_DATA" if verdict.code == "NO_DATA" else "PERSISTENCE_REQUIRED")
    persistence = {"status": "NOT_APPLICABLE", "reason": "PURE_DIRECTQUERY"}
    if persisted:
        if persisted.model_revision != final.model_revision:
            raise EvidenceError("MODEL_CHANGED")
        cache = _held_file(target.model_dir, ".pbi/cache.abf")
        committed = {"sha256": persisted.image.committed.sha256, "byte_count": persisted.image.committed.byte_count}
        if _facts(cache) != committed or persisted.image.intended != persisted.image.committed:
            raise EvidenceError("CACHE_CHANGED")
        persistence = {
            "status": "PERSISTED",
            "method": persisted.method,
            "catalogue": persisted.catalogue,
            "compatibility_level": persisted.compatibility_level,
            "model_revision": final.model_revision,
            "intended": {"sha256": persisted.image.intended.sha256, "byte_count": persisted.image.intended.byte_count},
            "committed": committed,
        }
    runtime.guard(bound)
    if (
        rev.model_revision(target.model_dir) != final.model_revision
        or _held_file(target.root, "migration-spec.json") != spec_blob
    ):
        raise EvidenceError("MODEL_CHANGED")
    return (
        {
            "schema_version": VERSION,
            "status": "DATA_OK",
            "code": "OBSERVED",
            "binding": _bound_metadata(bound),
            "model_revision": final.model_revision,
            "spec_sha256": _facts(spec_blob)["sha256"],
            "definition": final.facts(),
            "storage_modes": storage,
            "canaries": canaries,
            "refresh": {
                "catalogue": refreshed.catalogue,
                "refresh_type": refreshed.refresh_type,
                "scope": refreshed.scope,
                "tables": list(refreshed.tables),
            }
            if refreshed
            else None,
            "persistence": persistence,
        },
        tuple(payloads),
        bound,
    )


def _comparison_observation(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    target, case: dict, plan: Payload, runtime, bound, identity
) -> tuple[dict, tuple[Payload, ...]]:
    query = _bind_card_case(target, case)
    original = read_tableau_csv(
        target.root, identity, "oracle/oracle-manifest.json", case["view_luid"], case["view_kind"]
    )
    revision = rev.model_revision(target.model_dir)
    result = runtime.execute(bound, query).to_bytes()
    _shareable(rev.parse_json_bytes(result))
    comparison, left, right = recompute_comparison(case, original.blob, query.encode("utf-8"), result)
    group = uuid.uuid4().hex
    payloads = tuple(
        _payload(kind, blob, group)
        for kind, blob in (
            ("tableau.csv", original.blob),
            ("query.dax", query.encode("utf-8")),
            ("result.json", result),
            ("tableau.canonical.json", left),
            ("result.canonical.json", right),
        )
    )
    reread = read_tableau_csv(
        target.root, identity, "oracle/oracle-manifest.json", case["view_luid"], case["view_kind"]
    )
    if reread != original or rev.model_revision(target.model_dir) != revision:
        raise EvidenceError("INPUT_CHANGED")
    return {
        "id": case["id"],
        "page_id": case["page_id"],
        "visual_id": case["visual_id"],
        "plan": plan.facts(),
        "manifest": _facts(original.manifest_blob),
        "source_revision": original.source_sha256,
        "view_luid": case["view_luid"],
        "view_kind": case["view_kind"],
        "original": payloads[0].facts(),
        "query": payloads[1].facts(),
        "result": payloads[2].facts(),
        "source_canonical": payloads[3].facts(),
        "result_canonical": payloads[4].facts(),
        "model_revision": revision,
        "catalogue": bound.catalogue,
        "comparison": comparison,
    }, payloads


def _numeric_observation(
    target, request, runtime, bound, plan: ComparisonPlan | None
) -> tuple[dict, tuple[Payload, ...]]:
    if plan is None or bound is None:
        return _failure("NUMERIC_COVERAGE_MISSING"), ()
    identity = _unit_identity(target)
    plan_payload = _payload("plan.json", plan.blob)
    payloads, cases = [plan_payload], []
    for case in plan.cases:
        result, local = _comparison_observation(target, case, plan_payload, runtime, bound, identity)
        cases.append(result)
        payloads.extend(local)
    if _held_file(target.root, request.plan_role) != plan.blob:
        raise EvidenceError("PLAN_CHANGED")
    # No source-state witness is emitted by capture_tableau_oracle today. A plan is NOT one.
    return {
        "schema_version": VERSION,
        "status": "CANNOT_ESTABLISH",
        "code": "NUMERIC_CONTEXT_UNESTABLISHED",
        "cases": cases,
    }, tuple(payloads)


def _safe_error(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in REASONS - {"OBSERVED"}:
        return code
    exception_codes = {
        "CredentialMissingError": "CREDENTIAL_MISSING",
        "CredentialUnknownError": "CREDENTIAL_UNKNOWN",
        "DesktopGoneError": "DESKTOP_GONE",
        "DesktopUnreadyError": "DESKTOP_UNREADY",
        "CompatRollbackError": "PERSISTENCE_FAILED",
    }
    if type(error).__name__ in exception_codes:
        return exception_codes[type(error).__name__]
    if isinstance(error, PermissionError):
        return "ACCESS_DENIED"
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    return "TOOL_UNAVAILABLE"


def collect_completion_evidence(  # pylint: disable=too-many-locals
    package: Path, request: EvidenceRequest, *, _runtime=None
) -> CollectedEvidence:
    """Collect from actual tools, leaving every unknown explicit. No writes without authorization.

    The internal runtime seam is for direct tests, not a CLI input or persisted evidence importer.
    No native success is claimed by tests using it. All local payloads remain in memory for the
    existing iteration producer to retain; this adapter never invents an iteration/registry.
    """
    from iteration_receipt import assert_desktop_binding, resolve_package  # pylint: disable=import-outside-toplevel

    payloads = []
    try:
        if (
            not isinstance(request, EvidenceRequest)
            or type(request.pid) is not int
            or request.pid <= 0
            or type(request.authorize_refresh) is not bool
        ):
            raise EvidenceError("INPUT_INVALID")
        target = resolve_package(package)
        plan = read_plan(_held_file(target.root, request.plan_role)) if request.plan_role is not None else None
        # Pin the input plan BEFORE any operation, including an authorized refresh.
        assert_desktop_binding(target, request.pid)
        runtime = _runtime if _runtime is not None else _Runtime()
        data, local, bound = _data_observation(target, request, runtime)
        payloads.extend(local)
        baseline = rev.package_working_revision(target.root, target.model_dir)
        try:
            numeric, local = _numeric_observation(target, request, runtime, bound, plan)
            payloads.extend(local)
        except Exception as error:  # pylint: disable=broad-exception-caught
            numeric = _failure(_safe_error(error))
        current = runtime.witness(bound, target.model_dir)
        if current.model_revision != data["model_revision"] or current.facts() != data["definition"]:
            raise EvidenceError("MODEL_CHANGED")
        runtime.guard(bound)
        assert_desktop_binding(target, request.pid)
        if (
            resolve_package(package) != target
            or rev.package_working_revision(target.root, target.model_dir) != baseline
        ):
            raise EvidenceError("INPUT_CHANGED")
        if _facts(_held_file(target.root, "migration-spec.json"))["sha256"] != data["spec_sha256"]:
            raise EvidenceError("INPUT_CHANGED")
        if data["persistence"]["status"] == "PERSISTED":
            cache = _held_file(target.model_dir, ".pbi/cache.abf")
            if _facts(cache) != data["persistence"]["committed"]:
                raise EvidenceError("CACHE_CHANGED")
        observation = {"schema_version": VERSION, "data": data, "numeric": numeric}
        validate_observation(observation)
        return CollectedEvidence(observation, tuple(payloads))
    except Exception as error:  # pylint: disable=broad-exception-caught
        code = _safe_error(error)
        return CollectedEvidence({"schema_version": VERSION, "data": _failure(code), "numeric": _failure(code)}, ())


def verify_payloads(observation: dict, payloads: tuple[Payload, ...]) -> None:  # pylint: disable=too-many-locals
    """Strict same-byte role/hash/read/recompute check for the later pinned receipt reader.

    This proves integrity/consistency, never origin. It does not authorize completion, re-query a
    model, or substitute for Slice B's producer token and current-artifact rechecks.
    """
    validate_observation(observation)
    by_role = {payload.role: payload.blob for payload in payloads}
    if len(by_role) != len(payloads):
        raise EvidenceError("EVIDENCE_INVALID")
    expected = {}

    def visit(value):
        if isinstance(value, dict):
            if set(value) == {"role", "sha256", "byte_count"}:
                role = value["role"]
                if role in expected and expected[role] != value:
                    raise EvidenceError("EVIDENCE_INVALID")
                expected[role] = value
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(observation)
    if set(expected) != set(by_role):
        raise EvidenceError("EVIDENCE_INVALID")
    for role, facts in expected.items():
        if _facts(by_role[role]) != {"sha256": facts["sha256"], "byte_count": facts["byte_count"]}:
            code = "QUERY_HASH_MISMATCH" if role.endswith("query.dax") else "RESULT_HASH_MISMATCH"
            raise EvidenceError(code)
    for row in observation["data"].get("canaries", []):
        query = by_role[row["query"]["role"]]
        result = dax.read_typed_result(by_role[row["result"]["role"]])
        binding = rev.parse_json_bytes(by_role[row["binding"]["role"]])
        if set(binding) != {"table", "partition", "source_key", "mode"} or any(
            not isinstance(value, str) or not value for value in binding.values()
        ):
            raise EvidenceError("EVIDENCE_INVALID")
        _shareable(binding)
        _shareable(rev.parse_json_bytes(result.to_bytes()))
        expected_query = f"EVALUATE TOPN(1, '{binding['table'].replace(chr(39), chr(39) * 2)}')".encode()
        if query != expected_query or result.query_sha256 != _facts(query)["sha256"]:
            raise EvidenceError("QUERY_HASH_MISMATCH")
        if (
            result.row_count != row["sampled_rows"]
            or binding["source_key"] != row["source_key"]
            or binding["mode"] != row["mode"]
        ):
            raise EvidenceError("EVIDENCE_INVALID")
    for row in observation["numeric"].get("cases", []):
        plan = read_plan(by_role[row["plan"]["role"]])
        cases = [case for case in plan.cases if case["id"] == row["id"]]
        if len(cases) != 1:
            raise EvidenceError("PLAN_INVALID")
        comparison, left, right = recompute_comparison(
            cases[0], by_role[row["original"]["role"]], by_role[row["query"]["role"]], by_role[row["result"]["role"]]
        )
        if (
            comparison != row["comparison"]
            or left != by_role[row["source_canonical"]["role"]]
            or right != by_role[row["result_canonical"]["role"]]
        ):
            raise EvidenceError("COMPARISON_MISMATCH")
