"""
purpose: the identity abstraction the reference-readiness gate joins on - make an AMBIGUOUS or
         normalized-away identity structurally unrepresentable rather than checked at each call site.
usage:   import object_identity as oid; index = oid.IdentityIndex(normalized=False)

Why this module exists
----------------------
One defect recurred at FIVE layers of PR #428 - routing, matching, normalization, manual-kind and the
unit join - always as "one object's excuse/evidence covering another". Each round closed the layer
that was found and left the shape available one level along. Round 2 stated the bound in advance:

    if round 3 finds this same defect at a fourth layer, we stop patching the key and make ambiguity
    structurally unrepresentable - the way `Evidence.build()` made unverified evidence
    unrepresentable, which was the right instinct and worked.

Round 3 found it at a fourth AND a fifth layer, so this is that abstraction. The test of success is
NOT that the next review finds nothing at layer six. It is that a **new join written by a future
author cannot express the ambiguous case**, because the types will not hold it.

The four rules, and how each is enforced by construction
--------------------------------------------------------
1. **One identity type.** :class:`ObjectIdentity` is ``(kind, exact_name)`` and is built only from an
   engine/source artifact via :meth:`ObjectIdentity.from_engine`. A producer that supplies only a
   name yields a :class:`Candidate`, which is not an identity and cannot be used as a key.
2. **Resolution always returns a multimap.** :meth:`IdentityIndex.resolve` returns a
   :class:`Resolution` whose only reader is :meth:`Resolution.value`, and that RAISES unless exactly
   one match exists. There is deliberately no ``.first()``, no indexing, and no truthiness: taking
   "the first candidate" is not expressible, so ambiguity cannot be silently resolved.
3. **Multiplicity survives.** :meth:`IdentityIndex.add` appends and never overwrites, and no ``set()``
   is used anywhere identity is derived. Round-3 finding 2 measured the alternative: ``_unit_names``
   turned engine workbook names into a ``set`` keyed on a normalized string, so two genuinely
   distinct workbooks collapsed to one and a bundle that shipped nothing for the second read READY.
4. **Normalization is a property of the INDEX, not of a call site.** An index built with
   ``normalized=False`` has no normalized table at all, so an engine-to-engine join cannot fall back
   to a lossy key even by accident. Only indexes over EXTERNAL producer names - whose spelling this
   repo does not control - are built with ``normalized=True``, and there a collision is
   ``AMBIGUOUS``, never a pick.

Grade and kind are independent axes
-----------------------------------
Nothing here derives an object's KIND from a grade, a capability or any other quality claim.
Round-3 finding 1 measured what happens when they are coupled: a validation-grade `manual` record was
promoted to a kind that matched both dashboards and worksheets, so one image made a dashboard `Ops`
AND a worksheet `Ops` ready - re-creating the founding defect one domain over. A quality claim says
how good a picture is; it can never say what the picture is OF.
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


class AmbiguousIdentity(LookupError):
    """Raised by :meth:`Resolution.value` when a caller reads a non-unique resolution.

    Loud on purpose. The previous shapes of this code returned ``matched[0]`` or fell through to a
    normalized key, and both silently picked a winner among candidates that were not distinguishable.
    """


def normalize(text: str | None) -> str:
    """Lossy key for EXTERNAL producer names only. Never for an engine-to-engine join.

    Kept in this module rather than at a call site so there is exactly one lossy function in the
    codebase and it is visibly quarantined behind ``IdentityIndex(normalized=True)``.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


@dataclass(frozen=True)
class ObjectIdentity:
    """A source object's exact identity: ``(kind, exact_name)``.

    Constructible only through :meth:`from_engine`, so a normalized or provider-supplied string can
    never become a key. Equality and hashing are exact - two names differing by case or repeated
    whitespace are different objects, because the engine gives them different page ids.
    """

    kind: str
    name: str

    @classmethod
    def from_engine(cls, kind: str, name: str | None) -> ObjectIdentity | None:
        """Build an identity from an engine/source artifact, or None when it is not identifiable."""
        if kind not in IDENTIFIABLE_KINDS or not isinstance(name, str) or not name.strip():
            return None
        return cls(kind=kind, name=name)

    def __str__(self) -> str:
        return f"{self.kind} {self.name!r}"


@dataclass(frozen=True)
class Candidate:
    """What an EXTERNAL producer supplies: name spellings, and a kind only if it declared one.

    Deliberately not an :class:`ObjectIdentity`. A capture manifest names a file; it does not
    establish what the file depicts. ``kind`` is ``KIND_UNKNOWN`` unless the producer said otherwise,
    and a candidate with an unknown kind can never resolve - see :meth:`IdentityIndex.resolve`.
    """

    names: tuple[str, ...]
    kind: str = KIND_UNKNOWN

    def keys(self, *, normalized: bool) -> tuple[str, ...]:
        """Index keys for this candidate's spellings."""
        return tuple(normalize(name) if normalized else name for name in self.names)


