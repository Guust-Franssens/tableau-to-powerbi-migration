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
from pathlib import PurePosixPath
from typing import Any, Generic, TypeVar

KIND_DASHBOARD = "dashboard"
KIND_WORKSHEET = "worksheet"
KIND_UNKNOWN = "unknown"

#: How a render was tied to a unit's workbook. The first three ADMIT; the rest REFUSE.
WB_SHA = "sha256"
WB_LUID = "luid"
WB_NAME = "name"
WB_STALE = "stale"
WB_FOREIGN = "foreign"
WB_UNKNOWN = "unknown"

#: The axes that identify a workbook to a MACHINE. A record that claims one of these has told us how
#: it must be checked, and a unit that cannot answer on that axis has not established anything - see
#: :meth:`WorkbookIdentity.attribute`. ``WB_NAME`` is deliberately absent: a display name is not
#: unique across projects.
WB_MACHINE_AXES = (WB_SHA, WB_LUID)

#: Routes that admit, strongest first - also the order :meth:`WorkbookIdentity.attribute` consults
#: them.
WB_ROUTES = (WB_SHA, WB_LUID, WB_NAME)

#: Every refusal, so a census can carry them all and a refusal is never silently absent.
WB_REFUSALS = (WB_STALE, WB_FOREIGN, WB_UNKNOWN)

WB_ADMITTING = frozenset(WB_ROUTES)

#: `harvest_estate_assets.py` names every download `<luid>_<sanitized-name><ext>`, which is the only
#: offline route to a workbook LUID that needs no server. Mirrors
#: `stamp_tableau_provenance.HARVEST_STEM_RE`, kept here so both gates share one parser.
HARVEST_STEM_RE = re.compile(
    r"^(?P<luid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_(?P<rest>.+)$"
)

#: Either separator, because a path RECORDED BY ANOTHER HOST is text, not a path. See
#: :func:`persisted_name`.
PATH_SEPARATORS = re.compile(r"[\\/]+")

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
    """The result of one identity lookup. Never an implicit winner, and never a condition.

    An AMBIGUOUS resolution **retains no values at all** - only a count and the contender names.
    Round 5 measured why that is structural rather than cosmetic: renaming a field to ``_matches``
    does not hide it, because a dataclass has a serialisation surface independent of its API, and
    ``astuple(resolution)`` and ``vars(resolution)["_matches"][0][1]`` both handed back a selectable
    winner without invoking a single raising mechanism. There is now nothing to hand back.

    ``_unique`` is populated ONLY when exactly one candidate matched, where it is the legitimate
    answer that :meth:`value` returns anyway.
    """

    identity: ObjectIdentity
    count: int
    contenders: tuple[str, ...]
    _unique: T | None = None

    @classmethod
    def of(cls, identity: ObjectIdentity, matches: list[tuple[Candidate, T]]) -> Resolution[T]:
        """Build a resolution, keeping a value only when it is unambiguously the answer."""
        names = tuple(sorted({name for candidate, _ in matches for name in candidate.names}))
        return cls(
            identity=identity,
            count=len(matches),
            contenders=names,
            _unique=matches[0][1] if len(matches) == 1 else None,
        )

    @property
    def outcome(self) -> str:
        """``ABSENT``, ``UNIQUE`` or ``AMBIGUOUS``. Branch on this - it is the only safe reader."""
        if not self.count:
            return ABSENT
        return UNIQUE if self.count == 1 else AMBIGUOUS

    def value(self) -> T:
        """The single match. Raises :class:`AmbiguousIdentity` for zero or many."""
        if self.outcome != UNIQUE or self._unique is None:
            raise AmbiguousIdentity(f"{self.identity} resolved to {self.count} candidate(s), not one")
        return self._unique

    def contender_names(self) -> tuple[str, ...]:
        """The NAMES that matched - descriptions for a report, never the values themselves."""
        return self.contenders

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
    """Shared storage. Multiplicity survives: entries are appended, never overwritten.

    Two SEPARATE tables, which is not a detail. Round 6 measured what one shared table does: an
    already-lowercase name like ``ops`` has an exact spelling identical to its normalized one, so a
    single record landed twice under the same key and the exact lookup - which runs before any
    de-duplication - returned AMBIGUOUS for one genuinely verified render. That is a false FAIL, and
    a gate that rejects correct evidence gets switched off, which is how a good gate dies.
    """

    _exact: dict[tuple[str, str], list[tuple[Candidate, T]]] = field(default_factory=dict, repr=False)
    _loose: dict[tuple[str, str], list[tuple[Candidate, T]]] = field(default_factory=dict, repr=False)

    def _store_exact(self, key: tuple[str, str], candidate: Candidate, value: T) -> None:
        self._exact.setdefault(key, []).append((candidate, value))

    def _store_loose(self, key: tuple[str, str], candidate: Candidate, value: T) -> None:
        self._loose.setdefault(key, []).append((candidate, value))


