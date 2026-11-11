"""Tests for the WORKBOOK-identity half of `scripts/object_identity.py` (issue #450).

Everything in `test_object_identity.py` answers "which OBJECT is this a picture of". This file
answers the question one level up - "whose workbook is it" - which is where issue #450 lived, twice:

* `check_reference_readiness` discarded a LUID whenever ``origin.match`` said the bytes had changed,
  then fell back to comparing the artifact stem ``HR_Dashboard`` against the published display name
  ``HR Dashboard``. Measured on the reference estate: 23 correctly-typed, correctly-attributed
  renders, and 0 of 7 pages ready. **Fail-closed.**
* `check_unit` read a manifest key named ``workbook`` that no capture producer writes, so every
  record arrived ownerless - measured 360 of 360 - and was **admitted anyway**. **Fail-open.**

One cause: the two gates had each invented their own key for the same question. The join is now one
function, and these tests pin its two directions independently. A test here that only proves an
admission would be indistinguishable from deleting the guard, so every admission has a discriminating
refusal beside it.
"""

from __future__ import annotations

import hashlib
import io
import sys
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import object_identity as oid  # noqa: E402  # pylint: disable=wrong-import-position

LUID_A = "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"
LUID_B = "007f70ac-bf40-4838-9d73-134d40f504db"


def test_the_workbook_route_vocabulary_is_pinned_to_its_literal_values() -> None:
    """Every other assertion compares the code against its own constants, so redefining
    ``WB_FOREIGN = WB_LUID`` would change both sides; this pin is what makes them mean anything."""
    assert (oid.WB_SHA, oid.WB_LUID, oid.WB_NAME) == ("sha256", "luid", "name")
    assert (oid.WB_STALE, oid.WB_FOREIGN, oid.WB_UNKNOWN) == ("stale", "foreign", "unknown")
    assert oid.WB_UNCONFIRMED == "revision-unconfirmed"
    assert oid.WB_CONFLICT == "conflicting-identity"
    assert oid.LUID_CONTRADICTED == "<contradicted-luid-declarations>"
    assert oid.LUID_CONTRADICTED != oid.agreed_luid(), "a contradiction must not be spelled as absence"
    assert oid.WB_ROUTES == ("sha256", "luid"), "a display NAME is not an admitting route"
    assert oid.WB_REFUSALS == (
        "name",
        "revision-unconfirmed",
        "stale",
        "conflicting-identity",
        "foreign",
        "unknown",
    )
    assert oid.WB_MACHINE_AXES == ("sha256", "luid")
    assert oid.WB_ADMITTING == frozenset({"sha256", "luid"})


# ------------------------------------------------------------------------------------------------
# Attribution: every admission has a discriminating refusal beside it
# ------------------------------------------------------------------------------------------------


