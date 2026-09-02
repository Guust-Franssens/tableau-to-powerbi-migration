"""Machine-checked census of every ``_slug()`` call site in ``scripts/check_unit.py``.

Three review rounds running, the finding has been the same shape: *an identity was flattened
somewhere nobody had enumerated*. Round 5 answered it with a prose audit; round 6 adjudicated that
audit entry by entry and found **three** of its "safe" classifications wrong. The lesson was not that
the audit was useless - those sites were invisible before it existed - but that **an enumeration is a
hypothesis until each entry is tested**, and prose cannot be tested by CI.

So this replaces the prose with a gate. It is deliberately scoped to the call sites of ONE function.
A broader rule ("any lossy comparison") produces false positives and gets disabled, which is how the
sibling PR's quarantine rule earned its own narrow scope.

Every ``_slug(...)`` call must be pinned below with a classification, and each classification carries
a mechanical proof where one exists:

``uniqueness-guarded``
    The slug feeds a decision, and the enclosing function refuses when the match is not unique.
    **Proved mechanically**: the function must compare something against the literal ``1``.

``multiplicity-preserving``
    The slug becomes a key whose values keep their multiplicity, so a consumer can still count.
    **Proved mechanically**: the call may not sit inside a set literal or set comprehension - that is
    exactly the collapse that let two artifacts named ``Bo ok`` and ``Bo-ok`` share one key.

``reporting-only``
    The slug only shapes a message about a verdict that has already been reached elsewhere. There is
    no mechanical proof of "does not decide", so this class is held by the pin alone: the exact source
    line is recorded, and any edit to it fails this test and forces re-adjudication.

⚠️ **KNOWN GAP - ALL THREE PROOFS ARE VACUOUS. Issue #439. Read this before trusting a green run.**

The category error is that this file pins **where** ``_slug()`` is called and then claims a property
of **how its result is used**. Those are different questions, and the second does not follow from the
first. Each class was attacked and each was defeated, measured against this exact file:

==============================  =================================================================
class                            what it actually accepts
==============================  =================================================================
``uniqueness-guarded``           **any** comparison against the literal ``1`` anywhere in the
                                 enclosing function - including an unrelated ``if unrelated == 1``
                                 sitting beside a completely unguarded slug comparison.
``multiplicity-preserving``      only a ``_slug()`` call **syntactically inside** a set. It does not
                                 see ``slugs = [_slug(...)]`` followed by ``set(slugs)`` on the next
                                 line, which loses exactly the multiplicity being claimed.
``reporting-only``               nothing at all. There is no consumer guard: the value may be read
                                 by any decision anywhere.
==============================  =================================================================

⚠️ **The consequence that matters, and the reason this warning is not a formality:** a reporting-only
value can be **promoted into a decision without changing a single ``_slug()`` call site**, so every
test in this file still passes. Verified by promoting ``explanations["described"]`` into the omission
disposition logic: no pinned line changed and all eight tests passed. **That transition is the one
this gate was built to catch, and it does not catch it.**

So state the claim accurately: this file gives an **exhaustive, adjudicated enumeration** of the
lossy call sites - which is real, and is what makes a NEW site a CI failure - and it gives **no
guarantee whatever** about how any of those values are consumed. Do not cite a green run here as
evidence that an identity is not being flattened.

Fixing it means following the **value**, not the call site: tying each proof to the specific call
result and its downstream consumers, and replacing the ``reporting-only`` pin with either a consumer
allowlist or a behavioural non-interference test. Whether the third class is provable at all is an
open question that #439 must answer explicitly rather than assume - if following the value is
undecidable for it, the honest outcome is to narrow this file's claim rather than keep a proof that
passes on everything.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "check_unit.py"

GUARDED = "uniqueness-guarded"
MULTIPLICITY = "multiplicity-preserving"
REPORTING = "reporting-only"

# (enclosing function, classification, exact source line, calls on that line, why)
Census = tuple[str, str, str, int, str]

CENSUS: list[Census] = [
    (
        "_unit_workbook_keys",
        MULTIPLICITY,
        "return stems, [_slug(stem) for stem in sorted(stems)]",
        1,
        "one entry per DISTINCT stem so _bindable_workbooks can count; a set hid a Bo ok/Bo-ok collision",
    ),
    (
        "_bindable_workbooks",
        GUARDED,
        "slugs = [_slug(name) for name in names]",
        1,
        "binds only when the key is unique among handover names AND among shipped artifacts",
    ),
    (
        "_collect_drop_rows",
        REPORTING,
        "described.setdefault(_slug(name), []).append(f\"tier={row.get('tier')!r}, type={row.get('visual_type')!r}\")",
        1,
        "'described' never explains a drop; it only supplies text for _why_unexplained",
    ),
    (
        "_why_unexplained",
        REPORTING,
        'described = explanations["described"].get(_slug(page["name"]))',
        1,
        "the page is already unexplained; this only picks which sentence says so",
    ),
    (
        "resolve_exemptions",
        GUARDED,
        "if _slug(item) in {_slug(value) for value in {name, *aliases} if value}",
        2,
        "the slug fallback is applied only when len(matched) == 1",
    ),
    (
        "evidence_for",
        GUARDED,
        'key = _slug(page["name"])',
        1,
        "a normalized match satisfies a page only when unique on BOTH sides",
    ),
    (
        "_resolve_oracle_evidence",
        GUARDED,
        "alike = [name for name in unit_workbooks if _slug(name) == _slug(record.workbook)]",
        2,
        "a record is admitted by slugged workbook only when exactly one unit workbook slugs alike",
    ),
    (
        "_resolve_oracle_evidence",
        MULTIPLICITY,
        "by_normalized.setdefault(_slug(record.name), []).append(record)",
        1,
        "records keep their multiplicity so evidence_for can refuse a non-unique match",
    ),
    (
        "_resolve_oracle_evidence",
        MULTIPLICITY,
        'key = _slug(page["name"])',
        1,
        "counts expected pages per key so evidence_for can refuse an ambiguous expectation",
    ),
]


class _SlugCall:
    def __init__(self, function: str, lineno: int, line: str, in_set: bool) -> None:
        self.function = function
        self.lineno = lineno
        self.line = line
        self.in_set = in_set


def _collect(source: str) -> tuple[list[_SlugCall], dict[str, bool]]:
    """Every ``_slug(...)`` call site, plus whether each enclosing function guards on ``1``."""
    tree = ast.parse(source)
    lines = source.splitlines()
    calls: list[_SlugCall] = []
    guards: dict[str, bool] = {}

    def _guards_on_one(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Compare) and any(isinstance(op, ast.Eq | ast.NotEq) for op in sub.ops):
                for comparator in sub.comparators:
                    if isinstance(comparator, ast.Constant) and comparator.value == 1:
                        return True
        return False

    def _walk(node: ast.AST, function: str | None, set_depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                guards[child.name] = _guards_on_one(child)
                _walk(child, child.name, 0)
                continue
            inner = set_depth + (1 if isinstance(child, ast.Set | ast.SetComp) else 0)
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_slug"
                and function is not None
            ):
                calls.append(_SlugCall(function, child.lineno, lines[child.lineno - 1].strip(), inner > 0))
            _walk(child, function, inner)

    _walk(tree, None, 0)
    return calls, guards


def _normalize(text: str) -> str:
    """Compare on collapsed whitespace so a reflow is not read as a new decision site."""
    return " ".join(text.split())


def _pinned_key(function: str, line: str) -> tuple[str, str]:
    return (function, _normalize(line))


# --- detectors -------------------------------------------------------------------------------
# Pure functions over (calls, guards, census) so each one can be fired directly with a synthetic
# input. A gate whose failure branch is only reachable by editing production code is a gate nobody
# can prove works; these have a positive test AND a negative test apiece.


def unpinned_sites(calls: list[_SlugCall], census: list[Census]) -> list[tuple[str, str]]:
    """Call sites the census does not describe: a NEW or EDITED lossy site."""
    pinned = {_pinned_key(function, line) for function, _kind, line, _count, _why in census}
    return sorted({_pinned_key(call.function, call.line) for call in calls} - pinned)


def stale_entries(calls: list[_SlugCall], census: list[Census]) -> list[tuple[str, str]]:
    """Census rows describing a call site that no longer exists."""
    seen = {_pinned_key(call.function, call.line) for call in calls}
    return sorted({_pinned_key(function, line) for function, _kind, line, _count, _why in census} - seen)


def miscounted_sites(calls: list[_SlugCall], census: list[Census]) -> list[tuple[tuple[str, str], int, int]]:
    """Sites whose pinned call count differs from the source: a second ``_slug`` snuck onto the line."""
    seen = Counter(_pinned_key(call.function, call.line) for call in calls)
    pinned: Counter[tuple[str, str]] = Counter()
    for function, _kind, line, count, _why in census:
        pinned[_pinned_key(function, line)] += count
    return sorted((key, pinned[key], seen[key]) for key in pinned if pinned[key] != seen[key])


def unguarded_claims(guards: dict[str, bool], census: list[Census]) -> list[str]:
    """Functions claimed ``uniqueness-guarded`` that never compare anything against ``1``."""
    return sorted(
        {
            function
            for function, kind, _line, _count, _why in census
            if kind == GUARDED and not guards.get(function, False)
        }
    )


def collapsed_claims(calls: list[_SlugCall], census: list[Census]) -> list[str]:
    """``_slug`` calls inside a set, in a function that claims to preserve multiplicity.

    Scoped to the FUNCTION, not the pinned line, on purpose. Keyed by line, this check was vacuous:
    collapsing the list to a set also rewrites the line, so the mutated site no longer matched the
    pin and the detector went quiet exactly when it should have fired.
    """
    functions = {function for function, kind, _line, _count, _why in census if kind == MULTIPLICITY}
    return sorted(f"{call.function}:{call.lineno}" for call in calls if call.in_set and call.function in functions)


def malformed_entries(census: list[Census]) -> list[str]:
    """Census rows with an unknown classification or no recorded reason."""
    bad = [
        f"{function}: unknown classification {kind!r}"
        for function, kind, _line, _count, _why in census
        if kind not in {GUARDED, MULTIPLICITY, REPORTING}
    ]
    bad.extend(f"{function}: no recorded reason" for function, _kind, _line, _count, why in census if not why.strip())
    return sorted(bad)


# --- the gate --------------------------------------------------------------------------------


def _source() -> tuple[list[_SlugCall], dict[str, bool]]:
    return _collect(SOURCE.read_text(encoding="utf-8"))


def test_every_slug_call_site_is_pinned() -> None:
    """A NEW or EDITED ``_slug`` call site is a CI failure, not a round-7 review finding."""
    calls, _guards = _source()
    assert not unpinned_sites(calls, CENSUS), (
        "unclassified _slug() call site(s) - adjudicate each and add it to CENSUS in "
        f"{Path(__file__).name}: {unpinned_sites(calls, CENSUS)}"
    )


def test_no_census_entry_is_stale() -> None:
    """A row describing a site that no longer exists is a stale hypothesis, not coverage."""
    calls, _guards = _source()
    assert not stale_entries(calls, CENSUS), f"CENSUS rows not present in check_unit.py: {stale_entries(calls, CENSUS)}"


def test_census_call_counts_match_the_source() -> None:
    """A second ``_slug`` added to an already-pinned line must not ride in unadjudicated."""
    calls, _guards = _source()
    assert not miscounted_sites(calls, CENSUS), (
        f"(site, pinned, actual) call-count mismatch: {miscounted_sites(calls, CENSUS)}"
    )


def test_uniqueness_guarded_sites_actually_guard_on_one() -> None:
    """The classification is not taken on trust: the function must refuse a non-unique match."""
    _calls, guards = _source()
    assert not unguarded_claims(guards, CENSUS), (
        f"classified uniqueness-guarded but containing no comparison against 1: {unguarded_claims(guards, CENSUS)}"
    )


def test_multiplicity_preserving_functions_never_slug_into_a_set() -> None:
    """A set is exactly where multiplicity is lost, so it disproves this classification."""
    calls, _guards = _source()
    assert not collapsed_claims(calls, CENSUS), (
        f"claims to preserve multiplicity but slugs into a set: {collapsed_claims(calls, CENSUS)}"
    )


def test_census_rows_are_well_formed() -> None:
    """A typo'd classification must not silently opt a site out of both proofs."""
    assert not malformed_entries(CENSUS), f"malformed CENSUS rows: {malformed_entries(CENSUS)}"


