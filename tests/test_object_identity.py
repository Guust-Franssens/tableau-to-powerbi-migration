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


def test_an_identity_cannot_be_built_from_an_unknown_kind() -> None:
    """`KIND_UNKNOWN` is deliberately absent from `IDENTIFIABLE_KINDS`."""
    assert oid.ObjectIdentity.from_engine("worksheet", "Ops") == oid.ObjectIdentity("worksheet", "Ops")
    assert oid.ObjectIdentity.from_engine(oid.KIND_UNKNOWN, "Ops") is None
    assert oid.ObjectIdentity.from_engine("worksheet", "  ") is None
    assert oid.ObjectIdentity.from_engine("worksheet", None) is None


def test_reading_an_ambiguous_resolution_raises_rather_than_picking() -> None:
    """There is no `.first()`, no indexing and no truthiness - a pick is not expressible."""
    key = oid.ObjectIdentity("worksheet", "Ops")
    index: oid.IdentityIndex[str] = oid.IdentityIndex(normalized=False)
    index.add(oid.Candidate(names=("Ops",), kind="worksheet"), "a")
    index.add(oid.Candidate(names=("Ops",), kind="worksheet"), "b")

    resolution = index.resolve(key)
    assert resolution.outcome == oid.AMBIGUOUS
    with pytest.raises(oid.AmbiguousIdentity):
        resolution.value()
    for forbidden in ("__bool__", "__iter__", "__getitem__", "first"):
        assert not hasattr(resolution, forbidden)


def test_an_absent_resolution_also_raises_rather_than_returning_none() -> None:
    """Zero and many are both non-unique, and neither may be read as a value."""
    index: oid.IdentityIndex[str] = oid.IdentityIndex(normalized=False)
    resolution = index.resolve(oid.ObjectIdentity("worksheet", "Ops"))
    assert resolution.outcome == oid.ABSENT
    with pytest.raises(oid.AmbiguousIdentity):
        resolution.value()


def test_an_engine_index_has_no_normalized_layer_to_fall_back_to() -> None:
    """This is the structural answer to a defect that moved down one layer per review round.

    `normalized=False` means the lossy key table does not EXIST, so a future engine-to-engine join
    cannot slip into it even by accident.
    """
    index: oid.IdentityIndex[str] = oid.IdentityIndex(normalized=False)
    index.add(oid.Candidate(names=("Ops  Summary",), kind="worksheet"), "a")

    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops Summary")).outcome == oid.ABSENT
    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops  Summary")).outcome == oid.UNIQUE


def test_a_normalized_index_resolves_a_spelling_difference_but_refuses_a_collision() -> None:
    """The external-producer fallback: useful for one candidate, a refusal for two."""
    index: oid.IdentityIndex[str] = oid.IdentityIndex(normalized=True)
    index.add(oid.Candidate(names=("ops summary",), kind="worksheet"), "a")
    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops Summary")).outcome == oid.UNIQUE

    index.add(oid.Candidate(names=("OPS  SUMMARY",), kind="worksheet"), "b")
    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops Summary")).outcome == oid.AMBIGUOUS


def test_a_candidate_with_no_declared_kind_resolves_against_nothing() -> None:
    """ "I cannot tell what this depicts" must not satisfy a page of either type."""
    index: oid.IdentityIndex[str] = oid.IdentityIndex(normalized=False)
    index.add(oid.Candidate(names=("Ops",)), "a")

    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops")).outcome == oid.ABSENT
    assert index.resolve(oid.ObjectIdentity("dashboard", "Ops")).outcome == oid.ABSENT
    assert index.unresolvable() == ["Ops"]


def test_collisions_and_duplicates_preserve_multiplicity() -> None:
    """No `set()` where identity is derived - that is what deleted the collision in finding 2."""
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
