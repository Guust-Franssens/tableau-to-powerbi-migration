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
from typing import Any, Iterable
from xml.etree import ElementTree

from object_identity import (
    KIND_DASHBOARD,
    KIND_UNKNOWN,
    KIND_WORKSHEET,
    WB_LUID,
    WB_SHA,
    WB_STALE,
    WB_UNCONFIRMED,
    AmbiguousIdentity,
    Attribution,
    Candidate,
    WorkbookIdentity,
    agreed_luid,
    harvest_luid,
    persisted_stem,
)


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

# A `manual` record's kind comes from what the MANIFEST declares, never from its grade.
# Round-3 finding 1: `validation_grade` used to promote a manual record to a kind that matched both
# dashboards and worksheets, so one image made a dashboard `Ops` AND a worksheet `Ops` ready - the
# founding "one worksheet render satisfies a dashboard" defect, re-created one domain over. The
# `--manual-validation-grade` flag asserts GRADE; `capture_tableau_reference.py:264-266` says
# outright the tool cannot know "even that it is a screenshot of this dashboard". Grade and kind are
# independent axes and one may never widen the other.
MANUAL_KIND_HINT = (
    "a `manual` record carries no object type, so it cannot satisfy any page. Declare it in the "
    "manifest entry as `view_type`/`object_type` (`dashboard` or `worksheet`) - the grade flag "
    "asserts how good the picture is, never what it is of."
)


#: What is known about the REVISION the evidence depicts, as distinct from which workbook it is of.
#: Three values, because two were not enough: "I proved the bytes are the site copy", "I proved they
#: are not", and "I could not tell" are different claims, and only the middle one may refuse a page.
REVISION_CONFIRMED = "confirmed"
REVISION_UNCONFIRMED = "unconfirmed"
REVISION_MISMATCH = "mismatch"


@dataclass(frozen=True)
class UnitIdentity:
    """Who a unit is, for attributing evidence to it.

    ``workbook_luid`` is the published workbook's server id, and it is **the** identity: a display
    name is not unique across projects, and the local artifact stem is a filesystem-sanitised
    spelling of one (`HR_Dashboard` for `HR Dashboard`), so the name axis cannot bridge harvest.

    ⚠️ Round-2 review gated this LUID on ``origin.match == "sha256"``; issue #450 removed that gate
    because ``match`` answers a different question. ``stamp_tableau_provenance.find_origin`` says so
    itself: ``matched_by`` records *how the workbook was found* and ``match`` records *how strongly
    the bytes were confirmed*, and they are "two independent axes".

    ``revision`` is that second axis, kept and reported rather than discarded - round-1 review of PR
    #454 found dropping it made a capture of a different BUILD read as current. It is THREE-valued
    on measured grounds; see :func:`check_reference_readiness._provenance_origin` for why
    ``name_only`` alone is not evidence of a changed build.
    """

    name: str
    source_path: Path
    source_sha256: str
    workbook_luid: str | None = None
    revision: str = REVISION_UNCONFIRMED

    def workbook(self) -> WorkbookIdentity:
        """This unit's workbook, on the axes it can establish.

        The unit's ``name`` is an ARTIFACT STEM, not a published display name. It is offered on the
        name axis anyway because that is the only axis a locally-captured `reference/` manifest and a
        server oracle can share when no LUID is available - but it is the weakest route, and
        :meth:`WorkbookIdentity.attribute` reaches it only when the record claims no machine identity
        at all.
        """
        return WorkbookIdentity.of(luid=self.workbook_luid, name=self.name, sha256=self.source_sha256)


#: Provenance ``origin`` shapes that ESTABLISH which published workbook a local file is. Deliberately
#: distinct from ``origin.match``, which answers a REVISION question and was read as if it were this
#: one (issue #450).
IDENTITY_MATCHED_BY = frozenset({"luid"})


