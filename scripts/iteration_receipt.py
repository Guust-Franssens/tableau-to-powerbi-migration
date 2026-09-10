#!/usr/bin/env python
"""
purpose: Strict schema, canonical allocation and finalization for a package-local review iteration.
usage:   library, no CLI. Driven by `scripts/capture_powerbi_pages.py iterate|finalize`.

What one iteration is
---------------------
``<package>/validation/iterations/<NNN>/`` holding retained stable page PNGs and exactly one
``iteration.json``. The receipt has two halves that must never be confused:

* ``generated`` - produced HERE from the CURRENT package bytes (report/model revisions, PBIR page
  and visual inventory, screenshot hashes, Tableau evidence re-derived through
  ``reference_evidence``, predecessor linkage). Capture output is never its own denominator: the
  page set comes from ``definition/pages``, not from whatever PNGs happen to exist.
* ``judgement`` - the reviewer's verdicts, generated PENDING and constrained to a closed vocabulary.
  The producer never turns ``pending`` or ``unverified`` into ``pass``.

Finalization re-derives every generated identity immediately before writing, so an edit to the
report, the model, the cache, a retained screenshot or a prior receipt makes the iteration stale
rather than quietly authoritative.

This is EVIDENCE PRODUCTION, not the phase-2 verdict. The aggregator that reads a chain of these and
decides whether a unit is done is a separate, later slice (issue #363, slice B).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import current_artifact_revision as rev
import host_paths
import object_identity as oid
from reference_evidence import (
    Evidence,
    UnitIdentity,
    json_object,
    oracle_evidence,
    provenance_origin,
    reference_evidence,
    sha256_of,
)

SCHEMA_VERSION = 1
RECEIPT_NAME = "iteration.json"
ITERATIONS_RELPATH = ("validation", "iterations")
PAGES_DIRNAME = "pages"
PACKAGE_MANIFEST_NAME = "package-manifest.json"
MIGRATION_SPEC_NAME = "migration-spec.json"
TOOL_NAME = "capture_powerbi_pages"

ITERATION_NAME_RE = re.compile(r"^\d{3}$")
SAFE_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
FINDING_ID_RE = re.compile(r"^F-\d{3,}$")

MODE_SIGN_OFF = "sign_off"
MODE_TRIAGE = "triage"
MODES = (MODE_SIGN_OFF, MODE_TRIAGE)

SCOPE_ALL_PAGES = "all_pages"
SCOPE_SUBSET = "subset"

STATE_PENDING = "pending"
STATE_FINAL = "final"

STATUS_PENDING = "pending"
STATUS_PASS = "pass"
STATUS_MISMATCH = "mismatch"
STATUS_UNVERIFIED = "unverified"
STATUS_NOT_APPLICABLE = "not_applicable"
JUDGEMENT_STATUSES = (STATUS_PENDING, STATUS_PASS, STATUS_MISMATCH, STATUS_UNVERIFIED, STATUS_NOT_APPLICABLE)
#: A status that COMPLETES a sign-off. `unverified` is deliberately absent - it is not a pass.
COMPLETING_STATUSES = frozenset({STATUS_PASS, STATUS_NOT_APPLICABLE})

FINDING_OPEN = "still_open"
FINDING_RESOLVED = "resolved"
FINDING_ACCEPTED = "accepted_limitation"
FINDING_STATUSES = (FINDING_OPEN, FINDING_RESOLVED, FINDING_ACCEPTED)
FINDING_KINDS = ("visual", "numeric", "data", "other")
FINDING_SEVERITIES = ("low", "medium", "high")

OUTCOME_COMPLETE = "complete"
OUTCOME_INCOMPLETE = "incomplete"

DATA_STATUS_PENDING = "pending"
DATA_STATUS_ACCEPTED = "accepted"
DATA_MODE_LIVE = "live_query"
DATA_MODE_PERSISTED = "persisted_cache"
DATA_VERDICT_OK = "DATA_OK"
DATA_VERDICT_PERSISTED = "DATA_OK + PERSISTED"
DATA_TOOLS = ("probe_desktop_query", "refresh_pbip_model")

#: Why data evidence is `pending` unless a tool-produced record is handed in. Stated as a SEAM, not
#: as an excuse: `probe_desktop_query.probe()` emits `PREFLIGHT: DATA_OK` and its per-canary row
#: counts through `emit` (printed text) and returns an int exit code, and `refresh_pbip_model`
#: likewise prints `REFRESH: DATA_OK + PERSISTED` with no structured result object. Consuming either
#: in-process would mean editing those tools, which is outside this producer's closed surface.
DATA_PENDING_REASON = (
    "no tool-produced data record was supplied; probe_desktop_query.probe and refresh_pbip_model "
    "expose their verdict and canary row counts as printed text plus an int exit code only, so "
    "there is no structured result this producer can consume without editing them"
)

#: Free text a reviewer may write. Single line, bounded, so raw tool output and tracebacks - which
#: are multi-line and routinely carry host paths - cannot be pasted in.
MAX_DETAIL_CHARS = 500

#: A remote URL. ⚠️ Deliberately NOT a second copy of the host-path definition - `host_paths` owns
#: that question and is used for it below. This is the narrower one it cannot answer: a Tableau
#: Server/Cloud view URL names the SERVER, the SITE and the PROJECT, none of which may travel in a
#: shareable receipt even though none of them is a location on this host.
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://", re.IGNORECASE)


class ReceiptError(RuntimeError):
    """A named refusal. Tests assert on ``code``; ``detail`` is for the operator."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@contextmanager
def _named_refusals() -> Iterator[None]:
    """Re-raise a revision refusal as a receipt refusal, KEEPING its code.

    One exception type crosses this module's boundary, so a caller never has to know which helper
    refused - while the code stays the specific one, because "it failed" and "it failed for this
    named reason" are different claims and only the second is actionable.
    """
    try:
        yield
    except rev.RevisionError as error:
        raise ReceiptError(error.code, error.detail) from error


def report_inventory(report_dir: Path) -> list[rev.PageInventory]:
    """The current PBIR page/visual inventory, with revision refusals renamed to receipt ones."""
    with _named_refusals():
        return rev.report_inventory(report_dir)


