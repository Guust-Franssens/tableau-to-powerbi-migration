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

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Generic, TypeVar

KIND_DASHBOARD = "dashboard"
KIND_WORKSHEET = "worksheet"
KIND_UNKNOWN = "unknown"

#: How a render was tied to a unit's workbook. The first three ADMIT; the rest REFUSE.
WB_SHA = "sha256"
WB_LUID = "luid"
WB_NAME = "name"
WB_UNCONFIRMED = "revision-unconfirmed"
WB_STALE = "stale"
WB_FOREIGN = "foreign"
WB_UNKNOWN = "unknown"

#: Two MACHINE axes both answered and CONTRADICTED each other - the record's bytes are this unit's
#: source but its LUID names another workbook, or the reverse. Its own route, not folded into
#: ``WB_FOREIGN``, because it is a different operator action: `foreign` says "this is someone else's
#: capture, ignore it", `conflicting-identity` says "this record's own metadata disagrees with
#: itself, so one of the two claims is wrong and neither can be believed". Round-N review of PR #454
#: measured what folding it into the winning axis costs: a record whose sha256 matched and whose LUID
#: belonged to another workbook reached `READY 1/1` at the entry gate and `PASS visual=1 numeric=1` at
#: the exit gate, because the walk returned on the first axis that agreed and never looked at the one
#: that did not. Conflicting machine identities are LESS evidence than none.
WB_CONFLICT = "conflicting-identity"

#: The axes that identify a workbook to a MACHINE, and the ONLY ones that may certify a page. A
#: record that claims one has told us how it must be checked; a unit that cannot answer it has
#: established nothing.
WB_MACHINE_AXES = (WB_SHA, WB_LUID)

#: Routes that ADMIT, strongest first.
#:
#: ⚠️ ``WB_NAME`` is deliberately NOT here, and that is round-3's correction to the one rule. The rule
#: as first written handled "the record claims a machine identity this unit cannot answer" and left
#: the opposite case credited: a record claiming NO machine identity at all was admitted on an exact
#: display name, which is the case where the name is LEAST trustworthy because nothing corroborates
#: it. Measured by the reviewer::
#:
#:     unit:   luid=A, sha=AA, name=Book
#:     record: name=Book only
#:     -> route=name, admitted -> entry gate READY exit 0; exit gate PASS visual=1 numeric=1
#:
#: A workbook of the same display name in another project is indistinguishable from that record. A
#: name is decoration - two projects may hold one, which is the ambiguity `_runs/<NNN>-<slug>/`
#: numbering exists to avoid - so it may inform DISCOVERY and never certification. Measured cost on
#: the 407 reference estate: ``census["name"] == 0``, i.e. nothing was being admitted this way.
WB_ROUTES = (WB_SHA, WB_LUID)

#: Every refusal, so a census can carry them all and a refusal is never silently absent. ``WB_NAME``
#: is a refusal that is REPORTED rather than discarded: "a name matched, and a name is not identity"
#: is a different operator action from "nothing matched".
WB_REFUSALS = (WB_NAME, WB_UNCONFIRMED, WB_STALE, WB_CONFLICT, WB_FOREIGN, WB_UNKNOWN)

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

