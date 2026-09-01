"""
purpose: the identity abstraction every reference-readiness join runs through - make an AMBIGUOUS or
         normalized-away identity structurally unrepresentable, ENFORCED BY RAISING.
usage:   import object_identity as oid; index = oid.EngineIndex()

Why this module exists
----------------------
One defect recurred at FIVE layers of PR #428 - routing, matching, normalization, manual-kind and the
unit join - always as "one object's evidence or excuse covering another". Each round closed the layer
found and left the shape available one level along, so round 2 set a bound: if it appeared again,
stop patching the key and make ambiguity structurally unrepresentable. This is that abstraction.

ABSENCE IS NOT PROHIBITION - the correction that shaped this file
------------------------------------------------------------------
Round 4 measured the first attempt and found that **every operation its contract called impossible
was still available**, because the contract was written as a list of things the code did not define:

* a frozen dataclass has a PUBLIC constructor, so ``ObjectIdentity(KIND_UNKNOWN, "Ops")`` built fine
  even though ``from_engine`` refused it;
* an object with no ``__bool__`` is **truthy**, so ``bool(resolution)`` returned ``True`` on an
  ambiguous resolution - and the test asserting "no ``__bool__`` means no truthiness" was asserting a
  language property that is the exact opposite of Python's default;
* a public tuple is indexable, so ``resolution.matches[0]`` picked a winner;
* a normalized index happily accepted an engine identity, and uniquely resolved a DIFFERENT engine
  name through the lossy key.

So every "cannot" here is a mechanism that **raises**, not a method that is merely missing:

| the claim | the mechanism |
|---|---|
| an unidentifiable object has no identity | :meth:`ObjectIdentity.__post_init__` raises :class:`IdentityError` |
| a resolution is never a condition | :meth:`Resolution.__bool__` **always** raises |
| a pick is not expressible | matches are private; the only value reader raises unless unique |
| an engine identity is never stored lossily | :class:`CandidateIndex` raises if handed an :class:`ObjectIdentity` |
| a lookalike may not select evidence | :func:`name_lookalikes` returns descriptions, never the values |

Every one is mutation-proved by calling the forbidden operation and asserting it raises. A test that
a method is *missing* proves nothing - that is the ninth vacuity mode this repo has recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Generic, TypeVar

KIND_DASHBOARD = "dashboard"
KIND_WORKSHEET = "worksheet"
KIND_UNKNOWN = "unknown"

#: The only kinds an identity may carry. `KIND_UNKNOWN` is deliberately absent: an object whose kind
#: is unknown has no identity, which is the whole point.
IDENTIFIABLE_KINDS = frozenset({KIND_DASHBOARD, KIND_WORKSHEET})

ABSENT = "absent"
UNIQUE = "unique"
AMBIGUOUS = "ambiguous"

T = TypeVar("T")


class IdentityError(TypeError):
    """Raised when something that is not an identity is used as one."""


class AmbiguousIdentity(LookupError):
    """Raised when a caller reads or truth-tests a resolution that is not uniquely resolved.

    Loud on purpose. Earlier shapes returned ``matched[0]`` or fell through to a normalized key, and
    both silently picked a winner among candidates that were not distinguishable.
    """


def normalize(text: str | None) -> str:
    """Lossy key for EXTERNAL producer names only. Never for an engine-to-engine join.

    The single lossy function in this toolkit, quarantined here by
    ``scripts/check_identity_normalization.py`` - which fails the build if anything outside this
    module calls it.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


@dataclass(frozen=True)
class ObjectIdentity:
    """A source object's exact identity: ``(kind, exact_name)``.

    The PUBLIC constructor validates, so there is no way to build an unidentifiable identity - round
    4 measured the alternative, where ``from_engine`` refused ``KIND_UNKNOWN`` while
    ``ObjectIdentity(KIND_UNKNOWN, "Ops")`` sailed straight through it.

    Equality and hashing are exact: two names differing by case or repeated whitespace are different
    objects, because the engine gives them different page ids.
    """

    kind: str
    name: str

    def __post_init__(self) -> None:
        if self.kind not in IDENTIFIABLE_KINDS:
            raise IdentityError(
                f"{self.kind!r} is not an identifiable kind ({sorted(IDENTIFIABLE_KINDS)}) - an object "
                "whose kind is unknown has no identity and must not be used as a key"
            )
        if not isinstance(self.name, str) or not self.name.strip():
            raise IdentityError(f"an identity needs a non-blank exact name, got {self.name!r}")

    @classmethod
    def from_engine(cls, kind: str, name: str | None) -> ObjectIdentity | None:
        """Build from an engine/source artifact, or None when it is not identifiable."""
        try:
            return cls(kind=kind, name=name)  # type: ignore[arg-type]
        except IdentityError:
            return None

    def __str__(self) -> str:
        return f"{self.kind} {self.name!r}"


