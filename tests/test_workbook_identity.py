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
    """Without this the file is vacuous in one direction.

    Every other assertion compares the code's answer against the code's own constant, so redefining
    ``WB_FOREIGN = WB_LUID`` would change BOTH sides and every test would still pass. This is the
    same pin `test_check_reference_readiness.py` takes over the status vocabulary, for the same
    reason.
    """
    assert (oid.WB_SHA, oid.WB_LUID, oid.WB_NAME) == ("sha256", "luid", "name")
    assert (oid.WB_STALE, oid.WB_FOREIGN, oid.WB_UNKNOWN) == ("stale", "foreign", "unknown")
    assert oid.WB_ROUTES == ("sha256", "luid", "name")
    assert oid.WB_REFUSALS == ("stale", "foreign", "unknown")
    assert oid.WB_MACHINE_AXES == ("sha256", "luid")
    assert oid.WB_ADMITTING == frozenset({"sha256", "luid", "name"})
    # Every refusal is NOT an admitting route. Stated separately because `WB_ADMITTING` above is the
    # thing under test everywhere else, and a mutation that added one to it would otherwise only be
    # caught by the equality above.
    assert not oid.WB_ADMITTING & set(oid.WB_REFUSALS)
    # ⚠️ The name axis is deliberately NOT a machine axis: a display name is not unique across
    # projects, so it can never be the thing that makes a record's identity "answerable".
    assert oid.WB_NAME not in oid.WB_MACHINE_AXES


# ------------------------------------------------------------------------------------------------
# THE ONE RULE (round-1 review of PR #454): a machine identity the unit cannot answer is `unknown`
# ------------------------------------------------------------------------------------------------


def test_a_luid_the_unit_cannot_answer_is_unknown_and_the_name_never_rescues_it() -> None:
    """BLOCKER 1: the single highest-value assertion in this file.

    A real oracle record ALWAYS carries ``workbook_luid`` AND ``workbook_name``. Skipping an
    *unshared* LUID - because the unit could not establish one - and then admitting on an equal
    display name let a FOREIGN workbook certify a page in both gates. The record has told us how it
    must be checked; a unit that cannot check it has established nothing.
    """
    unit = oid.WorkbookIdentity(name="Book")
    record = oid.WorkbookIdentity(luid=LUID_B, name="Book")

    verdict = unit.attribute(record)
    assert verdict.route == oid.WB_UNKNOWN
    assert verdict.axis == oid.WB_LUID
    assert verdict.admitted is False


def test_a_sha_the_unit_cannot_answer_is_unknown_too() -> None:
    """BLOCKER 3, at the type level: a `reference/manifest.json` carries `source_workbook_sha256`.

    A unit that cannot hash its own source asset cannot check that claim, so location - the only
    thing left - must not stand in for it.
    """
    unit = oid.WorkbookIdentity(luid=LUID_A, name="Book")
    record = oid.WorkbookIdentity(sha256="ab" * 32)

    verdict = unit.attribute(record)
    assert (verdict.route, verdict.axis) == (oid.WB_UNKNOWN, oid.WB_SHA)


def test_the_unanswerable_rule_does_not_swallow_a_record_that_claims_no_machine_identity() -> None:
    """The twin that stops the rule collapsing into "refuse everything".

    A hand-written or `capture_tableau_reference.py` manifest carries no LUID at all, so the name is
    the only axis on offer and it must still work - it is simply the weakest, and it is counted
    separately wherever a gate reports a census.
    """
    unit = oid.WorkbookIdentity(luid=LUID_A, name="Book", sha256="ab" * 32)
    record = oid.WorkbookIdentity(name="Book")

    assert unit.attribute(record).route == oid.WB_NAME


def test_a_machine_axis_the_record_does_not_claim_falls_through_to_the_next_one() -> None:
    """An oracle record carries a LUID and no sha; that must reach the LUID axis, not stop at sha."""
    unit = oid.WorkbookIdentity(luid=LUID_A, name="Book", sha256="ab" * 32)
    record = oid.WorkbookIdentity(luid=LUID_A, name="Not Book")

    assert unit.attribute(record).route == oid.WB_LUID


# ------------------------------------------------------------------------------------------------
# Fail-open: a foreign workbook's render must never certify this unit's page
# ------------------------------------------------------------------------------------------------


def test_a_matching_luid_admits_and_names_the_luid_axis() -> None:
    """The positive control. Without it, "refuse everything" would pass every test below."""
    unit = oid.WorkbookIdentity(luid=LUID_A, name="HR_Dashboard")
    record = oid.WorkbookIdentity(luid=LUID_A, name="HR Dashboard")

    verdict = unit.attribute(record)
    assert verdict.route == oid.WB_LUID
    assert verdict.admitted is True