@dataclass
class EngineIndex(_Index[T]):
    """Exact keys only, for joins where BOTH sides are engine/source artifacts.

    There is no lossy key table populated here at all, so an engine-to-engine join cannot fall back
    to one even by accident - the structural answer to a defect that moved down one layer per review
    round.
    """

    def add(self, identity: ObjectIdentity, value: T) -> None:
        """Index a value under an exact engine identity."""
        if not isinstance(identity, ObjectIdentity):
            raise IdentityError(f"an EngineIndex is keyed by ObjectIdentity, got {type(identity).__name__}")
        self._store_exact((identity.kind, identity.name), Candidate(names=(identity.name,), kind=identity.kind), value)

    def resolve(self, identity: ObjectIdentity) -> Resolution[T]:
        """Look up an exact identity."""
        if not isinstance(identity, ObjectIdentity):
            raise IdentityError(f"an EngineIndex is keyed by ObjectIdentity, got {type(identity).__name__}")
        return Resolution.of(identity, list(self._exact.get((identity.kind, identity.name), ())))


@dataclass
class CandidateIndex(_Index[T]):
    """External producer names: exact first, then a normalized fallback, ambiguity as a refusal.

    Only external spellings belong here - this repo does not control how a capture manifest writes a
    name. An :class:`ObjectIdentity` is refused BY TYPE, because round 4 measured a normalized index
    accepting one and then uniquely resolving a *different* engine name through the lossy key.
    """

    def add(self, candidate: Candidate, value: T) -> None:
        """Index a value under every spelling the candidate offers, exact and normalized.

        The two spellings go in SEPARATE tables. Writing both into one made an already-normalized
        name self-collide - see :class:`_Index`.
        """
        if isinstance(candidate, ObjectIdentity):
            raise IdentityError(
                "a CandidateIndex normalizes its keys, so it must never hold an ObjectIdentity - use "
                "EngineIndex for a join whose both sides are engine artifacts"
            )
        if not isinstance(candidate, Candidate):
            raise IdentityError(f"a CandidateIndex is keyed by Candidate, got {type(candidate).__name__}")
        for name in candidate.names:
            self._store_exact((candidate.kind, name), candidate, value)
            self._store_loose((candidate.kind, normalize(name)), candidate, value)

    def resolve(self, identity: ObjectIdentity) -> Resolution[T]:
        """Exact match if there is one, else the normalized fallback. Many candidates is AMBIGUOUS.

        The exact table is authoritative and is consulted alone, so an exact hit beats a normalized
        one and cannot be diluted by it. Only an ABSENT exact result reaches the fallback, where
        entries are de-duplicated by value identity - one record offering several spellings that
        normalize alike is still one record, not an ambiguity.
        """
        if not isinstance(identity, ObjectIdentity):
            raise IdentityError(f"a CandidateIndex resolves an ObjectIdentity, got {type(identity).__name__}")
        exact = Resolution.of(identity, list(self._exact.get((identity.kind, identity.name), ())))
        if exact.outcome != ABSENT:
            return exact
        seen: list[tuple[Candidate, T]] = []
        for entry in self._loose.get((identity.kind, normalize(identity.name)), ()):
            if not any(existing[1] is entry[1] for existing in seen):
                seen.append(entry)
        return Resolution.of(identity, seen)


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


# ------------------------------------------------------------------------------------------------
# WORKBOOK identity - which workbook a render belongs to
# ------------------------------------------------------------------------------------------------
#
# Everything above answers "which OBJECT is this a picture of". This half answers the question one
# level up - "whose workbook is it" - and it lives here because BOTH gates ask it and, until issue
# #450, each had invented its own key for it. Measured on the reference estate:
#
# * `check_reference_readiness` joined an artifact stem against a display name, and trusted a LUID
#   ONLY when `origin.match == "sha256"` - a field that answers a revision question. So a workbook
#   proven by LUID whose bytes had since changed lost its identity, `HR_Dashboard` fell back to exact
#   equality against `HR Dashboard`, and 23 correctly-typed renders were discarded: 0 of 7 pages
#   ready. **Fail-closed.**
# * `check_unit` read `record["workbook"]`, a key NO capture producer writes, while the manifest
#   carries `workbook_luid` and `workbook_name`. Every record arrived ownerless - 360 of 360 - and
#   was admitted anyway, so its foreign-workbook guard had never once fired. **Fail-open.**
#
# One of each direction, from one cause: no shared definition of what identifies a workbook. So the
# definition is here, once, and both gates construct their two sides through it.