def provenance_origin(root: Path, source_sha: str, source: Path | None = None) -> tuple[str | None, str]:
    """``(workbook LUID, revision status)`` for one source, from every offline route.

    Lives beside :class:`UnitIdentity` because it builds one. Two INDEPENDENT answers -
    `stamp_tableau_provenance.find_origin` calls them "two independent axes" - and reading either as
    the other has now caused a defect in each direction:

    * **identity** - ``origin.workbook_luid``, trusted when ``matched_by == "luid"`` (the harvested
      filename's LUID was found on the site, so it is provably that item) **or** when
      ``match == "sha256"``. Reading ``match`` alone discarded a proven LUID and fell back to
      comparing the artifact stem ``HR_Dashboard`` against the published name ``HR Dashboard``
      (issue #450). The asset filename's own ``<luid>_`` prefix is a second claim and the two must
      agree: contradictory identities are less evidence than none.
    * **revision** - THREE-valued, and that is a measured correction rather than a hedge.

    ⚠️ **``match: "name_only"`` is NOT by itself evidence of a changed build.** ``find_origin``
    compares the harvested bytes against a fresh download, and a `.twbx` is repacked per request:
    measured 2026-09-03 on the live site, three downloads of every item in one run, the RAW digest
    differed for **27 of 49 archives** while the content-normalised key differed for **0 of 67**
    items. Reading ``name_only`` as drift marked 18 of 67 units and 125 pages unverifiable on an
    estate where nothing had changed. The reproducible answer is ``origin.revision_match``, from
    :class:`object_identity.RevisionKey`; see :func:`revision_status`.

    Anything with no comparable key - including no provenance at all - is ``unconfirmed``, which is
    admitted and DISCLOSED rather than silently claimed as current (round-1 review of PR #454,
    blocker 2).

    ⚠️ **Every record matching ``source_sha`` is read, and the verdict does not depend on their
    order.** This used to ``return`` on the first SHA hit, so two provenance records carrying the
    same source digest and DIFFERENT ``workbook_luid`` values resolved to whichever came first in the
    JSON. Measured by the round-N reviewer on byte-identical evidence, reversing only the array
    order::

        AMBIGUOUS_FIRST    = {"status":"READY",   "pages_ready":1, "luid":1,    "exit":0}
        AMBIGUOUS_REVERSED = {"status":"FINDINGS","pages_ready":0, "foreign":1, "exit":1}

    One of those records is about another workbook and nothing here says which, so this raises
    :class:`object_identity.AmbiguousIdentity` - a *cannot establish*, which
    `check_reference_readiness.assess_unit` turns into ``CANNOT_ESTABLISH`` and which is explicitly
    never a pass. Where the records merely disagree about the REVISION they are folded to the
    weakest claim they support, which is likewise order-free and fails closed.
    """
    stamped = harvest_luid(persisted_stem(source.name if source is not None else None))
    inputs = list((json_object(root / "source-provenance.json") or {}).get("inputs") or [])
    origins = [origin for origin in map(_stamped_origin, inputs) if origin is not None and origin[0] == source_sha]
    if not origins:
        return agreed_luid(stamped), REVISION_UNCONFIRMED
    claimed = {luid for _, luid in ((sha, _identity_claim(origin)) for sha, origin in origins) if luid}
    if len(claimed) > 1:
        raise AmbiguousIdentity(
            f"source-provenance.json carries {len(origins)} record(s) for source sha256 "
            f"{source_sha[:12]}... claiming {len(claimed)} different workbook LUIDs "
            f"({', '.join(sorted(claimed))}). One of them is about a different workbook and nothing "
            "here says which, so this unit has no workbook identity - re-stamp with "
            "scripts/stamp_tableau_provenance.py rather than letting array order decide"
        )
    return agreed_luid(stamped, *claimed), _weakest_revision(revision_status(origin, inputs) for _, origin in origins)


def _stamped_origin(record: Any) -> tuple[Any, dict[str, Any]] | None:
    """``(recorded input sha256, origin)`` for one `source-provenance.json` entry, or None."""
    if not isinstance(record, dict):
        return None
    stamped_input = record.get("input") if isinstance(record.get("input"), dict) else {}
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    return stamped_input.get("sha256"), origin


def _identity_claim(origin: dict[str, Any]) -> str | None:
    """The workbook LUID this origin ESTABLISHES, or None when it establishes none.

    ``matched_by``/``match`` decide whether the recorded LUID may be believed at all; see
    :func:`provenance_origin`. Case-folded, so two spellings of one LUID are one claim rather than an
    ambiguity.
    """
    if origin.get("matched_by") not in IDENTITY_MATCHED_BY and origin.get("match") != "sha256":
        return None
    luid = origin.get("workbook_luid")
    return luid.strip().casefold() if isinstance(luid, str) and luid.strip() else None