# --------------------------------------------------------------------------------------------------
# strict JSON
# --------------------------------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object hook that REFUSES a duplicate key rather than silently keeping the last one.

    ``json.loads`` keeps the last occurrence, so ``{"status":"pass","status":"pending"}`` reads as a
    pass with no diagnostic at all. A receipt is read by a gate; a document with two answers to one
    question has no answer.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ReceiptError("DUPLICATE_JSON_KEY", f"the key {key!r} appears twice in one object")
        seen[key] = value
    return seen


def read_strict_json(path: Path) -> Any:
    """Read JSON, refusing a duplicate key or unreadable bytes with a named error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReceiptError("RECEIPT_UNREADABLE", f"{path.name} could not be read ({error.strerror})") from error
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ReceiptError("RECEIPT_UNREADABLE", f"{path.name} is not valid JSON ({error.msg})") from error


# --------------------------------------------------------------------------------------------------
# closed schema
# --------------------------------------------------------------------------------------------------

Validator = Callable[[Any, str], None]


def _fail(pointer: str, detail: str) -> None:
    raise ReceiptError("SCHEMA", f"{pointer or '/'}: {detail}")


def _string(value: Any, pointer: str) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(pointer, "expected a non-empty string")


def _boolean(value: Any, pointer: str) -> None:
    if not isinstance(value, bool):
        _fail(pointer, "expected a boolean")


def _integer(value: Any, pointer: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(pointer, "expected an integer")


def _number(value: Any, pointer: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(pointer, "expected a number")


def _enum(*allowed: str) -> Validator:
    def check(value: Any, pointer: str) -> None:
        if value not in allowed:
            _fail(pointer, f"expected one of {list(allowed)}, got {value!r}")

    return check


def _const(expected: Any) -> Validator:
    def check(value: Any, pointer: str) -> None:
        if value != expected:
            _fail(pointer, f"expected {expected!r}, got {value!r}")

    return check


def _nullable(inner: Validator) -> Validator:
    def check(value: Any, pointer: str) -> None:
        if value is not None:
            inner(value, pointer)

    return check


def _list_of(inner: Validator) -> Validator:
    def check(value: Any, pointer: str) -> None:
        if not isinstance(value, list):
            _fail(pointer, "expected a list")
        for index, item in enumerate(value):
            inner(item, f"{pointer}/{index}")

    return check


def _obj(fields: dict[str, Validator]) -> Validator:
    """A CLOSED object: every declared key is required and every undeclared key is refused."""

    def check(value: Any, pointer: str) -> None:
        if not isinstance(value, dict):
            _fail(pointer, "expected an object")
        unknown = sorted(set(value) - set(fields))
        if unknown:
            raise ReceiptError("UNKNOWN_FIELD", f"{pointer or '/'}: unknown field(s) {unknown}")
        missing = sorted(set(fields) - set(value))
        if missing:
            _fail(pointer, f"missing field(s) {missing}")
        for key, validator in fields.items():
            validator(value[key], f"{pointer}/{key}")

    return check


_tableau_schema = _obj(
    {
        "manifest_sha256": _string,
        "path": _string,
        "sha256": _string,
        "grade": _string,
    }
)

_powerbi_schema = _obj(
    {
        "path": _string,
        "sha256": _string,
        "byte_count": _integer,
        "converged": _boolean,
        "frames": _integer,
        "stable_seconds": _number,
        "settled_seconds": _number,
    }
)

_generated_page_schema = _obj(
    {
        "page_id": _string,
        "display_name": _string,
        "expected_visual_ids": _list_of(_string),
        "tableau": _nullable(_tableau_schema),
        "tableau_reason": _nullable(_string),
        "powerbi": _powerbi_schema,
    }
)

_data_evidence_schema = _obj(
    {
        "status": _enum(DATA_STATUS_PENDING, DATA_STATUS_ACCEPTED),
        "mode": _nullable(_enum(DATA_MODE_LIVE, DATA_MODE_PERSISTED)),
        "verdict": _nullable(_enum(DATA_VERDICT_OK, DATA_VERDICT_PERSISTED)),
        "tool": _nullable(_enum(*DATA_TOOLS)),
        "canaries": _list_of(_obj({"table": _string, "row_count": _integer})),
        "model_revision": _nullable(_string),
        "cache_sha256": _nullable(_string),
        "pending_reason": _nullable(_string),
    }
)

_generated_schema = _obj(
    {
        "generated_at": _string,
        "scope": _enum(SCOPE_ALL_PAGES, SCOPE_SUBSET),
        "artifact": _obj(
            {
                "unit": _string,
                "kind": _string,
                "report_path": _string,
                "model_path": _nullable(_string),
                "package_revision": _string,
                "report_revision": _string,
                "model_revision": _nullable(_string),
                "cache_sha256": _nullable(_string),
                "cache_byte_count": _nullable(_integer),
            }
        ),
        "review": _obj(
            {
                "reviewer": _string,
                "session_id": _nullable(_string),
                "tool": _const(TOOL_NAME),
                "tool_version": _string,
                "desktop_binding_checked": _boolean,
                "desktop_binding_matches": _nullable(_boolean),
            }
        ),
        "previous": _nullable(
            _obj(
                {
                    "iteration": _string,
                    "receipt_sha256": _string,
                    "report_revision": _string,
                    "model_revision": _nullable(_string),
                }
            )
        ),
        "limitations": _obj({"spec_path": _nullable(_string), "entry_count": _integer}),
        "data_evidence": _data_evidence_schema,
        "pages": _list_of(_generated_page_schema),
        "changes_from_previous": _list_of(
            _obj(
                {
                    "page_id": _string,
                    "before_sha256": _nullable(_string),
                    "after_sha256": _nullable(_string),
                }
            )
        ),
    }
)

_judgement_schema = _obj(
    {
        "completed_at": _nullable(_string),
        "pages": _list_of(
            _obj(
                {
                    "page_id": _string,
                    "whole_page_status": _enum(*JUDGEMENT_STATUSES),
                    "visual_results": _list_of(
                        _obj(
                            {
                                "visual_id": _string,
                                "status": _enum(*JUDGEMENT_STATUSES),
                                "finding_ids": _list_of(_string),
                            }
                        )
                    ),
                    "numeric_results": _list_of(
                        _obj(
                            {
                                "visual_id": _string,
                                "status": _enum(*JUDGEMENT_STATUSES),
                                "tableau_evidence_sha256": _nullable(_string),
                                "powerbi_query_sha256": _nullable(_string),
                                "powerbi_result_sha256": _nullable(_string),
                                "finding_ids": _list_of(_string),
                            }
                        )
                    ),
                }
            )
        ),
        "findings": _list_of(
            _obj(
                {
                    "id": _string,
                    "page_id": _nullable(_string),
                    "visual_id": _nullable(_string),
                    "kind": _enum(*FINDING_KINDS),
                    "severity": _enum(*FINDING_SEVERITIES),
                    "status": _enum(*FINDING_STATUSES),
                    "detail": _string,
                    "limitation_ref": _nullable(_obj({"pointer": _string, "sha256": _string})),
                }
            )
        ),
    }
)

_receipt_schema = _obj(
    {
        "schema_version": _const(SCHEMA_VERSION),
        "iteration": _string,
        "mode": _enum(*MODES),
        "state": _enum(STATE_PENDING, STATE_FINAL),
        "outcome": _nullable(_enum(OUTCOME_COMPLETE, OUTCOME_INCOMPLETE)),
        "generated": _generated_schema,
        "judgement": _judgement_schema,
    }
)


def validate_receipt(payload: Any) -> dict[str, Any]:
    """Schema-check a receipt document and return it, or raise a named refusal."""
    _receipt_schema(payload, "")
    return payload


# --------------------------------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------------------------------


def _strings(value: Any, pointer: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{pointer}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{pointer}/{index}")
    elif isinstance(value, str):
        yield pointer, value


def assert_shareable(payload: Any) -> None:
    """Refuse a receipt carrying anything that must not leave the machine that produced it.

    A receipt is committed and reviewed, so the rule is the SHIPPED one:
    :func:`host_paths.discloses_host_location` (the single repo definition - drive, UNC and POSIX
    roots, spelling-normalised) rather than a local regex that any prefix defeats. On top of it:

    * a remote URL, because a Tableau Server/Cloud URL names the server, the site and the project;
    * a line break, which is how raw bridge output and a traceback arrive;
    * an over-long free-text field, for the same reason.

    The producer never writes tool output, exception text or a captured path into the receipt at
    all - this runs over the WHOLE serialized document immediately before every write, so the guard
    covers the reviewer's fields too, not merely the generated ones.
    """
    for pointer, text in _strings(payload):
        if "\n" in text or "\r" in text:
            raise ReceiptError("PRIVACY", f"{pointer}: contains a line break, so it may be raw tool output")
        if len(text) > MAX_DETAIL_CHARS:
            raise ReceiptError("PRIVACY", f"{pointer}: is longer than {MAX_DETAIL_CHARS} characters")
        if host_paths.discloses_host_location(text):
            raise ReceiptError("PRIVACY", f"{pointer}: discloses a location on a host")
        if URL_RE.search(text):
            raise ReceiptError("PRIVACY", f"{pointer}: contains a URL, which names a server and site")


# --------------------------------------------------------------------------------------------------
# the package
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageTarget:  # pylint: disable=too-many-instance-attributes
    """The ONE canonical report/model this package declares. Resolved without ancestor search.

    Both the declared package-relative path AND the resolved directory are kept for the report and
    the model, deliberately: the receipt records the DECLARED one (it is shareable and stable) while
    every read uses the RESOLVED one, and collapsing the pair would mean either recording a host
    path or re-deriving a resolution at each call site.
    """

    root: Path
    unit: str
    kind: str
    report_path: str
    report_dir: Path
    model_path: str | None
    model_dir: Path | None
    asset: Path | None


def _contained(root: Path, declared: str, label: str) -> Path:
    """A package-relative path resolved INSIDE the package, or a named refusal."""
    if not isinstance(declared, str) or not declared.strip():
        raise ReceiptError("PACKAGE_MANIFEST", f"{label} is missing from {PACKAGE_MANIFEST_NAME}")
    if declared.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", declared) or ".." in declared.split("/"):
        raise ReceiptError("UNSAFE_PATH", f"{label} is not a safe package-relative path")
    candidate = root / declared
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ReceiptError("UNSAFE_PATH", f"{label} resolves outside the package")
    return candidate


def resolve_package(package: Path) -> PackageTarget:
    """Read the package's own manifest and resolve its canonical report/model.

    NO ancestor search, deliberately. Walking upward to find "a report" is how one command ends up
    operating on a different artifact from the one the caller named, and a receipt that cannot say
    which artifact it measured is not evidence.
    """
    if not package.is_dir():
        raise ReceiptError("NOT_A_PACKAGE", "the supplied package path is not a directory")
    with _named_refusals():
        rev.assert_no_reparse_points(package)
    manifest_path = package / PACKAGE_MANIFEST_NAME
    if not manifest_path.is_file():
        raise ReceiptError("NOT_A_PACKAGE", f"no {PACKAGE_MANIFEST_NAME} beside the supplied path")
    manifest = read_strict_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ReceiptError("PACKAGE_MANIFEST", f"{PACKAGE_MANIFEST_NAME} is not a JSON object")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ReceiptError("PACKAGE_MANIFEST", f"{PACKAGE_MANIFEST_NAME} declares no artifacts")
    unit = manifest.get("unit")
    kind = manifest.get("kind")
    if not isinstance(unit, str) or not unit.strip() or not isinstance(kind, str) or not kind.strip():
        raise ReceiptError("PACKAGE_MANIFEST", f"{PACKAGE_MANIFEST_NAME} declares no unit/kind")
    report_rel = artifacts.get("report")
    report_dir = _contained(package, report_rel, "artifacts.report")
    if not report_dir.is_dir():
        raise ReceiptError("PACKAGE_MANIFEST", "artifacts.report does not exist in the package")
    model_rel = artifacts.get("model")
    model_dir = _contained(package, model_rel, "artifacts.model") if isinstance(model_rel, str) else None
    if model_dir is not None and not model_dir.is_dir():
        raise ReceiptError("PACKAGE_MANIFEST", "artifacts.model does not exist in the package")
    asset_rel = artifacts.get("asset")
    asset = _contained(package, asset_rel, "artifacts.asset") if isinstance(asset_rel, str) else None
    return PackageTarget(
        root=package,
        unit=unit,
        kind=kind,
        report_path=report_rel,
        report_dir=report_dir,
        model_path=model_rel if model_dir is not None else None,
        model_dir=model_dir,
        asset=asset if asset is not None and asset.is_file() else None,
    )


# --------------------------------------------------------------------------------------------------
# the iteration chain
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Iteration:
    """One already-written iteration, with its receipt verified readable and schema-valid."""

    name: str
    directory: Path
    receipt_path: Path
    receipt_sha256: str
    payload: dict[str, Any]


def iterations_root(package: Path) -> Path:
    """`<package>/validation/iterations`, without creating it."""
    return package.joinpath(*ITERATIONS_RELPATH)


def _assert_iteration_contents(directory: Path) -> None:
    """An iteration directory holds exactly one receipt and one `pages/` folder of PNGs."""
    allowed_files = {RECEIPT_NAME}
    for entry in sorted(directory.iterdir()):
        if rev._is_reparse_point(entry):  # pylint: disable=protected-access
            raise ReceiptError("REPARSE_POINT", f"{directory.name}/{entry.name} is a symlink/junction")
        if entry.is_dir():
            if entry.name != PAGES_DIRNAME:
                raise ReceiptError("EXTRA_FILE", f"{directory.name}/ carries an unexpected folder {entry.name!r}")
            continue
        if entry.name not in allowed_files:
            raise ReceiptError("EXTRA_FILE", f"{directory.name}/ carries an unexpected file {entry.name!r}")
    pages_dir = directory / PAGES_DIRNAME
    for entry in sorted(pages_dir.iterdir()) if pages_dir.is_dir() else []:
        if entry.is_dir() or entry.suffix.lower() != ".png":
            raise ReceiptError("EXTRA_FILE", f"{directory.name}/{PAGES_DIRNAME}/ carries a non-PNG {entry.name!r}")


def read_chain(package: Path) -> list[Iteration]:
    """Every existing iteration, validated as a canonical contiguous chain.

    Refuses BEFORE anything is allocated: a gap, a non-canonical name, a stray file, a reparse
    point, an unreadable or schema-invalid receipt, or a link that does not pin its predecessor. A
    chain that cannot be read is not an empty chain - allocating on top of one would silently drop
    whatever history it held.
    """
    root = iterations_root(package)
    if not root.exists():
        return []
    if not root.is_dir():
        raise ReceiptError("ITERATIONS_NOT_A_DIRECTORY", "validation/iterations exists but is not a directory")
    with _named_refusals():
        rev.assert_no_reparse_points(root)
    names: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            raise ReceiptError("EXTRA_FILE", f"validation/iterations carries a stray file {entry.name!r}")
        if not ITERATION_NAME_RE.match(entry.name):
            raise ReceiptError("NONCANONICAL_ITERATION", f"{entry.name!r} is not a three-digit iteration name")
        names.append(entry.name)
    expected = [f"{index:03d}" for index in range(1, len(names) + 1)]
    if names != expected:
        raise ReceiptError("ITERATION_GAP", f"iterations are {names}, expected the contiguous {expected}")

    chain: list[Iteration] = []
    for name in names:
        directory = root / name
        _assert_iteration_contents(directory)
        receipt_path = directory / RECEIPT_NAME
        if not receipt_path.is_file():
            raise ReceiptError("RECEIPT_MISSING", f"iteration {name} has no {RECEIPT_NAME}")
        payload = validate_receipt(read_strict_json(receipt_path))
        if payload["iteration"] != name:
            raise ReceiptError("ITERATION_MISLABELLED", f"iteration {name} calls itself {payload['iteration']!r}")
        digest = sha256_of(receipt_path) or ""
        _assert_link(chain, payload, name)
        chain.append(
            Iteration(name=name, directory=directory, receipt_path=receipt_path, receipt_sha256=digest, payload=payload)
        )
    return chain


def _assert_link(chain: list[Iteration], payload: dict[str, Any], name: str) -> None:
    """Iteration N must pin iteration N-1's receipt hash, and 001 must pin nothing."""
    previous = payload["generated"]["previous"]
    if not chain:
        if previous is not None:
            raise ReceiptError("BROKEN_CHAIN", f"iteration {name} names a predecessor but is the first")
        return
    prior = chain[-1]
    if previous is None:
        raise ReceiptError("BROKEN_CHAIN", f"iteration {name} names no predecessor")
    if previous["iteration"] != prior.name:
        raise ReceiptError("BROKEN_CHAIN", f"iteration {name} names {previous['iteration']!r}, not {prior.name!r}")
    if previous["receipt_sha256"] != prior.receipt_sha256:
        raise ReceiptError(
            "PREVIOUS_RECEIPT_MISMATCH",
            f"iteration {name} pins a different {RECEIPT_NAME} than iteration {prior.name} now holds",
        )


