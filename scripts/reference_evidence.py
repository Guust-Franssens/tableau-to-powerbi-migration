"""
purpose: the EVIDENCE layer of the reference-readiness entry gate - decide whether a captured render
         is usable at all, and whether it is provably evidence for THIS workbook at THIS revision.
usage:   import reference_evidence as ev; ev.reference_evidence([Path("reference")])

Split out of `check_reference_readiness.py` because it answers a different question from the gate:
the gate asks "is this bundle ready to build against", this module asks "is this a picture I may
believe, and of what". Rationale and every measured defect: docs/reference-readiness.md.

The one rule: **unverified evidence is unrepresentable.** :class:Evidence is reachable only through
:meth:Evidence.build, which returns either a fully verified record or a :class:RejectedEvidence
that can never be matched. Round-1 review of PR #428 found three fail-open paths and round 2 found
three more, and every one existed because validity was checked at a call site rather than at
construction.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

KIND_DASHBOARD = "dashboard"
KIND_WORKSHEET = "worksheet"
KIND_UNKNOWN = "unknown"


# Grade strings are `check_unit.py:868,906`'s, reused verbatim so one vocabulary describes evidence
# everywhere. Inventing a second spelling would make two gates disagree about the same artifact.
GRADE_VALIDATION = "validation-grade"
GRADE_ORACLE = "layout/text only (oracle capture, default view state)"
GRADE_UNKNOWN = "unknown"

CAP_LAYOUT = "layout_grade"
CAP_TEXT = "text_readable"
CAP_VALIDATION = "validation_grade"

# `capture_tableau_reference.py:44-48`. Closed on purpose: an unrecognised capability means the
# manifest was written by something this gate does not understand, which is a rejection rather than
# an unknown grade.
ALLOWED_CAPABILITIES = frozenset({CAP_LAYOUT, CAP_TEXT, "state_reproducible", "revision_bound", CAP_VALIDATION})

# The MAXIMUM a provider may claim, derived from what it can structurally produce. Round-2 review:
# grade came from the self-reported capability list alone, so an `embedded_thumbnail` record - a
# 192x192 worksheet render - could claim `validation_grade`, reach READY under
# `--require-validation-grade`, and SILENCE the ceiling warning. A claim above the ceiling is a
# rejection, because the only things that produce one are a hand-edited manifest or a producer this
# gate does not understand; both mean the record cannot be trusted.
#
# `manual` is the ONLY route to validation grade, and only via `capture_tableau_reference.py
# --manual-validation-grade`, which is an explicit human assertion that logs a warning naming what it
# did not verify (`:279-284`). An unknown provider gets an EMPTY ceiling, so it can claim nothing.
PROVIDER_CEILING = {
    "embedded_thumbnail": frozenset({CAP_LAYOUT}),
    "public_playwright": frozenset({CAP_LAYOUT, CAP_TEXT}),
    "manual": frozenset({CAP_LAYOUT, CAP_TEXT, CAP_VALIDATION}),
    "server_rest": frozenset(),
    "oracle_capture": frozenset({CAP_LAYOUT, CAP_TEXT}),
}

# A reference must be legible, not merely present. 64px sits well below the 192x192 embedded Tableau
# thumbnail (`extract_twb_thumbnails.py`), the smallest render this toolkit treats as real evidence,
# so the floor rejects placeholders without rejecting the genuine low-fidelity route.
MIN_RENDER_EDGE = 64
MIN_PDF_BYTES = 1024

# `collect_manual` (`capture_tableau_reference.py:105`) globs `tableau-*.png` and names the record
# `img.stem`, so a user-dropped reference is called `tableau-<object>`. Round-2 review measured the
# consequence: a manifest shaped like the real `--manual-validation-grade` output matched NOTHING,
# because every name carried the prefix. Stripping it is what makes the documented route walkable.
MANUAL_NAME_PREFIX = "tableau-"

# What object a reference provider's output can possibly be a render OF. This is the scope join that
# replaces the name slug, and each entry is a structural fact about the provider, not a guess -
# `embedded_thumbnail` is per-WORKSHEET ("dashboards are not thumbnailed per se",
# `extract_twb_thumbnails.py`), `public_playwright` is driven from the spec's dashboard list
# (`capture_tableau_reference.py:135`), and `manual` cannot know "even that it is a screenshot of
# this dashboard" (`:261-266`) so it satisfies nothing on its own - UNLESS the operator asserted
# validation grade, which is an explicit, logged claim about THIS object. Docs: reference-readiness.md.
PROVIDER_SCOPE = {
    "embedded_thumbnail": KIND_WORKSHEET,
    "public_playwright": KIND_DASHBOARD,
    "server_rest": KIND_DASHBOARD,
    "manual": KIND_UNKNOWN,
}

# A `manual` record the operator explicitly graded `validation_grade` carries an asserted identity,
# so it may satisfy a page of either kind. Nothing else may.
KIND_ASSERTED = "operator-asserted"


@dataclass(frozen=True)
class UnitIdentity:
    """Who a unit is, for attributing evidence to it.

    ``workbook_luid`` is populated ONLY when provenance is byte-confirmed. Round-2 review measured
    the alternative: ``stamp_tableau_provenance.py`` records ``origin.match: "name_only"`` when the
    local and server bytes DIFFER and says outright that figures will not reproduce, yet that LUID
    was making server oracle evidence ready. The repo's own provenance is 26 ``sha256`` / 15
    ``name_only`` / 6 unmatched, so trusting it unconditionally is the common case, not an edge one.
    """

    name: str
    source_path: Path
    source_sha256: str
    workbook_luid: str | None = None


@dataclass(frozen=True)
class RejectedEvidence:
    """A candidate render that failed a construction precondition, kept so it can be REPORTED.

    Rejections are printed rather than dropped: an operator who captured a picture that does not
    count needs to be told why, or they will conclude the gate is broken and route around it.
    """

    name: str
    origin: str
    path: str | None
    reason: str


@dataclass(frozen=True)
class RecordedFacts:
    """What the CAPTURE PRODUCER wrote down about a render, for integrity checking.

    Round-2 finding 1: both producers already record `sha256`, byte count and dimensions
    (`capture_tableau_reference.py:246-257`, `capture_tableau_oracle.py:687-705`) and this gate read
    NONE of them. Measured on the real bundle: zeroing every manifest hash and setting dimensions to
    1x1 still returned `READY 3/3` with zero rejections, so a captured image could be swapped
    wholesale without invalidating readiness - using integrity data that was already sitting there.
    """

    sha256: str | None = None
    byte_size: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class Evidence:  # pylint: disable=too-many-instance-attributes
    """A render proven usable AND attributable. Construct only via :meth:`build`.

    Every field is a precondition, not a hint. Round-1 review found three fail-open paths (zero-byte
    render, empty capabilities, wrong-workbook attribution) that all existed because validity was
    checked at call sites instead of at construction; round 2 found three more one level down
    (shallow PNG parse, self-promoted grade, untrusted provenance). Folding these into a smaller
    object would recreate the defect.
    """

    name: str
    kind: str
    grade: str
    origin: str
    provider: str
    path: str
    width: int | None
    height: int | None
    workbook_sha: str | None
    workbook_luid: str | None
    workbook_name: str | None

    @classmethod
    def build(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        cls,
        *,
        name: str,
        kind: str,
        capabilities: Any,
        origin: str,
        provider: str,
        render_path: Path,
        recorded: RecordedFacts,
        workbook_sha: str | None = None,
        workbook_luid: str | None = None,
        workbook_name: str | None = None,
    ) -> Evidence | RejectedEvidence:
        """Return verified evidence, or a rejection naming the precondition that failed."""
        display = str(render_path)

        def reject(reason: str) -> RejectedEvidence:
            return RejectedEvidence(name=name, origin=origin, path=display, reason=reason)

        if not name.strip():
            return reject("record has no view name")
        if not (workbook_sha or workbook_luid or workbook_name):
            return reject(
                "no workbook identity recorded, so this render cannot be attributed to any unit "
                "(a reference manifest must carry source_workbook_sha256; an oracle record must "
                "carry workbook_luid or workbook_name)"
            )
        grade = provider_grade(provider, capabilities)
        if isinstance(grade, str) and grade.startswith("!"):
            return reject(grade[1:])
        facts = render_facts(render_path, recorded)
        if isinstance(facts, str):
            return reject(facts)
        width, height = facts
        return cls(
            name=name,
            kind=KIND_ASSERTED if provider == "manual" and grade == GRADE_VALIDATION else kind,
            grade=grade,
            origin=origin,
            provider=provider,
            path=display,
            width=width,
            height=height,
            workbook_sha=workbook_sha,
            workbook_luid=workbook_luid,
            workbook_name=workbook_name,
        )

    def match_names(self) -> list[str]:
        """Every spelling of the source-object name this record could legitimately be filed under.

        A `manual` record is named from its FILE STEM (`img.stem`), and `collect_manual` only ever
        picks up `tableau-*.png`, so the documented drop convention puts a `tableau-` prefix on every
        such name. Round-2 review measured a manifest shaped like the real `--manual-validation-grade`
        output matching nothing at all because of it.
        """
        if self.provider == "manual" and self.name.casefold().startswith(MANUAL_NAME_PREFIX):
            return [self.name, self.name[len(MANUAL_NAME_PREFIX) :]]
        return [self.name]

    def is_for(self, unit: UnitIdentity) -> bool:
        """Whether this render is provably evidence for ``unit``.

        Reference evidence is keyed by the SOURCE SHA, so a capture taken against an older revision
        of the workbook does not silently remain valid - a stale picture is worse than a missing one
        because it looks like evidence.

        Oracle evidence carries BOTH a LUID and a workbook name, and both are used. Round-2 review
        measured the previous either/or: a record carrying a LUID discarded its name, so removing the
        (untrusted) source provenance made correctly-NAMED records return `0/3 blind`. A LUID is
        trusted only when the unit's provenance was byte-confirmed; otherwise the name is the
        fallback, exactly as documented.
        """
        if self.workbook_sha is not None:
            return self.workbook_sha.casefold() == unit.source_sha256.casefold()
        if self.workbook_luid and unit.workbook_luid:
            return self.workbook_luid.casefold() == unit.workbook_luid.casefold()
        return bool(self.workbook_name) and norm_name(self.workbook_name) == norm_name(unit.name)


def norm_name(text: str | None) -> str:
    """Whitespace/case-normalized name, for comparing two spellings of the same object."""
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def sha256_of(path: Path) -> str | None:
    """sha256 of a file, or None when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def json_object(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or None when it is absent, unreadable or not an object."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_size(blob: bytes) -> tuple[int, int] | None:
    """`(w, h)` for a STRUCTURALLY COMPLETE PNG, else None.

    Round-2 finding 1: this read only the signature, the `IHDR` marker and 8 dimension bytes, so a
    **24-byte blob** produced valid `Evidence` while Pillow rejected the same bytes with
    `Truncated File Read`. The whole chunk stream is now walked - every length/CRC verified, a
    13-byte IHDR required, and both IDAT and IEND required - so a truncated or forged file cannot
    pass by getting its first 24 bytes right.

    Deliberately a structural check, not a decode: this gate must run with no image dependency, and
    a complete, CRC-correct chunk stream is a far stronger claim than "the header looked plausible".
    It does NOT prove the pixels are meaningful - that is what the recorded `sha256` is for.
    """
    if len(blob) < len(PNG_SIGNATURE) + 12 or not blob.startswith(PNG_SIGNATURE):
        return None
    offset, size, seen = len(PNG_SIGNATURE), None, set()
    while offset + 8 <= len(blob):
        (length,) = struct.unpack(">I", blob[offset : offset + 4])
        tag = blob[offset + 4 : offset + 8]
        end = offset + 8 + length
        if length > len(blob) or end + 4 > len(blob):
            return None
        body = blob[offset + 4 : end]
        (declared_crc,) = struct.unpack(">I", blob[end : end + 4])
        if zlib.crc32(body) & 0xFFFFFFFF != declared_crc:
            return None
        if tag == b"IHDR":
            if length != 13 or seen:
                return None
            width, height = struct.unpack(">II", body[4:12])
            size = (int(width), int(height))
        seen.add(tag)
        offset = end + 4
        if tag == b"IEND":
            break
    if size is None or b"IDAT" not in seen or b"IEND" not in seen or offset != len(blob):
        return None
    return size


def _svg_size(blob: bytes) -> tuple[int, int] | None:
    try:
        root = ElementTree.fromstring(blob.decode("utf-8", "ignore"))  # noqa: S314
    except ElementTree.ParseError:
        return None
    numbers: list[float | None] = []
    for attr in ("width", "height"):
        match = re.match(r"\s*([0-9.]+)", str(root.get(attr) or ""))
        numbers.append(float(match.group(1)) if match else None)
    if numbers[0] is not None and numbers[1] is not None:
        return int(numbers[0]), int(numbers[1])
    box = re.findall(r"[-0-9.]+", str(root.get("viewBox") or ""))
    return (int(float(box[2])), int(float(box[3]))) if len(box) == 4 else None


def _render_size(blob: bytes, suffix: str) -> tuple[int, int] | None | str:
    """`(w, h)` for a parsed raster/vector render, `None` for PDF (accepted on header+size), or a reason."""
    if suffix == ".pdf":
        if not blob.startswith(b"%PDF-"):
            return "render is not a PDF despite its .pdf extension"
        if len(blob) < MIN_PDF_BYTES:
            return f"PDF render is only {len(blob)} bytes, below the {MIN_PDF_BYTES}-byte floor"
        return None
    size = _png_size(blob) if suffix == ".png" else _svg_size(blob) if suffix == ".svg" else None
    if size is None:
        return f"render did not parse as {suffix.lstrip('.') or 'an image'} (truncated, empty or mislabelled)"
    return size


def _integrity_mismatch(blob: bytes, size: tuple[int, int] | None, recorded: RecordedFacts) -> str | None:
    """The producer's own record, checked against the bytes on disk. None when they agree.

    Round-2 finding 1: the `sha256`, byte count and dimensions BOTH capture producers already write
    (`capture_tableau_reference.py:246-257`, `capture_tableau_oracle.py:687-705`) were never read, so
    a captured image could be replaced wholesale and readiness survived - measured on the real
    bundle, zeroed hashes and 1x1 dimensions still returned `READY 3/3` with zero rejections.

    A recorded hash is REQUIRED: both producers always emit one, so its absence means a hand-written
    or foreign manifest whose integrity nothing can confirm.
    """
    if not recorded.sha256:
        return (
            "the manifest records no sha256 for this render, so its integrity cannot be confirmed "
            "(both capture producers always write one)"
        )
    actual = hashlib.sha256(blob).hexdigest()
    if actual.casefold() != recorded.sha256.casefold():
        return (
            f"render does not match its recorded sha256 (recorded {recorded.sha256[:12]}..., actual {actual[:12]}...)"
        )
    if recorded.byte_size is not None and recorded.byte_size != len(blob):
        return f"render is {len(blob)} bytes but the manifest records {recorded.byte_size}"
    if size is not None and recorded.width is not None and recorded.height is not None:
        if (recorded.width, recorded.height) != size:
            return f"render is {size[0]}x{size[1]} but the manifest records {recorded.width}x{recorded.height}"
    return None


def render_facts(  # pylint: disable=too-many-return-statements
    path: Path, recorded: RecordedFacts
) -> tuple[int | None, int | None] | str:
    """`(width, height)` for a render that parses AND matches its recorded facts, else a reason.

    This is the check `Path.is_file()` was standing in for. Round-1 review measured a **zero-byte**
    file reaching `READY`; round 2 measured a **24-byte blob** doing the same, and a real image being
    swapped for another without invalidating readiness. So three things must agree: the bytes must
    form a structurally complete file of the format their extension claims, the result must be big
    enough to read, and it must be the file the producer actually captured.

    A PDF has no cheap dimension read (the MediaBox may sit behind an object stream), so it is
    accepted on a `%PDF-` header plus a size floor. That is a deliberately weaker check, and it is
    stated rather than hidden - the recorded sha256 still pins the bytes.
    """
    try:
        blob = path.read_bytes()
    except OSError as exc:
        return f"render could not be read: {exc}"
    if not blob:
        return "render is zero bytes"
    size = _render_size(blob, path.suffix.lower())
    if isinstance(size, str):
        return size
    mismatch = _integrity_mismatch(blob, size, recorded)
    if mismatch is not None:
        return mismatch
    if size is None:
        return (None, None)
    width, height = size
    if min(width, height) < MIN_RENDER_EDGE:
        return f"render is {width}x{height}, below the {MIN_RENDER_EDGE}px legibility floor"
    return width, height


def _entry_scope(entry: dict[str, Any], provider: str | None) -> str:
    """What kind of object a reference entry is a render of.

    An explicit `view_type`/`object_type` on the entry wins, so a manifest enriched with the oracle's
    view-type join (PR #422) is honoured without a code change here. Otherwise the provider decides.
    Anything unrecognised is UNKNOWN, which satisfies nothing - never a guess at either type.
    """
    declared = entry.get("view_type") or entry.get("object_type")
    if isinstance(declared, str) and declared.strip().casefold() in (KIND_DASHBOARD, KIND_WORKSHEET):
        return declared.strip().casefold()
    return PROVIDER_SCOPE.get(str(provider or ""), KIND_UNKNOWN)


def provider_grade(provider: str, capabilities: Any) -> str:
    """The grade a record may claim, CAPPED BY ITS PRODUCER. A leading `!` is a rejection reason.

    Round-2 finding 2: this read the self-reported capability list alone, so an `embedded_thumbnail`
    record - structurally a 192x192 worksheet render - could claim `validation_grade`, reach READY
    under `--require-validation-grade`, and silence the ceiling warning. The maximum is now derived
    from the PROVIDER (`PROVIDER_CEILING`), which is a fact about what the producer can physically
    make; a claim above it means a hand-edited manifest or an unrecognised producer, and either way
    the record cannot be trusted.
    """
    ceiling = PROVIDER_CEILING.get(provider)
    if ceiling is None:
        return f"!unrecognised capture provider {provider!r}, so nothing bounds what it may claim"
    if not isinstance(capabilities, list) or not capabilities:
        return "!no usable capability grade (the manifest records an empty or non-list capabilities)"
    caps = {cap for cap in capabilities if isinstance(cap, str)}
    if not caps or not caps <= ALLOWED_CAPABILITIES:
        return f"!capabilities outside the known vocabulary: {sorted(caps - ALLOWED_CAPABILITIES)}"
    over = caps - ceiling
    if over:
        return (
            f"!provider {provider!r} claims {sorted(over)} but can only produce {sorted(ceiling)} "
            "- a capture cannot grade itself above what its producer is able to capture"
        )
    if provider == "oracle_capture":
        # Kept as its own string so one vocabulary describes evidence across gates
        # (`check_unit.py:906`), and so the default-view-state caveat travels with the grade.
        return GRADE_ORACLE
    return GRADE_VALIDATION if CAP_VALIDATION in caps else "/".join(sorted(caps))


def _reference_states(directory: Path, entry: dict[str, Any], workbook_sha: Any) -> list[Evidence | RejectedEvidence]:
    """Build every state of one `reference/manifest.json` entry."""
    name = str(entry.get("name") or "")
    built: list[Evidence | RejectedEvidence] = []
    for state in entry.get("states") or []:
        if not isinstance(state, dict):
            continue
        image = state.get("image")
        if not isinstance(image, str) or not image:
            built.append(RejectedEvidence(name, "reference", None, "state declares no image"))
            continue
        dims = state.get("dimensions") if isinstance(state.get("dimensions"), dict) else {}
        built.append(
            Evidence.build(
                name=name,
                kind=_entry_scope({**entry, **state}, state.get("provider")),
                capabilities=state.get("capabilities"),
                origin="reference",
                provider=str(state.get("provider") or ""),
                render_path=directory / image,
                recorded=RecordedFacts(
                    sha256=state.get("sha256") if isinstance(state.get("sha256"), str) else None,
                    byte_size=state.get("bytes") if isinstance(state.get("bytes"), int) else None,
                    width=dims.get("w") if isinstance(dims.get("w"), int) else None,
                    height=dims.get("h") if isinstance(dims.get("h"), int) else None,
                ),
                workbook_sha=str(workbook_sha) if isinstance(workbook_sha, str) and workbook_sha else None,
            )
        )
    return built


def _split(built: list[Evidence | RejectedEvidence]) -> tuple[list[Evidence], list[RejectedEvidence]]:
    """Partition build results, so no call site can accidentally treat a rejection as evidence."""
    usable = [item for item in built if isinstance(item, Evidence)]
    return usable, [item for item in built if isinstance(item, RejectedEvidence)]


def reference_evidence(reference_dirs: list[Path]) -> tuple[list[Evidence], list[RejectedEvidence]]:
    """Evidence declared by `reference/manifest.json` files, split into usable and rejected.

    Note the manifest's top-level key is `dashboards`, but `capture_tableau_reference.py:199` files
    WORKSHEET thumbnails there too. The key is therefore not evidence of scope; the provider is.
    """
    built: list[Evidence | RejectedEvidence] = []
    for directory in reference_dirs:
        payload = json_object(directory / "manifest.json")
        entries = (payload or {}).get("dashboards")
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                built.extend(_reference_states(directory, entry, (payload or {}).get("source_workbook_sha256")))
    return _split(built)


def _oracle_workbook_ids(record: dict[str, Any]) -> tuple[str | None, str | None]:
    """`(luid, workbook_name)` - BOTH, because either may be the only usable one.

    Round-2 finding 3: this returned one or the other, so a record carrying a LUID discarded its
    name; removing the (untrusted) source provenance then made correctly-named records return
    `0/3 blind`. `Evidence.is_for` decides which to trust.
    """
    luid = record.get("workbook_luid")
    name = record.get("workbook_name")
    return (
        luid if isinstance(luid, str) and luid else None,
        name if isinstance(name, str) and name else None,
    )


def _oracle_view_kind(record: dict[str, Any]) -> str:
    """PR #422's `view_type`, or UNKNOWN. Absent and `unknown` both mean "cannot establish"."""
    declared = record.get("view_type")
    if isinstance(declared, str) and declared.strip().casefold() in (KIND_DASHBOARD, KIND_WORKSHEET):
        return declared.strip().casefold()
    return KIND_UNKNOWN


def _oracle_leg(directory: Path, record: dict[str, Any]) -> tuple[Path, RecordedFacts] | None:
    """The first render leg the record claims succeeded, with the facts the producer recorded."""
    for name in ("image", "svg", "pdf"):
        leg = record.get(name)
        if not (isinstance(leg, dict) and leg.get("status") == "ok" and isinstance(leg.get("path"), str)):
            continue
        dims = leg.get("dimensions_px") if isinstance(leg.get("dimensions_px"), dict) else {}
        return directory / leg["path"], RecordedFacts(
            sha256=leg.get("sha256") if isinstance(leg.get("sha256"), str) else None,
            byte_size=leg.get("bytes") if isinstance(leg.get("bytes"), int) else None,
            width=dims.get("w") if isinstance(dims.get("w"), int) else None,
            height=dims.get("h") if isinstance(dims.get("h"), int) else None,
        )
    return None


def _oracle_record(directory: Path, record: dict[str, Any]) -> Evidence | RejectedEvidence:
    """Build one oracle view record."""
    name = str(record.get("view_name") or record.get("view_url_name") or "")
    leg = _oracle_leg(directory, record)
    if leg is None:
        return RejectedEvidence(name, "oracle", None, "no render leg reported status ok")
    render_path, recorded = leg
    luid, workbook_name = _oracle_workbook_ids(record)
    return Evidence.build(
        name=name,
        kind=_oracle_view_kind(record),
        capabilities=[CAP_LAYOUT, CAP_TEXT],
        origin="oracle",
        provider="oracle_capture",
        render_path=render_path,
        recorded=recorded,
        workbook_luid=luid,
        workbook_name=workbook_name,
    )


def oracle_evidence(oracle_dirs: list[Path]) -> tuple[list[Evidence], list[RejectedEvidence]]:
    """Evidence declared by `_oracle/oracle-manifest.json` files, split into usable and rejected.

    `view_type` comes from PR #422's Metadata-API join and is consumed if present. It fails closed by
    design there (a disabled Metadata API yields `unknown` for everything), and it fails closed here
    too: absent or `unknown` means this record cannot satisfy any page, rather than being allowed to
    satisfy either kind. An oracle capture is default-view-state with no `?vf_` pinning, so its grade
    is layout/text only regardless of render leg.
    """
    built: list[Evidence | RejectedEvidence] = []
    for directory in oracle_dirs:
        payload = json_object(directory / "oracle-manifest.json")
        built.extend(
            _oracle_record(directory, record)
            for record in (payload or {}).get("views") or []
            if isinstance(record, dict)
        )
    return _split(built)


# Sentinel for `match_evidence`: several records normalize to one name, so no single one can be
# credited. Round-2 finding 5 - ambiguity must be a refusal, not a resolution.
AMBIGUOUS = "__ambiguous__"