def _weakest_revision(claims: Iterable[str]) -> str:
    """The least that every one of ``claims`` supports - order-free, and fails closed.

    Two records for one source that disagree about the build settle nothing between them, so the
    weakest claim stands: a single ``mismatch`` refuses, a single ``unconfirmed`` withholds, and only
    unanimous confirmation confirms.
    """
    seen = set(claims)
    if REVISION_MISMATCH in seen:
        return REVISION_MISMATCH
    return REVISION_CONFIRMED if seen == {REVISION_CONFIRMED} else REVISION_UNCONFIRMED


def revision_status(origin: dict[str, Any], inputs: list[Any]) -> str:
    """``confirmed`` / ``mismatch`` / ``unconfirmed`` for one stamped origin.

    Reads the REPRODUCIBLE key first. ``stamp_tableau_provenance.find_origin`` records
    ``revision_match`` from :class:`object_identity.RevisionKey` on both sides, which normalises a
    `.twbx`/`.tdsx` archive so zip member order and mtimes cannot change it. Measured 2026-09-03
    against the live site, three downloads of every item in one run:

    | over 48 workbooks + 19 datasources | raw sha256 differs | content key differs |
    |---|---|---|
    | archives (49) | **27** | **0** |
    | non-archives (18, plain `.twb` XML) | **0** | n/a - raw IS the content |

    ⚠️ ``match: "name_only"`` is therefore NOT evidence of a changed build, and round 2 of PR #454
    was right to refuse to treat it as one - but wrong to conclude that no reproducible comparison
    existed. It did; the wrong digest was being taken. A raw *match* still implies a content match,
    so ``match == "sha256"`` remains a valid confirmation; a raw *difference* establishes nothing.

    ⚠️ An origin carrying no comparable key is ``unconfirmed``, never ``mismatch``. That covers a
    manifest stamped before this key existed, two different key algorithms, and an archive that would
    not open - the last of which yields NO key at all rather than a raw one, because silently
    re-hashing those bytes raw is precisely the defect this replaces.
    """
    _ = inputs
    declared = origin.get("revision_match")
    if declared == "same":
        return REVISION_CONFIRMED
    if declared == "differs":
        return REVISION_MISMATCH
    return REVISION_CONFIRMED if origin.get("match") == "sha256" else REVISION_UNCONFIRMED


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
    #: sha256 of the render bytes, VERIFIED against the producer's recorded hash by :meth:uild.
    #: Exclusivity keys on this rather than on a path, because a path cannot identify a hard link or
    #: a drive alias and a text fallback is what round 5 found reopening the defect (see
    #: `check_reference_readiness._render_key`).
    render_digest: str

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
        # render_facts has already proven the bytes hash to recorded.sha256, so this is the
        # VERIFIED content identity rather than a claim taken from the manifest.
        digest = (recorded.sha256 or "").casefold()
        return cls(
            name=name,
            kind=kind,
            grade=grade,
            origin=origin,
            provider=provider,
            path=display,
            width=width,
            height=height,
            workbook_sha=workbook_sha,
            workbook_luid=workbook_luid,
            workbook_name=workbook_name,
            render_digest=digest,
        )

    def candidate(self) -> Candidate:
        """This record as an external-producer CANDIDATE - never as an identity.

        A capture manifest names a file; it does not establish what the file depicts. So the kind
        here is whatever the producer DECLARED, and `KIND_UNKNOWN` otherwise, which can never resolve
        against a real page (`object_identity.IdentityIndex.resolve`).

        A `manual` record is named from its file stem, and `collect_manual`
        (`capture_tableau_reference.py:105`) only globs `tableau-*.png`, so the prefix is imposed by
        the glob rather than chosen by the operator - stripping it recovers the name they typed.
        Both spellings are offered as candidate names, and the index refuses if they turn out to
        match more than one object: round-3 finding 1 measured one image making two distinct
        worksheets ready because the alias had no uniqueness check.
        """
        names = [self.name]
        if self.provider == "manual" and self.name.casefold().startswith(MANUAL_NAME_PREFIX):
            names.append(self.name[len(MANUAL_NAME_PREFIX) :])
        return Candidate(names=tuple(names), kind=self.kind)

    def workbook(self) -> WorkbookIdentity:
        """The workbook this render declares it came from, on whichever axes its producer wrote."""
        return WorkbookIdentity.of(luid=self.workbook_luid, name=self.workbook_name, sha256=self.workbook_sha)

    def attribution(self, unit: UnitIdentity) -> Attribution:
        """Why this render does or does not belong to ``unit``, naming the axis that decided.

        The identity half is :meth:`WorkbookIdentity.attribute`, shared with ``check_unit.py`` - see
        issue #450, where the two gates disagreed about how a workbook is identified and produced one
        fail-closed and one fail-open defect from that single disagreement.

        The REVISION half is here, because it is this gate's contract rather than a property of
        identity: evidence must be for "THIS workbook, at THIS revision". A sha256 join is
        revision-bound by construction, so it passes through confirmed. A LUID join says *which*
        workbook, never *which build of it*, so it inherits the unit's revision status:

        * :data:`REVISION_MISMATCH` -> :data:`object_identity.WB_STALE`;
        * :data:`REVISION_UNCONFIRMED` -> :data:`object_identity.WB_UNCONFIRMED`.

        ⚠️ Round 3: ``unconfirmed`` used to be ADMITTED with a note, and that was fail-open. The human
        output's first line said `READY` and the caveat came later, which is not disclosure a
        consumer acts on. Neither refusal certifies anything now; both are counted, and
        :func:`check_reference_readiness._page_row` reports the page UNVERIFIABLE with the reason.

        ⚠️ Absence of provenance, and a byte comparison that could not be made soundly, are both
        UNCONFIRMED rather than mismatched. "I cannot tell whether the bytes moved" and "I can tell,
        and they did" are different statements, and they call for different operator actions -
        re-stamp provenance versus re-capture against the migrated build.
        """
        verdict = unit.workbook().attribute(self.workbook())
        if verdict.route != WB_LUID:
            return verdict
        if unit.revision == REVISION_MISMATCH:
            return Attribution(
                WB_STALE,
                "the same workbook, a DIFFERENT build: this unit's source bytes differ from a "
                "REPRODUCIBLE content digest of the site copy, and a luid join says which workbook a "
                "render is of, never which revision",
                axis=verdict.axis,
            )
        if unit.revision != REVISION_CONFIRMED:
            return Attribution(
                WB_UNCONFIRMED,
                "the right workbook, at an UNESTABLISHED build: a luid join says which workbook a "
                "render is of, never which revision, and this unit's provenance carries no "
                "comparable revision key - re-stamp it with scripts/stamp_tableau_provenance.py",
                axis=verdict.axis,
            )
        return verdict

    def revision_for(self, unit: UnitIdentity) -> str:
        """What this render establishes about the REVISION, once it has been admitted.

        A sha256 join pins the bytes, so it is confirmed whatever provenance says. Anything weaker
        inherits the unit's status - and since round 3 an admitted render is always CONFIRMED,
        because everything else is refused. The field stays because a reader should see the claim
        stated rather than inferred from its absence.
        """
        return REVISION_CONFIRMED if self.attribution(unit).route == WB_SHA else unit.revision


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


