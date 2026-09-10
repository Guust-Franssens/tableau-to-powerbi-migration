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
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from object_identity import RevisionKey, revision_key  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import pat_secret, redact, resolve_env, scrub_tree  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("provenance")

WORKBOOK_SUFFIXES = (".twb", ".twbx")
SUCCESS_STATUSES = frozenset({"success", "local_only"})
SCHEMA = "tableau-source-provenance/1"

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
# to a parent that owns the deadline. The channel is deliberately TINY and closed: four numeric or
# already-safe message kinds, nothing free-form, and no credential ever travels back over it.

MSG_INPUTS_DISCOVERED = "inputs-discovered"
MSG_OPERATION = "operation"
MSG_CHECKPOINT = "checkpoint"
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


def _error(code: str, operation: str, exc: BaseException | None = None, **facts: int) -> dict[str, Any]:
    """A stable failure record containing no exception or response text."""
    record: dict[str, Any] = {"code": code, "operation": operation}
    if exc is not None:
        record["exception_class"] = type(exc).__name__
        for attribute in ("errno", "winerror"):
            value = getattr(exc, attribute, None)
            if isinstance(value, int):
                record[attribute] = value
    record.update({key: value for key, value in facts.items() if isinstance(value, int)})
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
    if not isinstance(status, str):
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


def checkpoint_record(record: dict[str, Any]) -> dict[str, Any]:
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
    return reduced


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

    def checkpoint(self, index: int, record: dict[str, Any]) -> None:
        """Ignore a completed-input checkpoint."""

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

    @property
    def cancelled(self) -> bool:  # type: ignore[override]
        """Whether the supervisor has asked for this run to stop."""
        try:
            return self._cancel is not None and bool(self._cancel.is_set())
        except (OSError, ValueError):  # pragma: no cover - the event died with the parent
            return True

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._conn.send(message)
        except (OSError, ValueError, EOFError):  # pragma: no cover - parent closed the pipe at expiry
            LOG.debug("provenance progress channel closed")

    def inputs_discovered(self, total: int) -> None:
        """Numeric only: how many physical inputs discovery found."""
        self._send({"kind": MSG_INPUTS_DISCOVERED, "total": int(total)})

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

    def checkpoint(self, index: int, record: dict[str, Any]) -> None:
        """Derived-only evidence for one completed input, addressed by ordinal."""
        self._send({"kind": MSG_CHECKPOINT, "index": int(index), "record": checkpoint_record(record)})

    def safe_snapshot(self, result: dict[str, Any]) -> None:
        """The whole result once it is scrubbed - sent BEFORE sign-out, which can hang."""
        self._send({"kind": MSG_SAFE_SNAPSHOT, "result": result})

    def terminal(self, result: dict[str, Any]) -> None:
        """The final result, only after cleanup has finished or produced a typed error."""
        self._send({"kind": MSG_TERMINAL, "result": result})


