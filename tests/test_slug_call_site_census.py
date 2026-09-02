"""What the lossy key `check_unit._slug()` is allowed to decide - and how each claim is PROVED.

Round 6 answered "an identity was flattened somewhere nobody had enumerated" with a census of
`_slug()` **call sites**. Round 7 proved that census vacuous in all three of its classes, and named
the reason in one sentence worth keeping:

    *a census that pins WHERE a function is called cannot prove HOW its result is used.*

Measured against the round-6 gate: an unrelated ``if unrelated == 1`` satisfied "uniqueness-guarded";
``slugs = [_slug(...)]`` followed by ``set(slugs)`` one line later was invisible to
"multiplicity-preserving"; and a reporting-only value could be **promoted into a decision without
touching a single pinned line**, which is exactly the transition the gate existed to catch.

Following a value through arbitrary Python is undecidable, so this file no longer tries. The
guarantee moved into the production code - ``check_unit.NormalizedIndex`` - and what remains here is
three claims, each with a proof that can fail:

* **Every lossy DECISION goes through ``NormalizedIndex``.** AST census of every ``_slug()`` call
  site; anything outside that class and outside the reporting allowlist fails.
* **``NormalizedIndex`` cannot answer without a cardinality check.** Behavioural (refuses 0 *and*
  refuses >1) plus a public-surface pin, because absence of a leak is not prohibition of one.
* **The allowlisted sites do not decide.** Behavioural non-interference: blank the reporting map and
  every verdict must be identical while the explanatory text is observably different.

⚠️ **What is NOT proved, stated plainly.** The third claim is established by non-interference over
the scenarios exercised below, not by a general argument - "this value never influences any verdict
on any input" is the undecidable question itself. A promotion that only fires on an input shape
absent from that matrix would pass. That is a narrower claim than round 6 made, and it is the honest
one: the matrix IS the coverage, so widening the matrix is how the claim gets stronger.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "scripts" / "check_unit.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_unit as cu  # noqa: E402  # pylint: disable=wrong-import-position

#: The class every lossy DECISION must go through.
INDEX_CLASS = "NormalizedIndex"

#: Functions allowed to call ``_slug()`` outside the index. Membership is not a licence: each is
#: bound by the behavioural non-interference test below, which is what makes this allowlist a
#: checkable claim rather than a promise.
REPORTING_ONLY = {
    "_collect_drop_rows": "keys the 'described' text map, which explains an omission but never settles one",
    "_why_unexplained": "chooses which sentence describes an omission already classified elsewhere",
}


class SlugCall:
    """One ``_slug()`` call site and the scope chain enclosing it."""

    def __init__(self, scope: tuple[str, ...], lineno: int) -> None:
        self.scope = scope
        self.lineno = lineno

    @property
    def enclosing(self) -> str:
        """Innermost enclosing class or function name."""
        return self.scope[-1] if self.scope else "<module>"

    @property
    def inside_index(self) -> bool:
        """Whether this call is lexically inside :data:`INDEX_CLASS`."""
        return INDEX_CLASS in self.scope

    def __repr__(self) -> str:
        return f"{'.'.join(self.scope)}:{self.lineno}"


def collect_slug_calls(source: str) -> list[SlugCall]:
    """Every ``_slug(...)`` call site in ``source``, with its enclosing class/function chain."""
    calls: list[SlugCall] = []
    scope: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            named = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            if named:
                scope.append(child.name)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "_slug":
                calls.append(SlugCall(tuple(scope), child.lineno))
            walk(child)
            if named:
                scope.pop()

    walk(ast.parse(source))
    return calls


def unguarded_call_sites(calls: list[SlugCall]) -> list[SlugCall]:
    """Call sites that are neither inside the index nor in the reporting allowlist."""
    return [call for call in calls if not call.inside_index and call.enclosing not in REPORTING_ONLY]


# --- claim 1: every lossy decision goes through the index ---------------------------------------


def test_every_slug_call_site_is_inside_the_index_or_allowlisted() -> None:
    """A new lossy call site is a CI failure, not a round-8 discovery."""
    calls = collect_slug_calls(SOURCE.read_text(encoding="utf-8"))
    assert calls, "found no _slug() call sites at all - the collector is broken, not the source"
    offenders = unguarded_call_sites(calls)
    assert not offenders, (
        f"_slug() called outside {INDEX_CLASS} and outside the reporting allowlist: {offenders}. "
        "Route the decision through NormalizedIndex, or add the function to REPORTING_ONLY and "
        "extend the non-interference matrix so the claim is actually proved."
    )


def test_the_collector_finds_calls_outside_the_index() -> None:
    """The detector fires: a gate that can only ever return an empty list proves nothing."""
    fixture = """
