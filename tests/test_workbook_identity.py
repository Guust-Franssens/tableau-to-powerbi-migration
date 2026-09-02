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

import sys
from pathlib import Path

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
    assert (oid.WB_FOREIGN, oid.WB_UNKNOWN) == ("foreign", "unknown")
    assert oid.WB_ROUTES == ("sha256", "luid", "name")
    assert oid.WB_ADMITTING == frozenset({"sha256", "luid", "name"})
    # The two refusals are NOT admitting routes. Stated separately because `WB_ADMITTING` above is
    # the thing under test everywhere else, and a mutation that added "foreign" to it would
    # otherwise only be caught by the equality above.
    assert oid.WB_FOREIGN not in oid.WB_ADMITTING
    assert oid.WB_UNKNOWN not in oid.WB_ADMITTING


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