def test_workbook_attribution_admits_only_on_an_agreeing_machine_axis() -> None:
    """Each row is a reviewed defect (PR #454) or its positive control.

    * A machine axis the RECORD claims and the unit cannot answer is ``unknown``; an equal display
      name never rescues it (BLOCKER 1/3: a foreign workbook certified a page in both gates).
    * A machine axis the record does NOT claim is silent, so the walk falls through to the next one;
      collapsing "absent" into "contradictory" would refuse almost every real oracle capture.
    * Two machine axes that disagree are ``conflicting-identity``, named, whichever is consulted
      first (finding B: a matching sha admitted a record whose LUID named another workbook).
    * A stale capture of the RIGHT workbook is ``foreign`` on the sha axis.
    * A display name is decoration: it never vetoes a proven identity (``HR_Dashboard`` vs
      ``HR Dashboard`` is the NORMAL harvested state), it is compared exactly (``Ops  Summary`` was
      once attributed to ``Ops Summary``), and a name-only match is reported but refused, with the
      axis named so `check_unit`'s lossy rescue can tell a name refusal from a LUID one.
    * No shared axis, or a unit with no identity, is ``unknown`` and admits nothing (issue #450:
      360 of 360 ownerless records were admitted).
    * A LUID is a machine id and compares case-insensitively.
    """
    W = oid.WorkbookIdentity  # noqa: N806
    sha_a, sha_b = "a" * 64, "b" * 64
    cases = [
        # (unit, record, route, axis or None, admitted, detail fragment or None)
        (W(name="Book"), W(luid=LUID_B, name="Book"), oid.WB_UNKNOWN, oid.WB_LUID, False, None),
        (W(luid=LUID_A, name="Book"), W(sha256="ab" * 32), oid.WB_UNKNOWN, oid.WB_SHA, False, None),
        (W(luid=LUID_A, name="Book", sha256=sha_a), W(name="Book"), oid.WB_NAME, None, False, "not identity"),
        (W(luid=LUID_A, name="Book", sha256=sha_a), W(luid=LUID_A, name="Not Book"), oid.WB_LUID, None, True, None),
        (
            W(luid=LUID_A, name="WB", sha256=sha_a),
            W(luid=LUID_B, name="WB", sha256=sha_a),
            oid.WB_CONFLICT,
            oid.WB_LUID,
            False,
            "contradictory machine identities",
        ),
        (
            W(luid=LUID_A, name="WB", sha256=sha_a),
            W(luid=LUID_A, name="WB", sha256=sha_b),
            oid.WB_FOREIGN,
            oid.WB_SHA,
            False,
            None,
        ),
        (W(luid=LUID_A, name="WB", sha256=sha_a), W(luid=LUID_A, name="WB"), oid.WB_LUID, None, True, None),
        (W(name="WB", sha256=sha_a), W(luid=LUID_B, name="WB"), oid.WB_UNKNOWN, oid.WB_LUID, False, None),
        (
            W(luid=LUID_A, name="HR_Dashboard", sha256=sha_a),
            W(luid=LUID_A, name="HR Dashboard", sha256=sha_a),
            oid.WB_SHA,
            None,
            True,
            None,
        ),
        (W(luid=LUID_A, name="HR_Dashboard"), W(luid=LUID_A, name="HR Dashboard"), oid.WB_LUID, None, True, None),
        (W(luid=LUID_A, name="Sales"), W(luid=LUID_B, name="Sales"), oid.WB_FOREIGN, oid.WB_LUID, False, None),
        (W(name="HR_Dashboard"), W(name="HR Dashboard"), oid.WB_FOREIGN, oid.WB_NAME, False, None),
        (W(name="Ops Summary"), W(name="Ops  Summary"), oid.WB_FOREIGN, None, False, None),
        (W(name="Ops Summary"), W(name="ops summary"), oid.WB_FOREIGN, None, False, None),
        (W(name="Ops Summary"), W(name="Ops Summary"), oid.WB_NAME, None, False, None),
        (W(luid=LUID_A, name="WB"), W(), oid.WB_UNKNOWN, None, False, None),
        (W(), W(luid=LUID_A, name="WB"), oid.WB_UNKNOWN, None, False, None),
        (W(luid=LUID_A.upper()), W(luid=LUID_A), oid.WB_LUID, None, True, None),
    ]
    for unit, record, route, axis, admitted, detail in cases:
        verdict = unit.attribute(record)
        assert (verdict.route, verdict.admitted) == (route, admitted), (unit, record)
        assert axis is None or verdict.axis == axis, (unit, record, verdict.axis)
        assert detail is None or detail in verdict.detail, (unit, record, verdict.detail)


def test_blank_or_non_string_fields_are_absence_not_a_match_or_a_crash() -> None:
    """Two records with `workbook_name: ""` must not attribute to each other on the name axis, and
    manifests are external input: a number or a dict where a name was expected is not identity."""
    unit = oid.WorkbookIdentity.of(luid="   ", name="", sha256=None)
    record = oid.WorkbookIdentity.of(luid=None, name="  ", sha256="")

    assert (unit.luid, unit.name, unit.sha256) == (None, None, None)
    assert unit.established is False
    assert unit.attribute(record).route == oid.WB_UNKNOWN
    identity = oid.WorkbookIdentity.of(luid=17, name={"a": 1}, sha256=["x"])
    assert (identity.luid, identity.name, identity.sha256) == (None, None, None)


def test_an_attribution_is_never_a_condition() -> None:
    """`route` is a non-empty string for a REFUSAL too, so `if unit.attribute(r):` would admit it."""
    verdict = oid.WorkbookIdentity(luid=LUID_A).attribute(oid.WorkbookIdentity(luid=LUID_B))

    with pytest.raises(oid.AmbiguousIdentity):
        bool(verdict)


