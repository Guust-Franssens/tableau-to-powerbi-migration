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
from dataclasses import astuple, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import object_identity as oid  # noqa: E402  # pylint: disable=wrong-import-position


# Round 4: every "cannot" must RAISE. Absence is not prohibition: a frozen dataclass has a public
# constructor, an object with no `__bool__` is TRUTHY and a public tuple is indexable, so each test
# CALLS the forbidden operation and asserts it raises.


def test_both_doors_refuse_an_unidentifiable_kind_or_a_blank_name() -> None:
    """Measured: `ObjectIdentity(KIND_UNKNOWN, "Ops")` built fine while `from_engine` refused it."""
    for kind, name in ((oid.KIND_UNKNOWN, "Ops"), ("something-else", "Ops"), ("worksheet", ""), ("worksheet", "   ")):
        with pytest.raises(oid.IdentityError):
            oid.ObjectIdentity(kind, name)
    with pytest.raises(oid.IdentityError):
        oid.ObjectIdentity("worksheet", None)
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


def test_a_resolution_is_never_a_condition_and_only_a_unique_one_is_readable() -> None:
    """Measured: `bool(resolution)` returned True on an AMBIGUOUS two-match resolution. It raises
    always, so a caller must branch on `.outcome`; `.value()` refuses zero and many alike."""
    ambiguous = ambiguous_resolution()
    unique_index: oid.CandidateIndex[str] = oid.CandidateIndex()
    unique_index.add(oid.Candidate(names=("Ops",), kind="worksheet"), "a")
    unique = unique_index.resolve(oid.ObjectIdentity("worksheet", "Ops"))
    absent = oid.CandidateIndex().resolve(oid.ObjectIdentity("worksheet", "Ops"))

    assert (ambiguous.outcome, unique.outcome, absent.outcome) == (oid.AMBIGUOUS, oid.UNIQUE, oid.ABSENT)
    for resolution in (ambiguous, unique):
        with pytest.raises(oid.AmbiguousIdentity):
            bool(resolution)
    for resolution in (ambiguous, absent):
        with pytest.raises(oid.AmbiguousIdentity):
            resolution.value()
    assert unique.value() == "a"


def test_an_ambiguous_resolution_hands_out_no_selectable_winner_by_any_route() -> None:
    """Measured: `resolution.matches[0]` selected a winner; after the rename to `_matches`,
    `astuple(resolution)` still returned both values (round 5). An ambiguous resolution retains no
    values, while the unique twin must still carry its answer."""
    resolution = ambiguous_resolution()
    assert not hasattr(resolution, "matches")
    assert resolution.count == 2
    assert resolution.contender_names() == ("Ops",)
    assert astuple(resolution) == (("worksheet", "Ops"), 2, ("Ops",), None)
    state = vars(resolution)
    assert state["_unique"] is None
    assert set(state) == {"identity", "count", "contenders", "_unique"}
    with pytest.raises(TypeError):
        list(resolution)

    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    index.add(oid.Candidate(names=("Ops",), kind="worksheet"), "a")
    assert astuple(index.resolve(oid.ObjectIdentity("worksheet", "Ops")))[-1] == "a"


def test_the_two_indexes_refuse_each_others_key_type() -> None:
    """Measured: a normalized index accepted an engine identity and then uniquely resolved a
    DIFFERENT engine name through the lossy key. The type split runs both ways."""
    candidates: oid.CandidateIndex[str] = oid.CandidateIndex()
    engine: oid.EngineIndex[str] = oid.EngineIndex()
    for bad in (
        lambda: candidates.add(oid.ObjectIdentity("worksheet", "Ops  Summary"), "x"),
        lambda: candidates.add("Ops", "x"),
        lambda: engine.add(oid.Candidate(names=("Ops",), kind="worksheet"), "a"),
        lambda: engine.resolve(oid.Candidate(names=("Ops",), kind="worksheet")),
    ):
        with pytest.raises(oid.IdentityError):
            bad()