@dataclass(frozen=True)
class Attribution:
    """Which axis tied a render to a unit, or why it could not be tied. Never a condition.

    ``__bool__`` raises for the same reason :meth:`Resolution.__bool__` does, and here the reason is
    concrete: ``route`` is a non-empty string for ``foreign`` and ``unknown`` too, so ``if
    identity.attribute(record):`` would admit a FOREIGN workbook's render. :attr:`admitted` is the
    only reader that answers the question.

    ``axis`` names the axis that DECIDED - the same value as ``route`` for an admission, and for a
    refusal the axis whose two sides disagreed. It exists so a caller can tell "the LUIDs differ"
    from "the display names differ": a lossy name fallback may legitimately widen the latter and must
    never be allowed to override the former.
    """

    route: str
    detail: str
    axis: str | None = None

    @property
    def admitted(self) -> bool:
        """Whether this render may certify a page in the unit it was attributed against."""
        return self.route in WB_ADMITTING

    def __bool__(self) -> bool:
        raise AmbiguousIdentity(
            f"an Attribution is not a condition - read .admitted; this one is {self.route} ({self.detail})"
        )


@dataclass(frozen=True)
class WorkbookIdentity:
    """Who a Tableau workbook is, on the three axes a producer can actually record.

    * ``sha256`` - the bytes of the source workbook. Strongest: it is revision-bound as well as
      identity-bound, which is why a stale capture cannot hide behind it.
    * ``luid`` - the published workbook's server id. **This is the identity.** Exact, unique across
      projects, and the one thing `capture_tableau_oracle.py` always writes.
    * ``name`` - the display name. **Decoration.** Two projects may hold workbooks with the same
      name, which is the ambiguity `_runs/<NNN>-<slug>/` numbering exists to avoid elsewhere.

    Every axis is optional because every producer records a different subset; an identity carrying
    none of them is *unestablished* and, by :meth:`attribute`, certifies nothing.
    """

    luid: str | None = None
    name: str | None = None
    sha256: str | None = None

    @classmethod
    def of(cls, *, luid: Any = None, name: Any = None, sha256: Any = None) -> WorkbookIdentity:
        """Build from raw manifest values, keeping only non-blank strings.

        A blank or non-string value is *absent*, never an empty-string identity that would compare
        equal to another absent one.
        """
        return cls(luid=_text(luid), name=_text(name), sha256=_text(sha256))

    @property
    def established(self) -> bool:
        """Whether this identity carries anything at all to compare on."""
        return any((self.luid, self.name, self.sha256))

    def describe(self) -> str:
        """A short, reportable rendering - so a refusal can name what it compared."""
        parts = [f"{axis}={value!r}" for axis, value in (("luid", self.luid), ("name", self.name)) if value]
        if self.sha256:
            parts.append(f"sha256={self.sha256[:12]}...")
        return ", ".join(parts) or "no workbook identity"

    def attribute(self, record: WorkbookIdentity) -> Attribution:
        """Tie ``record`` to this unit. **A machine identity the unit cannot answer is ``unknown``.**

        This is the one rule, and it replaced four separate fail-open patches (round-1 review of PR
        #454). Read it as a single sentence:

            *If a record carries a machine identity - a source sha256 or a workbook LUID - that this
            unit cannot establish or compare, the result is ``unknown``. There is no fall-through to a
            weaker axis, and location never substitutes for a failed or unestablished join.*

        Concretely, walking :data:`WB_MACHINE_AXES` strongest-first:

        * the record makes no claim on this axis -> try the next one;
        * the record claims it and the unit cannot answer -> **``unknown``**, stop. ⚠️ This is the
          half that was missing. A real oracle record carries ``workbook_luid`` AND ``workbook_name``,
          so skipping an *unshared* LUID and admitting on an equal display name let a FOREIGN
          workbook certify a page in both gates - measured, `READY 1/1` and `PASS visual+numeric`,
          on a record whose LUID belonged to another workbook entirely;
        * both sides answer -> that axis is **decisive**: equal admits, unequal is ``foreign``, and
          neither is ever re-litigated by a weaker axis.

        Only when the record claims **no** machine axis at all does the display name decide, and then
        exactly: a normalized comparison would attribute one workbook's captures to another whose
        name differs only by case or spacing.

        ⚠️ The rule is deliberately one-directional, and the asymmetry is a known residual: a record
        carrying ONLY a display name is still admitted against a unit that does have a LUID, because
        nothing stronger is on offer from the producer. That route is the weakest one and every
        admission through it is counted separately (``census["name"]``) so a reader can see it.
        """
        for axis in WB_MACHINE_AXES:
            mine, theirs = getattr(self, _FIELD[axis]), getattr(record, _FIELD[axis])
            if theirs is None:
                continue
            if mine is None:
                return Attribution(
                    WB_UNKNOWN,
                    f"the record is identified by {axis} ({record.describe()}) and this unit "
                    f"establishes none, so nothing can be compared - unit {self.describe()}",
                    axis=axis,
                )
            if _axis_equal(axis, mine, theirs):
                return Attribution(axis, f"{axis} matches ({self.describe()})", axis=axis)
            return Attribution(
                WB_FOREIGN,
                f"{axis} differs: unit {self.describe()} vs record {record.describe()}",
                axis=axis,
            )
        if self.name is not None and record.name is not None:
            if _axis_equal(WB_NAME, self.name, record.name):
                return Attribution(WB_NAME, f"name matches ({self.describe()})", axis=WB_NAME)
            return Attribution(
                WB_FOREIGN,
                f"name differs: unit {self.describe()} vs record {record.describe()}",
                axis=WB_NAME,
            )
        return Attribution(
            WB_UNKNOWN,
            f"no shared identity axis: unit {self.describe()} vs record {record.describe()}",
        )