def allocate_iteration(package: Path) -> tuple[Path, Iteration | None]:
    """Create the next canonical iteration directory EXCLUSIVELY, returning it and its predecessor.

    ``mkdir(exist_ok=False)`` is the whole concurrency story: two producers racing for the same
    number cannot both succeed, and the loser is REFUSED rather than retried into overwriting
    somebody else's evidence.
    """
    chain = read_chain(package)
    if chain and chain[-1].payload["state"] != STATE_FINAL:
        raise ReceiptError(
            "PREVIOUS_NOT_FINAL",
            f"iteration {chain[-1].name} is still {chain[-1].payload['state']}; finalize it first",
        )
    root = iterations_root(package)
    root.mkdir(parents=True, exist_ok=True)
    name = f"{len(chain) + 1:03d}"
    directory = root / name
    try:
        directory.mkdir(exist_ok=False)
    except FileExistsError as error:
        raise ReceiptError("ITERATION_NUMBER_TAKEN", f"iteration {name} already exists") from error
    return directory, (chain[-1] if chain else None)


# --------------------------------------------------------------------------------------------------
# Tableau evidence, re-derived through the existing authorities
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TableauMatch:
    """One page's Tableau render, or the reason there is none. Never both."""

    evidence: dict[str, Any] | None
    reason: str | None