#: A `.twbx`/`.tdsx` is a zip; a `.twb`/`.tds` is XML. The magic decides which revision-key algorithm
#: applies, rather than a file extension a caller may not have.
ZIP_MAGIC = b"PK\x03\x04"

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

    def attribute(self, record: WorkbookIdentity) -> Attribution:  # pylint: disable=too-many-return-statements
        """Tie ``record`` to this unit. **A machine identity the unit cannot answer is ``unknown``.**

        ⚠️ ``too-many-return-statements`` is disabled deliberately, for the same reason as
        :func:`revision_key`: all seven returns are *distinct verdicts*, each carrying the ``detail``
        string an operator acts on, and this whole file exists so a caller can tell one refusal from
        another. Funnelling them through shared exits would satisfy the checker by making "which
        guard refused" unreadable in the one place this repo most needs it legible.

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

        Only when the record claims **no** machine axis at all is the display name consulted, and
        then it is reported as :data:`WB_NAME` - a REFUSAL that names what it saw. ⚠️ Round 3: it used
        to be an admission, which is the moved-boundary shape. Machine-bearing records were made
        safe while records carrying only the non-unique decoration stayed credited, and that is the
        case where a name is least trustworthy because nothing corroborates it.

        ⚠️ **Round N: an axis that agrees does not end the walk.** "Decisive" above was read as
        "returns", so the FIRST agreeing axis won and the rest were never consulted - and a record
        whose sha256 matched this unit while its LUID named another workbook was admitted on the
        sha256 and reached `READY 1/1` / `PASS visual=1 numeric=1`. Every machine axis both sides can
        answer is now compared before anything is admitted, and an active CONTRADICTION between two
        of them is :data:`WB_CONFLICT`: a refusal in its own right, never resolved by axis priority.
        Note the asymmetry that :meth:`_contradiction` keeps: an axis the unit *cannot answer* is
        silent, an axis that *disagrees* is fatal, and collapsing the two would either re-open the
        fall-through above or refuse every record whose producer wrote fewer fields than ours.
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
                # ⚠️ `or` is unavailable here: `Attribution.__bool__` RAISES on purpose, so a
                # truthiness shortcut would blow up on exactly the contradictory records this guard
                # exists to catch. Compare against None explicitly.
                conflict = self._contradiction(record, decided=axis)
                if conflict is not None:
                    return conflict
                return Attribution(axis, f"{axis} matches ({self.describe()})", axis=axis)
            return Attribution(
                WB_FOREIGN,
                f"{axis} differs: unit {self.describe()} vs record {record.describe()}",
                axis=axis,
            )
        if self.name is not None and record.name is not None:
            if _axis_equal(WB_NAME, self.name, record.name):
                return Attribution(
                    WB_NAME,
                    f"only a display NAME matches ({self.describe()}), and a name is not identity - "
                    "another project may hold a workbook of the same name, so this certifies nothing",
                    axis=WB_NAME,
                )
            return Attribution(
                WB_FOREIGN,
                f"name differs: unit {self.describe()} vs record {record.describe()}",
                axis=WB_NAME,
            )
        return Attribution(
            WB_UNKNOWN,
            f"no shared identity axis: unit {self.describe()} vs record {record.describe()}",
        )

    def _contradiction(self, record: WorkbookIdentity, *, decided: str) -> Attribution | None:
        """A machine axis OTHER than ``decided`` that both sides answer and that DISAGREES, or None.

        Only a disagreement counts. An axis the record does not claim, or one this unit cannot
        answer, is silent here: those are "less evidence", and the walk in :meth:`attribute` already
        decides what they mean. This function answers only the narrower question the axis-priority
        return used to skip - *does anything the record says contradict the axis that just agreed?*
        """
        for axis in WB_MACHINE_AXES:
            if axis == decided:
                continue
            mine, theirs = getattr(self, _FIELD[axis]), getattr(record, _FIELD[axis])
            if mine is None or theirs is None or _axis_equal(axis, mine, theirs):
                continue
            return Attribution(
                WB_CONFLICT,
                f"the record's {decided} matches this unit but its {axis} does not: unit "
                f"{self.describe()} vs record {record.describe()} - contradictory machine "
                "identities are less evidence than none, so neither claim may certify anything",
                axis=axis,
            )
        return None


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


# ------------------------------------------------------------------------------------------------
# REVISION identity - which BUILD of a workbook, as opposed to which workbook
# ------------------------------------------------------------------------------------------------

#: Content-normalised digest of a Tableau archive: sha256 over the sorted, LENGTH-PREFIXED
#: ``(member name, sha256(normalised member bytes))`` pairs, read entry-by-entry from ``infolist()``.
#: ``v3`` because the method has been corrected twice - see :func:`revision_key`.
REVISION_ALGO_ARCHIVE = "twbx-content-v3"

#: Comment-normalised digest of a flat Tableau XML payload - a `.twb`/`.tds` served whole rather than
#: inside an archive. Same normalisation as an XML member of an archive, because it is the same file.
REVISION_ALGO_XML = "tableau-xml-v1"

#: Raw sha256, for a payload that is neither an archive nor XML. Kept as an explicit, named SHAPE so
#: no payload silently falls through to it - see :func:`revision_key`.
REVISION_ALGO_FLAT = "raw-sha256-v1"

#: Members whose bytes are Tableau XML, and so carry the build-stamp comment normalised below.
XML_MEMBER_SUFFIXES = (".twb", ".tds")

#: ``<!-- build 20263.26.0824.1544 -->`` - the TABLEAU SERVER build that serialised the file, stamped
#: into every `.twb` it hands out. It is not workbook content, and it changes when the SERVER is
#: upgraded. See :func:`_normalise_xml`.
XML_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)

#: What a Tableau XML payload starts with, after any BOM. Cheap and exact enough to pick the shape.
XML_PREFIXES = (b"<?xml", b"<workbook", b"<datasource")


@dataclass(frozen=True)
class RevisionKey:
    """A reproducible digest of WHICH BUILD a Tableau asset is, with the algorithm that made it.

    ⚠️ **Measured 2026-09-03 against the live site, 3 downloads of every item in one run.** A raw
    ``sha256`` of a `.twbx` download is **not** reproducible: Tableau Server repacks the archive per
    request, so the same unchanged workbook hashes differently.

    | over 48 workbooks + all datasources | raw sha256 | content digest |
    |---|---|---|
    | DIFFERS across downloads in one run | **13 of 48** workbooks | **0** |

    Every repacker had identical byte length and an identical content digest, and the population is
    itself unstable - ``World Indicators`` differed in one sample and agreed minutes later - so a raw
    digest does not merely fail for a fixed subset: any ``confirmed`` verdict it produces is luck.
    That measurement is why the revision key exists rather than a bare ``sha256`` comparison, and why
    it is **versioned**: a value computed by a different algorithm must read as *cannot compare*, not
    as *drift*, or every pre-existing capture raises a false alarm.

    ⚠️ ``REVISION_ALGO_FLAT`` is **not a fallback**, and the distinction is the whole safety property.
    A payload that IS an archive but cannot be read yields **no key at all** (see :func:`revision_key`)
    - silently re-hashing those bytes raw is exactly what re-opens the defect. A payload that is not
    an archive is a different SHAPE, whose bytes are already its content: measured, every non-archive
    item on the site returned a stable raw digest across all three downloads.
    """

    algo: str
    value: str

    def as_json(self) -> dict[str, str]:
        """The persisted form. Both halves, always - a value without its algorithm is uncomparable."""
        return {"algo": self.algo, "value": self.value}

    @classmethod
    def from_json(cls, payload: Any) -> RevisionKey | None:
        """Read a persisted key, or None for anything that is not a complete one."""
        if not isinstance(payload, dict):
            return None
        algo, value = _text(payload.get("algo")), _text(payload.get("value"))
        return cls(algo=algo, value=value) if algo and value else None

    def agrees_with(self, other: RevisionKey | None) -> bool | None:
        """``True``/``False`` when the two are comparable, ``None`` when they are NOT.

        ``None`` is the important return. Two keys made by different algorithms say nothing about
        each other, so the answer is *cannot establish* - never *drift*. An old manifest carrying an
        earlier algorithm therefore reads ``unconfirmed``, which is the whole reason the key is
        versioned.
        """
        if other is None or self.algo != other.algo:
            return None
        return self.value.casefold() == other.value.casefold()


def _normalise_xml(data: bytes) -> bytes:
    """Tableau XML with the SERVER's own nonces removed.

    ⚠️ Measured 2026-09-03, and it is why the archive key is ``v2`` and the flat key exists at all.
    Normalising the zip alone was not enough: comparing the 78 harvested assets against the site,
    **28** reported a content difference, and diffing six of them - three inside a `.twbx`, three
    served flat - showed the ENTIRE difference was one line, every time::

        -<!-- build 20263.26.0824.1544 -->
        +<!-- build 20263.26.0828.1352 -->

    That is the Tableau **server** build that serialised the file. It moved because the site was
    upgraded between the harvest and the check, not because any workbook changed - most of those
    files were byte-identical in length. Left in, it produces a false drift alarm for every asset
    harvested before a server upgrade: the same false-alarm-at-scale failure the content key exists
    to remove, one layer further in. With it removed, 28 false ``differs`` became 0.

    Only XML comments are stripped: a comment is not migrated content, and everything that is -
    marks, calculations, connections, parameters - lives in elements and attributes.
    """
    return XML_COMMENT_RE.sub(b"", data)


def _is_xml(payload: bytes) -> bool:
    """Whether a non-archive payload is Tableau XML, so the same normalisation applies."""
    head = payload.lstrip(b"\xef\xbb\xbf").lstrip()[:16]
    return head.startswith(XML_PREFIXES)


def revision_key(payload: bytes) -> RevisionKey | None:  # pylint: disable=too-many-return-statements
    """The reproducible build digest of one Tableau asset, or None when it cannot be computed.

    ⚠️ ``too-many-return-statements`` is disabled ON PURPOSE rather than refactored away. All eight
    returns are distinct verdicts this function exists to make - three payload SHAPES and four named
    REFUSALS, each with its own comment saying what it refuses and why - and this repo's rule is that
    a fail-closed guard must be identifiable, not merely present. Merging them behind shared exit
    points would satisfy the checker by making "which guard refused" unreadable, which is the wrong
    trade in exactly the code where it matters most.

    Three explicit SHAPES, each with its own versioned algorithm, and no silent path between them:

    * an archive -> every member digested, XML members comment-normalised (``twbx-content-v3``);
    * flat Tableau XML -> comment-normalised (``tableau-xml-v1``);
    * anything else -> its raw bytes, named as such (``raw-sha256-v1``).

    ⚠️ **The archive method is corrected, and the corrections are the point.** An earlier version read
    members with ``ZipFile.read(name)``, which resolves a NAME: a zip carrying two entries with the
    same filename would have had the first one read twice, so a change to it alone left the digest
    identical. Entries are now read from :meth:`ZipFile.infolist` through :meth:`ZipFile.open`, which
    addresses the entry itself. Names and digests are LENGTH-PREFIXED before hashing, so no two
    different member lists can serialise to the same byte stream.

    ⚠️ **Duplicate member names, an archive comment and member extra fields are REFUSED, not
    ignored.** Each is a place to hide bytes that the digest would not cover, and refusing yields no
    key at all - which reads as *cannot establish*, never as agreement. Measured on the 407 reference
    estate: 49 of 49 archives carry none of the three, so the strict rule refuses nothing real.

    ⚠️ None also means the bytes look like an archive but will not open. That case is never quietly
    downgraded to a raw hash, because a raw hash of a repacked archive is the unstable value this
    whole type exists to avoid - see :class:`RevisionKey` for the measurement.
    """
    if not payload:
        return None
    if not payload.startswith(ZIP_MAGIC):
        if _is_xml(payload):
            return RevisionKey(algo=REVISION_ALGO_XML, value=hashlib.sha256(_normalise_xml(payload)).hexdigest())
        return RevisionKey(algo=REVISION_ALGO_FLAT, value=hashlib.sha256(payload).hexdigest())
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.comment:
                return None
            entries = archive.infolist()
            if len({info.filename for info in entries}) != len(entries):
                return None
            if any(info.extra for info in entries):
                return None
            members = sorted(
                (info.filename, _normalise_member(info.filename, _read_entry(archive, info))) for info in entries
            )
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return None
    digest = hashlib.sha256()
    for name, data in members:
        encoded = name.encode("utf-8")
        # Length-prefixed, so `("ab", X) ("c", Y)` and `("a", X') ("bc", Y')` cannot serialise alike.
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(hashlib.sha256(data).digest())
    return RevisionKey(algo=REVISION_ALGO_ARCHIVE, value=digest.hexdigest())


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """One member's bytes, addressed by ENTRY rather than by name. See :func:`revision_key`."""
    with archive.open(info) as handle:
        return handle.read()


def _normalise_member(name: str, data: bytes) -> bytes:
    """One archive member's bytes, comment-normalised when it is Tableau XML. See :func:`_normalise_xml`."""
    return _normalise_xml(data) if name.lower().endswith(XML_MEMBER_SUFFIXES) else data


def revision_key_of(path: Path) -> RevisionKey | None:
    """:func:`revision_key` for a file on disk, or None when it cannot be read."""
    try:
        return revision_key(path.read_bytes())
    except OSError:
        return None