# ------------------------------------------------------------------------------------------------
# The offline LUID routes
# ------------------------------------------------------------------------------------------------


def test_harvest_luid_reads_the_prefix_and_refuses_every_other_shape() -> None:
    """`harvest_estate_assets.py` names downloads `<luid>_<sanitized-name>`; nothing else counts.

    A hand-placed or renamed workbook must gain nothing it did not ask for - it keeps the weaker
    routes rather than acquiring an identity from a filename that never encoded one.
    """
    assert oid.harvest_luid(f"{LUID_A}_HR_Dashboard") == LUID_A
    assert oid.harvest_luid(f"{LUID_A.upper()}_HR_Dashboard") == LUID_A.upper()
    assert oid.harvest_luid("HR_Dashboard") is None
    assert oid.harvest_luid(f"{LUID_A}") is None  # a bare LUID with no `_<name>` is not the shape
    assert oid.harvest_luid("not-a-luid_HR_Dashboard") is None
    assert oid.harvest_luid(None) is None


# ------------------------------------------------------------------------------------------------
# BLOCKER 4: a path recorded by ANOTHER host is text, not a Path
# ------------------------------------------------------------------------------------------------


def test_a_recorded_path_yields_its_luid_on_either_host() -> None:
    """Kills blocker 4 on ANY host. ``pathlib.Path`` is the RUNNING host's flavour, so a handover
    slice recorded on Windows (``_runs\\407\\assets\\<luid>_HR_Dashboard.twbx``) lost its LUID on
    Linux CI only. ``PurePosixPath`` reproduces the defect here; the shipped parse agrees with
    neither host's accident."""
    for recorded in (
        r"_runs\407-dryrun-gates\assets\adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twbx",
        "_runs/407-dryrun-gates/assets/adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twbx",
        r"D:\build\_runs\407\assets\adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twb",
        "/var/lib/ci/_runs/407/assets/adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twb",
        "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twbx",
    ):
        assert oid.harvest_luid(oid.persisted_stem(recorded)) == LUID_A, recorded
    windows = r"_runs\407\assets\adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twbx"
    assert oid.harvest_luid(PureWindowsPath(windows).stem) == LUID_A
    assert oid.harvest_luid(PurePosixPath(windows).stem) is None, "this is the defect, reproduced"


def test_persisted_name_and_stem_are_separator_agnostic_and_keep_dots_in_names() -> None:
    """The parse is a filename split, not a heuristic: pin the edges it has to get right."""
    assert oid.persisted_name(r"a\b\c.twbx") == "c.twbx"
    assert oid.persisted_name("a/b/c.twbx") == "c.twbx"
    assert oid.persisted_name(r"a/b\c.twbx") == "c.twbx"
    assert oid.persisted_name("c.twbx") == "c.twbx"
    assert oid.persisted_name("a\\b\\") == "b"
    assert oid.persisted_name("") == ""
    assert oid.persisted_name(None) == ""
    assert oid.persisted_stem("Q1.2026 Sales.twbx") == "Q1.2026 Sales"
    assert oid.persisted_stem(r"a\b\c.tar.gz") == "c.tar"


def test_agreed_luid_refuses_two_claims_that_disagree() -> None:
    """Two identities that contradict each other are LESS evidence than none.

    A filename prefix that disagrees with the stamped provenance means one of them is about a
    different workbook, and picking whichever was read first is the coin toss issue #438 named.

    ⚠️ **Round 2 of PR #454 changed what a disagreement RETURNS, and the change is the fix.** It used
    to be ``None``, which is indistinguishable from "nobody declared a LUID" - and absence is
    deliberately SILENT, so both readers went on to admit the record on a weaker axis. A
    contradiction now returns :data:`object_identity.LUID_CONTRADICTED`, which
    :meth:`WorkbookIdentity.attribute` refuses as ``conflicting-identity``. The two are asserted to
    be DIFFERENT values here, because collapsing them again is precisely the fail-open.
    """
    assert oid.agreed_luid(LUID_A, LUID_A) == LUID_A
    assert oid.agreed_luid(LUID_A, None) == LUID_A
    assert oid.agreed_luid(None, LUID_A) == LUID_A
    assert oid.agreed_luid(LUID_A.upper(), LUID_A) == LUID_A  # case is not a disagreement
    assert oid.agreed_luid(LUID_A, LUID_B) == oid.LUID_CONTRADICTED
    assert oid.agreed_luid(LUID_A, LUID_B, LUID_A) == oid.LUID_CONTRADICTED, "one dissenter is enough"
    assert oid.LUID_CONTRADICTED is not None, "absence is silent; a contradiction must not be"
    assert oid.agreed_luid(None, None) is None
    assert oid.agreed_luid("  ", "") is None
    # Absence stays absence however it is spelled, including through the values a JSON reader hands
    # over: a blank LUID must never become a refusal (`WorkbookIdentity.of` normalises the same way).
    assert oid.agreed_luid(" \t ", None, LUID_A) == LUID_A
    assert oid.agreed_luid(0, [], {}, LUID_A) == LUID_A