def fingerprint(path: Path) -> dict[str, Any]:
    """Size + sha256 + a reproducible REVISION KEY, plus per-member CRCs for a ``.twbx``.

    The members matter more than the outer hash: a ``.twbx`` is a zip, and zip metadata (timestamps,
    compression) can differ between two downloads of the same content, so two identical workbooks can
    hash differently. Member CRCs compare the content itself.

    ⚠️ That warning was written here and then not acted on where it counted - the origin comparison
    below hashed raw bytes on BOTH sides, so an unchanged workbook read as ``name_only``.
    :func:`object_identity.revision_key` is the content-normalised digest that makes the comparison
    reproducible, and it is recorded on both sides so a consumer never has to guess which it holds.
    """
    raw = path.read_bytes()
    record: dict[str, Any] = {
        "file": path.name,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    key = revision_key(raw)
    if key is not None:
        record["revision_key"] = key.as_json()
    if path.suffix.lower() == ".twbx" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            record["members"] = [
                {"name": info.filename, "size_bytes": info.file_size, "crc32": f"{info.CRC:08x}"}
                for info in sorted(archive.infolist(), key=lambda i: i.filename)
            ]
    return record


class TableauLookup:  # pylint: disable=too-many-instance-attributes
    """Minimal read-only REST client, used only to identify a workbook we already hold.

    Every remote answer is fetched **at most once per instance**, because one instance is one
    provenance run. Measured 2026-09-09 against a recording loopback site on the pre-cache code, 66
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
        # Seven fields describe the site and the session; the four after them are this run's answer
        # cache, kept as plain fields rather than a container so each one reads at its use site.
        self.base = env["TABLEAU_SERVER_URL"].rstrip("/")
        self.version = env.get("TABLEAU_REST_API_VERSION", "3.21")
        self.site = env["TABLEAU_SITE"]
        self.product_version = env.get("TABLEAU_PRODUCT_VERSION")
        self._pat = (env["TABLEAU_PAT_NAME"], pat_secret(env))
        self.token: str | None = None
        self.site_id: str | None = None
        self._inventory: list[dict[str, Any]] | None = None
        self._inventory_failure: Exception | None = None
        self._content_cache: dict[str, bytes | None] = {}
        self._content_failure: dict[str, Exception] = {}
        self._content_unavailable: dict[str, str] = {}

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
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
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
        creds = json.loads(payload)["credentials"]
        self.token, self.site_id = creds["token"], creds["site"]["id"]

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
                LOG.debug("Tableau sign-out failed (%s) - session left to expire", type(exc).__name__)
                failure = exc
            finally:
                self.token = None
        return failure

    def redact_text(self, text: str) -> str:
        """Redact credentials that an authenticated response might reflect."""
        return redact(text, self._pat[0], self._pat[1], self.token or "")

    def workbooks(self) -> list[dict[str, Any]]:
        """Every workbook on the site, listed **once per run** - the failure included.

        The listing does not vary between inputs, so asking again for the second and every later
        input bought nothing and cost one round trip each (66 of 66 measured). Latching the failure
        matters just as much: a dead site answered 66 identical errors, ~9 s apiece on a real host.
        The cached exception is re-raised so each input still records its own redacted reason.
        """
        if self._inventory_failure is not None:
            raise self._inventory_failure
        if self._inventory is None:
            try:
                self._inventory = self._fetch_workbooks()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._inventory_failure = exc
                raise
        return self._inventory

    def _fetch_workbooks(self) -> list[dict[str, Any]]:
        status, payload = self._call("GET", f"/sites/{self.site_id}/workbooks?pageSize=1000", accept="application/json")
        if status != 200:
            raise RuntimeError(f"listing workbooks failed: HTTP {status}")
        return json.loads(payload).get("workbooks", {}).get("workbook", [])

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

        A cache hit is not an attempt: two inputs resolving to one LUID are one download (#582), and
        progress that counted them twice would report remote work that never happened.
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
    they meant when each rule was a separate scan of the whole list. Building the index is local CPU
    over an inventory that is now fetched once per run; the cost this module cares about is round
    trips, and there is exactly one.

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


def find_origin(lookup: TableauLookup, stem: str, local: dict[str, Any]) -> dict[str, Any] | None:
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
    luid, name_part = split_harvest_stem(stem)
    index = _WorkbookIndex(lookup.workbooks())
    matched_by, candidates = index.match(luid, name_part)
    if not candidates:
        return None

    workbook = candidates[0]
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


def collect_inputs(target: Path) -> list[Path]:
    """The workbook(s) to stamp: one file, or every workbook in a folder."""
    if target.is_file():
        return [target]
    return sorted(p for p in target.iterdir() if p.suffix.lower() in WORKBOOK_SUFFIXES)


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
    try:
        inputs = collect_inputs(target)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("provenance input discovery failed (%s)", type(exc).__name__)
        return failure_result("collect-inputs-failed", OP_COLLECT_INPUTS, exc)
    reporter.inputs_discovered(len(inputs))
    reporter.operation(OP_COLLECT_INPUTS, 1, 1)
    if not inputs:
        return _result([], "empty", [_error("empty-input", OP_COLLECT_INPUTS)])

    errors: list[dict[str, Any]] = []
    records = _fingerprint_pass(inputs, errors, reporter)

    lookup: TableauLookup | None = None
    live_requested = bool(env.get("TABLEAU_SERVER_URL") and env.get("TABLEAU_PAT_NAME"))
    if live_requested and not reporter.cancelled:
        lookup = _open_lookup(env, errors, reporter)
    if lookup is not None:
        _origin_pass(inputs, records, lookup, errors, reporter)

    usable = sum(record["input"].get("status") != "unavailable" for record in records)
    status = "failed" if not usable else ("partial" if errors else ("success" if live_requested else "local_only"))
    result = _result(records, status, errors)
    if lookup is None:
        return result
    return _finish_live(result, lookup, reporter)


def _fingerprint_pass(inputs: list[Path], errors: list[dict[str, Any]], reporter: NullReporter) -> list[dict[str, Any]]:
    """Every input's LOCAL evidence, checkpointed one by one so a later stall cannot discard it."""
    records: list[dict[str, Any]] = []
    for index, path in enumerate(inputs):
        if reporter.cancelled:
            break
        try:
            local = fingerprint(path)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            error = _error("local-fingerprint-failed", OP_FINGERPRINT, exc)
            errors.append(error)
            record: dict[str, Any] = {"input": {"status": "unavailable"}, "fingerprint_error": error}
        else:
            record = {"input": local}
        records.append(record)
        reporter.operation(OP_FINGERPRINT, len(records), len(inputs))
        reporter.checkpoint(index, record)
    return records


def _open_lookup(env: dict[str, str], errors: list[dict[str, Any]], reporter: NullReporter) -> TableauLookup | None:
    """Sign in, or record WHY there is no live half and continue with the local one."""
    reporter.operation(OP_SIGN_IN, 0, 1)
    try:
        lookup = TableauLookup(env)
        lookup.sign_in()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("no Tableau lookup (%s) - fingerprints only", type(exc).__name__)
        errors.append(_error("live-lookup-refused", OP_SIGN_IN, exc))
        reporter.operation(OP_SIGN_IN, 1, 1)
        return None
    reporter.operation(OP_SIGN_IN, 1, 1)
    return lookup


def _origin_pass(
    inputs: list[Path],
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
    try:
        lookup.workbooks()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.debug("site inventory unavailable (%s) - each input records its own reason", type(exc).__name__)
    reporter.operation(OP_INVENTORY, 1, 1)

    for path, record in zip(inputs, records):
        if reporter.cancelled:
            break
        if record["input"].get("status") == "unavailable":
            continue
        _attach_origin(record, lookup, path.stem, errors)
        reporter.operation(OP_CONTENT, lookup.content_attempts(), None)


def _attach_origin(record: dict[str, Any], lookup: TableauLookup, stem: str, errors: list[dict[str, Any]]) -> None:
    """One input's site half, or the typed reason there is none."""
    try:
        origin = find_origin(lookup, stem, record["input"])
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        origin = None
        record["lookup_error"] = _error("live-lookup-failed", "lookup-origin", exc)
        errors.append(record["lookup_error"])
    record["origin"] = origin
    if origin is None:
        record["origin_note"] = "no workbook of this LUID or name on the site - local-only input"
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
    try:
        result, _paths = scrub_tree(result, lookup.redact_text)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("provenance redaction failed (%s) - live origin fields withheld", type(exc).__name__)
        result["phase"]["errors"].append(_error("scrub-failed", OP_SCRUB, exc))
        result = _without_live_fields(result, lookup.redact_text)
    reporter.operation(OP_SCRUB, 1, 1)
    if result["phase"]["errors"]:
        result["phase"]["status"] = "partial"
    reporter.safe_snapshot(result)

    reporter.operation(OP_SIGN_OUT, 0, 1)
    try:
        signout_failure = lookup.sign_out()
        if signout_failure is not None:
            result["phase"]["errors"].append(_error("sign-out-failed", OP_SIGN_OUT, signout_failure))
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("Tableau sign-out failed (%s) - session left to expire", type(exc).__name__)
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
DERIVED_INPUT_FIELDS = ("size_bytes", "sha256", "revision_key")
DERIVED_MEMBER_FIELDS = ("size_bytes", "crc32")


def _without_live_fields(result: dict[str, Any], redactor) -> dict[str, Any]:
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
    reduced = {
        **result,
        "inputs": [
            {"input": record["input"], "origin": None, "origin_note": WITHHELD_NOTE} for record in result["inputs"]
        ],
    }
    try:
        scrubbed, _paths = scrub_tree(reduced, redactor)
        return scrubbed
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        LOG.warning("provenance redaction is unusable (%s) - keeping DERIVED evidence only", type(exc).__name__)
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
    """Stamp provenance. Exit 1 if there was nothing to stamp."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help=".twb/.twbx file, or a folder of them")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials file")
    parser.add_argument("--out", type=Path, help="output JSON (default: source-provenance.json beside the input)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = build(args.input, resolve_env(args.env))
    if not result["input_count"]:
        LOG.error("no .twb/.twbx found under %s", args.input)
        return 1

    out = args.out or ((args.input if args.input.is_dir() else args.input.parent) / "source-provenance.json")
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    for record in result["inputs"]:
        origin = record.get("origin")
        where = f"{origin['site']} / {origin['project']} ({origin['match']})" if origin else "local only"
        LOG.info("  %-34s %s", record["input"]["file"], where)
        if record.get("origin_note"):
            LOG.warning("      %s", record["origin_note"])
    LOG.info("stamped %d input(s) -> %s", result["input_count"], out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