def _unit_identity(target: PackageTarget) -> UnitIdentity | str:
    """The package's own workbook identity, or the reason it could not be established."""
    if target.asset is None:
        return "the package declares no source asset, so no render can be attributed to it"
    digest = sha256_of(target.asset)
    if digest is None:
        return "the package's source asset could not be hashed"
    try:
        luid, revision = provenance_origin(target.root, digest, target.asset)
    except oid.AmbiguousIdentity:
        return "the package's provenance is stamped for more than one workbook"
    return UnitIdentity(
        name=target.unit, source_path=target.asset, source_sha256=digest, workbook_luid=luid, revision=revision
    )


def _evidence_manifests(target: PackageTarget) -> tuple[list[Evidence], dict[str, str]]:
    """Admissible Tableau renders in this package, plus the manifest sha that declared each."""
    oracle_dir = target.root / "oracle"
    reference_dir = target.root / "reference"
    found: list[Evidence] = []
    manifest_shas: dict[str, str] = {}
    if oracle_dir.is_dir():
        evidence, _ = oracle_evidence([oracle_dir])
        digest = sha256_of(oracle_dir / "oracle-manifest.json") or ""
        for item in evidence:
            manifest_shas[item.render_digest] = digest
        found.extend(evidence)
    if reference_dir.is_dir():
        evidence, _ = reference_evidence([reference_dir])
        digest = sha256_of(reference_dir / "manifest.json") or ""
        for item in evidence:
            manifest_shas[item.render_digest] = digest
        found.extend(evidence)
    return found, manifest_shas


