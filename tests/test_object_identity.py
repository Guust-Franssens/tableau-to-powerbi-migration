"""Tests for the identity abstraction (issue #421, round 3).

One defect recurred at FIVE layers of PR #428 - routing, matching, normalization, manual-kind and the
unit join - always as "one object's evidence or excuse covering another". Each round closed the layer
that was found and left the shape available one level along, so round 2 set a bound in advance: if it
appeared again, stop patching the key and make ambiguity **structurally unrepresentable**.

scripts/object_identity.py is that abstraction, and these are its tests. They assert the properties
that make a *future* join unable to express the ambiguous case, which is the real measure of success -
not that the next review finds nothing at layer six.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import object_identity as oid  # noqa: E402  # pylint: disable=wrong-import-position


# --------------------------------------------------------------------------------------------
# Round 4: every "cannot" must RAISE. Absence is not prohibition.
#
# The previous version of this file asserted that operations were MISSING - no `__bool__`, no
# `__getitem__` - and review measured every one of them still working, because Python's defaults run
# the other way: a frozen dataclass has a public constructor, an object with no `__bool__` is TRUTHY,
# and a public tuple is indexable. So each test below CALLS the forbidden operation and asserts it
# raises. A test that a method is absent proves nothing; that is a vacuity mode in its own right.
# --------------------------------------------------------------------------------------------


def test_the_public_constructor_refuses_an_unidentifiable_kind() -> None:
    """Measured: `ObjectIdentity(KIND_UNKNOWN, "Ops")` built fine while `from_engine` refused it."""
    with pytest.raises(oid.IdentityError):
        oid.ObjectIdentity(oid.KIND_UNKNOWN, "Ops")
    with pytest.raises(oid.IdentityError):
        oid.ObjectIdentity("something-else", "Ops")


def test_the_public_constructor_refuses_a_blank_name() -> None:
    """A blank or missing name is not an identity either."""
    for blank in ("", "   ", None):
        with pytest.raises(oid.IdentityError):
            oid.ObjectIdentity("worksheet", blank)


def test_from_engine_returns_none_where_the_constructor_raises() -> None:
    """The two doors must agree - that they did not is exactly what round 4 found."""
    assert oid.ObjectIdentity.from_engine("worksheet", "Ops") == oid.ObjectIdentity("worksheet", "Ops")
    assert oid.ObjectIdentity.from_engine(oid.KIND_UNKNOWN, "Ops") is None
    assert oid.ObjectIdentity.from_engine("worksheet", "  ") is None
    assert oid.ObjectIdentity.from_engine("worksheet", None) is None


def ambiguous_resolution() -> oid.Resolution[str]:
    """Two records under one name - the shape every "cannot pick" test needs."""
    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    index.add(oid.Candidate(names=("Ops",), kind="worksheet"), "a")
    index.add(oid.Candidate(names=("Ops",), kind="worksheet"), "b")
    return index.resolve(oid.ObjectIdentity("worksheet", "Ops"))


def test_truth_testing_a_resolution_raises() -> None:
    """Measured: `bool(resolution)` returned True on an AMBIGUOUS two-match resolution.

    Omitting `__bool__` does not prevent truth-testing - it makes the object truthy, so
    `if resolution:` silently read ambiguity as success. It raises now, always.
    """
    resolution = ambiguous_resolution()
    assert resolution.outcome == oid.AMBIGUOUS
    with pytest.raises(oid.AmbiguousIdentity):
        bool(resolution)
    with pytest.raises(oid.AmbiguousIdentity):
        if resolution:  # pragma: no cover - the condition itself is what must raise
            pass


def test_a_unique_resolution_also_refuses_truth_testing() -> None:
    """Always raising is the point: a caller must branch on `.outcome`, never on the object."""
    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    index.add(oid.Candidate(names=("Ops",), kind="worksheet"), "a")
    resolution = index.resolve(oid.ObjectIdentity("worksheet", "Ops"))
    assert resolution.outcome == oid.UNIQUE
    with pytest.raises(oid.AmbiguousIdentity):
        bool(resolution)


def test_reading_an_ambiguous_resolution_raises_rather_than_picking() -> None:
    """alue() is the only reader, and it refuses anything but a single match."""
    resolution = ambiguous_resolution()
    with pytest.raises(oid.AmbiguousIdentity):
        resolution.value()


def test_an_absent_resolution_also_raises_rather_than_returning_none() -> None:
    """Zero and many are both non-unique, and neither may be read as a value."""
    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    resolution = index.resolve(oid.ObjectIdentity("worksheet", "Ops"))
    assert resolution.outcome == oid.ABSENT
    with pytest.raises(oid.AmbiguousIdentity):
        resolution.value()


def test_the_matches_are_not_reachable_as_a_public_collection() -> None:
    """Measured: `resolution.matches[0]` selected a winner from the raw tuple.

    Only descriptions are public now, so there is nothing selectable to index.
    """
    resolution = ambiguous_resolution()
    assert not hasattr(resolution, "matches")
    assert resolution.count == 2
    assert resolution.contender_names() == ("Ops",)
    assert all(isinstance(name, str) for name in resolution.contender_names())


def test_a_candidate_index_refuses_an_object_identity_by_type() -> None:
    """Measured: a normalized index accepted an engine identity and then uniquely resolved a
    DIFFERENT engine name through the lossy key. It is a type error now, not a convention."""
    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    with pytest.raises(oid.IdentityError):
        index.add(oid.ObjectIdentity("worksheet", "Ops  Summary"), "x")
    with pytest.raises(oid.IdentityError):
        index.add("Ops", "x")


def test_an_engine_index_has_no_normalized_layer_to_fall_back_to() -> None:
    """`EngineIndex` has no lossy key table, so an engine join cannot slip into one."""
    index: oid.EngineIndex[str] = oid.EngineIndex()
    index.add(oid.ObjectIdentity("worksheet", "Ops  Summary"), "a")

    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops Summary")).outcome == oid.ABSENT
    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops  Summary")).outcome == oid.UNIQUE


def test_an_engine_index_refuses_a_candidate_by_type() -> None:
    """The type split runs both ways: an exact index will not take an external candidate."""
    index: oid.EngineIndex[str] = oid.EngineIndex()
    with pytest.raises(oid.IdentityError):
        index.add(oid.Candidate(names=("Ops",), kind="worksheet"), "a")
    with pytest.raises(oid.IdentityError):
        index.resolve(oid.Candidate(names=("Ops",), kind="worksheet"))


def test_a_candidate_index_resolves_a_spelling_difference_but_refuses_a_collision() -> None:
    """The external-producer fallback: useful for one candidate, a refusal for two."""
    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    index.add(oid.Candidate(names=("ops summary",), kind="worksheet"), "a")
    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops Summary")).outcome == oid.UNIQUE

    index.add(oid.Candidate(names=("OPS  SUMMARY",), kind="worksheet"), "b")
    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops Summary")).outcome == oid.AMBIGUOUS


def test_an_exact_candidate_match_is_not_double_counted() -> None:
    """A name is indexed exactly AND normalized, so one record must not read as two."""
    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    index.add(oid.Candidate(names=("Ops Summary",), kind="worksheet"), "a")
    resolution = index.resolve(oid.ObjectIdentity("worksheet", "Ops Summary"))
    assert resolution.outcome == oid.UNIQUE
    assert resolution.value() == "a"


def test_a_candidate_with_no_declared_kind_resolves_against_nothing() -> None:
    """ "I cannot tell what this depicts" must not satisfy a page of either type."""
    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    index.add(oid.Candidate(names=("Ops",)), "a")

    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops")).outcome == oid.ABSENT
    assert index.resolve(oid.ObjectIdentity("dashboard", "Ops")).outcome == oid.ABSENT


def test_name_lookalikes_cannot_be_used_to_select_evidence() -> None:
    """Measured: the previous `shares_name()` returned a bool and so worked as a resolution
    predicate, bypassing the boundary without needing a new lossy function at all."""
    key = oid.ObjectIdentity("dashboard", "Ops")
    found = oid.name_lookalikes(key, [oid.Candidate(names=("ops",), kind="worksheet")])

    assert found == [oid.Lookalike(name="ops", kind="worksheet")]
    assert not hasattr(found[0], "value")
    assert set(vars(found[0])) == {"name", "kind"}


def test_collisions_and_duplicates_preserve_multiplicity() -> None:
    """No `set()` where identity is derived - that is what deleted the collision in round 3."""
    merged = oid.collisions(
        [
            oid.ObjectIdentity("worksheet", "Ops  Summary"),
            oid.ObjectIdentity("worksheet", "Ops Summary"),
            oid.ObjectIdentity("dashboard", "Ops Summary"),
        ]
    )
    assert merged == [("Ops  Summary", "Ops Summary")]
    assert oid.duplicates(["WB", "WB", "Other"]) == ["WB"]
    assert oid.duplicates(["WB", "Other"]) == []