#: Which field each axis reads. A mapping rather than a tuple of triples so the axis names in
#: :data:`WB_MACHINE_AXES` and the fields cannot drift apart.
_FIELD = {WB_SHA: "sha256", WB_LUID: "luid", WB_NAME: "name"}


def _text(value: Any) -> str | None:
    """A non-blank string, or None. Blank is absence, not an identity that compares equal."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _axis_equal(axis: str, mine: str, theirs: str) -> bool:
    """Axis comparison. Machine ids are case-insensitive; a display NAME is compared exactly."""
    return mine == theirs if axis == WB_NAME else mine.casefold() == theirs.casefold()


def harvest_luid(stem: str | None) -> str | None:
    """The LUID prefix of a `harvest_estate_assets.py` filename stem, or None for any other name.

    ``<luid>_<sanitized-name>`` is the harvester's own convention (display names are not unique
    across projects), so the prefix is this repo's own record of which published workbook it
    downloaded - exact, and available with no server and no credentials. A hand-placed or renamed
    workbook simply yields None and keeps the weaker routes, gaining nothing it did not ask for.

    ⚠️ Feed this :func:`persisted_stem`, never ``Path(...).stem``. See that function.
    """
    found = HARVEST_STEM_RE.match(stem or "")
    return found.group("luid") if found else None


def persisted_name(text: str | None) -> str:
    """The final segment of a path RECORDED BY ANOTHER HOST, on either host.

    ⚠️ ``pathlib.Path`` is the *running* host's flavour, and that is a safety bug rather than a
    portability nicety. A real handover slice records
    ``_runs\\407-dryrun-gates\\assets\\<luid>_HR_Dashboard.twbx``; on POSIX those backslashes are
    ordinary filename characters, so ``Path(...).stem`` returns the whole string, `harvest_luid` sees
    no prefix, and the unit ends up with **no LUID** - which is exactly the state that used to let a
    weaker axis admit a foreign record. Measured: the same input yields the LUID under
    ``PureWindowsPath`` and ``None`` under ``PurePosixPath``, so a guard behaved differently in the
    two places we look (a Windows workstation and a Linux CI runner).

    Splitting on BOTH separators is host-independent by construction. It is safe for this data
    because these paths are written by `harvest_estate_assets.safe_component`, which rewrites every
    character outside ``[A-Za-z0-9-_]`` - a literal backslash can never appear inside a name.
    """
    raw = (text or "").strip()
    stripped = PATH_SEPARATORS.sub("/", raw).rstrip("/")
    return stripped.rsplit("/", 1)[-1] if stripped else ""


def persisted_stem(text: str | None) -> str:
    """:func:`persisted_name` without its extension - what :func:`harvest_luid` expects."""
    return PurePosixPath(persisted_name(text)).stem


def agreed_luid(*claims: str | None) -> str | None:
    """The one LUID every non-blank claim agrees on, or None when they disagree.

    Two identities that disagree are LESS evidence than none: a filename prefix that contradicts the
    stamped provenance means one of them is about a different workbook, and picking whichever was
    read first is exactly how a foreign render gets admitted. Disagreement therefore fails closed.
    """
    seen = {claim.strip().casefold() for claim in claims if isinstance(claim, str) and claim.strip()}
    return seen.pop() if len(seen) == 1 else None