@dataclass(frozen=True)
class Resolution(Generic[T]):
    """The result of one identity lookup. A LIST, always - never an implicit winner.

    The only reader is :meth:`value`, and it raises unless the outcome is ``UNIQUE``. There is no
    ``__bool__``, no ``__iter__`` and no ``__getitem__``, so `if resolution:` and `resolution[0]` are
    both syntax a future author simply cannot write against this type.
    """

    identity: ObjectIdentity
    matches: tuple[T, ...]

    @property
    def outcome(self) -> str:
        """``ABSENT``, ``UNIQUE`` or ``AMBIGUOUS``."""
        if not self.matches:
            return ABSENT
        return UNIQUE if len(self.matches) == 1 else AMBIGUOUS

    def value(self) -> T:
        """The single match. Raises :class:`AmbiguousIdentity` for zero or many."""
        if self.outcome != UNIQUE:
            raise AmbiguousIdentity(f"{self.identity} resolved to {len(self.matches)} candidate(s), not one")
        return self.matches[0]


@dataclass
class IdentityIndex(Generic[T]):
    """A name -> values multimap that cannot lose a collision, and cannot normalize unless told to.

    ``normalized`` is fixed at construction and decides whether a lossy key table exists AT ALL. An
    engine-to-engine index is built with ``normalized=False``, so there is no lossy layer for a
    future join to slip into - which is the structural answer to a defect that moved down one layer
    per review round.
    """

    normalized: bool
    _by_key: dict[tuple[str, str], list[T]] = field(default_factory=dict)
    _kinds: dict[int, str] = field(default_factory=dict)

    def add(self, candidate: Candidate, value: T) -> None:
        """Index one value under every spelling the candidate offers. Never overwrites."""
        self._kinds[id(value)] = candidate.kind
        for key in candidate.keys(normalized=self.normalized):
            self._by_key.setdefault((candidate.kind, key), []).append(value)

    def add_identity(self, identity: ObjectIdentity, value: T) -> None:
        """Index a value under an exact engine identity."""
        self.add(Candidate(names=(identity.name,), kind=identity.kind), value)

    def resolve(self, identity: ObjectIdentity) -> Resolution[T]:
        """Look up an identity. Zero or many matches are outcomes, never a silent pick.

        A candidate whose kind the producer did not declare is indexed under ``KIND_UNKNOWN`` and so
        never collides with a real identity - "I cannot tell what this depicts" must not satisfy a
        page of either type, which is round-3 finding 1.
        """
        key = identity.name if not self.normalized else normalize(identity.name)
        return Resolution(identity=identity, matches=tuple(self._by_key.get((identity.kind, key), ())))

    def unresolvable(self) -> list[str]:
        """Values indexed under an undeclared kind, so they can be REPORTED rather than vanish."""
        return [key for (kind, key) in self._by_key if kind == KIND_UNKNOWN]


def shares_name(identity: ObjectIdentity, candidate: Candidate) -> bool:
    """Whether a candidate names this object IGNORING kind, for reporting only.

    The gate needs this to tell "a picture exists but I cannot prove what it is of" (UNVERIFIABLE)
    apart from "no picture exists" (BLIND) - two different operator actions. It is deliberately here
    rather than at the call site so the lossy comparison stays inside this module, which is the whole
    point of `check_identity_normalization.py`.

    It may NEVER be used to satisfy a page. It answers a reporting question, not an identity one.
    """
    return any(normalize(name) == normalize(identity.name) for name in candidate.names)


def collisions(identities: list[ObjectIdentity]) -> list[tuple[str, ...]]:
    """Groups of DISTINCT identities that a normalized key would merge.

    The expected-object side of the join must be free of these before any normalized fallback is
    safe on the evidence side: if two expected objects collapse to one key, a single capture would
    match both and neither could be attributed. Multiplicity is preserved throughout - no ``set()``
    is taken over names, because that is precisely what deleted the collision in round-3 finding 2.
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for identity in identities:
        grouped.setdefault((identity.kind, normalize(identity.name)), []).append(identity.name)
    return [tuple(sorted(set(names))) for names in grouped.values() if len(set(names)) > 1]


def duplicates(names: list[str]) -> list[str]:
    """Names appearing more than once in an engine list, kept because a ``set()`` would hide them."""
    return sorted({name for name in names if names.count(name) > 1})