def test_a_luid_mismatch_is_foreign_even_when_the_display_names_are_identical() -> None:
    """Kills: falling through to a weaker axis after a stronger one has already answered.

    This is the whole fail-open direction. Two projects may hold workbooks with the SAME display
    name - the ambiguity `_runs/<NNN>-<slug>/` numbering exists to avoid elsewhere in this repo - so
    a name that matches after a LUID that does not is precisely the case where the name must not be
    consulted.
    """
    unit = oid.WorkbookIdentity(luid=LUID_A, name="Sales Dashboard")
    record = oid.WorkbookIdentity(luid=LUID_B, name="Sales Dashboard")

    verdict = unit.attribute(record)
    assert verdict.route == oid.WB_FOREIGN
    assert verdict.axis == oid.WB_LUID
    assert verdict.admitted is False


def test_a_sha_mismatch_is_foreign_even_when_the_luid_matches() -> None:
    """The same rule one axis up: a stale capture of the RIGHT workbook is still refused.

    A capture taken against an older revision does not silently remain valid, because a stale picture
    is worse than a missing one - it looks like evidence.
    """
    unit = oid.WorkbookIdentity(luid=LUID_A, name="WB", sha256="a" * 64)
    record = oid.WorkbookIdentity(luid=LUID_A, name="WB", sha256="b" * 64)

    verdict = unit.attribute(record)
    assert verdict.route == oid.WB_FOREIGN
    assert verdict.axis == oid.WB_SHA


def test_a_name_axis_refusal_is_reported_as_such_so_a_lossy_rescue_can_tell_them_apart() -> None:
    """`check_unit`'s lossy rescue is allowed on the NAME axis only, and it reads ``axis`` to know.

    Without a distinguishable axis the rescue would have to guess, and a rescue that cannot tell "the
    LUIDs differ" from "the display names differ" re-opens the fail-open above through the back door.
    """
    unit = oid.WorkbookIdentity(name="HR_Dashboard")
    record = oid.WorkbookIdentity(name="HR Dashboard")

    verdict = unit.attribute(record)
    assert (verdict.route, verdict.axis) == (oid.WB_FOREIGN, oid.WB_NAME)


# ------------------------------------------------------------------------------------------------
# Fail-closed: an unestablished workbook certifies nothing
# ------------------------------------------------------------------------------------------------


def test_no_shared_axis_is_unknown_and_unknown_never_admits() -> None:
    """Kills issue #450's actual fail-open: an ownerless record defaulting to "belongs to this unit".

    `check_unit._admissible_oracle_records` used to count such a record in ``unattributed`` and then
    fall through to ``admissible`` - and because the field it read was one no producer writes, that
    was EVERY record on a real capture.
    """
    unit = oid.WorkbookIdentity(luid=LUID_A, name="WB")
    record = oid.WorkbookIdentity()

    verdict = unit.attribute(record)
    assert verdict.route == oid.WB_UNKNOWN
    assert verdict.admitted is False


def test_a_unit_with_no_identity_admits_nothing_either() -> None:
    """Symmetric: "I do not know who I am" is not a licence to accept any render."""
    verdict = oid.WorkbookIdentity().attribute(oid.WorkbookIdentity(luid=LUID_A, name="WB"))

    assert verdict.route == oid.WB_UNKNOWN
    assert verdict.admitted is False


def test_blank_fields_are_absence_rather_than_an_empty_string_that_matches_itself() -> None:
    """Kills: two records with `workbook_name: ""` attributing to each other on the name axis."""
    unit = oid.WorkbookIdentity.of(luid="   ", name="", sha256=None)
    record = oid.WorkbookIdentity.of(luid=None, name="  ", sha256="")

    assert (unit.luid, unit.name, unit.sha256) == (None, None, None)
    assert unit.established is False
    assert unit.attribute(record).route == oid.WB_UNKNOWN


def test_a_non_string_manifest_value_is_absence_not_a_crash() -> None:
    """Manifests are external input; a number or a dict where a name was expected is not identity."""
    identity = oid.WorkbookIdentity.of(luid=17, name={"a": 1}, sha256=["x"])

    assert (identity.luid, identity.name, identity.sha256) == (None, None, None)


# ------------------------------------------------------------------------------------------------
# The join is not a condition, and the name axis is not normalized
# ------------------------------------------------------------------------------------------------