def _relative_render(target: PackageTarget, evidence: Evidence) -> str | None:
    """The render's PACKAGE-RELATIVE path, or None when it lies outside the package."""
    try:
        return Path(evidence.path).resolve().relative_to(target.root.resolve()).as_posix()
    except ValueError:
        return None


def tableau_matches(  # pylint: disable=too-many-locals
    target: PackageTarget, pages: list[rev.PageInventory]
) -> dict[str, TableauMatch]:
    """Per current page, the Tableau render that proves what it should look like - or why not.

    Every admission decision is delegated: :func:`reference_evidence.oracle_evidence` /
    :func:`reference_evidence.reference_evidence` prove the bytes against the producer's recorded
    hash and CAP the grade at what the producer can physically make, and
    :meth:`Evidence.attribution` decides whether the render belongs to THIS workbook at THIS
    revision. An oracle capture stays layout/text grade here exactly as it is there; nothing in this
    module can raise a grade.
    """
    identity = _unit_identity(target)
    if isinstance(identity, str):
        return {page.page_id: TableauMatch(None, identity) for page in pages}
    evidence, manifest_shas = _evidence_manifests(target)
    index: oid.CandidateIndex[Evidence] = oid.CandidateIndex()
    for item in evidence:
        index.add(item.candidate(), item)

    matches: dict[str, TableauMatch] = {}
    claimed: dict[str, list[str]] = {}
    for page in pages:
        resolved = _resolve_one(page, index)
        if isinstance(resolved, str):
            matches[page.page_id] = TableauMatch(None, resolved)
            continue
        attribution = resolved.attribution(identity)
        if not attribution.admitted:
            matches[page.page_id] = TableauMatch(None, f"the only render named for this page is {attribution.route}")
            continue
        relative = _relative_render(target, resolved)
        if relative is None:
            matches[page.page_id] = TableauMatch(None, "the render for this page lies outside the package")
            continue
        claimed.setdefault(resolved.render_digest, []).append(page.page_id)
        matches[page.page_id] = TableauMatch(
            {
                "manifest_sha256": manifest_shas.get(resolved.render_digest, ""),
                "path": relative,
                "sha256": resolved.render_digest,
                "grade": resolved.grade,
            },
            None,
        )
    contested = {digest for digest, page_ids in claimed.items() if len(page_ids) > 1}
    for page_id in [page_id for digest in contested for page_id in claimed[digest]]:
        matches[page_id] = TableauMatch(None, "one render is claimed by more than one page, so no page owns it")
    return matches


def _resolve_one(page: rev.PageInventory, index: oid.CandidateIndex[Evidence]) -> Evidence | str:
    """One page's render by display name, refusing when both Tableau object kinds could claim it."""
    hits: list[Evidence] = []
    for kind in (oid.KIND_DASHBOARD, oid.KIND_WORKSHEET):
        key = oid.ObjectIdentity.from_engine(kind, page.display_name)
        if key is None:
            continue
        resolution = index.resolve(key)
        if resolution.outcome == oid.UNIQUE:
            hits.append(resolution.value())
        elif resolution.outcome == oid.AMBIGUOUS:
            return "more than one Tableau render is named for this page"
    unique = {item.render_digest: item for item in hits}
    if not unique:
        return "no Tableau render in this package is named for this page"
    if len(unique) > 1:
        return "a dashboard and a worksheet render share this page's name, so neither can prove it"
    return next(iter(unique.values()))


# --------------------------------------------------------------------------------------------------
# data evidence
# --------------------------------------------------------------------------------------------------

_data_input_schema = _obj(
    {
        "tool": _enum(*DATA_TOOLS),
        "verdict": _enum(DATA_VERDICT_OK, DATA_VERDICT_PERSISTED),
        "mode": _enum(DATA_MODE_LIVE, DATA_MODE_PERSISTED),
        "canaries": _list_of(_obj({"table": _string, "row_count": _integer})),
        "model_revision": _string,
        "cache_sha256": _nullable(_string),
    }
)


def pending_data_evidence() -> dict[str, Any]:
    """The honest default: no data proof, and the exact seam that blocks producing one."""
    return {
        "status": DATA_STATUS_PENDING,
        "mode": None,
        "verdict": None,
        "tool": None,
        "canaries": [],
        "model_revision": None,
        "cache_sha256": None,
        "pending_reason": DATA_PENDING_REASON,
    }