def test_a_contradicted_luid_is_refused_by_name_even_where_the_sha_would_admit() -> None:
    """Round 2 of PR #454: a side may contradict ITSELF before any comparison happens. Both gates
    took the first non-blank LUID of several scopes, so a contradicting claim was discarded and the
    matching sha256 certified the record. The route is asserted (never ``foreign`` or ``unknown``),
    and the agreeing twins prove this is not "refuse every sha-bearing record"."""
    unit = oid.WorkbookIdentity(luid=LUID_A, name="WB", sha256="a" * 64)

    def record(*claims: str | None, sha256: str | None = "a" * 64) -> oid.WorkbookIdentity:
        return oid.WorkbookIdentity.of(luid=oid.agreed_luid(*claims), sha256=sha256)

    from_record = unit.attribute(record(LUID_A, LUID_B))
    assert (from_record.route, from_record.axis, from_record.admitted) == (oid.WB_CONFLICT, oid.WB_LUID, False)
    assert "the record declares two different workbook LUIDs" in from_record.detail
    from_unit = record(LUID_A, LUID_B).attribute(oid.WorkbookIdentity(luid=LUID_A, sha256="a" * 64))
    assert (from_unit.route, from_unit.admitted) == (oid.WB_CONFLICT, False)
    assert "this unit declares two different workbook LUIDs" in from_unit.detail

    assert unit.attribute(record(LUID_A, LUID_A)).route == oid.WB_SHA
    assert unit.attribute(record(LUID_A.upper(), LUID_A)).route == oid.WB_SHA
    assert unit.attribute(record(None, "  ")).route == oid.WB_SHA
    assert unit.attribute(record(LUID_A, LUID_A, sha256=None)).route == oid.WB_LUID
    assert unit.attribute(record(LUID_A, LUID_B, sha256=None)).route == oid.WB_CONFLICT


# ------------------------------------------------------------------------------------------------
# ROUND 3: the revision key must survive a REPACK, and only a repack
# ------------------------------------------------------------------------------------------------