@dataclass(frozen=True)
class Candidate:
    """What an EXTERNAL producer supplies: name spellings, and a kind only if it declared one.

    Deliberately not an :class:`ObjectIdentity`. A capture manifest names a file; it does not
    establish what the file depicts. ``kind`` is ``KIND_UNKNOWN`` unless the producer said otherwise,
    and such a candidate can never resolve.
    """

    names: tuple[str, ...]
    kind: str = KIND_UNKNOWN


@dataclass(frozen=True)
class Lookalike:
    """A candidate whose NAME resembles an identity. Carries no value, by design.

    :func:`name_lookalikes` returns these so a caller can report "a picture exists but I cannot prove
    what it is of" without being handed anything selectable. Round 4: the previous helper returned a
    bool and so worked perfectly well as a resolution predicate, bypassing the whole boundary.
    """

    name: str
    kind: str


@dataclass(frozen=True)
class Resolution(Generic[T]):
    """The result of one identity lookup. Never an implicit winner, and never a condition."""

    identity: ObjectIdentity
    _matches: tuple[tuple[Candidate, T], ...] = field(repr=False)

    @property
    def outcome(self) -> str:
        """``ABSENT``, ``UNIQUE`` or ``AMBIGUOUS``. Branch on this - it is the only safe reader."""
        if not self._matches:
            return ABSENT
        return UNIQUE if len(self._matches) == 1 else AMBIGUOUS

    @property
    def count(self) -> int:
        """How many candidates matched, for reporting."""
        return len(self._matches)

    def value(self) -> T:
        """The single match. Raises :class:`AmbiguousIdentity` for zero or many."""
        if self.outcome != UNIQUE:
            raise AmbiguousIdentity(f"{self.identity} resolved to {self.count} candidate(s), not one")
        return self._matches[0][1]

    def contender_names(self) -> tuple[str, ...]:
        """The NAMES that matched - descriptions for a report, never the values themselves."""
        return tuple(sorted({name for candidate, _ in self._matches for name in candidate.names}))

    def __bool__(self) -> bool:
        """ALWAYS raises. A resolution is not a condition; branch on :attr:`outcome`.

        Round 4: omitting ``__bool__`` does not prevent truth-testing, it makes the object truthy -
        so ``if resolution:`` silently treated an AMBIGUOUS result as success.
        """
        raise AmbiguousIdentity(
            f"a Resolution is not a condition - branch on .outcome ({ABSENT}/{UNIQUE}/{AMBIGUOUS}); "
            f"this one is {self.outcome}"
        )


@dataclass
class _Index(Generic[T]):
    """Shared storage. Multiplicity survives: entries are appended, never overwritten."""

    _by_key: dict[tuple[str, str], list[tuple[Candidate, T]]] = field(default_factory=dict, repr=False)

    def _store(self, key: tuple[str, str], candidate: Candidate, value: T) -> None:
        self._by_key.setdefault(key, []).append((candidate, value))

    def _resolve(self, identity: ObjectIdentity, key: tuple[str, str]) -> Resolution[T]:
        return Resolution(identity=identity, _matches=tuple(self._by_key.get(key, ())))