def ingest_data_evidence(path: Path, model_revision: str | None, cache_sha256: str | None) -> dict[str, Any]:
    """Accept a TOOL-PRODUCED data record, bound to the current model revision and cache bytes.

    Three states stay distinct and only the third counts (audit "Proving data loaded"): a cache that
    merely EXISTS proves nothing, an implicit single-table probe earns only ``TABLE_OK`` and is not
    representable here at all, and a persisted claim must additionally pin the cache bytes it was
    written against. A canary that returned zero rows is a refusal, not a small pass.
    """
    payload = read_strict_json(path)
    _data_input_schema(payload, "/data_evidence")
    canaries = payload["canaries"]
    if not canaries:
        raise ReceiptError("DATA_EVIDENCE_NO_CANARIES", "a data record with no explicit canary is not DATA_OK")
    empty = [row["table"] for row in canaries if row["row_count"] < 1]
    if empty:
        raise ReceiptError("DATA_EVIDENCE_EMPTY_CANARY", f"canary table(s) {sorted(empty)} returned no rows")
    if payload["mode"] == DATA_MODE_PERSISTED:
        if payload["verdict"] != DATA_VERDICT_PERSISTED:
            raise ReceiptError(
                "DATA_EVIDENCE_VERDICT", f"a persisted claim needs the literal {DATA_VERDICT_PERSISTED!r}"
            )
        if not payload["cache_sha256"]:
            raise ReceiptError("DATA_EVIDENCE_CACHE_MISMATCH", "a persisted claim names no cache")
        if payload["cache_sha256"] != cache_sha256:
            raise ReceiptError(
                "DATA_EVIDENCE_CACHE_MISMATCH", "the record names a different cache than the model now holds"
            )
    if payload["model_revision"] != model_revision:
        raise ReceiptError("DATA_EVIDENCE_STALE_MODEL", "the record was produced against a different model revision")
    return {
        "status": DATA_STATUS_ACCEPTED,
        "mode": payload["mode"],
        "verdict": payload["verdict"],
        "tool": payload["tool"],
        "canaries": [{"table": row["table"], "row_count": row["row_count"]} for row in canaries],
        "model_revision": payload["model_revision"],
        "cache_sha256": payload["cache_sha256"],
        "pending_reason": None,
    }


# --------------------------------------------------------------------------------------------------
# building a receipt
# --------------------------------------------------------------------------------------------------


def now_rfc3339() -> str:
    """The current instant, to the second, in UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def page_image_name(page_id: str) -> str:
    """A safe, deterministic PNG name for a page id - never the raw id when it is unsafe."""
    if SAFE_STEM_RE.match(page_id):
        return f"{page_id}.png"
    return f"page-{hashlib.sha256(page_id.encode('utf-8')).hexdigest()[:16]}.png"


def artifact_facts(target: PackageTarget) -> dict[str, Any]:
    """The package's CURRENT identity: revisions re-derived from the bytes on disk right now."""
    with _named_refusals():
        cache = rev.cache_facts(target.model_dir) if target.model_dir is not None else None
        return {
            "unit": target.unit,
            "kind": target.kind,
            "report_path": target.report_path,
            "model_path": target.model_path,
            "package_revision": rev.package_working_revision(target.root),
            "report_revision": rev.report_revision(target.report_dir),
            "model_revision": rev.model_revision(target.model_dir) if target.model_dir is not None else None,
            "cache_sha256": cache.sha256 if cache else None,
            "cache_byte_count": cache.byte_count if cache else None,
        }


def limitation_facts(package: Path) -> dict[str, Any]:
    """How many `limitations_encountered` entries the CURRENT spec has, and where it is."""
    spec = package / MIGRATION_SPEC_NAME
    payload = json_object(spec)
    entries = (payload or {}).get("limitations_encountered")
    if not spec.is_file() or not isinstance(entries, list):
        return {"spec_path": None, "entry_count": 0}
    return {"spec_path": MIGRATION_SPEC_NAME, "entry_count": len(entries)}


def pending_judgement(pages: list[rev.PageInventory]) -> dict[str, Any]:
    """One PENDING judgement row per current page, visual and numeric slot. Never a pass."""
    return {
        "completed_at": None,
        "pages": [
            {
                "page_id": page.page_id,
                "whole_page_status": STATUS_PENDING,
                "visual_results": [
                    {"visual_id": visual_id, "status": STATUS_PENDING, "finding_ids": []}
                    for visual_id in page.visual_ids
                ],
                "numeric_results": [
                    {
                        "visual_id": visual_id,
                        "status": STATUS_PENDING,
                        "tableau_evidence_sha256": None,
                        "powerbi_query_sha256": None,
                        "powerbi_result_sha256": None,
                        "finding_ids": [],
                    }
                    for visual_id in page.visual_ids
                ],
            }
            for page in pages
        ],
        "findings": [],
    }