def _reference_workbook_luid(entry: dict[str, Any], state: dict[str, Any], manifest: dict[str, Any]) -> str | None:
    """The workbook LUID a `reference/manifest.json` declares, on the state, entry or manifest.

    ⚠️ **This used to be thrown away**, and that is the entry-gate half of the round-N contradiction
    finding: only ``source_workbook_sha256`` was carried into :class:`Evidence`, so a manifest whose
    sha256 matched this unit while its ``workbook_luid`` named another workbook had nothing left to
    contradict with by the time :meth:`WorkbookIdentity.attribute` saw it - `READY 1/1`, exit 0,
    ``attribution.luid == 0``. Key names mirror `check_unit._declared_workbook`, so one vocabulary
    describes a manifest at both gates.
    """
    for source in (state, entry, manifest):
        luid = source.get("workbook_luid") or source.get("source_workbook_luid")
        if isinstance(luid, str) and luid.strip():
            return luid.strip()
    return None


def _reference_states(
    directory: Path, entry: dict[str, Any], manifest: dict[str, Any]
) -> list[Evidence | RejectedEvidence]:
    """Build every state of one `reference/manifest.json` entry."""
    name = str(entry.get("name") or "")
    workbook_sha = manifest.get("source_workbook_sha256")
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
                workbook_luid=_reference_workbook_luid(entry, state, manifest),
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
        payload = json_object(directory / "manifest.json") or {}
        entries = payload.get("dashboards")
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                built.extend(_reference_states(directory, entry, payload))
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
