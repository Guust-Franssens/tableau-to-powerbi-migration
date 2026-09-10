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
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from object_identity import RevisionKey, revision_key  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import pat_secret, redact, redacted_note, resolve_env, scrub_tree  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("provenance")

TABLEAU_INPUT_SUFFIXES = (".twb", ".twbx", ".tds", ".tdsx")
WORKBOOK_SUFFIXES = (".twb", ".twbx")
DEFAULT_TIMEOUT_SEC = 120.0
DEADLINE_EXPIRED = "deadline-expired"
PAGINATION_RESIDUAL = "pagination-beyond-1000-workbooks-not-addressed"
DATASOURCE_ORIGIN_UNAVAILABLE = "datasource-origin-unavailable"


class DeadlineExceeded(RuntimeError):
    """The whole provenance phase budget was exhausted."""

    def __init__(self) -> None:
        super().__init__("provenance phase deadline expired")


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
        self._deadline_at: float | None = None
        self._clock: Callable[[], float] = time.monotonic
        self._progress: Callable[[dict[str, Any]], None] | None = None
        self._remote_completed = 0
        self.signout_failure_code: str | None = None

    def set_run_context(
        self,
        *,
        deadline_at: float | None,
        clock: Callable[[], float],
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Attach the run's whole-phase clock/progress sink without widening fake constructors."""
        self._deadline_at = deadline_at
        self._clock = clock
        self._progress = progress

    def _remaining_sec(self) -> float | None:
        if self._deadline_at is None:
            return None
        return max(0.0, self._deadline_at - self._clock())

    def _check_deadline(self) -> None:
        remaining = self._remaining_sec()
        if remaining is not None and remaining <= 0:
            raise DeadlineExceeded()

    def _timeout_sec(self) -> float:
        remaining = self._remaining_sec()
        if remaining is None:
            return 180.0
        return max(0.001, min(180.0, remaining))

    def _operation_label(self, path: str) -> str:
        if path == "/auth/signin":
            return "sign-in"
        if path == "/auth/signout":
            return "sign-out"
        if "/content" in path:
            return "content"
        if "/workbooks?" in path:
            return "inventory"
        return "request"

    def _emit_remote_progress(self, operation: str) -> None:
        self._remote_completed += 1
        if self._progress is not None:
            self._progress(
                {"event": "remote-operation", "operation": operation, "remote_completed": self._remote_completed}
            )

    def _read_body(self, response) -> bytes:
        self._check_deadline()
        timer = None
        remaining = self._remaining_sec()
        if remaining is not None:
            timer = threading.Timer(max(0.001, remaining), response.close)
            timer.daemon = True
            timer.start()
        try:
            payload = response.read()
        except Exception as exc:
            if self._remaining_sec() == 0:
                raise DeadlineExceeded() from exc
            raise
        finally:
            if timer is not None:
                timer.cancel()
        self._check_deadline()
        return payload

    def _call(self, method: str, path: str, body: dict | None = None, accept: str | None = None):
        self._check_deadline()
        operation = self._operation_label(path)
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
            with urllib.request.urlopen(request, timeout=self._timeout_sec()) as response:
                answer = response.status, self._read_body(response)
            return answer
        except urllib.error.HTTPError as exc:
            answer = exc.code, self._read_body(exc)
            return answer
        finally:
            self._emit_remote_progress(operation)

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

    def sign_out(self) -> None:
        """Best-effort release of the session - a transport failure here must cost nothing.

        ⚠️ Measured 2026-09-09: a sign-out whose connection was closed without a response raised out
        of :func:`build` *after every input had been fingerprinted and matched*, and the CLI exited 1
        with **no file at all** (3 of 3 fingerprints lost); under ``run_estate`` the same failure was
        swallowed into a one-line warning and no phase evidence. The session expires on its own, so
        the only correct behaviour is to drop the token and continue.
        """
        if self.token:
            try:
                self._call("POST", "/auth/signout")
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                self.signout_failure_code = _exception_code(exc, "signout-failed")
                LOG.debug("Tableau sign-out failed (%s) - session left to expire", type(exc).__name__)
            finally:
                self.token = None

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


# pylint: disable-next=too-many-locals
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
    content_error_code = None
    try:
        remote_sha = lookup.content_sha256(workbook["id"])
        remote_key = lookup.content_revision_key(workbook["id"])
        unavailable = lookup.content_unavailable(workbook["id"])
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        remote_sha = None
        remote_key = None
        content_error_code = _exception_code(exc, "content-unavailable")
        unavailable = content_error_code
    local_key = RevisionKey.from_json(local.get("revision_key"))
    agreement = local_key.agrees_with(remote_key) if local_key is not None else None
    if remote_sha == local["sha256"]:
        verdict = "sha256"
    elif remote_sha is None:
        verdict = "unavailable"
    else:
        verdict = "name_only"
    origin = {
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
    if content_error_code is not None:
        origin["content_error_code"] = content_error_code
    return origin


def collect_inputs(target: Path) -> list[Path]:
    """The Tableau input(s) to stamp: one file, or every workbook/datasource in a folder."""
    if target.is_file():
        return [target]
    return sorted(p for p in target.iterdir() if p.suffix.lower() in TABLEAU_INPUT_SUFFIXES)


def _phase_record(timeout_sec: float | None) -> dict[str, Any]:
    return {
        "status": "running",
        "timeout_sec": None if timeout_sec is None else round(float(timeout_sec), 3),
        "errors": [],
        "progress": [],
        "residuals": [PAGINATION_RESIDUAL],
    }


def _exception_code(exc: BaseException, default: str = "lookup-unavailable") -> str:
    return DEADLINE_EXPIRED if isinstance(exc, DeadlineExceeded) else default


def _phase_error(
    phase: dict[str, Any], code: str, exc: BaseException | None = None, *, operation: str | None = None
) -> None:
    error: dict[str, Any] = {"code": code}
    if operation is not None:
        error["operation"] = operation
    if exc is not None:
        error["class"] = type(exc).__name__
    if error not in phase["errors"]:
        phase["errors"].append(error)


def _build_progress_sink(
    phase: dict[str, Any],
    *,
    started: float,
    deadline_at: float | None,
    clock: Callable[[], float],
) -> Callable[[dict[str, Any]], None]:
    def emit(event: dict[str, Any]) -> None:
        elapsed = max(0.0, clock() - started)
        progress = {**event, "elapsed_sec": round(elapsed, 1)}
        if deadline_at is not None:
            progress["remaining_sec"] = round(max(0.0, deadline_at - clock()), 1)
        phase["progress"].append(progress)
        visible = f"PROVENANCE progress: event={progress.get('event')} elapsed={progress['elapsed_sec']}s"
        if "remaining_sec" in progress:
            visible += f" remaining={progress['remaining_sec']}s"
        if "input_completed" in progress:
            visible += f" inputs={progress['input_completed']}/{progress['input_total']}"
        if "operation" in progress:
            visible += f" operation={progress['operation']} remote={progress.get('remote_completed')}"
        LOG.info(visible)

    return emit


def _deadline_expired(deadline_at: float | None, clock: Callable[[], float]) -> bool:
    return deadline_at is not None and clock() >= deadline_at


def _raise_deadline(_signum, _frame) -> None:
    raise DeadlineExceeded()


def _arm_deadline_alarm(deadline_at: float | None, clock: Callable[[], float]):
    if (
        deadline_at is None
        or clock is not time.monotonic
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "setitimer")
    ):
        return None
    previous_handler = signal.getsignal(signal.SIGALRM)
    remaining = max(0.001, deadline_at - clock())
    signal.signal(signal.SIGALRM, _raise_deadline)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    return previous_handler


def _disarm_deadline_alarm(previous_handler) -> None:
    if previous_handler is None:
        return
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, previous_handler)


# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def build(
    target: Path,
    env: dict[str, str],
    *,
    timeout_sec: float | None = DEFAULT_TIMEOUT_SEC,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Fingerprint every input, and attach its Tableau origin when credentials allow."""
    started = clock()
    deadline_at = None if timeout_sec is None else started + max(0.0, float(timeout_sec))
    phase = _phase_record(timeout_sec)
    progress = _build_progress_sink(phase, started=started, deadline_at=deadline_at, clock=clock)
    progress({"event": "phase-start", "input_completed": 0, "input_total": 0})
    alarm = _arm_deadline_alarm(deadline_at, clock)
    try:
        try:
            inputs = collect_inputs(target)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            _phase_error(phase, "collect-inputs-failed", exc)
            phase["status"] = "failed"
            return {
                "schema": "tableau-source-provenance/1",
                "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "input_count": 0,
                "phase": phase,
                "inputs": [],
            }
        progress({"event": "inputs-discovered", "input_completed": 0, "input_total": len(inputs)})
        lookup: TableauLookup | None = None
        has_workbook_inputs = any(path.suffix.lower() in WORKBOOK_SUFFIXES for path in inputs)
        has_live_credentials = bool(env.get("TABLEAU_SERVER_URL") and env.get("TABLEAU_PAT_NAME"))
        if has_live_credentials and has_workbook_inputs:
            try:
                lookup = TableauLookup(env)
                if hasattr(lookup, "set_run_context"):
                    lookup.set_run_context(deadline_at=deadline_at, clock=clock, progress=progress)
                lookup.sign_in()
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                _phase_error(phase, _exception_code(exc), exc, operation="sign-in")
                LOG.warning(
                    "no Tableau lookup (%s) - fingerprints only",
                    type(exc).__name__,
                )
                lookup = None

        records = []
        for index, path in enumerate(inputs, start=1):
            record: dict[str, Any]
            if _deadline_expired(deadline_at, clock):
                _phase_error(phase, DEADLINE_EXPIRED, DeadlineExceeded())
                records.append(
                    {
                        "input": {},
                        "origin": None,
                        "fingerprint_error": {"code": DEADLINE_EXPIRED, "class": "DeadlineExceeded"},
                        "origin_note": "local fingerprint unavailable - provenance phase deadline expired",
                    }
                )
                progress({"event": "input-complete", "input_completed": index, "input_total": len(inputs)})
                continue
            try:
                record = {"input": fingerprint(path)}
            except DeadlineExceeded as exc:
                _phase_error(phase, DEADLINE_EXPIRED, exc)
                records.append(
                    {
                        "input": {},
                        "origin": None,
                        "fingerprint_error": {"code": DEADLINE_EXPIRED, "class": type(exc).__name__},
                        "origin_note": "local fingerprint unavailable - provenance phase deadline expired",
                    }
                )
                progress({"event": "input-complete", "input_completed": index, "input_total": len(inputs)})
                continue
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                _phase_error(phase, "fingerprint-unavailable", exc)
                record = {
                    "input": {},
                    "origin": None,
                    "fingerprint_error": {"code": "fingerprint-unavailable", "class": type(exc).__name__},
                }
                records.append(record)
                progress({"event": "input-complete", "input_completed": index, "input_total": len(inputs)})
                continue
            if path.suffix.lower() not in WORKBOOK_SUFFIXES:
                record["origin"] = None
                if has_live_credentials:
                    record["lookup_error_code"] = DATASOURCE_ORIGIN_UNAVAILABLE
                    record["origin_note"] = (
                        "Tableau datasource live origin lookup is not implemented - local-only input"
                    )
                    _phase_error(phase, DATASOURCE_ORIGIN_UNAVAILABLE, operation="datasource-origin")
            elif lookup is not None:
                if _deadline_expired(deadline_at, clock):
                    origin = None
                    record["lookup_error_code"] = DEADLINE_EXPIRED
                    record["lookup_error"] = "DeadlineExceeded: provenance phase deadline expired"
                    _phase_error(phase, DEADLINE_EXPIRED, DeadlineExceeded())
                else:
                    try:
                        origin = find_origin(lookup, path.stem, record["input"])
                    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                        code = _exception_code(exc)
                        _phase_error(phase, code, exc)
                        origin, record["lookup_error_code"], record["lookup_error"] = (
                            None,
                            code,
                            (f"{type(exc).__name__}: {redacted_note(str(exc), lookup.redact_text, limit=150)}"),
                        )
                record["origin"] = origin
                if origin is None:
                    if record.get("lookup_error_code") == DEADLINE_EXPIRED:
                        record["origin_note"] = "remote origin unavailable - provenance phase deadline expired"
                    elif record.get("lookup_error_code"):
                        record["origin_note"] = "remote origin unavailable - Tableau lookup failed"
                    else:
                        record["origin_note"] = "no workbook of this LUID or name on the site - local-only input"
                elif origin["match"] == "name_only":
                    record["origin_note"] = (
                        f"matched by {origin['matched_by']}, but the bytes DIFFER from the site copy - "
                        "figures measured here will not reproduce against it"
                    )
                elif origin["match"] == "unavailable":
                    reason = origin.get("content_unavailable") or "the site refused the download"
                    code = origin.get("content_error_code") or "content-unavailable"
                    record["lookup_error_code"] = code
                    _phase_error(phase, code, operation="content")
                    record["origin_note"] = (
                        f"matched by {origin['matched_by']}, but the site copy could NOT be read "
                        f"({reason}) - no byte or revision comparison was made"
                    )
                    record["lookup_error"] = f"content unavailable: {reason}"
            records.append(record)
            progress({"event": "input-complete", "input_completed": index, "input_total": len(inputs)})
        result = {
            "schema": "tableau-source-provenance/1",
            "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "input_count": len(records),
            "phase": phase,
            "inputs": records,
        }
        if lookup is None:
            phase["status"] = "partial" if phase["errors"] else "local_only"
            return result
        return _finish_live(result, lookup)
    finally:
        _disarm_deadline_alarm(alarm)


def _finish_live(result: dict[str, Any], lookup: TableauLookup) -> dict[str, Any]:
    """Scrub the live-derived record and release the session, without either being able to lose it.

    ⚠️ Both steps used to sit unguarded after all the work was done, and that cost the whole file:
    measured 2026-09-09, a sign-out whose connection closed without a response discarded 3 of 3
    fingerprints and exited the CLI 1 with no artifact, and under ``run_estate`` the same failure was
    swallowed to a warning with no artifact either. Fingerprints are computed from local bytes and
    owe nothing to the site, so nothing the site does may delete them.

    Redaction failing is the one case where fingerprints are NOT simply kept alongside the rest: an
    unscrubbed live record can carry a reflected credential, so the response-derived half is withheld
    and the local half survives. Fail-closed on the secret, fail-open on the evidence.
    """
    if getattr(lookup, "_remaining_sec", lambda: None)() == 0:
        _phase_error(result["phase"], DEADLINE_EXPIRED, DeadlineExceeded(), operation="scrub")
        result = _derived_result_only(result)
    else:
        try:
            result, _paths = scrub_tree(result, lookup.redact_text)
        except DeadlineExceeded as exc:
            _phase_error(result["phase"], DEADLINE_EXPIRED, exc, operation="scrub")
            result = _derived_result_only(result)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            _phase_error(result["phase"], "redaction-failed", exc, operation="scrub")
            LOG.warning("provenance redaction failed (%s) - live origin fields withheld", type(exc).__name__)
            result = _without_live_fields(result, lookup.redact_text)
    try:
        lookup.sign_out()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        _phase_error(result["phase"], _exception_code(exc, "signout-failed"), exc, operation="sign-out")
        LOG.warning("Tableau sign-out failed (%s) - session left to expire", type(exc).__name__)
    if getattr(lookup, "signout_failure_code", None):
        _phase_error(result["phase"], lookup.signout_failure_code or "signout-failed", operation="sign-out")
    if result["phase"]["errors"] or any(record.get("lookup_error_code") for record in result["inputs"]):
        result["phase"]["status"] = "partial"
    else:
        result["phase"]["status"] = "complete"
    return result


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
        return _derived_result_only(result)


def _derived_result_only(result: dict[str, Any]) -> dict[str, Any]:
    def reduced(record: dict[str, Any]) -> dict[str, Any]:
        kept = {"input": _derived_only(record["input"]), "origin": None, "origin_note": WITHHELD_NOTE}
        for key in ("fingerprint_error", "lookup_error_code"):
            if key in record:
                kept[key] = record[key]
        return kept

    return {
        **result,
        "inputs": [reduced(record) for record in result["inputs"]],
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
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help="whole provenance phase deadline in seconds; use 0 to allow local fingerprints only",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = build(args.input, resolve_env(args.env), timeout_sec=args.timeout_sec)
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
