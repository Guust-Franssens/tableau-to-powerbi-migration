"""Mutation harness for the reference-readiness ENTRY gate (issue #421).

    python tests/mutation_reference_readiness.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest, exactly as
``tests/mutation_harness.py`` does. It **imports** that harness's scoring machinery rather than
extending its ``MUTATIONS`` dict, for two reasons: the scoring is the load-bearing part and must not
be forked, and ``mutation_harness.py`` is a shared file another change may be editing.

Scoring is therefore the shared, lifecycle-record-based verdict (``observed_mutation`` /
``session_is_trustworthy``), never terminal-text scraping. The harness's own docstring records why:
its first run reported 22/22 caught because a plugin *import* error exits non-zero before any test
runs, and a naive verdict scored that as a detection.

What this file adds is an EXPECTATION per mutation, so it is a gate rather than a report. Three
verdicts are expected and all three are meaningful:

* ``CAUGHT``   -- a fail-closed property the suite must defend.
* ``SURVIVED`` -- the discriminating **control**. A cosmetic change MUST survive; if it were caught,
  the suite is asserting on incidental wording and its detections prove less than they appear.
* ``INVALID``  -- the **absent-anchor** control. A mutation naming something that does not exist must
  be reported as invalid, never as caught. This is the exact false-green the shared harness guards.

⚠️ Round-1 review of PR #428 found two mutations that left all 31 tests passing, and both were
fixture blind spots rather than code defects: ``GRADE_ORACLE = GRADE_VALIDATION`` survived because the
literal pin omitted ``GRADE_ORACLE`` and the only oracle assertion compared against that same mutable
constant, and a flat-``pbip_warnings[]`` fallback survived because the routing fixture never supplied
``pbip_warnings``. Both are now mutations here, and both fixtures were repaired.

Exit 0 only when every mutation matched its expectation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_harness import (  # noqa: E402  # pylint: disable=wrong-import-position
    PY,
    ROOT,
    observed_mutation,
    run,
    sanitized_env,
    session_ended_abnormally,
    session_is_trustworthy,
)

TARGET = "tests/test_check_reference_readiness.py"

CAUGHT = "CAUGHT"
SURVIVED = "SURVIVED"
INVALID = "INVALID"

# name -> (expected verdict, patch injected as a pytest plugin at interpreter start)
MUTATIONS: dict[str, tuple[str, str]] = {
    # --- the exit contract itself (round-1 finding 1) --------------------------------------
    "reinstate-warn-only": (
        CAUGHT,
        """
import argparse
import check_reference_readiness as crr
# Put the flag back exactly as it was: parsed, and returning EXIT_OK before FINDINGS or
# CANNOT_ESTABLISH are mapped. Measured on real bundles, this returned 0 while the gate's own
# output said "CANNOT_ESTABLISH is NOT a pass".
_orig = crr.main
def main(argv=None):
    argv = [a for a in (argv or []) if a != "--warn-only"]
    _orig(argv)
    return crr.EXIT_OK
crr.main = main
""",
    ),
    "cannot-establish-exits-zero": (
        CAUGHT,
        """
import check_reference_readiness as crr
crr.EXIT_CANNOT_ESTABLISH = crr.EXIT_OK
""",
    ),
    "unverifiable-is-treated-as-ready": (
        CAUGHT,
        """
import check_reference_readiness as crr
crr.UNVERIFIABLE = crr.READY
""",
    ),
    "oracle-grade-equals-validation-grade": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Round-1 finding 8a: this SURVIVED the whole suite, because the literal pin omitted GRADE_ORACLE
# and the only oracle assertion compared against the same mutable constant.
crr.GRADE_ORACLE = crr.GRADE_VALIDATION
""",
    ),
    # --- NOT_APPLICABLE must be earned (round-1 finding 2) ----------------------------------
    "a-vanished-report-is-not-applicable": (
        CAUGHT,
        """
import check_reference_readiness as crr
# The measured fail-open: a workbook the engine lists, with NO shipping report, granted a clean
# exit because some semantic model existed somewhere.
crr._units_without_reports = lambda engine_report, reports: []
_orig = crr._empty_bundle_unit
def _empty_bundle_unit(root, engine_report):
    return crr.UnitResult(unit=root.name, status=crr.STATUS_NOT_APPLICABLE, detail="models only")
crr._empty_bundle_unit = _empty_bundle_unit
""",
    ),
    "no-pages-found-means-not-applicable": (
        CAUGHT,
        """
import check_reference_readiness as crr
_orig = crr.assess_unit
def assess_unit(root, report_dir, *args, **kwargs):
    pages, _ = crr.page_map(report_dir)
    if not pages:
        return crr.UnitResult(unit=report_dir.name[: -len(".Report")],
                              status=crr.STATUS_NOT_APPLICABLE, detail="no pages found")
    return _orig(root, report_dir, *args, **kwargs)
crr.assess_unit = assess_unit
""",
    ),
    # --- evidence must be USABLE (round-1 finding 3) ----------------------------------------
    "existence-is-evidence": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Restore `Path.is_file()` as the whole validity check: a zero-byte or truncated render counts.