@dataclass
class EngineIndex(_Index[T]):
    """Exact keys only, for joins where BOTH sides are engine/source artifacts.

    There is no lossy key table here at all, so an engine-to-engine join cannot fall back to one even
    by accident - the structural answer to a defect that moved down one layer per review round.
    """

    def add(self, identity: ObjectIdentity, value: T) -> None:
        """Index a value under an exact engine identity."""
        if not isinstance(identity, ObjectIdentity):
            raise IdentityError(f"an EngineIndex is keyed by ObjectIdentity, got {type(identity).__name__}")
        self._store((identity.kind, identity.name), Candidate(names=(identity.name,), kind=identity.kind), value)

    def resolve(self, identity: ObjectIdentity) -> Resolution[T]:
        """Look up an exact identity."""
        if not isinstance(identity, ObjectIdentity):
            raise IdentityError(f"an EngineIndex is keyed by ObjectIdentity, got {type(identity).__name__}")
        return self._resolve(identity, (identity.kind, identity.name))


@dataclass
class CandidateIndex(_Index[T]):
    """External producer names: exact first, then a normalized fallback, ambiguity as a refusal.

    Only external spellings belong here - this repo does not control how a capture manifest writes a
    name. An :class:`ObjectIdentity` is refused BY TYPE, because round 4 measured a normalized index
    accepting one and then uniquely resolving a *different* engine name through the lossy key.
    """

    def add(self, candidate: Candidate, value: T) -> None:
        """Index a value under every spelling the candidate offers, exact and normalized."""
        if isinstance(candidate, ObjectIdentity):
            raise IdentityError(
                "a CandidateIndex normalizes its keys, so it must never hold an ObjectIdentity - use "
                "EngineIndex for a join whose both sides are engine artifacts"
            )
        if not isinstance(candidate, Candidate):
            raise IdentityError(f"a CandidateIndex is keyed by Candidate, got {type(candidate).__name__}")
        for name in candidate.names:
            self._store((candidate.kind, name), candidate, value)
            self._store((candidate.kind, normalize(name)), candidate, value)

    def resolve(self, identity: ObjectIdentity) -> Resolution[T]:
        """Exact match if there is one, else the normalized fallback. Many candidates is AMBIGUOUS."""
        if not isinstance(identity, ObjectIdentity):
            raise IdentityError(f"a CandidateIndex resolves an ObjectIdentity, got {type(identity).__name__}")
        exact = self._resolve(identity, (identity.kind, identity.name))
        if exact.outcome != ABSENT:
            return exact
        loose = self._resolve(identity, (identity.kind, normalize(identity.name)))
        # An exact key and its normalized twin both index the same entry, so a single record can be
        # stored twice under one lookup. De-duplicate by value identity before judging multiplicity,
        # or every unambiguous match would read as AMBIGUOUS.
        seen: list[tuple[Candidate, T]] = []
        for entry in loose._matches:  # pylint: disable=protected-access
            if not any(existing[1] is entry[1] for existing in seen):
                seen.append(entry)
        return Resolution(identity=identity, _matches=tuple(seen))


def name_lookalikes(identity: ObjectIdentity, candidates: list[Candidate]) -> list[Lookalike]:
    """Candidates whose NAME resembles this identity, as descriptions that carry no value.

    For reporting only: it separates "a picture exists but I cannot prove what it is of"
    (UNVERIFIABLE) from "no picture exists" (BLIND), which are different operator actions. Returning
    :class:`Lookalike` rather than the matched objects is what stops it being used as a resolution
    predicate - round 4 found the previous bool-returning helper doing exactly that, bypassing the
    boundary without needing a new lossy function at all.
    """
    return [
        Lookalike(name=name, kind=candidate.kind)
        for candidate in candidates
        for name in candidate.names
        if normalize(name) == normalize(identity.name)
    ]


def collisions(identities: list[ObjectIdentity]) -> list[tuple[str, ...]]:
    """Groups of DISTINCT identities that a normalized key would merge.

    The expected-object side must be free of these before any normalized fallback is safe on the
    evidence side: if two expected objects collapse to one key, a single capture would match both and
    neither could be attributed. Multiplicity is preserved - no ``set()`` over names, which is
    precisely what deleted a workbook collision in round 3.
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for identity in identities:
        grouped.setdefault((identity.kind, normalize(identity.name)), []).append(identity.name)
    return [tuple(sorted(set(names))) for names in grouped.values() if len(set(names)) > 1]


def duplicates(names: list[str]) -> list[str]:
    """Names appearing more than once in an engine list, kept because a ``set()`` would hide them."""
    return sorted({name for name in names if names.count(name) > 1})