def test_an_attribution_is_never_a_condition() -> None:
    """`route` is a non-empty string for a REFUSAL too, so `if unit.attribute(r):` would admit it.

    Absence is not prohibition (the rule this module was built on): omitting ``__bool__`` would make
    the object truthy, which is the opposite of a guard. It raises.
    """
    verdict = oid.WorkbookIdentity(luid=LUID_A).attribute(oid.WorkbookIdentity(luid=LUID_B))

    with pytest.raises(oid.AmbiguousIdentity):
        bool(verdict)
    with pytest.raises(oid.AmbiguousIdentity):
        assert verdict  # the exact expression a call site would write


def test_the_name_axis_is_exact_and_does_not_normalize() -> None:
    """Kills: attributing one workbook's captures to another whose name differs only in spelling.

    `Evidence.is_for` once compared workbook names through the lossy key, so a capture for
    `Ops  Summary` was attributed to a unit named `Ops Summary` - the collapse defect, on WORKBOOK
    identity. If a published name genuinely differs from the local stem, the LUID is the answer.
    """
    unit = oid.WorkbookIdentity(name="Ops Summary")

    assert unit.attribute(oid.WorkbookIdentity(name="Ops  Summary")).route == oid.WB_FOREIGN
    assert unit.attribute(oid.WorkbookIdentity(name="ops summary")).route == oid.WB_FOREIGN
    assert unit.attribute(oid.WorkbookIdentity(name="Ops Summary")).route == oid.WB_NAME


def test_a_luid_is_compared_case_insensitively() -> None:
    """A LUID is a machine id: the REST API and a filename may disagree on case, never on identity."""
    assert oid.WorkbookIdentity(luid=LUID_A.upper()).attribute(oid.WorkbookIdentity(luid=LUID_A)).route == oid.WB_LUID


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


@pytest.mark.parametrize(
    "recorded",
    [
        r"_runs\407-dryrun-gates\assets\adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twbx",
        "_runs/407-dryrun-gates/assets/adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twbx",
        r"D:\build\_runs\407\assets\adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twb",
        "/var/lib/ci/_runs/407/assets/adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twb",
        "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twbx",
    ],
)
def test_a_recorded_path_yields_its_luid_on_either_host(recorded: str) -> None:
    """Kills blocker 4, ON ANY HOST - which is the whole point, since the hosts disagreed.

    ``pathlib.Path`` is the RUNNING host's flavour. A real handover slice records
    ``_runs\\407-...\\assets\\<luid>_HR_Dashboard.twbx``; on POSIX those backslashes are ordinary
    filename characters, so ``Path(...).stem`` returns the whole string, `harvest_luid` sees no
    prefix, and the unit ends up with NO machine identity - on Linux CI only, while a Windows
    workstation reports the guard working. A safety guard that behaves differently in the two places
    we look is the worst possible shape.

    This test needs no POSIX host: it asserts the parse is separator-agnostic, and
    :func:`test_the_recorded_path_parse_does_not_use_the_running_hosts_flavour` proves the same input
    under BOTH flavours explicitly.
    """
    assert oid.harvest_luid(oid.persisted_stem(recorded)) == LUID_A