def _archive(members: list[tuple[str, bytes]], *, date_time: tuple[int, int, int, int, int, int]) -> bytes:
    """A zip written with an explicit member ORDER and mtime - the two things a repack changes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(zipfile.ZipInfo(name, date_time=date_time), data)
    return buffer.getvalue()


MEMBERS = [("Book.twb", b"<workbook><worksheets/></workbook>"), ("Data/Extracts/x.hyper", b"\x00\x01extract-bytes")]
BUILD_STAMP_LOCAL = b"<?xml version='1.0'?>\n<!-- build 20263.26.0824.1544   -->\n<workbook><worksheets/></workbook>"
BUILD_STAMP_REMOTE = b"<?xml version='1.0'?>\n<!-- build 20263.26.0828.1352   -->\n<workbook><worksheets/></workbook>"
EPOCH = (2026, 1, 1, 0, 0, 0)


def test_a_repack_or_a_server_build_stamp_changes_the_raw_digest_but_not_the_revision_key() -> None:
    """MEASURED 2026-09-03 on the live site: the raw digest differed for 27 of 49 archives across
    three downloads (member order and mtimes), and a site upgrade re-stamped
    ``<!-- build ... -->`` in 28 of 78 assets; the content key differed for 0. Each pair is asserted
    to differ RAW first, or the fixture could not detect a raw digest at all."""
    pairs = [
        (
            _archive(MEMBERS, date_time=EPOCH),
            _archive(list(reversed(MEMBERS)), date_time=(2026, 9, 3, 11, 22, 33)),
            oid.REVISION_ALGO_ARCHIVE,
        ),
        (BUILD_STAMP_LOCAL, BUILD_STAMP_REMOTE, oid.REVISION_ALGO_XML),
        (
            _archive([("Book.twb", BUILD_STAMP_LOCAL), MEMBERS[1]], date_time=EPOCH),
            _archive([MEMBERS[1], ("Book.twb", BUILD_STAMP_REMOTE)], date_time=(2026, 9, 3, 11, 22, 33)),
            oid.REVISION_ALGO_ARCHIVE,
        ),
    ]
    for left, right, algo in pairs:
        assert hashlib.sha256(left).hexdigest() != hashlib.sha256(right).hexdigest(), "the fixture is not a repack"
        assert oid.revision_key(left) == oid.revision_key(right)
        assert oid.revision_key(left).algo == algo


def test_genuinely_different_content_still_differs() -> None:
    """Normalisation must not launder a real change. ``Cook.twb`` keeps ``Book.twb``'s sort position,
    so only hashing the member NAME can detect the rename (``Renamed.twb`` would pass by order alone,
    which a mutation probe caught). Only COMMENTS are stripped from XML, never elements."""
    original = _archive(MEMBERS, date_time=EPOCH)
    edited = _archive(
        [("Book.twb", b"<workbook><worksheets><worksheet/></worksheets></workbook>"), MEMBERS[1]], date_time=EPOCH
    )
    renamed = _archive([("Cook.twb", MEMBERS[0][1]), MEMBERS[1]], date_time=EPOCH)
    xml_edit = BUILD_STAMP_LOCAL.replace(b"<worksheets/>", b"<worksheets><worksheet name='New'/></worksheets>")

    assert oid.revision_key(original).agrees_with(oid.revision_key(edited)) is False
    assert oid.revision_key(original).agrees_with(oid.revision_key(renamed)) is False
    assert oid.revision_key(BUILD_STAMP_LOCAL).agrees_with(oid.revision_key(xml_edit)) is False


def test_the_key_algorithm_is_chosen_by_shape_and_an_unreadable_archive_gets_none() -> None:
    """18 of 48 live workbooks download as flat ``.twb`` XML with a stable digest, so they get their
    own algorithm rather than "unconfirmed". Bytes that ARE a zip and will not open must not be
    hashed raw: that is the unstable value this type exists to avoid."""
    xml = oid.revision_key(b"<workbook><worksheets/></workbook>")
    assert xml.algo == oid.REVISION_ALGO_XML
    assert xml.value == hashlib.sha256(b"<workbook><worksheets/></workbook>").hexdigest()
    flat = oid.revision_key(b"\x00\x01not xml and not a zip")
    assert flat.algo == oid.REVISION_ALGO_FLAT
    truncated = _archive(MEMBERS, date_time=EPOCH)[:64]
    assert truncated.startswith(oid.ZIP_MAGIC)
    assert oid.revision_key(truncated) is None
    assert oid.revision_key(b"") is None


def test_a_key_compares_only_within_its_algorithm_and_round_trips_with_it() -> None:
    """An old manifest reads as CANNOT COMPARE (None), never as drift; a value persisted without its
    algorithm is not a key."""
    archive = oid.RevisionKey(algo=oid.REVISION_ALGO_ARCHIVE, value="a" * 64)

    assert archive.agrees_with(oid.RevisionKey(algo=oid.REVISION_ALGO_FLAT, value="a" * 64)) is None
    assert archive.agrees_with(None) is None
    assert archive.agrees_with(oid.RevisionKey(algo=oid.REVISION_ALGO_ARCHIVE, value="a" * 64)) is True
    assert archive.agrees_with(oid.RevisionKey(algo=oid.REVISION_ALGO_ARCHIVE, value="b" * 64)) is False
    assert oid.RevisionKey.from_json(archive.as_json()) == archive
    assert oid.RevisionKey.from_json({"value": "a" * 64}) is None
    assert oid.RevisionKey.from_json({"algo": oid.REVISION_ALGO_ARCHIVE}) is None
    assert oid.RevisionKey.from_json("a" * 64) is None