class NormalizedIndex:
    def add(self, name):
        return _slug(name)


def elsewhere(name):
    return _slug(name)
"""
    calls = collect_slug_calls(fixture)
    assert [call.enclosing for call in calls] == ["add", "elsewhere"]
    assert [call.inside_index for call in calls] == [True, False]
    assert [call.enclosing for call in unguarded_call_sites(calls)] == ["elsewhere"]


def test_the_allowlist_is_current_and_documented() -> None:
    """An allowlisted function that no longer calls ``_slug()`` is a stale licence."""
    names = {call.enclosing for call in collect_slug_calls(SOURCE.read_text(encoding="utf-8"))}
    stale = sorted(set(REPORTING_ONLY) - names)
    assert not stale, f"REPORTING_ONLY names functions that no longer call _slug(): {stale}"
    assert all(reason.strip() for reason in REPORTING_ONLY.values())


# --- claim 2: the index cannot answer without a cardinality check -------------------------------


def test_the_index_refuses_zero_and_refuses_many() -> None:
    """``unique`` is the only accessor, and it refuses BOTH failure directions."""
    index: cu.NormalizedIndex[str] = cu.NormalizedIndex()
    assert index.unique("absent") is None

    index.add("A-B", "first")
    assert index.unique("A B") == "first"
    assert index.count("a/b") == 1

    index.add("A B", "second")
    assert index.unique("A-B") is None, "a key answered by two candidates must refuse, not pick one"
    assert index.count("A-B") == 2


def test_add_spelling_does_not_inflate_a_single_candidate() -> None:
    """Aliases of ONE finding are one candidate; two different findings sharing a key are two."""
    index: cu.NormalizedIndex[str] = cu.NormalizedIndex()
    index.add_spelling("A-B", "finding")
    index.add_spelling("A B", "finding")
    assert index.unique("a b") == "finding"
    assert index.count("a b") == 1

    index.add_spelling("A_B", "other")
    assert index.unique("a b") is None
    assert index.count("a b") == 2


def test_the_index_exposes_no_way_to_read_a_bucket() -> None:
    """Absence of a leak is not prohibition of one, so the public surface is pinned.

    ``unique`` refusing is worthless if a caller can reach the list and take ``[0]`` itself. The
    bucket dict is name-mangled and this pins the surface, so adding an accessor becomes a
    deliberate, visible act rather than a quiet one.
    """
    index: cu.NormalizedIndex[str] = cu.NormalizedIndex()
    assert sorted(name for name in dir(index) if not name.startswith("_")) == [
        "add",
        "add_spelling",
        "count",
        "unique",
    ]
    index.add("A", "value")
    # Python has no true private, so the claim is precise: the store is NAME-MANGLED and there is no
    # public or single-underscore alias for it. Reaching `_NormalizedIndex__buckets` is possible and
    # is meant to be - it is loud enough to be caught in review, which `_buckets` would not be.
    assert hasattr(index, "_NormalizedIndex__buckets"), "the candidate store is no longer name-mangled"
    leaks = [
        name
        for name in dir(index)
        if not name.startswith("_NormalizedIndex__")
        and not name.startswith("__")
        and isinstance(getattr(index, name, None), dict | list)
    ]
    assert not leaks, f"public/single-underscore attributes expose the candidate store: {leaks}"


# --- claim 3: the allowlisted sites do not decide (behavioural non-interference) -----------------


def _write_spec(unit: Path, dashboards: list[str]) -> None:
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "migration-spec.json").write_text(
        json.dumps(
            {
                "migration_spec_version": "1.0",
                "dashboards": [{"id": f"dash.{i}", "name": name} for i, name in enumerate(dashboards)],
                "worksheets": [],
            }
        ),
        encoding="utf-8",
    )


def _write_report(unit: Path, pages: list[str]) -> None:
    root = unit / "fabric" / "Book.Report" / "definition" / "pages"
    root.mkdir(parents=True, exist_ok=True)
    order = []
    for index, name in enumerate(pages):
        page_id = f"p{index + 1}"
        order.append(page_id)
        page = root / page_id
        (page / "visuals" / "v0").mkdir(parents=True, exist_ok=True)
        (page / "page.json").write_text(json.dumps({"name": page_id, "displayName": name}), encoding="utf-8")
        (page / "visuals" / "v0" / "visual.json").write_text(json.dumps({"name": "v0"}), encoding="utf-8")
    (root / "pages.json").write_text(json.dumps({"pageOrder": order}), encoding="utf-8")


def _write_handover(unit: Path, rows: list[dict[str, object]]) -> None:
    handover = unit / "handover"
    handover.mkdir(parents=True, exist_ok=True)
    (handover / "Book.json").write_text(
        json.dumps({"estate": {"tool": "test"}, "workbook": {"name": "Book", "viz_fidelity": rows}}),
        encoding="utf-8",
    )


def _scenarios(root: Path) -> list[Path]:
    """Four units spanning every state the reporting map can be in.

    The first is the load-bearing one: a handover that MENTIONS the missing page without proving
    anything about it is what populates ``described``. A matrix of units with no handover would leave
    the map empty and make the non-interference assertion vacuously true - vacuity mode 2.
    """
    degraded = root / "degraded"
    _write_spec(degraded, ["Kept", "Gone"])
    _write_report(degraded, ["Kept"])
    _write_handover(degraded, [{"worksheet": "Gone", "tier": "degraded", "visual_type": "line", "status": "warned"}])

    declared = root / "declared"
    _write_spec(declared, ["Kept", "Gone"])
    _write_report(declared, ["Kept"])
    _write_handover(
        declared, [{"worksheet": "Gone", "tier": "empty", "visual_type": "unsupported", "status": "warned"}]
    )

    bare = root / "bare"
    _write_spec(bare, ["Kept", "Gone"])
    _write_report(bare, ["Kept"])

    clean = root / "clean"
    _write_spec(clean, ["Kept"])
    _write_report(clean, ["Kept"])

    return [degraded, declared, bare, clean]


def _verdict(unit: Path) -> dict[str, Any]:
    """Only the fields that constitute a VERDICT - deliberately not the explanatory text."""
    parity = cu.check_page_parity(unit, cu.load_exemptions(unit))
    oracle = cu.check_oracle_coverage(unit, None, None)
    return {
        "parity_status": parity["status"],
        "dispositions": sorted(row["disposition"] for row in parity.get("omissions", [])),
        "unsigned": len(parity.get("unsigned_omissions", [])),
        "oracle_status": oracle["status"],
        "oracle_pages": oracle.get("pages"),
    }


def _explanatory_text(unit: Path) -> list[str]:
    parity = cu.check_page_parity(unit, cu.load_exemptions(unit))
    return [str(row.get("why")) for row in parity.get("omissions", [])]


def test_reporting_only_evidence_does_not_change_any_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank the ``described`` map: every verdict must be identical, and the TEXT must differ.

    This is what makes ``REPORTING_ONLY`` a claim rather than a label. Round 6's gate accepted a
    reporting-only value being promoted into a decision, because the promotion touched no pinned
    ``_slug`` line; a promotion cannot survive this test, because the value it would now depend on
    is gone.
    """
    units = _scenarios(tmp_path)
    baseline = [_verdict(unit) for unit in units]
    text_before = [_explanatory_text(unit) for unit in units]

    described = cu.page_drop_explanations(units[0])["described"]
    assert described, "fixture does not populate the reporting map; the assertion below would be vacuous"

    real = cu.page_drop_explanations

    def blanked(target: Path) -> dict[str, Any]:
        out = real(target)
        out["described"] = {}
        return out

    monkeypatch.setattr(cu, "page_drop_explanations", blanked)

    after = [_verdict(unit) for unit in units]
    text_after = [_explanatory_text(unit) for unit in units]

    changed = [(before, now) for before, now in zip(baseline, after, strict=True) if before != now]
    assert not changed, (
        "blanking the reporting-only map changed a VERDICT, so that value is a decision input and "
        f"must not be allowlisted: {changed}"
    )
    assert text_before[0] != text_after[0], (
        "blanking the reporting map changed no explanatory text either, so this test did not "
        "actually remove the value it claims to have removed"
    )
