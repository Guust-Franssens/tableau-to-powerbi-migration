"""
purpose: stamp a migration input with where it came from, so a finding filed weeks later is still
         reproducible - and so a reader can tell whether their copy is the same build as ours.
usage:   python scripts/stamp_tableau_provenance.py --input <folder-or-.twbx> [--env .env] [--out PATH]

Why this exists
---------------
The deterministic engine already records the LOCAL half in ``input_manifest.json`` - name, size,
sha256, mtime, staged path, ``source_kind: "LocalFilesSource"``. What no artifact records is the
UPSTREAM half: which Tableau site the file came from, which workbook LUID, which project, who owns
it, when it was last published, and which Tableau build produced it.

That gap is not theoretical. Filing three defects against Tableau's **Superstore** sample required
reconstructing all of it by hand, and it mattered: Tableau's samples differ between releases and
between the Desktop-bundled copy and the Cloud *Samples* project copy, so "we tested on Superstore"
is not a reproducible statement. Figures cited in a defect report - a row count, a column total - do
not reproduce against a different build, and the reader cannot tell that is what happened.

What it emits
-------------
Two independent layers, so the file is useful even with no Tableau access at all:

* **fingerprint** (always) - size, sha256, and for a ``.twbx`` the inner zip entries with their CRCs.
  The entry CRCs are the useful part for a third party: they can compare their own copy member by
  member without either side redistributing a vendor's sample workbook, which matters when the other
  repo is a clean room that deliberately commits no third-party content.
* **origin** (when credentials are supplied and the workbook is found on the site) - server, site,
  workbook LUID, project, owner, ``updatedAt``, plus the Tableau product and REST API versions.

Matching prefers the **workbook LUID**, and falls back to the **name**; either way it is confirmed by
re-downloading and comparing the sha256, because a name alone is not identity - a point this
toolchain has now been bitten by four separate times. When the hash does not match, that is recorded
as ``origin.match: "name_only"`` rather than silently claimed as the source: a same-named workbook
that is a different build is exactly the situation this file exists to make visible.

NOTE: **The LUID path is not an optimisation, it is the only thing that works on harvested input.**
``harvest_estate_assets.py`` names every download ``<luid>_<sanitized-name><ext>`` on purpose
(display names are not unique across projects), so a stem-vs-``name`` comparison compares
``4f2c...-a1_Sales_Q3_Review`` against ``Sales - Q3 Review`` and can never match. Measured: **20 of
20** harvested workbooks reported ``no workbook of this name on the site`` while every one of them
was present. Stripping the prefix alone is not sufficient either - ``safe_component`` also rewrites
every non ``[A-Za-z0-9-_]`` character to ``_`` and truncates to 60 chars, so the remainder is lossy
and cannot be inverted. The LUID is exact, which is why it is tried first.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Literal, NamedTuple
from urllib.parse import unquote, urlencode, urlsplit

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_tableau  # noqa: E402  # pylint: disable=wrong-import-position
from object_identity import RevisionKey, revision_key  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import pat_secret, redact, resolve_env, scrub_tree  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("provenance")

WORKBOOK_SUFFIXES = (".twb", ".twbx")
SUCCESS_STATUSES = frozenset({"success", "local_only"})
SCHEMA = "tableau-source-provenance/1"
PUBLISHED_DEPENDENCIES_SCHEMA = "tableau-published-dependencies/v1"
WORKER_PROTOCOL = "tableau-provenance-worker/2"
LUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

#: The operation named by every fault this module records about a result's OWN SHAPE, so a consumer
#: can tell "the site refused us" from "this result does not describe anything".
CONSISTENCY_OPERATION = "validate-result"

#: The status a self-contradictory result is normalised to. Deliberately one of the statuses that
#: already exist rather than a new vocabulary: a consumer that already refuses `failed` refuses this
#: too, without learning a second state machine.
UNASSESSABLE_STATUS = "failed"

# --------------------------------------------------------------------------- the worker protocol
#
# Issue #576: every operation below can block past any socket timeout - a trickled response body is
# not a connect timeout, and neither a local `read_bytes` nor a recursive scrub has one at all. The
# only mechanism measured to preempt all of them on Windows AND POSIX is a separate process the
# supervisor can terminate, so `build()` is instrumented to report progress and completed evidence
# to a parent that owns the deadline. The channel is deliberately TINY and closed: numeric, boolean
# or already-safe messages, nothing free-form, and no credential ever travels back over it.

MSG_INPUTS_DISCOVERED = "inputs-discovered"
MSG_OPERATION = "operation"
MSG_CHECKPOINT = "checkpoint"
MSG_WORKBOOK_IDENTITY = "workbook-identity"
MSG_PUBLISHED_EVIDENCE = "published-evidence"
MSG_LOOKUP_INTENT = "lookup-intent"
MSG_INVENTORY_FACTS = "inventory-facts"
MSG_INVENTORY_FAILED = "inventory-failed"
MSG_SAFE_SNAPSHOT = "safe-snapshot"
MSG_TERMINAL = "terminal"

OP_COLLECT_INPUTS = "collect-inputs"
OP_FINGERPRINT = "fingerprint"
OP_SIGN_IN = "sign-in"
OP_INVENTORY = "inventory"
OP_CONTENT = "content"
OP_SCRUB = "scrub"
OP_SIGN_OUT = "sign-out"

#: Every operation the WORKER may name. The supervisor allows two more (`phase`, `publish`) that
#: describe parent-owned work and can therefore never arrive over the pipe.
WORKER_OPERATIONS = frozenset(
    {OP_COLLECT_INPUTS, OP_FINGERPRINT, OP_SIGN_IN, OP_INVENTORY, OP_CONTENT, OP_SCRUB, OP_SIGN_OUT}
)

#: The stable code a supervisor records for evidence the worker never finished.
DEADLINE_CODE = "deadline-expired"
CANCELLED_CODE = "cancelled"
RESULT_STATUSES = SUCCESS_STATUSES | {"partial", "failed", "empty"}
NO_ORIGIN_NOTE = "no workbook of this LUID or name on the site - local-only input"
INCOMPLETE_INVENTORY_NOTE = "workbook not found in the incomplete inventory page - origin cannot be established"
INVENTORY_PAGE_SIZE = 1000
INVENTORY_MAX_COUNT = (1 << 63) - 1
INVENTORY_ERROR_CODES = frozenset({"inventory-truncated", "inventory-cannot-establish"})
INVENTORY_FACT_KEYS = frozenset(
    {"returned_count", "requested_page_size", "page_number", "page_size", "total_available"}
)
INVENTORY_RAW_FACT_KEYS = INVENTORY_FACT_KEYS | {"invalid_fields"}

# Only these labels, never an exception's dynamically supplied name or text, cross the channel.
ERROR_CLASSES = frozenset(
    {
        "Exception",
        "OSError",
        "PermissionError",
        "FileNotFoundError",
        "IsADirectoryError",
        "NotADirectoryError",
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "HTTPError",
        "URLError",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "RuntimeError",
        "BadZipFile",
        "LargeZipFile",
        "UnicodeDecodeError",
        "JSONDecodeError",
        "RecursionError",
        "MemoryError",
    }
)


class ProvenanceChannel:
    """Spawn-picklable socket endpoint; length-prefixed UTF-8 JSON, never received pickle.

    Reads may block on an incomplete frame. ONLY the parent's daemon transport thread reads this
    channel; the supervising thread never does. A bounded header is checked before body allocation.
    """

    def __init__(self, endpoint: socket.socket) -> None:
        self.endpoint = endpoint

    def send(self, message: dict[str, Any]) -> None:
        """Send one JSON frame. Serialization and blocking writes belong to the leaf worker."""
        payload = json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.endpoint.sendall(struct.pack("!I", len(payload)) + payload)

    def recv_bytes(self, maximum: int) -> bytes:
        """Read a capped frame; this is deliberately NOT advertised as a deadline-aware read."""
        size = struct.unpack("!I", self._read_exact(4, allow_eof=True))[0]
        if not 0 < size <= maximum:
            raise ValueError("invalid frame size")
        return self._read_exact(size)

    def _read_exact(self, size: int, allow_eof: bool = False) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.endpoint.recv(min(size - len(chunks), 65536))
            if not chunk:
                if allow_eof and not chunks:
                    raise EOFError
                raise ValueError("truncated frame")
            chunks.extend(chunk)
        return bytes(chunks)

    def shutdown(self) -> None:
        """Interrupt a parent-side read without waiting for the peer or acquiring a peer lock."""
        self.endpoint.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        """Release this process's socket handle."""
        self.endpoint.close()