crr.render_facts = lambda path: (None, None) if path.is_file() else "missing"
""",
    ),
    "empty-capabilities-grade-as-unknown-and-still-count": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Round-1 finding 3b: `capabilities: []` reached `ready [unknown]`.
crr.reference_grade = lambda capabilities: (
    crr.GRADE_VALIDATION if isinstance(capabilities, list) and crr.CAP_VALIDATION in capabilities
    else "/".join(sorted(str(c) for c in capabilities)) if isinstance(capabilities, list) and capabilities
    else "layout_grade"
)
""",
    ),
    "the-legibility-floor-is-removed": (
        CAUGHT,
        """
import check_reference_readiness as crr
crr.MIN_RENDER_EDGE = 0
""",
    ),
    # --- evidence must be ATTRIBUTABLE (round-1 finding 4) -----------------------------------
    "evidence-matches-any-workbook": (
        CAUGHT,
        """
import check_reference_readiness as crr
# The measured shape: one synthetic record made two DIFFERENT units report 2/2 READY.
crr.Evidence.is_for = lambda self, unit: True
""",
    ),
    "missing-workbook-identity-is-accepted": (
        CAUGHT,
        """
import check_reference_readiness as crr
_orig = crr.Evidence.build.__func__
def build(cls, **kwargs):
    kwargs["workbook_key"] = kwargs.get("workbook_key") or "any"
    return _orig(cls, **kwargs)
crr.Evidence.build = classmethod(build)
""",
    ),
    # --- completeness needs a readable, unique mapping (round-1 finding 6) --------------------
    "unreadable-page-json-falls-back-to-the-directory-name": (
        CAUGHT,
        """
import check_reference_readiness as crr
def page_map(report_dir):
    root = report_dir / "definition" / "pages"
    found = {}
    if root.is_dir():
        for page_json in sorted(root.rglob("page.json")):
            payload = crr._json_object(page_json) or {}
            found[str(payload.get("name") or page_json.parent.name)] = str(payload.get("displayName") or "")
    return found, []
crr.page_map = page_map
""",
    ),
    "colliding-page-ids-are-not-detected": (
        CAUGHT,
        """
import check_reference_readiness as crr
_orig = crr._expectation
def _expectation(unit, report_dir, source):
    objects = crr.source_objects(source)
    return objects if objects else _orig(unit, report_dir, source)
crr._expectation = _expectation
""",
    ),
    # --- scope and drop-explanation joins ------------------------------------------------------
    "worksheet-render-satisfies-dashboard-page": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Restore the pre-#421 behaviour: match on NAME alone, discarding scope. This is what
# `check_unit.py:265`'s `_slug` join does today.
def match_evidence(obj, evidence):
    named = [e for e in evidence if crr._norm(e.name) == crr._norm(obj.name)]
    return (named[0], []) if named else (None, [])
crr.match_evidence = match_evidence
""",
    ),
    "unknown-scope-counts-as-a-match": (
        CAUGHT,
        """
import check_reference_readiness as crr
def match_evidence(obj, evidence):
    named = [e for e in evidence if crr._norm(e.name) == crr._norm(obj.name)]
    ok = [e for e in named if e.kind in (obj.kind, crr.KIND_UNKNOWN)]
    return (ok[0], []) if ok else (None, named)
crr.match_evidence = match_evidence
""",
    ),
    "drop-explanations-key-on-name-alone": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Round-1 finding 5: a WORKSHEET warning for `Ops` excused a genuinely missing DASHBOARD `Ops`.
_orig = crr.drop_explanations
def drop_explanations(handover):
    byname = {}
    for (kind, name), reason in _orig(handover).items():
        byname[name] = reason
    return {(k, name): reason for name, reason in byname.items()
            for k in (crr.KIND_DASHBOARD, crr.KIND_WORKSHEET)}
crr.drop_explanations = drop_explanations
""",
    ),
    "flat-pbip-warnings-explain-any-drop": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Round-1 finding 8b: this SURVIVED, because the routing fixture supplied no `pbip_warnings` at all.
_orig = crr.drop_explanations
def drop_explanations(handover):
    explained = dict(_orig(handover))
    workbook = (handover or {}).get("workbook") or {}
    flat = [w for w in (workbook.get("pbip_warnings") or [])
            if isinstance(w, str) and any(m in w for m in crr.DELIBERATE_DROP_MARKERS)]
    if flat:
        explained.setdefault("__any__", flat[0])
    return _Any(explained, flat[0] if flat else None)
class _Any(dict):
    def __init__(self, base, fallback):
        super().__init__(base)
        self._fallback = fallback
    def __contains__(self, key):
        return dict.__contains__(self, key) or self._fallback is not None
    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return self._fallback
crr.drop_explanations = drop_explanations
""",
    ),
    "every-dropped-page-is-a-finding": (
        CAUGHT,
        """