# --- the detectors themselves fire ------------------------------------------------------------
# Each of the six gates above asserts an empty list. An empty list is also what a BROKEN detector
# returns, so each one is fired here against an input that must trip it.

_FIXTURE = """
def _slug(value):
    return value


def keeps_multiplicity(names):
    return [_slug(name) for name in names]


def guarded(names, wanted):
    hits = [name for name in names if _slug(name) == _slug(wanted)]
    return hits[0] if len(hits) == 1 else None


def unguarded(names, wanted):
    return [name for name in names if _slug(name) == wanted]


def collapses(names):
    return {_slug(name) for name in names}
"""


def test_detectors_fire_on_a_synthetic_source() -> None:
    calls, guards = _collect(_FIXTURE)
    assert {call.function for call in calls} == {"keeps_multiplicity", "guarded", "unguarded", "collapses"}

    empty: list[Census] = []
    assert unpinned_sites(calls, empty), "unpinned_sites did not report an uncensused call site"

    ghost: list[Census] = [("nowhere", REPORTING, "x = _slug(y)", 1, "why")]
    assert stale_entries(calls, ghost), "stale_entries did not report a row with no matching site"

    undercount: list[Census] = [
        ("guarded", GUARDED, "hits = [name for name in names if _slug(name) == _slug(wanted)]", 1, "why")
    ]
    assert miscounted_sites(calls, undercount) == [
        (("guarded", "hits = [name for name in names if _slug(name) == _slug(wanted)]"), 1, 2)
    ]

    assert unguarded_claims(guards, [("unguarded", GUARDED, "x", 1, "why")]) == ["unguarded"]
    assert unguarded_claims(guards, [("guarded", GUARDED, "x", 1, "why")]) == []

    fired = collapsed_claims(calls, [("collapses", MULTIPLICITY, "x", 1, "why")])
    assert [entry.split(":")[0] for entry in fired] == ["collapses"]
    assert collapsed_claims(calls, [("keeps_multiplicity", MULTIPLICITY, "x", 1, "why")]) == []

    assert malformed_entries([("f", "made-up", "x", 1, "why")]) == ["f: unknown classification 'made-up'"]
    assert malformed_entries([("f", REPORTING, "x", 1, "  ")]) == ["f: no recorded reason"]
    assert malformed_entries([("f", REPORTING, "x", 1, "why")]) == []


def test_whitespace_reflow_is_not_read_as_a_new_site() -> None:
    """A formatter rewrapping a line must not fail the gate; a semantic edit still must."""
    assert _pinned_key("f", "a  =  _slug( b )") == _pinned_key("f", "a = _slug( b )")
    assert _pinned_key("f", "a = _slug(b)") != _pinned_key("f", "a = _slug(c)")