def _exception_class(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if name in ERROR_CLASSES else "Exception"


def _error(code: str, operation: str, exc: BaseException | None = None, **facts: int) -> dict[str, Any]:
    """A stable failure record containing no exception or response text."""
    record: dict[str, Any] = {"code": code, "operation": operation}
    if exc is not None:
        record["exception_class"] = _exception_class(exc)
        for attribute in ("errno", "winerror"):
            value = getattr(exc, attribute, None)
            if isinstance(value, int) and not isinstance(value, bool):
                record[attribute] = value
    record.update(
        {key: value for key, value in facts.items() if isinstance(value, int) and not isinstance(value, bool)}
    )
    return record


def _result(inputs: list[dict[str, Any]], status: str, errors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One complete provenance result in the schema published by every build path."""
    return {
        "schema": SCHEMA,
        "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "input_count": len(inputs),
        "inputs": inputs,
        "phase": {"status": status, "errors": errors or []},
    }


def failure_result(code: str, operation: str, exc: BaseException) -> dict[str, Any]:
    """A complete safe result for a failure before :func:`build` could return one."""
    return _result([], "failed", [_error(code, operation, exc)])


def consistency_faults(result: Any) -> list[str]:
    """Stable codes for every way ``result`` CONTRADICTS ITSELF. Empty means self-consistent.

    ⚠️ A status is a CLAIM, not evidence. A result carrying ``input_count: 0``, ``inputs: []`` and a
    ``success``/``local_only`` status describes no input at all while reading as a pass, so a reader
    that trusts the status alone lets a run that stamped nothing continue to adjudication and
    handover. This is the single place that decides self-consistency: :func:`normalize_result` uses
    it to decide what may be PUBLISHED and :func:`is_success` to decide the VERDICT, so the two can
    never drift apart.

    An honest zero-input result is `empty`, and an honest local-only result has a positive count
    matching its list - neither is a fault here.
    """
    if not isinstance(result, dict):
        return ["result-not-a-mapping"]

    faults: list[str] = []
    records = result.get("inputs")
    if not isinstance(records, list):
        faults.append("inputs-not-a-list")
        records = []

    count = result.get("input_count")
    counted: int | None = None
    if isinstance(count, bool) or not isinstance(count, int):
        faults.append("input-count-not-an-integer")
    elif count < 0:
        faults.append("input-count-negative")
    else:
        counted = count
        if count != len(records):
            faults.append("input-count-mismatch")

    phase = result.get("phase")
    status = phase.get("status") if isinstance(phase, dict) else None
    if not isinstance(status, str) or status not in RESULT_STATUSES:
        faults.append("phase-status-unassessable")
    elif status in SUCCESS_STATUSES and not (counted and records):
        faults.append("success-without-inputs")
    return faults


def normalize_result(result: Any) -> dict[str, Any]:
    """The result that may be PUBLISHED: a self-contradictory one is rewritten as a failure.

    Exactly one artifact is still published - suppressing it would destroy the only evidence of what
    went wrong - but it carries the existing typed phase-error shape, the faults it was normalised
    for, and a status that no consumer reads as a pass. Prior errors are kept: the contradiction is
    added to the record, never substituted for it.
    """
    faults = consistency_faults(result)
    if not faults:
        return result

    source = result if isinstance(result, dict) else {}
    records = source.get("inputs")
    records = records if isinstance(records, list) else []
    claimed = source.get("input_count")
    facts = {"claimed_input_count": claimed} if isinstance(claimed, int) and not isinstance(claimed, bool) else {}

    phase = source.get("phase")
    prior = phase.get("errors") if isinstance(phase, dict) else None
    errors = list(prior) if isinstance(prior, list) else []
    errors.extend(_error(code, CONSISTENCY_OPERATION, **facts) for code in faults)

    normalized = dict(source)
    normalized["schema"] = source.get("schema") if isinstance(source.get("schema"), str) else SCHEMA
    if not isinstance(normalized.get("stamped_at"), str):
        normalized["stamped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    normalized["input_count"] = len(records)
    normalized["inputs"] = records
    normalized["phase"] = {"status": UNASSESSABLE_STATUS, "errors": errors}
    return normalized


def is_success(result: Any) -> bool:
    """Whether ``result`` may be treated as a pass: self-consistent AND claiming a success status."""
    if consistency_faults(result):
        return False
    return result["phase"]["status"] in SUCCESS_STATUSES


def phase_error(code: str, operation: str, **facts: int) -> dict[str, Any]:
    """A stable failure record a SUPERVISOR may record about work this module never finished.

    The same shape :func:`_error` builds for the worker's own faults, exported so the deadline owner
    does not grow a second error vocabulary that a consumer would have to learn.
    """
    return _error(code, operation, **facts)


def phase_result(inputs: list[dict[str, Any]], status: str, errors: list[dict[str, Any]] | None = None) -> dict:
    """One complete provenance document assembled by a SUPERVISOR from the evidence it accepted."""
    return _result(inputs, status, errors)


def unavailable_input(code: str, operation: str = OP_FINGERPRINT) -> dict[str, Any]:
    """The placeholder for an input whose evidence never arrived.

    Explicit rather than absent: ``input_count`` must keep equalling ``len(inputs)`` (that identity is
    what :func:`consistency_faults` refuses to let a result contradict), and "we did not get to this
    one" is a different statement from "this one is fine". It names no file: an input that was never
    fingerprinted is identified by its ORDINAL, so a placeholder cannot leak a filename.
    """
    return {"input": {"status": "unavailable"}, "fingerprint_error": _error(code, operation)}


def checkpoint_record(record: dict[str, Any], source: _PublishedSource | None = None) -> dict[str, Any]:
    """One completed input reduced to what this module DERIVED, ready to cross a process boundary.

    A checkpoint is emitted BEFORE the live half has been scrubbed, so it may carry nothing copied
    from the environment - not the filename, not a member name. Sizes, digests and CRCs cannot carry
    a credential (:func:`_derived_only` is the same reduction the unusable-redactor path falls back
    to), and the typed fingerprint error carries a class name and an errno, never a message.
    """
    derived = _derived_only(record.get("input") or {})
    reduced: dict[str, Any] = {"input": derived or {"status": "unavailable"}}
    if isinstance(record.get("fingerprint_error"), dict):
        reduced["fingerprint_error"] = record["fingerprint_error"]
    if source is not None:
        reduced["published_occurrences"] = (
            None if source.dependencies is None else published_occurrence_checkpoint(source.dependencies)
        )
        reduced["launch_identity"] = workbook_identity_checkpoint(source.path, split_harvest_stem(source.path.stem)[0])
    return reduced


def workbook_identity_checkpoint(path: Path, luid: str | None) -> dict:
    """Bind the launched path and independently observed workbook without transmitting either."""
    return {
        "file_sha256": hashlib.sha256(str(path.absolute()).encode("utf-8", errors="surrogatepass")).hexdigest(),
        "basename_sha256": hashlib.sha256(path.name.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "workbook_luid_sha256": workbook_luid_digest(luid) if isinstance(luid, str) else None,
    }


def workbook_luid_digest(luid: str) -> str:
    """Private case-insensitive identity evidence; never a copied REST response value."""
    return hashlib.sha256(luid.lower().encode("utf-8", errors="surrogatepass")).hexdigest()


def published_occurrence_checkpoint(rows: list[dict]) -> list[dict]:
    """Private wire evidence: keep physical ordinals and digest keys, never copy source text."""
    return [
        {
            "source_ordinal": row["source_ordinal"],
            "published_key_sha256": hashlib.sha256(
                json.dumps(row["published_key"], ensure_ascii=True).encode()
            ).hexdigest(),
        }
        for row in rows
    ]


def published_outcome_checkpoint(rows: list[dict]) -> list[dict]:
    """Bind each catalog/detail outcome to its held-source occurrence, without copying its text."""
    return [
        identity
        | {"state": row["state"], "candidate_count": row["candidate_count"]}
        | ({"datasource_luid_sha256": workbook_luid_digest(row["datasource_luid"])} if "datasource_luid" in row else {})
        for identity, row in zip(published_occurrence_checkpoint(rows), rows)
    ]


def has_identity_controls(text: str) -> bool:
    """C0, DEL and C1 are not source/request identities; ordinary Unicode remains usable."""
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text)


def valid_published_key(value: object) -> bool:
    """The exact parser key must survive, not a sanitized replacement for an invalid identity."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 1024 and not has_identity_controls(value)


class NullReporter:
    """The no-op channel :func:`build` uses when nobody is supervising it.

    The standalone CLI and every direct test call :func:`build` in-process; instrumenting it must not
    make it depend on having a parent.
    """

    cancelled = False

    def inputs_discovered(self, total: int) -> None:
        """Ignore the discovered input count."""

    def operation(self, operation: str, completed: int, total: int | None = None) -> None:
        """Ignore an operation counter."""

    def checkpoint(self, index: int, record: dict[str, Any], source: _PublishedSource | None = None) -> None:
        """Ignore a completed-input checkpoint."""

    def workbook_identity(self, index: int, source: _PublishedSource, luid: str | None) -> None:
        """Ignore an independently observed workbook identity."""

    def published_evidence(self, index: int, evidence: dict) -> None:
        """Ignore the pre-publication source rehash and per-occurrence lookup evidence."""

    def lookup_intent(self, requested: bool) -> None:
        """Ignore whether the completed local pass is followed by live work."""

    def inventory_facts(self, facts: dict[str, int | None]) -> None:
        """Ignore the parsed first page's numeric facts."""

    def inventory_failed(self) -> None:
        """Ignore the failed inventory operation; it produced no completeness facts."""

    def safe_snapshot(self, result: dict[str, Any]) -> None:
        """Ignore the scrubbed snapshot."""

    def terminal(self, result: dict[str, Any]) -> None:
        """Ignore the terminal result."""


class WorkerReporter(NullReporter):
    """The worker's end of the pipe, plus the cancellation flag the supervisor sets at the deadline.

    Sending is best-effort: once the supervisor has latched expiry it closes its receive end, and a
    write to that pipe is then an ordinary broken-pipe error. The worker is about to be terminated,
    so the only correct response is to keep going quietly rather than to raise something the parent
    will never see.

    ``cancelled`` is an OPTIMISATION, never the enforcement. It lets the worker stop before starting
    the next expensive operation; what actually bounds an operation already in flight is the parent
    terminating this process.
    """

    def __init__(self, conn: Any, cancel_event: Any = None) -> None:
        self._conn = conn
        self._cancel = cancel_event
        self._discovered = False

    @property
    def cancelled(self) -> bool:  # type: ignore[override]
        """Whether the supervisor has asked for this run to stop."""
        try:
            return self._cancel is not None and bool(self._cancel.value)
        except (OSError, ValueError):  # pragma: no cover - the shared byte died with the parent
            return True

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._conn.send(message)
        except (OSError, ValueError, EOFError):  # pragma: no cover - parent closed the pipe at expiry
            LOG.debug("provenance progress channel closed")

    def inputs_discovered(self, total: int) -> None:
        """Numeric only: how many physical inputs discovery found."""
        self._discovered = True
        self._send({"kind": MSG_INPUTS_DISCOVERED, "total": int(total), "protocol": WORKER_PROTOCOL})

    def operation(self, operation: str, completed: int, total: int | None = None) -> None:
        """One allowlisted operation label and two numbers - never what it was operating on."""
        self._send(
            {
                "kind": MSG_OPERATION,
                "operation": operation,
                "completed": int(completed),
                "total": None if total is None else int(total),
            }
        )

    def checkpoint(self, index: int, record: dict[str, Any], source: _PublishedSource | None = None) -> None:
        """Derived-only evidence for one completed input, addressed by ordinal."""
        self._send({"kind": MSG_CHECKPOINT, "index": int(index), "record": checkpoint_record(record, source)})

    def workbook_identity(self, index: int, source: _PublishedSource, luid: str | None) -> None:
        """Emit the inventory selection before download, origin construction or association issuance."""
        self._send(
            {"kind": MSG_WORKBOOK_IDENTITY, "index": index, "identity": workbook_identity_checkpoint(source.path, luid)}
        )

    def published_evidence(self, index: int, evidence: dict) -> None:
        """Send acquisition facts before constructing the public authority or scrubbing it."""
        self._send({"kind": MSG_PUBLISHED_EVIDENCE, "index": index, "evidence": evidence})

    def lookup_intent(self, requested: bool) -> None:
        """Declare live intent before sign-in without sending any credential or host identity."""
        self._send({"kind": MSG_LOOKUP_INTENT, "requested": bool(requested)})

    def inventory_facts(self, facts: dict[str, int | None]) -> None:
        """Send raw bounded numbers, never the worker's completeness classification."""
        self._send({"kind": MSG_INVENTORY_FACTS, "facts": facts})

    def inventory_failed(self) -> None:
        """Distinguish a completed failed request/parse from a successfully parsed inventory."""
        self._send({"kind": MSG_INVENTORY_FAILED})

    def safe_snapshot(self, result: dict[str, Any]) -> None:
        """The whole result once it is scrubbed - sent BEFORE sign-out, which can hang."""
        self._send({"kind": MSG_SAFE_SNAPSHOT, "result": result})

    def terminal(self, result: dict[str, Any]) -> None:
        """The final result, only after cleanup has finished or produced a typed error."""
        if not self._discovered:
            self.inputs_discovered(0)
        self._send({"kind": MSG_TERMINAL, "result": result})


def fingerprint(path: Path, raw: bytes | None = None) -> dict[str, Any]:
    """Size + sha256 + a reproducible REVISION KEY, plus per-member CRCs for a ``.twbx``.

    The members matter more than the outer hash: a ``.twbx`` is a zip, and zip metadata (timestamps,
    compression) can differ between two downloads of the same content, so two identical workbooks can
    hash differently. Member CRCs compare the content itself.

    ⚠️ That warning was written here and then not acted on where it counted - the origin comparison
    below hashed raw bytes on BOTH sides, so an unchanged workbook read as ``name_only``.
    :func:`object_identity.revision_key` is the content-normalised digest that makes the comparison
    reproducible, and it is recorded on both sides so a consumer never has to guess which it holds.
    """
    raw = path.read_bytes() if raw is None else raw
    record: dict[str, Any] = {
        "file": path.name,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    key = revision_key(raw)
    if key is not None:
        record["revision_key"] = key.as_json()
    if path.suffix.lower() == ".twbx" and zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            record["members"] = [
                {"name": info.filename, "size_bytes": info.file_size, "crc32": f"{info.CRC:08x}"}
                for info in sorted(archive.infolist(), key=lambda i: i.filename)
            ]
    return record


class _PublishedSource(NamedTuple):
    path: Path
    dependencies: list[dict] | None


def _published_source(path: Path, raw: bytes | None) -> _PublishedSource:
    """Assess the SAME immutable bytes as fingerprinting; None is unassessable, [] is proven empty."""
    if raw is None:
        return _PublishedSource(path, None)
    try:
        if path.suffix.lower() not in WORKBOOK_SUFFIXES:
            return _PublishedSource(path, None)
        if path.suffix.lower() == ".twbx":
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = [name for name in archive.namelist() if name.lower().endswith(".twb")]
                if not members:
                    return _PublishedSource(path, None)
                # load_twb_root selects the first member in archive order, not sorted filename order.
                raw = archive.read(members[0])
        root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
        if root.tag != "workbook" or root.getroottree().docinfo.doctype:
            return _PublishedSource(path, None)
        rows = []
        sources = (source for source in root.findall("datasources/datasource") if source.get("name") != "Parameters")
        for ordinal, source in enumerate(sources):
            # Identity belongs to the existing parser, including its deliberately weaker fallbacks.
            published = parse_tableau._parse_published_datasource(  # pylint: disable=protected-access
                source,
                parse_tableau._parse_connection(source, {}),  # pylint: disable=protected-access
            )
            if published is not None:
                rows.append(
                    {
                        "source_ordinal": ordinal,
                        "published_key": published["key"],
                        "content_url": published["id"] if published["name_source"] == "derived-from" else None,
                        "site": published["site"],
                        "derived_from": published["derived_from"],
                    }
                )
        return _PublishedSource(path, rows)
    except Exception:  # pylint: disable=broad-exception-caught
        # Unreadable source identity earns no association, never an invented empty dependency list.
        return _PublishedSource(path, None)


class InventoryCompleteness(NamedTuple):
    """Numeric evidence from the same response as the cached first-page rows."""

    status: Literal["complete", "truncated", "cannot_establish"]
    returned_count: int
    page_number: int | None = None
    page_size: int | None = None
    total_available: int | None = None
    invalid_fields: int = 0

    def facts(self) -> dict[str, int | None]:
        """Keep malformed distinct from absent without copying the malformed response value."""
        return {
            "returned_count": self.returned_count,
            "requested_page_size": INVENTORY_PAGE_SIZE,
            "page_number": self.page_number,
            "page_size": self.page_size,
            "total_available": self.total_available,
            "invalid_fields": self.invalid_fields,
        }

    def error(self) -> dict[str, Any] | None:
        """Only an incomplete inventory adds a finding; no copied response fields survive."""
        if self.status == "complete":
            return None
        code = "inventory-truncated" if self.status == "truncated" else "inventory-cannot-establish"
        facts = {"returned_count": self.returned_count, "requested_page_size": INVENTORY_PAGE_SIZE}
        for key, value in (
            ("page_number", self.page_number),
            ("page_size", self.page_size),
            ("total_available", self.total_available),
        ):
            if value is not None:
                facts[key] = value
        return _error(code, OP_INVENTORY, **facts)


def _pagination_count(value: object) -> int | None:
    """REST counts may be decimal strings or integers, never coercible/nonfinite values."""
    if isinstance(value, str):
        if re.fullmatch(r"[0-9]{1,19}", value) is None:
            return None
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= INVENTORY_MAX_COUNT:
        return value
    return None


def _inventory_completeness(returned_count: int, metadata: object) -> InventoryCompleteness:
    """Parse only bounded numeric facts, retaining malformed-versus-missing evidence."""
    if not isinstance(metadata, dict):
        return InventoryCompleteness("cannot_establish", returned_count, invalid_fields=1)
    counts = {
        key: _pagination_count(metadata[key]) for key in ("pageNumber", "pageSize", "totalAvailable") if key in metadata
    }
    page = InventoryCompleteness(
        "cannot_establish",
        returned_count,
        counts.get("pageNumber"),
        counts.get("pageSize"),
        counts.get("totalAvailable"),
        sum(value is None for value in counts.values()),
    )
    return classify_inventory(page.facts())


def classify_inventory(facts: dict[str, int | None]) -> InventoryCompleteness:
    """Classify parsed numeric facts; the supervisor calls this independently of the worker verdict."""
    page = InventoryCompleteness(
        "cannot_establish",
        facts["returned_count"],
        facts["page_number"],
        facts["page_size"],
        facts["total_available"],
        facts["invalid_fields"],
    )
    # An independently valid total proves missing rows even when a sibling field is malformed.
    if page.total_available is not None and page.total_available > page.returned_count:
        return page._replace(status="truncated")
    if page.invalid_fields:
        return page
    if (
        page.returned_count > facts["requested_page_size"]
        or page.page_number not in (None, 1)
        or page.page_size not in (None, facts["requested_page_size"])
        or (page.total_available is not None and page.total_available < page.returned_count)
    ):
        return page
    if page.total_available is not None:
        return page._replace(status="complete")
    return page._replace(status="complete") if page.returned_count < facts["requested_page_size"] else page


def _inventory_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """No authority-bearing REST object may silently replace a duplicate field."""
    record = dict(pairs)
    if len(record) != len(pairs):
        raise ValueError("duplicate REST response field")
    return record


def _finite_json_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("nonfinite REST response number")
    return value


def _rest_json(payload: bytes) -> Any:
    """One strict decoder for sign-in, inventory, user visibility, catalog and detail."""
    return json.loads(
        payload,
        object_pairs_hook=_inventory_object,
        parse_float=_finite_json_number,
        parse_constant=_finite_json_number,
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Authority and credentials belong to the configured endpoint, never to a redirect target."""

    def redirect_request(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise urllib.error.HTTPError(req.full_url, code, "Tableau redirect refused", headers, fp)


class TableauLookup:  # pylint: disable=too-many-instance-attributes
    """Minimal read-only REST client, used only to identify a workbook we already hold.

    Initial remote answers are fetched **at most once per instance**, because one instance is one
    provenance run. Published associations separately acquire current workbook identity and uncached
    visibility/catalog/detail, then revalidate remote content just before issuance. Only duplicate
    occurrences within one physical input share an association acquisition.
    Measured 2026-09-09 against a recording loopback site on the pre-cache code, 66
    harvested inputs cost **200** remote calls -- ``2 + N + 2M``: one site-wide inventory listing per
    input, and *two* full downloads of every matched workbook, because
    :meth:`content_sha256` and :meth:`content_revision_key` each fetched independently. The bytes were
    identical on every repeat for 66 of 66 LUIDs, so every repeat was pure cost, and at an ordinary
    large-``.twbx`` latency of ~9 s that is the >20 min field stall reported in #576.

    Failures are cached too, and separately per operation: a dead inventory is asked **once** rather
    than once per input (measured: 66 identical failing listings), while a workbook whose content
    cannot be read latches only *that* LUID, so a different workbook is still tried honestly.
    """

    def __init__(self, env: dict[str, str]) -> None:
        # Site/session fields precede this run's cached answers and their evidence.
        _tableau_url(env["TABLEAU_SERVER_URL"], configured=True)
        self.base = env["TABLEAU_SERVER_URL"].rstrip("/")
        self.version = env.get("TABLEAU_REST_API_VERSION", "3.21")
        self.site = env["TABLEAU_SITE"]
        self.product_version = env.get("TABLEAU_PRODUCT_VERSION")
        self._pat = (env["TABLEAU_PAT_NAME"], pat_secret(env))
        self.token: str | None = None
        self.site_id: str | None = None
        self.user_id: str | None = None
        self._inventory: list[dict[str, Any]] | None = None
        self._inventory_failure: Exception | None = None
        self.inventory_completeness: InventoryCompleteness | None = None
        self.matched_luids: set[str] = set()
        self._content_cache: dict[str, bytes | None] = {}
        self._content_failure: dict[str, Exception] = {}
        self._content_unavailable: dict[str, str] = {}
        self._opener = urllib.request.build_opener(_NoRedirectHandler())
        self.reporter: NullReporter = NullReporter()

    def _call(self, method: str, path: str, body: dict | None = None, accept: str | None = None):
        request = urllib.request.Request(
            f"{self.base}/api/{self.version}{path}",
            data=json.dumps(body).encode() if body else None,
            method=method,
        )
        if accept:
            request.add_header("Accept", accept)
        if body:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("X-Tableau-Auth", self.token)
        if method == "GET":
            request.add_header("Cache-Control", "no-cache")
            request.add_header("Pragma", "no-cache")
        try:
            with self._opener.open(request, timeout=180) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def sign_in(self) -> None:
        """Exchange the PAT for a session token."""
        status, payload = self._call(
            "POST",
            "/auth/signin",
            accept="application/json",
            body={
                "credentials": {
                    "personalAccessTokenName": self._pat[0],
                    "personalAccessTokenSecret": self._pat[1],
                    "site": {"contentUrl": self.site},
                }
            },
        )
        if status != 200:
            raise RuntimeError(f"Tableau sign-in failed: HTTP {status}")
        creds = _rest_json(payload)["credentials"]
        self.token, self.site_id = creds["token"], creds["site"]["id"]
        self.user_id = (creds.get("user") or {}).get("id")

    def sign_out(self) -> Exception | None:
        """Best-effort release of the session - a transport failure here must cost nothing.

        ⚠️ Measured 2026-09-09: a sign-out whose connection was closed without a response raised out
        of :func:`build` *after every input had been fingerprinted and matched*, and the CLI exited 1
        with **no file at all** (3 of 3 fingerprints lost); under ``run_estate`` the same failure was
        swallowed into a one-line warning and no phase evidence. The session expires on its own, so
        the only correct behaviour is to drop the token and continue.
        """
        failure = None
        if self.token:
            try:
                self._call("POST", "/auth/signout")
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                LOG.debug("Tableau sign-out failed (%s) - session left to expire", _exception_class(exc))
                failure = exc
            finally:
                self.token = None
        return failure

    def redact_text(self, text: str) -> str:
        """Redact credentials that an authenticated response might reflect."""
        return redact(text, self._pat[0], self._pat[1], self.token or "")

    def workbooks(self) -> list[dict[str, Any]]:
        """The first inventory page, cached **once per run** with its completeness or failure.

        This is the legacy origin observation, not current published-association authority.
        Latching its failure also avoids repeating a dead initial listing for every input.
        """
        if self._inventory_failure is not None:
            raise self._inventory_failure
        if self._inventory is None:
            try:
                self._inventory = self._fetch_workbooks()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._inventory_failure = exc
                self.reporter.inventory_failed()
                raise
        return self._inventory

    def _fetch_workbooks(self) -> list[dict[str, Any]]:
        rows, self.inventory_completeness = self._workbook_inventory()
        self.reporter.inventory_facts(self.inventory_completeness.facts())
        return rows

    def _workbook_inventory(self) -> tuple[list[dict[str, Any]], InventoryCompleteness]:
        if self.reporter.cancelled:
            raise RuntimeError(CANCELLED_CODE)
        status, payload = self._call(
            "GET", f"/sites/{self.site_id}/workbooks?pageSize={INVENTORY_PAGE_SIZE}", accept="application/json"
        )
        if status != 200:
            raise RuntimeError(f"listing workbooks failed: HTTP {status}")
        document = _rest_json(payload)
        if not isinstance(document, dict) or not isinstance(document.get("workbooks"), dict):
            raise ValueError("invalid workbook inventory")
        rows = document["workbooks"].get("workbook", [])
        rows = ([rows] if rows else []) if isinstance(rows, dict) else rows
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("invalid workbook inventory rows")
        return rows, _inventory_completeness(len(rows), document.get("pagination", {}))

    def current_workbook(self, stem: str) -> dict | None:
        """Reacquire this input's identity/uniqueness without changing the legacy inventory event."""
        try:
            rows, completeness = self._workbook_inventory()
            index = _WorkbookIndex(rows)
            _, candidates = index.match(*split_harvest_stem(stem))
            luid = candidates[0].get("id") if candidates else None
            valid = isinstance(luid, str) and LUID_RE.fullmatch(luid) is not None
            return {
                "inventory": completeness.facts(),
                "candidate_count": len(candidates),
                "luid_count": len(index.by_luid.get(luid.lower(), [])) if valid else 0,
                "workbook_luid_sha256": workbook_luid_digest(luid) if valid else None,
            }
        except Exception:  # pylint: disable=broad-exception-caught
            return None

    def content_sha256(self, workbook_id: str) -> str | None:
        """sha256 of the workbook as the server would hand it to us, or ``None`` if it cannot be read.

        ``None`` means **unread**, never "different" - :meth:`content_unavailable` carries the reason.
        """
        payload = self._content(workbook_id)
        return hashlib.sha256(payload).hexdigest() if payload is not None else None

    def content_revision_key(self, workbook_id: str) -> RevisionKey | None:
        """The REPRODUCIBLE build digest of the site copy, or None when it cannot be computed.

        ⚠️ :meth:`content_sha256` above is not reproducible and never was. Measured 2026-09-03 against
        this site, three downloads of every item inside one run: the raw digest differed across
        downloads for **27 of 49 archives** (18 of 48 workbooks, 9 of 19 datasources) while the
        content-normalised digest differed for **0 of 67**. Every raw-unstable item had an identical
        byte length, and the population is itself unstable - ``World Indicators`` differed in one
        sample and agreed minutes later - so a raw comparison does not merely fail for a fixed
        subset: any "confirmed" verdict it produces is luck.

        Both digests read the SAME cached payload, which is not merely cheaper: two downloads of one
        archive can differ byte for byte, so digesting two of them describes two different blobs.
        """
        payload = self._content(workbook_id)
        return revision_key(payload) if payload is not None else None

    def content_unavailable(self, workbook_id: str) -> str | None:
        """Why this workbook's bytes could not be read, or ``None`` when they were read.

        The reason is the HTTP **status number only** - never response text, which an authenticated
        site can reflect a credential into. Cached with the miss, so the answer costs no extra call
        and every input resolving to that LUID gets the same honest reason.
        """
        return self._content_unavailable.get(workbook_id)

    def content_attempts(self) -> int:
        """How many DISTINCT workbooks this run actually asked the site for.

        A cache hit is not a new identity attempt (#582). Published-source revalidation may download
        that same workbook again, but it does not increase this distinct-workbook counter.
        """
        return len(self._content_cache) + len(self._content_failure)

    def _content(self, workbook_id: str) -> bytes | None:
        """The site's bytes for one workbook, downloaded at most once - miss and failure cached.

        The cache is keyed by LUID, so a folder holding two copies of one harvested workbook (or two
        inputs resolving to one site item) pays for one download. A transport failure latches that
        LUID only: a *different* workbook may well be readable, and pretending otherwise would turn
        one dead item into a site-wide "local only" verdict.

        ⚠️ A non-200 answer caches as ``None`` **plus a reason**. Without the reason a 404 was
        indistinguishable from a download whose bytes disagreed, and the caller duly recorded
        ``match: "name_only"`` with "the bytes DIFFER from the site copy" - a drift claim about bytes
        nobody ever saw.
        """
        if workbook_id in self._content_failure:
            raise self._content_failure[workbook_id]
        if workbook_id not in self._content_cache:
            try:
                if self.reporter.cancelled:
                    raise RuntimeError(CANCELLED_CODE)
                status, payload = self._call(
                    "GET", f"/sites/{self.site_id}/workbooks/{workbook_id}/content?includeExtract=True"
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._content_failure[workbook_id] = exc
                raise
            if status != 200:
                self._content_unavailable[workbook_id] = f"HTTP {int(status)}"
            self._content_cache[workbook_id] = payload if status == 200 else None
        return self._content_cache[workbook_id]

    def current_content(self, workbook_id: str) -> dict | None:
        """One uncached, cancellable recheck; never overwrite the initial content observation."""
        if self.reporter.cancelled:
            return None
        try:
            status, payload = self._call(
                "GET", f"/sites/{self.site_id}/workbooks/{workbook_id}/content?includeExtract=True"
            )
            if status != 200:
                return None
            key = revision_key(payload)
            return {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "revision_key": key.as_json() if key is not None else None,
            }
        except Exception:  # pylint: disable=broad-exception-caught
            # A failed recheck earns no current authority; never reflect its response or exception.
            return None

    def _catalog_visible(self) -> bool:
        """Pagination counts only visible rows; an independently queried admin role is also required."""
        if not all(isinstance(value, str) and LUID_RE.fullmatch(value) for value in (self.site_id, self.user_id)):
            return False
        document = self._dependency_json("/".join((f"/sites/{self.site_id}", "users", f"{self.user_id}")))
        user = document.get("user")
        return (
            isinstance(user, dict)
            and user.get("id") == self.user_id
            and user.get("siteRole") in {"ServerAdministrator", "SiteAdministratorCreator", "SiteAdministratorExplorer"}
        )

    def _dependency_json(self, path: str) -> dict:
        if self.reporter.cancelled:
            raise RuntimeError(CANCELLED_CODE)
        status, payload = self._call("GET", path, accept="application/json")
        if status != 200:
            raise ValueError("published dependency request unavailable")
        document = _rest_json(payload)
        if not isinstance(document, dict):
            raise ValueError("invalid published dependency response")
        return document

    def _datasource_detail(self, luid: str) -> dict | None:
        document = self._dependency_json(f"/sites/{self.site_id}/datasources/{luid}")
        detail = document.get("datasource")
        return detail if isinstance(detail, dict) else None

    def published_dependency(self, content_url: str) -> tuple[dict, dict]:
        """Acquire one current association and its private facts, never a run-cached answer."""
        outcome = {"state": "cannot_establish", "candidate_count": None}
        acquisition = {
            "visible": False,
            "catalog": None,
            "candidate_luid_sha256": None,
            "candidate_sha256": None,
            "detail_sha256": None,
        }
        try:
            acquisition["visible"] = self._catalog_visible()
            if acquisition["visible"]:
                outcome = self._query_published_dependency(content_url, acquisition)
        except Exception:  # pylint: disable=broad-exception-caught
            # Neither response rows nor exception text belongs in diagnostics or the authority.
            pass
        return outcome, acquisition

    def _query_published_dependency(self, content_url: str, acquisition: dict) -> dict:
        query = urlencode({"filter": f"contentUrl:eq:{content_url}", "pageSize": INVENTORY_PAGE_SIZE, "pageNumber": 1})
        document = self._dependency_json(f"/sites/{self.site_id}/datasources?{query}")
        rows = document["datasources"].get("datasource", [])
        rows = ([rows] if rows else []) if isinstance(rows, dict) else rows
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or row.get("contentUrl") != content_url for row in rows
        ):
            raise ValueError("invalid published dependency candidates")
        page = _inventory_completeness(len(rows), document.get("pagination"))
        acquisition["catalog"] = page.facts()
        if not (
            page.status == "complete"
            and page.page_number == 1
            and page.page_size == INVENTORY_PAGE_SIZE
            and page.total_available == len(rows)
        ):
            raise ValueError("published dependency catalog incomplete")
        if not rows:
            # A filtered search can lag a current datasource even for an administrator.
            return {"state": "cannot_establish", "candidate_count": None}
        if len(rows) != 1:
            return {"state": "ambiguous", "candidate_count": len(rows)}
        selected = rows[0]
        luid = selected.get("id")
        if not isinstance(luid, str) or LUID_RE.fullmatch(luid) is None:
            raise ValueError("invalid published datasource identity")
        acquisition["candidate_luid_sha256"] = workbook_luid_digest(luid)
        acquisition["candidate_sha256"] = _published_candidate_sha256(selected)
        acquisition["detail_sha256"] = _published_candidate_sha256(self._datasource_detail(luid))
        if acquisition["candidate_sha256"] is None or acquisition["candidate_sha256"] != acquisition["detail_sha256"]:
            raise ValueError("published datasource detail unconfirmed")
        return {"state": "resolved", "candidate_count": 1, "datasource_luid": luid}


def _published_candidate_sha256(candidate: dict | None) -> str | None:
    """Digest the exact independently acquired candidate/detail fields without transmitting text."""
    fields = ("id", "contentUrl", "name", "updatedAt")
    if candidate is None or any(not isinstance(candidate.get(key), str) or not candidate[key] for key in fields):
        return None
    return hashlib.sha256(json.dumps([candidate[key] for key in fields], ensure_ascii=True).encode()).hexdigest()


HARVEST_STEM_RE = re.compile(
    r"^(?P<luid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_(?P<rest>.+)$"
)


def split_harvest_stem(stem: str) -> tuple[str | None, str]:
    """Split ``harvest_estate_assets.py``'s ``<luid>_<sanitized-name>`` stem into its two parts.

    Returns ``(None, stem)`` unchanged for any other filename, so a hand-placed or hand-renamed
    workbook keeps the plain name-matching behaviour and gains nothing it did not ask for.
    """
    match = HARVEST_STEM_RE.match(stem)
    return (match.group("luid"), match.group("rest")) if match else (None, stem)


def _sanitized(text: str) -> str:
    """``harvest_estate_assets.safe_component(text, 60)``, replicated so this script stays standalone.

    Kept deliberately in sync with that function: `[A-Za-z0-9-_]` survives, everything else becomes
    ``_``, then a 60-character truncation. Because it is lossy AND truncating it can only ever be
    used to compare a *remote* name forward into filename space, never to recover a display name.
    """
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in text)[:60]


class _WorkbookIndex:
    """The site inventory keyed by the three EXACT rules :func:`find_origin` matches on.

    Buckets keep inventory order, so ``candidates[0]`` and ``same_name_count`` mean precisely what
    they meant when each rule was a separate scan of the whole list. The legacy origin inventory is
    fetched once per run; published associations build a separate index over each input's fresh page.

    ⚠️ **Only a real string is a name.** An earlier revision of this index kept a malformed ``name``
    hashable by keying it on ``repr``, which invented identity out of nothing: a response whose
    ``name`` was ``["Superstore"]`` became the key ``"['Superstore']"`` and matched a local file
    called exactly that. A non-string name does not participate in name matching at all - it cannot
    equal a filename stem, which is the answer the pre-index scan gave.
    """

    def __init__(self, workbooks: list[dict[str, Any]]) -> None:
        self.workbooks = workbooks
        self.by_luid: dict[str, list[dict[str, Any]]] = {}
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        self.by_sanitized_name: dict[str, list[dict[str, Any]]] = {}
        for workbook in workbooks:
            name = workbook.get("name")
            self.by_luid.setdefault(str(workbook.get("id") or "").lower(), []).append(workbook)
            if isinstance(name, str):
                self.by_name.setdefault(name, []).append(workbook)
                self.by_sanitized_name.setdefault(_sanitized(name), []).append(workbook)

    def same_name_count(self, name: Any) -> int:
        """How many workbooks on the site carry this exact name - ambiguity is worth recording.

        A malformed (non-string) name is not a name, so it counts **0** rather than being coerced
        into one. It is still a safe answer for a workbook resolved by LUID: the question asked is
        "is this display name ambiguous", and a value that is not a display name has no answer.
        """
        return len(self.by_name.get(name, [])) if isinstance(name, str) else 0

    def match(self, luid: str | None, name_part: str) -> tuple[str | None, list[dict[str, Any]]]:
        """Resolve a filename stem to site workbooks, and say WHICH rule found them.

        Identity first: the exact LUID, then the exact name, and only for a stem that carries
        harvest's LUID prefix - because only then do we know ``safe_component()`` was applied - the
        sanitized name. The fallback is never offered to a hand-placed file, which is the
        name-is-not-identity error this module exists to prevent.
        """
        if luid is not None:
            candidates = self.by_luid.get(luid.lower(), [])
            if candidates:
                return "luid", candidates
        candidates = self.by_name.get(name_part, [])
        if candidates:
            return "name", candidates
        if luid is not None:
            candidates = self.by_sanitized_name.get(name_part, [])
            if candidates:
                return "sanitized_name", candidates
        return None, []


def find_origin(
    lookup: TableauLookup,
    stem: str,
    local: dict[str, Any],
    on_identity: Callable[[str | None], None] | None = None,
) -> dict[str, Any] | None:
    """Identify a local workbook on the site by LUID or name, then CONFIRM by content hash.

    Returns ``None`` when no workbook matches. When one does, ``matched_by`` records *how it was
    found* (``"luid"`` / ``"name"`` / ``"sanitized_name"``) and ``match`` records *how strongly it was
    confirmed* -- ``"sha256"`` when the bytes agree, ``"name_only"`` when they do not. Those are two
    independent axes: a LUID match with ``name_only`` means "this is provably the same item on the
    site, and it has changed since we harvested it", which is a different and more useful statement
    than a name collision.

    ⚠️ ``match`` alone could never carry that second claim, and saying it did was wrong. It compares
    RAW bytes, and a `.twbx` is repacked per download - measured 2026-09-03 on this site, three
    downloads of every item in one run: **27 of 49 archives** returned a different raw digest for
    identical content, **0 of 67** items returned a different content digest. ``revision_match`` is
    therefore the load-bearing verdict; ``match`` is kept unchanged so an existing consumer is not
    silently re-interpreted, and because a raw MATCH still implies a content match.

    ``revision_match`` is ``"same"`` / ``"differs"`` / ``None``, and ``None`` means *the two keys are
    not comparable* - a missing key on either side, or two different algorithms. It is never
    ``"differs"`` in that case: a false drift alarm on every pre-existing capture would be worse than
    the gap it closes.

    ⚠️ ``match`` has a third value, ``"unavailable"``, for the case where the site copy was never
    read - a 404, a 403, any non-200. It used to fall through to ``"name_only"``, which claimed the
    bytes DIFFER from a copy nobody had seen: a drift verdict manufactured out of a failed download.
    ``content_unavailable`` carries the sanitized reason, and both digests stay ``None``.
    """
    index = _WorkbookIndex(lookup.workbooks())
    matched_by, candidates = index.match(*split_harvest_stem(stem))
    if on_identity is not None:
        on_identity(candidates[0].get("id") if candidates else None)
    if not candidates:
        return None

    workbook = candidates[0]
    lookup.matched_luids.add(workbook["id"])
    remote_sha = lookup.content_sha256(workbook["id"])
    remote_key = lookup.content_revision_key(workbook["id"])
    unavailable = lookup.content_unavailable(workbook["id"])
    local_key = RevisionKey.from_json(local.get("revision_key"))
    agreement = local_key.agrees_with(remote_key) if local_key is not None else None
    if remote_sha == local["sha256"]:
        verdict = "sha256"
    elif remote_sha is None:
        verdict = "unavailable"
    else:
        verdict = "name_only"
    return {
        "server": lookup.base,
        "site": lookup.site,
        "workbook_luid": workbook["id"],
        "workbook_name": workbook.get("name"),
        "project": (workbook.get("project") or {}).get("name"),
        "owner_luid": (workbook.get("owner") or {}).get("id"),
        "created_at": workbook.get("createdAt"),
        "updated_at": workbook.get("updatedAt"),
        "tableau_product_version": lookup.product_version,
        "rest_api_version": lookup.version,
        "matched_by": matched_by,
        "match": verdict,
        "content_unavailable": unavailable,
        "revision_match": None if agreement is None else ("same" if agreement else "differs"),
        "remote_revision_key": remote_key.as_json() if remote_key is not None else None,
        "remote_sha256": remote_sha,
        "same_name_count": index.same_name_count(workbook.get("name")),
    }


def _dependency_source_match(lookup: TableauLookup, source: _PublishedSource, local: dict, origin: dict) -> str:
    """Association authority is stronger than the legacy first-candidate origin observation."""
    completeness = lookup.inventory_completeness
    if completeness is None or completeness.status != "complete" or origin["content_unavailable"] is not None:
        return "unestablished"
    luid, name = split_harvest_stem(source.path.stem)
    index = _WorkbookIndex(lookup.workbooks())
    matched_by, candidates = index.match(luid, name)
    if (
        len(candidates) != 1
        or candidates[0].get("id") != origin["workbook_luid"]
        or len(index.by_luid.get(origin["workbook_luid"].lower(), [])) != 1
        or (luid is not None and matched_by != "luid")
    ):
        return "unestablished"
    local_key = RevisionKey.from_json(local.get("revision_key"))
    remote_key = RevisionKey.from_json(origin["remote_revision_key"])
    agreement = local_key.agrees_with(remote_key) if local_key is not None else None
    if agreement is False or origin["revision_match"] == "differs":
        return "unestablished"
    if origin["match"] == "sha256" and origin["remote_sha256"] == local["sha256"]:
        return "sha256"
    if origin["match"] == "name_only" and agreement is True and origin["revision_match"] == "same":
        return "revision_same"
    return "unestablished"


def published_remote_agrees(current: dict | None, local: dict, origin: dict) -> bool:
    """Require fresh content to agree with BOTH the held source and the initial remote observation."""
    if current is None:
        return False
    current_key = RevisionKey.from_json(current["revision_key"])
    for observed_sha, observed_key in (
        (local["sha256"], RevisionKey.from_json(local.get("revision_key"))),
        (origin["remote_sha256"], RevisionKey.from_json(origin["remote_revision_key"])),
    ):
        agreement = current_key.agrees_with(observed_key) if current_key is not None else None
        if agreement is False or (current["sha256"] != observed_sha and agreement is not True):
            return False
    return True


def published_workbook_agrees(current: dict | None, identity: dict) -> bool:
    """Fresh inventory must uniquely select the same workbook as the input-bound observation."""
    return (
        current is not None
        and classify_inventory(current["inventory"]).status == "complete"
        and current["candidate_count"] == current["luid_count"] == 1
        and current["workbook_luid_sha256"] == identity["workbook_luid_sha256"]
    )


def _dependency_url_path(path: str) -> list[str]:
    """Decode each segment once, refusing ambiguous separators, traversal and malformed escapes."""
    if path in ("", "/"):
        return []
    if not path.startswith("/") or re.search(r"%(?![0-9a-fA-F]{2})", path):
        raise ValueError("unassessable datasource URL path")
    parts = [unquote(part, errors="strict") for part in path[1:].removesuffix("/").split("/")]
    if any(
        part in ("", ".", "..") or part != part.strip() or has_identity_controls(part) or re.search(r"[/\\%?#;]", part)
        for part in parts
    ):
        raise ValueError("unassessable datasource URL segment")
    return parts


def _tableau_url(address: str, *, configured: bool = False) -> tuple[tuple[str, str, int], list[str]]:
    """Validate before any request or origin copy; return only an origin and decoded route."""
    if (
        not isinstance(address, str)
        or not 0 < len(address) <= 4096
        or has_identity_controls(address)
        or any(char.isspace() for char in address)
        or re.search(r"[\\#]", address)
    ):
        raise ValueError("unassessable Tableau URL")
    url = urlsplit(address)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.netloc.endswith(":")
    ):
        raise ValueError("unsupported Tableau URL origin")
    if (
        re.search(r"[%/?\\]", url.hostname)
        or (configured and "?" in address)
        or re.fullmatch(r"(?:rev=[0-9]+(?:\.[0-9]+)*)?", url.query) is None
    ):
        raise ValueError("unsupported Tableau URL origin or parameters")
    if ":" not in url.hostname:
        host = url.hostname.encode("idna").decode("ascii").removesuffix(".")
        if any(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) is None for part in host.split(".")
        ):
            raise ValueError("unsupported Tableau URL host")
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    if port == 0:
        raise ValueError("unsupported Tableau URL port")
    return (url.scheme, url.hostname, port), _dependency_url_path(url.path)


def _dependency_url_paths(derived_from: str, configured: str) -> tuple[list[str], list[str]]:
    """Require one complete validated origin, not merely the same hostname."""
    derived, path = _tableau_url(derived_from)
    base, base_path = _tableau_url(configured, configured=True)
    if derived != base:
        raise ValueError("different datasource URL origin")
    return path, base_path


def _dependency_query_allowed(dependency: dict, lookup: TableauLookup) -> bool:
    """Accept only a datasource route inside the configured origin/base and the same source site."""
    segment = dependency["content_url"]
    if (
        not valid_published_key(dependency["published_key"])
        or not isinstance(segment, str)
        or has_identity_controls(segment)
        or re.fullmatch(r"[^,:/?#\\%]{1,1024}", segment) is None
    ):
        return False
    try:
        source_site = dependency["site"]
        if (source_site is not None and not isinstance(source_site, str)) or (source_site or "") != lookup.site:
            return False
        path, base_path = _dependency_url_paths(dependency["derived_from"], lookup.base)
        if path[: len(base_path)] != base_path:
            return False
        route = path[len(base_path) :]
        if len(route) == 4 and route[0] == "t":
            if route[1] != lookup.site:
                return False
            route = route[2:]
        return route == ["datasources", segment]
    except (TypeError, ValueError):
        return False


def _published_rows(lookup: TableauLookup, dependencies: list[dict], confirmed: bool) -> tuple[list[dict], list[dict]]:
    """Only duplicate occurrences in this physical input may share current catalog/detail reads."""
    acquired = {}
    rows, evidence = [], []
    for dependency in dependencies:
        outcome, acquisition = {"state": "cannot_establish", "candidate_count": None}, None
        if confirmed and _dependency_query_allowed(dependency, lookup):
            content_url = dependency["content_url"]
            if content_url not in acquired:
                acquired[content_url] = lookup.published_dependency(content_url)
            outcome, acquisition = acquired[content_url]
        row = {key: dependency[key] for key in ("source_ordinal", "published_key")} | outcome
        rows.append(row)
        evidence.append(published_outcome_checkpoint([row])[0] | {"acquisition": acquisition})
    return rows, evidence


def _attach_published_dependencies(record: dict, lookup: TableauLookup, source: _PublishedSource, index: int) -> None:
    origin, local = record["origin"], record["input"]
    if (
        not source.dependencies
        or any(not valid_published_key(row["published_key"]) for row in source.dependencies)
        or not isinstance(origin["workbook_luid"], str)
        or not LUID_RE.fullmatch(origin["workbook_luid"])
    ):
        return
    source_match = _dependency_source_match(lookup, source, local, origin)
    identity = workbook_identity_checkpoint(source.path, origin["workbook_luid"])
    current_workbook = lookup.current_workbook(source.path.stem) if source_match != "unestablished" else None
    confirmed = published_workbook_agrees(current_workbook, identity)
    rows, evidence = _published_rows(lookup, source.dependencies, confirmed)
    current_remote = lookup.current_content(origin["workbook_luid"]) if confirmed else None
    try:
        current_sha256 = hashlib.sha256(source.path.read_bytes()).hexdigest()
    except OSError:
        current_sha256 = None
    lookup.reporter.published_evidence(
        index,
        {
            "identity": identity,
            "source_sha256": local["sha256"],
            "current_sha256": current_sha256,
            "current_remote": current_remote,
            "current_workbook": current_workbook,
            "source_match": source_match,
            "rows": evidence,
        },
    )
    if not confirmed or current_sha256 != local["sha256"] or not published_remote_agrees(current_remote, local, origin):
        source_match = "unestablished"
        rows = [
            {key: row[key] for key in ("source_ordinal", "published_key")}
            | {"state": "cannot_establish", "candidate_count": None}
            for row in rows
        ]
    origin["published_dependencies"] = {
        "schema": PUBLISHED_DEPENDENCIES_SCHEMA,
        "source_sha256": local["sha256"],
        "workbook_luid": origin["workbook_luid"],
        "source_match": source_match,
        "rows": rows,
    }


def collect_inputs(target: Path) -> list[Path]:
    """The workbook(s) to stamp: one file, or every workbook in a folder."""
    if target.is_file():
        return [target]
    return sorted(p for p in target.iterdir() if p.suffix.lower() in WORKBOOK_SUFFIXES)


def _cancelled_result(
    records: list[dict[str, Any]], total: int, operation: str, errors: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Never keep an unscrubbed live record or lose an unfinished physical input on cancellation."""
    reduced = [checkpoint_record(record) for record in records]
    reduced.extend(unavailable_input(CANCELLED_CODE) for _ in range(total - len(reduced)))
    usable = any(record["input"].get("status") != "unavailable" for record in reduced)
    return _result(reduced, "partial" if usable else "failed", [*(errors or []), _error(CANCELLED_CODE, operation)])


def build(target: Path, env: dict[str, str], reporter: NullReporter | None = None) -> dict[str, Any]:
    """Fingerprint every input, and attach its Tableau origin when credentials allow.

    **Local evidence first.** The two passes used to be interleaved per input, so one slow remote
    call stalled every LATER file's fingerprint as well - and a run preempted at the deadline then
    had local evidence for nothing beyond the first input, although the bytes were sitting on this
    machine the whole time. Fingerprints owe the site nothing, so they are all taken before the
    first remote call and checkpointed as they complete.

    ``reporter`` is the optional supervisor channel (issue #576). With none it is a no-op, which is
    what the standalone CLI and every direct test use.
    """
    reporter = reporter or NullReporter()
    reporter.operation(OP_COLLECT_INPUTS, 0, 1)
    if reporter.cancelled:
        reporter.inputs_discovered(0)
        return _cancelled_result([], 0, OP_COLLECT_INPUTS)
    try:
        inputs = collect_inputs(target)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("provenance input discovery failed (%s)", _exception_class(exc))
        reporter.inputs_discovered(0)
        return failure_result("collect-inputs-failed", OP_COLLECT_INPUTS, exc)
    reporter.inputs_discovered(len(inputs))
    reporter.operation(OP_COLLECT_INPUTS, 1, 1)
    if not inputs:
        return _result([], "empty", [_error("empty-input", OP_COLLECT_INPUTS)])

    errors: list[dict[str, Any]] = []
    records, sources = _fingerprint_pass(inputs, errors, reporter)
    if reporter.cancelled:
        return _cancelled_result(records, len(inputs), OP_FINGERPRINT, errors)
    return _complete_provenance(sources, records, env, reporter, errors)


def _complete_provenance(
    inputs: list[_PublishedSource],
    records: list[dict[str, Any]],
    env: dict[str, str],
    reporter: NullReporter,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Only after local evidence has been checkpointed may a live lookup begin."""
    lookup: TableauLookup | None = None
    live_requested = bool(env.get("TABLEAU_SERVER_URL") and env.get("TABLEAU_PAT_NAME"))
    reporter.lookup_intent(live_requested)
    if live_requested and not reporter.cancelled:
        lookup = _open_lookup(env, errors, reporter)
    if reporter.cancelled:
        return _cancelled_result(records, len(inputs), OP_SIGN_IN, errors)
    if lookup is not None:
        _origin_pass(inputs, records, lookup, errors, reporter)
    if reporter.cancelled:
        return _cancelled_result(records, len(inputs), OP_CONTENT if lookup is not None else OP_SIGN_IN, errors)

    for source, record in zip(inputs, records):
        if source.dependencies and (
            "published_dependencies" not in (record.get("origin") or {})
            or any(not valid_published_key(row["published_key"]) for row in source.dependencies)
        ):
            errors.append(_error("published-authority-unavailable", "lookup-origin"))
    usable = sum(record["input"].get("status") != "unavailable" for record in records)
    status = "failed" if not usable else ("partial" if errors else ("success" if live_requested else "local_only"))
    result = _result(records, status, errors)
    if lookup is None:
        return result
    return _finish_live(result, lookup, reporter)


def _fingerprint_pass(
    inputs: list[Path], errors: list[dict[str, Any]], reporter: NullReporter
) -> tuple[list[dict[str, Any]], list[_PublishedSource]]:
    """Every input's LOCAL evidence, checkpointed one by one so a later stall cannot discard it."""
    records: list[dict[str, Any]] = []
    sources = []
    for index, path in enumerate(inputs):
        reporter.operation(OP_FINGERPRINT, index, len(inputs))
        if reporter.cancelled:
            break
        raw = None
        try:
            raw = path.read_bytes()
            local = fingerprint(path, raw)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            error = _error("local-fingerprint-failed", OP_FINGERPRINT, exc)
            errors.append(error)
            record: dict[str, Any] = {"input": {"status": "unavailable"}, "fingerprint_error": error}
        else:
            record = {"input": local}
        source = _published_source(path, raw if "fingerprint_error" not in record else None)
        if source.dependencies is None and "fingerprint_error" not in record:
            errors.append(_error("published-assessment-unavailable", OP_FINGERPRINT))
        sources.append(source)
        records.append(record)
        reporter.operation(OP_FINGERPRINT, len(records), len(inputs))
        reporter.checkpoint(index, record, source)
    return records, sources


def _open_lookup(env: dict[str, str], errors: list[dict[str, Any]], reporter: NullReporter) -> TableauLookup | None:
    """Sign in, or record WHY there is no live half and continue with the local one."""
    reporter.operation(OP_SIGN_IN, 0, 1)
    if reporter.cancelled:
        return None
    try:
        lookup = TableauLookup(env)
        lookup.reporter = reporter
        if reporter.cancelled:
            return None
        lookup.sign_in()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("no Tableau lookup (%s) - fingerprints only", _exception_class(exc))
        errors.append(_error("live-lookup-refused", OP_SIGN_IN, exc))
        reporter.operation(OP_SIGN_IN, 1, 1)
        return None
    reporter.operation(OP_SIGN_IN, 1, 1)
    return lookup


def _origin_pass(
    inputs: list[_PublishedSource],
    records: list[dict[str, Any]],
    lookup: TableauLookup,
    errors: list[dict[str, Any]],
    reporter: NullReporter,
) -> None:
    """Attach the site half to every input that has local evidence, in place.

    The inventory is primed once, ahead of the loop, purely so the phase can be REPORTED as one
    operation rather than as N: :meth:`TableauLookup.workbooks` already latches both the listing and
    its failure (#582), so this costs no extra round trip and a failure here is deliberately dropped
    - :func:`find_origin` re-raises the latched one so each input still records its own reason.
    """
    reporter.operation(OP_INVENTORY, 0, 1)
    if reporter.cancelled:
        return
    try:
        lookup.workbooks()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.debug("site inventory unavailable (%s) - each input records its own reason", _exception_class(exc))
        errors.append(_error(MSG_INVENTORY_FAILED, OP_INVENTORY, exc))
    else:
        completeness = lookup.inventory_completeness
        if completeness is not None and (finding := completeness.error()) is not None:
            errors.append(finding)
    reporter.operation(OP_INVENTORY, 1, 1)

    for index, (source, record) in enumerate(zip(inputs, records)):
        if record["input"].get("status") == "unavailable":
            continue
        reporter.operation(OP_CONTENT, lookup.content_attempts(), len(lookup.matched_luids))
        if reporter.cancelled:
            break
        _attach_origin(record, lookup, source, errors, index)
        reporter.operation(OP_CONTENT, lookup.content_attempts(), len(lookup.matched_luids))


def _attach_origin(
    record: dict[str, Any], lookup: TableauLookup, source: _PublishedSource, errors: list[dict[str, Any]], index: int
) -> None:
    """One input's site half, or the typed reason there is none."""
    try:
        origin = find_origin(
            lookup,
            source.path.stem,
            record["input"],
            lambda luid: lookup.reporter.workbook_identity(index, source, luid),
        )
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        origin = None
        record["lookup_error"] = _error("live-lookup-failed", "lookup-origin", exc)
        errors.append(record["lookup_error"])
    record["origin"] = origin
    if origin is not None:
        _attach_published_dependencies(record, lookup, source, index)
    if origin is None:
        completeness = lookup.inventory_completeness
        record["origin_note"] = (
            INCOMPLETE_INVENTORY_NOTE
            if completeness is not None and completeness.status != "complete"
            else NO_ORIGIN_NOTE
        )
    elif origin["match"] == "name_only":
        record["origin_note"] = (
            f"matched by {origin['matched_by']}, but the bytes DIFFER from the site copy - "
            "figures measured here will not reproduce against it"
        )
    elif origin["match"] == "unavailable":
        reason = origin.get("content_unavailable") or "the site refused the download"
        record["origin_note"] = (
            f"matched by {origin['matched_by']}, but the site copy could NOT be read "
            f"({reason}) - no byte or revision comparison was made"
        )
        status = int(reason.removeprefix("HTTP ")) if reason.startswith("HTTP ") else 0
        record["lookup_error"] = _error("content-unavailable", "download-workbook", http_status=status)
        errors.append(record["lookup_error"])


def _authority_identity(record: dict) -> str | None:
    """Retain a digest before scrub; metadata redaction is allowed, authority rewriting is not."""
    origin = record.get("origin")
    if origin is None or "published_dependencies" not in origin:
        return None
    scope = {
        key: origin[key]
        for key in (
            "server",
            "site",
            "workbook_luid",
            "match",
            "remote_sha256",
            "revision_match",
            "remote_revision_key",
            "published_dependencies",
        )
    }
    return hashlib.sha256(json.dumps([record["input"], scope], ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def _finish_live(result: dict[str, Any], lookup: TableauLookup, reporter: NullReporter | None = None) -> dict[str, Any]:
    """Scrub the live-derived record and release the session, without either being able to lose it.

    ⚠️ Both steps used to sit unguarded after all the work was done, and that cost the whole file:
    measured 2026-09-09, a sign-out whose connection closed without a response discarded 3 of 3
    fingerprints and exited the CLI 1 with no artifact, and under ``run_estate`` the same failure was
    swallowed to a warning with no artifact either. Fingerprints are computed from local bytes and
    owe nothing to the site, so nothing the site does may delete them.

    Redaction failing is the one case where fingerprints are NOT simply kept alongside the rest: an
    unscrubbed live record can carry a reflected credential, so the response-derived half is withheld
    and the local half survives. Fail-closed on the secret, fail-open on the evidence.

    The SAFE SNAPSHOT goes to the supervisor between the two steps and nowhere else: after scrubbing
    the result is safe to hold, and sign-out is a network call that can hang past any deadline (#576).
    Sending it first is what stops a hung cleanup from costing a completed run its evidence.
    """
    reporter = reporter or NullReporter()
    reporter.operation(OP_SCRUB, 0, 1)
    if reporter.cancelled:
        return _cancelled_result(result["inputs"], result["input_count"], OP_SCRUB, result["phase"]["errors"])
    try:
        identities = [_authority_identity(record) for record in result["inputs"]]
        result, _paths = scrub_tree(result, lookup.redact_text)
        for record, identity in zip(result["inputs"], identities):
            if identity is not None and _authority_identity(record) != identity:
                record["origin"] = None
                record["origin_note"] = IDENTITY_WITHHELD_NOTE
                result["phase"]["errors"].append(_error("published-identity-redacted", OP_SCRUB))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("provenance redaction failed (%s) - live origin fields withheld", _exception_class(exc))
        result["phase"]["errors"].append(_error("scrub-failed", OP_SCRUB, exc))
        result = _without_live_fields(result, lookup.redact_text, reporter)
    if reporter.cancelled:
        return _cancelled_result(result["inputs"], result["input_count"], OP_SCRUB, result["phase"]["errors"])
    reporter.operation(OP_SCRUB, 1, 1)
    if result["phase"]["errors"]:
        result["phase"]["status"] = "partial"
    reporter.safe_snapshot(result)

    reporter.operation(OP_SIGN_OUT, 0, 1)
    if reporter.cancelled:
        result["phase"]["status"] = "partial"
        result["phase"]["errors"].append(_error(CANCELLED_CODE, OP_SIGN_OUT))
        return result
    try:
        signout_failure = lookup.sign_out()
        if signout_failure is not None:
            result["phase"]["errors"].append(_error("sign-out-failed", OP_SIGN_OUT, signout_failure))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("Tableau sign-out failed (%s) - session left to expire", _exception_class(exc))
        result["phase"]["errors"].append(_error("sign-out-failed", OP_SIGN_OUT, exc))
    reporter.operation(OP_SIGN_OUT, 1, 1)
    if result["phase"]["errors"]:
        result["phase"]["status"] = "partial"
    return result


def provenance_worker(conn: Any, cancel_event: Any, payload: dict[str, str]) -> None:
    """The whole provenance computation, as one LEAF process a supervisor can terminate (#576).

    It resolves its own credentials from the ``.env`` path it is handed, so no secret ever crosses
    the pipe in either direction, and it owns no artifact path: publication is the parent's, exactly
    once, whatever happens here. A failure it can describe is returned as a typed terminal result; a
    failure it cannot is what the parent's deadline and exit-code inspection are for.

    ⚠️ It must stay a LEAF. Killing a process does not kill its descendants on Windows (measured: a
    grandchild survived and had to be terminated by PID), so the supervisor's guarantee is only as
    good as this function starting no subprocess and no pool. The parent starts it as a DAEMON
    process, which makes that structural: `multiprocessing` refuses to let a daemon have children.
    """
    reporter = WorkerReporter(conn, cancel_event)
    try:
        env = resolve_env(Path(payload.get("env") or ".env"))
        result = build(Path(payload["input"]), env, reporter)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        result = failure_result("build-failed", "build", exc)
    try:
        reporter.terminal(result)
    finally:
        try:
            conn.close()
        except (OSError, ValueError):  # pragma: no cover - the parent may already have closed it
            LOG.debug("provenance progress channel already closed")


WITHHELD_NOTE = "live origin withheld - provenance redaction failed for this run"
IDENTITY_WITHHELD_NOTE = "live origin withheld - published dependency identity changed during redaction"
DERIVED_INPUT_FIELDS = ("size_bytes", "sha256", "revision_key")
DERIVED_MEMBER_FIELDS = ("size_bytes", "crc32")


def _without_live_fields(result: dict[str, Any], redactor, reporter: NullReporter | None = None) -> dict[str, Any]:
    """The stamp with the response-derived half dropped and the local half made safe to persist.

    ⚠️ Keeping the local record verbatim is NOT safe, and an earlier revision did exactly that. A
    fingerprint is full of strings that came from the environment rather than from us -
    ``input.file`` and every ``members[].name`` - and a workbook whose FILENAME is the PAT secret
    (or a member named after the PAT name, or the session token) then writes that credential into an
    artifact that is committed beside findings and pasted into issues. The local half being local
    does not make it clean; it makes it *unscrubbed*.

    So it is redacted independently, by the audited :func:`tableau_env.scrub_tree` over the reduced
    tree - the first attempt may well have failed on the live half. If **that** fails too, redaction
    itself cannot be trusted, and only fields this module DERIVED survive: sizes, the sha256, the
    revision key, member sizes and CRCs. Every copied string goes, filename included. Losing the
    filename hurts a consumer; persisting a credential is not recoverable.
    """
    reporter = reporter or NullReporter()
    if reporter.cancelled:
        return _cancelled_result(result["inputs"], result["input_count"], OP_SCRUB, result["phase"]["errors"])
    reduced = {
        **result,
        "inputs": [
            {"input": record["input"], "origin": None, "origin_note": WITHHELD_NOTE} for record in result["inputs"]
        ],
    }
    try:
        if reporter.cancelled:
            return _cancelled_result(result["inputs"], result["input_count"], OP_SCRUB, result["phase"]["errors"])
        scrubbed, _paths = scrub_tree(reduced, redactor)
        return scrubbed
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("provenance redaction is unusable (%s) - keeping DERIVED evidence only", _exception_class(exc))
        result["phase"]["errors"].append(_error("scrub-failed", "scrub-local-fields", exc))
        return {
            **result,
            "inputs": [
                {"input": _derived_only(record["input"]), "origin": None, "origin_note": WITHHELD_NOTE}
                for record in result["inputs"]
            ],
        }


def _derived_only(record: dict[str, Any]) -> dict[str, Any]:
    """A fingerprint reduced to what this module COMPUTED - no string copied from the environment.

    A digest, a byte count and a CRC cannot carry a credential: none of them is a copy of anything
    the operator configured. ``file`` and ``members[].name`` are copies, and are what goes.
    """
    reduced = {key: value for key, value in record.items() if key in DERIVED_INPUT_FIELDS}
    if isinstance(record.get("members"), list):
        reduced["members"] = [
            {key: value for key, value in member.items() if key in DERIVED_MEMBER_FIELDS}
            for member in record["members"]
        ]
    return reduced


def main() -> int:
    """Publish the normalized evidence; exit 1 whenever its phase is not successful."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help=".twb/.twbx file, or a folder of them")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials file")
    parser.add_argument("--out", type=Path, help="output JSON (default: source-provenance.json beside the input)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = normalize_result(build(args.input, resolve_env(args.env)))
    if not result["input_count"]:
        LOG.error("no .twb/.twbx found under %s", args.input)
        return 1

    out = args.out or ((args.input if args.input.is_dir() else args.input.parent) / "source-provenance.json")
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    for index, record in enumerate(result["inputs"]):
        origin = record.get("origin")
        where = f"{origin['site']} / {origin['project']} ({origin['match']})" if origin else "local only"
        LOG.info("  %-34s %s", record["input"].get("file", f"<input {index}>"), where)
        if record.get("origin_note"):
            LOG.warning("      %s", record["origin_note"])
    LOG.info("stamped %d input(s) -> %s", result["input_count"], out)
    return 0 if is_success(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