def changes_from_previous(previous: Iteration | None, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per page, the screenshot hash BEFORE this iteration and AFTER it. Generated, never claimed."""
    if previous is None:
        return []
    before = {row["page_id"]: row["powerbi"]["sha256"] for row in previous.payload["generated"]["pages"]}
    rows = [
        {
            "page_id": row["page_id"],
            "before_sha256": before.get(row["page_id"]),
            "after_sha256": row["powerbi"]["sha256"],
        }
        for row in pages
    ]
    captured = {row["page_id"] for row in pages}
    rows.extend(
        {"page_id": page_id, "before_sha256": digest, "after_sha256": None}
        for page_id, digest in sorted(before.items())
        if page_id not in captured
    )
    return rows


def write_receipt(directory: Path, payload: dict[str, Any]) -> str:
    """Validate, privacy-scrub and write one receipt atomically; return its sha256."""
    validate_receipt(payload)
    assert_shareable(payload)
    body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    staged = directory / f".{RECEIPT_NAME}.writing"
    staged.write_text(body, encoding="utf-8")
    os.replace(staged, directory / RECEIPT_NAME)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------
# finalization
# --------------------------------------------------------------------------------------------------


def _assert_inventory(target: PackageTarget, payload: dict[str, Any]) -> dict[str, rev.PageInventory]:
    """The report's CURRENT page/visual inventory must still be the one this iteration measured.

    Checked BEFORE the coarser revision comparison on purpose: an added page and a re-themed report
    both move ``report_revision``, and an operator can only act on the difference between them.
    """
    generated = payload["generated"]
    inventory = report_inventory(target.report_dir)
    by_id = {page.page_id: page for page in inventory}
    captured = {row["page_id"]: row for row in generated["pages"]}
    unknown = sorted(set(captured) - set(by_id))
    if unknown:
        raise ReceiptError("INVENTORY_CHANGED", f"the receipt names page(s) {unknown} the report no longer has")
    if generated["scope"] == SCOPE_ALL_PAGES and set(captured) != set(by_id):
        raise ReceiptError("INVENTORY_CHANGED", "the report's page set differs from the captured page set")
    for page_id, row in captured.items():
        page = by_id[page_id]
        if row["display_name"] != page.display_name or tuple(row["expected_visual_ids"]) != page.visual_ids:
            raise ReceiptError("INVENTORY_CHANGED", f"page {page_id!r} no longer has the recorded visual inventory")
    return by_id


def _assert_images(payload: dict[str, Any], directory: Path) -> None:
    """Every retained screenshot is still the captured bytes, and there are no others."""
    for row in payload["generated"]["pages"]:
        _assert_image(directory, row)
    named = {row["powerbi"]["path"] for row in payload["generated"]["pages"]}
    pages_dir = directory / PAGES_DIRNAME
    present = (
        {f"{PAGES_DIRNAME}/{item.name}" for item in sorted(pages_dir.iterdir()) if item.is_file()}
        if pages_dir.is_dir()
        else set()
    )
    extra = sorted(present - named)
    if extra:
        raise ReceiptError("EXTRA_FILE", f"the iteration retains screenshot(s) {extra} no current page named")


def _assert_changes_from_previous(payload: dict[str, Any], prior: Iteration | None) -> None:
    """`changes_from_previous` is GENERATED, so it must still equal what the two receipts imply.

    Without this the before/after pair is the one immutable field a reviewer could rewrite freely -
    and "this page did not change" is exactly the claim a stale sign-off wants to make.
    """
    expected = changes_from_previous(prior, payload["generated"]["pages"])
    if payload["generated"]["changes_from_previous"] != expected:
        raise ReceiptError(
            "CHANGES_MISDECLARED", "changes_from_previous is not the before/after pair these receipts imply"
        )


def _assert_tableau(target: PackageTarget, payload: dict[str, Any], by_id: dict[str, rev.PageInventory]) -> None:
    """Tableau evidence is re-derived through the same authorities that admitted it."""
    captured = {row["page_id"]: row for row in payload["generated"]["pages"]}
    for page_id, match in tableau_matches(target, [by_id[page_id] for page_id in captured]).items():
        if captured[page_id]["tableau"] != match.evidence:
            raise ReceiptError("TABLEAU_EVIDENCE_CHANGED", f"page {page_id!r}'s Tableau evidence is no longer the same")


def _assert_revisions(target: PackageTarget, payload: dict[str, Any]) -> None:
    """The artifact identities, re-derived from the bytes on disk right now."""
    generated = payload["generated"]
    current = artifact_facts(target)
    recorded = generated["artifact"]
    for field, code in (
        ("report_revision", "REPORT_CHANGED"),
        ("model_revision", "MODEL_CHANGED"),
        ("cache_sha256", "CACHE_CHANGED"),
        ("package_revision", "PACKAGE_CHANGED"),
    ):
        if current[field] != recorded[field]:
            raise ReceiptError(code, f"{field} differs from the value this iteration recorded")

    data = generated["data_evidence"]
    if data["status"] == DATA_STATUS_ACCEPTED:
        if data["model_revision"] != current["model_revision"]:
            raise ReceiptError("DATA_EVIDENCE_STALE_MODEL", "the data evidence names a superseded model revision")
        if data["mode"] == DATA_MODE_PERSISTED and data["cache_sha256"] != current["cache_sha256"]:
            raise ReceiptError("DATA_EVIDENCE_CACHE_MISMATCH", "the data evidence names a superseded cache")


def _assert_image(directory: Path, row: dict[str, Any]) -> None:
    """A retained screenshot must still be present, non-empty and byte-identical to its record."""
    relative = row["powerbi"]["path"]
    if relative.startswith(("/", "\\")) or ".." in relative.split("/"):
        raise ReceiptError("UNSAFE_PATH", f"page {row['page_id']!r} names an unsafe screenshot path")
    image = directory / relative
    if rev._is_reparse_point(image):  # pylint: disable=protected-access
        raise ReceiptError("REPARSE_POINT", f"page {row['page_id']!r}'s screenshot is a symlink/junction")
    if not image.is_file():
        raise ReceiptError("SCREENSHOT_MISSING", f"page {row['page_id']!r} has no retained screenshot")
    blob = image.read_bytes()
    if not blob:
        raise ReceiptError("SCREENSHOT_EMPTY", f"page {row['page_id']!r}'s retained screenshot is zero bytes")
    if len(blob) != row["powerbi"]["byte_count"] or hashlib.sha256(blob).hexdigest() != row["powerbi"]["sha256"]:
        raise ReceiptError("SCREENSHOT_CHANGED", f"page {row['page_id']!r}'s screenshot is not the captured bytes")


def _assert_previous(chain: list[Iteration], payload: dict[str, Any]) -> Iteration | None:
    """The predecessor link must still pin the receipt and revisions it was written against."""
    previous = payload["generated"]["previous"]
    index = [item.name for item in chain].index(payload["iteration"])
    prior = chain[index - 1] if index else None
    if prior is None:
        if previous is not None:
            raise ReceiptError("BROKEN_CHAIN", "the first iteration names a predecessor")
        return None
    if previous is None:
        raise ReceiptError("BROKEN_CHAIN", "a later iteration names no predecessor")
    if previous["iteration"] != prior.name or previous["receipt_sha256"] != prior.receipt_sha256:
        raise ReceiptError("PREVIOUS_RECEIPT_MISMATCH", "the pinned predecessor receipt is not the one on disk")
    prior_artifact = prior.payload["generated"]["artifact"]
    if (
        previous["report_revision"] != prior_artifact["report_revision"]
        or previous["model_revision"] != prior_artifact["model_revision"]
    ):
        raise ReceiptError(
            "PREVIOUS_REVISION_MISMATCH", "the pinned predecessor revisions are not the ones it recorded"
        )
    return prior


def _assert_judgement(  # pylint: disable=too-many-branches
    target: PackageTarget, payload: dict[str, Any], prior: Iteration | None
) -> None:
    """The reviewer's half: complete, in-vocabulary, and losing no prior finding.

    ⚠️ The branches are separate REFUSALS, each naming a different thing a reviewer got wrong -
    a pending slot, an invented visual, an undeclared finding id, a lost prior finding. Merging them
    would satisfy the checker by making "which guard refused" unreadable, which is the one thing this
    receipt exists to keep legible.
    """
    generated = payload["generated"]
    expected = {row["page_id"]: tuple(row["expected_visual_ids"]) for row in generated["pages"]}
    judged = payload["judgement"]["pages"]
    if [row["page_id"] for row in judged] != [row["page_id"] for row in generated["pages"]]:
        raise ReceiptError("JUDGEMENT_PAGE_SET", "the judgement rows do not match the captured pages")
    findings = payload["judgement"]["findings"]
    ids = [finding["id"] for finding in findings]
    if len(set(ids)) != len(ids):
        raise ReceiptError("FINDING_ID_DUPLICATE", "two findings share one id")
    for finding in findings:
        if not FINDING_ID_RE.match(finding["id"]):
            raise ReceiptError("FINDING_ID_MALFORMED", f"{finding['id']!r} is not an F-NNN finding id")
    known = set(ids)
    for row in judged:
        if row["whole_page_status"] == STATUS_PENDING:
            raise ReceiptError("PENDING_JUDGEMENT", f"page {row['page_id']!r} still has a pending verdict")
        for section in ("visual_results", "numeric_results"):
            if tuple(item["visual_id"] for item in row[section]) != expected[row["page_id"]]:
                raise ReceiptError("JUDGEMENT_VISUAL_SET", f"page {row['page_id']!r}'s {section} do not match the PBIR")
            for item in row[section]:
                if item["status"] == STATUS_PENDING:
                    raise ReceiptError("PENDING_JUDGEMENT", f"{item['visual_id']!r} still has a pending verdict")
                unknown = sorted(set(item["finding_ids"]) - known)
                if unknown:
                    raise ReceiptError("UNKNOWN_FINDING_ID", f"finding id(s) {unknown} are referenced but not declared")
    _assert_accepted_limitations(target.root, findings)
    if prior is not None:
        missing = sorted({finding["id"] for finding in prior.payload["judgement"]["findings"]} - known)
        if missing:
            raise ReceiptError("FINDING_DISAPPEARED", f"prior finding(s) {missing} do not reappear in this iteration")


def _assert_accepted_limitations(package: Path, findings: list[dict[str, Any]]) -> None:
    """An accepted limitation must bind to a CURRENT spec entry, by index and by entry hash."""
    entries = (json_object(package / MIGRATION_SPEC_NAME) or {}).get("limitations_encountered")
    for finding in findings:
        if finding["status"] != FINDING_ACCEPTED:
            if finding["limitation_ref"] is not None:
                raise ReceiptError(
                    "LIMITATION_REF_UNEXPECTED", f"{finding['id']} names a limitation but is not accepted"
                )
            continue
        reference = finding["limitation_ref"]
        if reference is None:
            raise ReceiptError("ACCEPTED_LIMITATION_UNBOUND", f"{finding['id']} is accepted but names no limitation")
        match = re.fullmatch(r"/limitations_encountered/(\d+)", reference["pointer"])
        if not match or not isinstance(entries, list) or int(match.group(1)) >= len(entries):
            raise ReceiptError(
                "ACCEPTED_LIMITATION_UNBOUND", f"{finding['id']} points at no current limitations_encountered entry"
            )
        entry = entries[int(match.group(1))]
        digest = hashlib.sha256(
            json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if digest != reference["sha256"]:
            raise ReceiptError(
                "ACCEPTED_LIMITATION_UNBOUND", f"{finding['id']} names a limitation entry whose text has changed"
            )


def limitation_entry_sha256(entry: Any) -> str:
    """The canonical hash a finding must record when it accepts a limitation."""
    return hashlib.sha256(
        json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _outcome(payload: dict[str, Any]) -> str:
    """`complete` only for an all-page sign-off with no residue. Never inferred from exit codes."""
    if payload["mode"] != MODE_SIGN_OFF or payload["generated"]["scope"] != SCOPE_ALL_PAGES:
        return OUTCOME_INCOMPLETE
    if any(not row["powerbi"]["converged"] for row in payload["generated"]["pages"]):
        return OUTCOME_INCOMPLETE
    for row in payload["judgement"]["pages"]:
        statuses = (
            [row["whole_page_status"]]
            + [item["status"] for item in row["visual_results"]]
            + [item["status"] for item in row["numeric_results"]]
        )
        if any(status not in COMPLETING_STATUSES for status in statuses):
            return OUTCOME_INCOMPLETE
    if any(finding["status"] == FINDING_OPEN for finding in payload["judgement"]["findings"]):
        return OUTCOME_INCOMPLETE
    if payload["generated"]["data_evidence"]["status"] != DATA_STATUS_ACCEPTED:
        return OUTCOME_INCOMPLETE
    return OUTCOME_COMPLETE


def finalize(package: Path, iteration: str | None = None) -> dict[str, Any]:
    """Validate a pending iteration against CURRENT truth and seal it. Returns the sealed receipt."""
    target = resolve_package(package)
    chain = read_chain(package)
    if not chain:
        raise ReceiptError("NO_ITERATION", "this package has no iteration to finalize")
    name = iteration or chain[-1].name
    selected = next((item for item in chain if item.name == name), None)
    if selected is None:
        raise ReceiptError("NO_ITERATION", f"this package has no iteration {name!r}")
    payload = selected.payload
    if payload["state"] == STATE_FINAL:
        raise ReceiptError("ALREADY_FINAL", f"iteration {name} is already final")
    prior = _assert_previous(chain, payload)
    inventory = _assert_inventory(target, payload)
    _assert_images(payload, selected.directory)
    _assert_changes_from_previous(payload, prior)
    _assert_judgement(target, payload, prior)
    _assert_tableau(target, payload, inventory)
    _assert_revisions(target, payload)
    payload["judgement"]["completed_at"] = now_rfc3339()
    payload["state"] = STATE_FINAL
    payload["outcome"] = _outcome(payload)
    write_receipt(selected.directory, payload)
    return payload