import check_reference_readiness as crr
# The cry-wolf direction: no drop is ever accounted for, so a CORRECT bundle reports findings.
crr.DELIBERATE_DROP_MARKERS = ()
""",
    ),
    # --- expectation must not be circular -------------------------------------------------------
    "expectation-falls-back-to-the-pages-that-were-built": (
        CAUGHT,
        """
import check_reference_readiness as crr
_orig = crr.assess_unit
def assess_unit(root, report_dir, engine_report, evidence, explicit_source, require_validation_grade):
    result = _orig(root, report_dir, engine_report, evidence, explicit_source, require_validation_grade)
    if result.status == crr.STATUS_CANNOT_ESTABLISH:
        pages, _ = crr.page_map(report_dir)
        result.status = crr.STATUS_READY
        result.pages = [
            {"source_object": pid, "source_type": crr.KIND_UNKNOWN, "page_id": pid,
             "page_status": crr.PAGE_EMITTED, "grade": crr.GRADE_UNKNOWN, "matched_by": "self",
             "evidence": "present", "readiness": crr.READY}
            for pid in pages
        ]
    return result
crr.assess_unit = assess_unit
""",
    ),
    "orphan-worksheets-are-not-expected": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Reinstate `check_unit.expected_pages`'s "dashboards only, never worksheets" docstring rule.
_orig = crr.source_objects
def source_objects(path):
    objects = _orig(path)
    return None if objects is None else [o for o in objects if o.kind == crr.KIND_DASHBOARD]
crr.source_objects = source_objects
""",
    ),
    "page-id-drops-the-scope-prefix": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Collapse the cryptographic identity: a dashboard and its same-named principal worksheet land on
# the SAME page id again.
crr.SourceObject.page_id = property(lambda self: crr.engine_page_id(self.name))
""",
    ),
    # --- grade bar (round-1 finding 7) -----------------------------------------------------------
    "require-validation-grade-does-not-change-page-readiness": (
        CAUGHT,
        """
import check_reference_readiness as crr
_orig = crr._page_row
def _page_row(obj, page_status, evidence, reason, require_validation_grade):
    return _orig(obj, page_status, evidence, reason, False)
crr._page_row = _page_row
""",
    ),
    "one-validation-grade-page-silences-the-ceiling": (
        CAUGHT,
        """
import check_reference_readiness as crr
_orig = crr._merge
def _merge(root, units, evidence, rejected):
    report = _orig(root, units, evidence, rejected)
    report["all_evidence_validation_grade"] = crr.GRADE_VALIDATION in report["grades_present"]
    return report
crr._merge = _merge
""",
    ),
    # --- discriminating controls -------------------------------------------------------------
    "control-cosmetic-reword-of-a-rendered-line": (
        SURVIVED,
        """
import check_reference_readiness as crr
# Pure presentation. If this is CAUGHT, the suite is asserting on incidental wording.
_orig = crr._render_page
crr._render_page = lambda page: _orig(page).replace(" -> ", " ==> ").replace("    - ", "  * ")
""",
    ),
    "control-absent-anchor": (
        INVALID,
        """
import check_reference_readiness as crr
# Names something that does not exist. Must be reported as invalid, never as a detection.
crr.no_such_function_exists.disabled = True
""",
    ),
}


def baseline_is_clean() -> bool:
    """A mutation is only evidence against a clean baseline."""
    proc = subprocess.run(
        [PY, "-m", "pytest", TARGET, "-q", "--no-header", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=sanitized_env(),
    )
    print(f"BASELINE {TARGET} exit={proc.returncode}")
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
    return proc.returncode == 0


def verdict_for(name: str, code: str) -> tuple[str, str]:
    """Score one mutation, mapping the shared harness's outcomes onto the three verdicts."""
    try:
        _, exit_code, detail, outcomes = run(name, code, TARGET)
    except SystemExit as exc:
        # `run()` raises when the injected plugin never imported, which is the absent-anchor case.
        return INVALID, str(exc)
    if observed_mutation(outcomes):
        note = detail if not session_ended_abnormally(outcomes) else f"{detail} [abnormal exit {exit_code}]"
        return CAUGHT, note
    if session_is_trustworthy(outcomes):
        return SURVIVED, detail
    return INVALID, f"no verdict (exit {exit_code}, {detail})"


def main() -> int:
    """Run every mutation and fail unless each matched its documented expectation."""
    if not baseline_is_clean():
        print("\nHARNESS ERROR: baseline is not clean, so no mutation verdict is trustworthy.")
        return 2
    print()
    mismatches = []
    for name, (expected, code) in MUTATIONS.items():
        actual, detail = verdict_for(name, code)
        flag = "ok " if actual == expected else "BAD"
        print(f"{flag} {actual:8s} (want {expected:8s})  {name:56s} -> {detail}")
        if actual != expected:
            mismatches.append(f"{name}: expected {expected}, got {actual} ({detail})")
    print()
    if mismatches:
        print("MUTATION EXPECTATIONS NOT MET:")
        for item in mismatches:
            print(f"  {item}")
        return 1
    print(f"All {len(MUTATIONS)} mutations matched their expected verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