def test_an_engine_index_has_no_normalized_layer_to_fall_back_to() -> None:
    """`EngineIndex` has no lossy key table, so an engine join cannot slip into one."""
    index: oid.EngineIndex[str] = oid.EngineIndex()
    index.add(oid.ObjectIdentity("worksheet", "Ops  Summary"), "a")

    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops Summary")).outcome == oid.ABSENT
    assert index.resolve(oid.ObjectIdentity("worksheet", "Ops  Summary")).outcome == oid.UNIQUE


def _resolve(entries: list[tuple[tuple[str, ...], str | None, str]], name: str, kind: str = "worksheet"):
    index: oid.CandidateIndex[str] = oid.CandidateIndex()
    for names, cand_kind, value in entries:
        index.add(oid.Candidate(names=names, kind=cand_kind) if cand_kind else oid.Candidate(names=names), value)
    return index.resolve(oid.ObjectIdentity(kind, name))


def test_a_candidate_index_resolves_one_spelling_and_refuses_a_guess() -> None:
    """The external-producer fallback, with round 6's false FAIL and its discriminating control.

    One record is one record however many alike spellings it offers or its exact and normalized
    keys coincide (`ops`, `main`: most real names), and an exact match is authoritative. But two
    candidates that only normalize to the identity are a guess, and a kindless candidate depicts
    nothing a page of either type may claim.
    """
    one = _resolve([(("ops summary",), "worksheet", "a")], "Ops Summary")
    assert one.outcome == oid.UNIQUE
    two = _resolve([(("ops summary",), "worksheet", "a"), (("OPS  SUMMARY",), "worksheet", "b")], "Ops Summary")
    assert two.outcome == oid.AMBIGUOUS
    exact = _resolve([(("Ops Summary",), "worksheet", "a")], "Ops Summary")
    assert (exact.outcome, exact.value()) == (oid.UNIQUE, "a")
    lower = _resolve([(("ops",), "worksheet", "a")], "ops")
    assert (lower.outcome, lower.count, lower.value()) == (oid.UNIQUE, 1, "a")
    for name in ("ops", "Ops"):
        assert _resolve([(("Ops", "ops"), "worksheet", "a")], name).outcome == oid.UNIQUE
    beats = _resolve([(("ops",), "worksheet", "exact"), (("OPS",), "worksheet", "lookalike")], "ops")
    assert beats.outcome == oid.UNIQUE and beats.value() == "exact"
    guess = _resolve([(("Ops Summary",), "worksheet", "a"), (("ops  summary",), "worksheet", "b")], "OPS SUMMARY")
    assert (guess.outcome, guess.count) == (oid.AMBIGUOUS, 2)
    for kind in ("worksheet", "dashboard"):
        assert _resolve([(("Ops",), None, "a")], "Ops", kind).outcome == oid.ABSENT


def test_name_lookalikes_cannot_be_used_to_select_evidence() -> None:
    """Measured: the previous `shares_name()` returned a bool and so worked as a resolution
    predicate, bypassing the boundary without needing a new lossy function at all."""
    key = oid.ObjectIdentity("dashboard", "Ops")
    found = oid.name_lookalikes(key, [oid.Candidate(names=("ops",), kind="worksheet")])

    assert found == [oid.Lookalike(name="ops", kind="worksheet")]
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


def test_kind_is_part_of_identity_and_replace_revalidates() -> None:
    """Round 5 probed and CLEARED these; pinned so a later change cannot quietly undo them."""
    key = oid.ObjectIdentity("worksheet", "Ops")
    assert key != oid.ObjectIdentity("dashboard", "Ops")
    assert hash(key) != hash(oid.ObjectIdentity("dashboard", "Ops"))
    with pytest.raises(oid.IdentityError):
        replace(key, kind=oid.KIND_UNKNOWN)