def test_the_recorded_path_parse_does_not_use_the_running_hosts_flavour() -> None:
    """The controlled experiment behind the parametrised test above, runnable anywhere.

    ``PureWindowsPath`` and ``PurePosixPath`` are pure classes, so both flavours can be exercised on
    one host. The measurement that made this a blocker: the SAME input yields the LUID under one and
    ``None`` under the other, and the shipped code must agree with neither host's accident.
    """
    recorded = r"_runs\407\assets\adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_HR_Dashboard.twbx"

    assert oid.harvest_luid(PureWindowsPath(recorded).stem) == LUID_A
    assert oid.harvest_luid(PurePosixPath(recorded).stem) is None, "this is the defect, reproduced"
    assert oid.harvest_luid(oid.persisted_stem(recorded)) == LUID_A, "the fix must agree with neither"


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
    """
    assert oid.agreed_luid(LUID_A, LUID_A) == LUID_A
    assert oid.agreed_luid(LUID_A, None) == LUID_A
    assert oid.agreed_luid(None, LUID_A) == LUID_A
    assert oid.agreed_luid(LUID_A.upper(), LUID_A) == LUID_A  # case is not a disagreement
    assert oid.agreed_luid(LUID_A, LUID_B) is None
    assert oid.agreed_luid(None, None) is None
    assert oid.agreed_luid("  ", "") is None


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


def test_a_repack_changes_the_raw_digest_and_not_the_revision_key() -> None:
    """THE round-3 test. Kills: reverting the revision key to a raw digest.

    ⚠️ Both halves are asserted, and the first is what makes the second mean anything. Measured
    2026-09-03 against the live site, three downloads of every item in one run: the RAW digest
    differed for **27 of 49 archives** (18 of 48 workbooks, 9 of 19 datasources) while the
    content-normalised key differed for **0 of 67**. This fixture reproduces that mechanism offline -
    same members, different order and mtimes - so a raw digest MUST differ here or the fixture is not
    a repack at all and the test would be vacuous.
    """
    original = _archive(MEMBERS, date_time=(2026, 1, 1, 0, 0, 0))
    repacked = _archive(list(reversed(MEMBERS)), date_time=(2026, 9, 3, 11, 22, 33))

    assert hashlib.sha256(original).hexdigest() != hashlib.sha256(repacked).hexdigest(), (
        "the fixture is not a repack, so this test could not detect a raw digest"
    )
    assert oid.revision_key(original) == oid.revision_key(repacked)
    assert oid.revision_key(original).algo == oid.REVISION_ALGO_ARCHIVE


def test_genuinely_different_content_still_differs() -> None:
    """The negative control: normalisation must not launder a real change into agreement.

    ⚠️ The rename is `Cook.twb`, not `Renamed.twb`, and that is load-bearing. `Renamed.twb` sorts
    AFTER `Data/Extracts/x.hyper` while `Book.twb` sorts before it, so a renamed-member assertion
    built that way passes because the member ORDER changed - it would still pass with the member name
    dropped from the digest entirely. The mutation probe caught exactly that vacuity in an earlier
    draft of this test. `Cook.twb` keeps the sort position, so only hashing the NAME can detect it.
    """
    original = _archive(MEMBERS, date_time=(2026, 1, 1, 0, 0, 0))
    edited = _archive(
        [("Book.twb", b"<workbook><worksheets><worksheet/></worksheets></workbook>"), MEMBERS[1]],
        date_time=(2026, 1, 1, 0, 0, 0),
    )
    renamed = _archive([("Cook.twb", MEMBERS[0][1]), MEMBERS[1]], date_time=(2026, 1, 1, 0, 0, 0))

    assert [name for name, _ in sorted(MEMBERS)] != ["Cook.twb", MEMBERS[1][0]], "sanity: the name changed"
    assert sorted(n for n, _ in MEMBERS).index("Book.twb") == 0, "sanity: the rename keeps sort position"
    assert oid.revision_key(original).agrees_with(oid.revision_key(edited)) is False
    assert oid.revision_key(original).agrees_with(oid.revision_key(renamed)) is False, (
        "a member NAME is part of the content: hashing only the bytes would miss a rename"
    )


def test_a_non_archive_uses_its_own_shape_and_says_so() -> None:
    """⚠️ Contradicts the round-3 brief's "not a readable zip -> unconfirmed", with a measurement.

    18 of 48 workbooks on the live site download as plain `.twb` XML rather than a zip, and ALL 18
    returned a stable raw digest across three downloads - zero counter-examples. Refusing to confirm
    them would make 37% of the estate permanently unconfirmable on the strength of a rule aimed at
    zip repacking, which does not apply to them.

    This is NOT the silent fallback the brief warns about: the algorithm is chosen by SHAPE, recorded
    in the key, and compared only against the same algorithm. The dangerous case - bytes that ARE an
    archive and will not open - is the test below.
    """
    key = oid.revision_key(b"<workbook><worksheets/></workbook>")

    assert key.algo == oid.REVISION_ALGO_XML
    assert key.value == hashlib.sha256(b"<workbook><worksheets/></workbook>").hexdigest(), (
        "with no comment to strip, the XML key is just the sha of the bytes"
    )


def test_an_unreadable_archive_yields_no_key_rather_than_a_raw_one() -> None:
    """The silent fallback the brief names, refused: truncated zip bytes must NOT be hashed raw.

    A raw hash of a repacked archive is the unstable value this type exists to avoid, so producing
    one here would re-open the defect while looking like a fix.
    """
    truncated = _archive(MEMBERS, date_time=(2026, 1, 1, 0, 0, 0))[:64]

    assert truncated.startswith(oid.ZIP_MAGIC)
    assert oid.revision_key(truncated) is None
    assert oid.revision_key(b"") is None


def test_a_key_never_compares_across_algorithms() -> None:
    """Versioning: an old manifest reads as CANNOT COMPARE, never as drift.

    `agrees_with` returns None rather than False, because a false drift alarm on every pre-existing
    capture would be a nastier regression than the gap it closes.
    """
    archive = oid.RevisionKey(algo=oid.REVISION_ALGO_ARCHIVE, value="a" * 64)
    flat = oid.RevisionKey(algo=oid.REVISION_ALGO_FLAT, value="a" * 64)

    assert archive.agrees_with(flat) is None, "same value, different algorithm: not comparable"
    assert archive.agrees_with(None) is None
    assert archive.agrees_with(oid.RevisionKey(algo=oid.REVISION_ALGO_ARCHIVE, value="a" * 64)) is True
    assert archive.agrees_with(oid.RevisionKey(algo=oid.REVISION_ALGO_ARCHIVE, value="b" * 64)) is False


def test_a_key_round_trips_through_json_with_its_algorithm() -> None:
    """A value persisted without its algorithm is uncomparable, so both halves always travel."""
    key = oid.RevisionKey(algo=oid.REVISION_ALGO_ARCHIVE, value="c" * 64)

    assert oid.RevisionKey.from_json(key.as_json()) == key
    assert oid.RevisionKey.from_json({"value": "c" * 64}) is None, "a bare value is not a key"
    assert oid.RevisionKey.from_json({"algo": oid.REVISION_ALGO_ARCHIVE}) is None
    assert oid.RevisionKey.from_json("c" * 64) is None


BUILD_STAMP_LOCAL = b"<?xml version='1.0'?>\n<!-- build 20263.26.0824.1544   -->\n<workbook><worksheets/></workbook>"
BUILD_STAMP_REMOTE = b"<?xml version='1.0'?>\n<!-- build 20263.26.0828.1352   -->\n<workbook><worksheets/></workbook>"


def test_the_server_build_stamp_does_not_count_as_a_changed_workbook() -> None:
    """⚠️ MEASURED, and it is why the archive key is v2 and a flat XML key exists at all.

    Normalising the zip alone was not enough. Re-stamping the 78 harvested assets of the reference
    estate against the live site, **28 reported a content difference**; diffing six of them - three
    inside a `.twbx`, three served flat - showed the ENTIRE difference was one line, every time::

        -<!-- build 20263.26.0824.1544 -->
        +<!-- build 20263.26.0828.1352 -->

    That is the Tableau SERVER build that serialised the file. It moved because the site was upgraded
    between the harvest and the check; most of those files were byte-identical in length. With it
    normalised, 28 false `differs` became 0.
    """
    assert hashlib.sha256(BUILD_STAMP_LOCAL).hexdigest() != hashlib.sha256(BUILD_STAMP_REMOTE).hexdigest(), (
        "the fixture must genuinely differ raw, or this test cannot detect the normalisation"
    )
    assert oid.revision_key(BUILD_STAMP_LOCAL) == oid.revision_key(BUILD_STAMP_REMOTE)
    assert oid.revision_key(BUILD_STAMP_LOCAL).algo == oid.REVISION_ALGO_XML


def test_the_build_stamp_is_normalised_inside_an_archive_too() -> None:
    """The same nonce, the same file, one layer in - a `.twb` is XML whether or not it is zipped."""
    local = _archive([("Book.twb", BUILD_STAMP_LOCAL), MEMBERS[1]], date_time=(2026, 1, 1, 0, 0, 0))
    remote = _archive([MEMBERS[1], ("Book.twb", BUILD_STAMP_REMOTE)], date_time=(2026, 9, 3, 11, 22, 33))

    assert hashlib.sha256(local).hexdigest() != hashlib.sha256(remote).hexdigest()
    assert oid.revision_key(local) == oid.revision_key(remote)


def test_normalisation_does_not_reach_past_comments_into_content() -> None:
    """The negative control for the nonce fix: only COMMENTS are stripped.

    Everything a migration cares about - marks, calculations, connections, parameters - lives in
    elements and attributes, and a change to any of them must still read as a different build.
    """
    edited = BUILD_STAMP_LOCAL.replace(b"<worksheets/>", b"<worksheets><worksheet name='New'/></worksheets>")

    assert oid.revision_key(BUILD_STAMP_LOCAL).agrees_with(oid.revision_key(edited)) is False


def test_a_non_xml_non_archive_payload_keeps_its_raw_bytes_and_a_distinct_algorithm() -> None:
    """The third SHAPE, named rather than fallen into - and never comparable with the other two."""
    key = oid.revision_key(b"\x00\x01not xml and not a zip")

    assert key.algo == oid.REVISION_ALGO_FLAT
    assert key.agrees_with(oid.RevisionKey(algo=oid.REVISION_ALGO_XML, value=key.value)) is None
