"""Mutation harness for the reference-readiness ENTRY gate (issue #421).

    python tests/mutation_reference_readiness.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest, exactly as
``tests/mutation_harness.py`` does. It **imports** that harness's scoring machinery rather than
extending its ``MUTATIONS`` dict, for two reasons: the scoring is the load-bearing part and must not
be forked, and ``mutation_harness.py`` itself is a shared file another change may be editing.

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
    # --- fail-closed properties the suite must defend -------------------------------------
    "worksheet-render-satisfies-dashboard-page": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Restore the pre-#421 behaviour: match on NAME alone, discarding scope. This is what
# `check_unit.py:265`'s `_slug` join does today, and it is why an `embedded_thumbnail`
# worksheet render filed under the manifest's `dashboards` key satisfies a dashboard page.
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
# Let evidence whose object type could not be established satisfy a page anyway. PR #422 fails
# closed to `unknown` when the Metadata API is disabled, so this is the live failure mode.
def match_evidence(obj, evidence):
    named = [e for e in evidence if crr._norm(e.name) == crr._norm(obj.name)]
    ok = [e for e in named if e.kind in (obj.kind, crr.KIND_UNKNOWN)]
    return (ok[0], []) if ok else (None, named)
crr.match_evidence = match_evidence
""",
    ),
    "expectation-falls-back-to-the-pages-that-were-built": (
        CAUGHT,
        """
import check_reference_readiness as crr
# The circularity in `check_oracle_coverage:925`: when no expectation can be derived, grade the
# artifact against itself. Every page then has evidence by construction.
_orig = crr.assess_unit
def assess_unit(root, report_dir, engine_report, evidence, explicit_source, require_validation_grade):
    result = _orig(root, report_dir, engine_report, evidence, explicit_source, require_validation_grade)
    if result.status == crr.STATUS_CANNOT_ESTABLISH:
        result.status = crr.STATUS_READY
        result.pages = [
            {"source_object": page_id, "source_type": crr.KIND_UNKNOWN, "page_id": page_id,
             "page_status": crr.PAGE_EMITTED, "grade": crr.GRADE_UNKNOWN, "matched_by": "self",
             "evidence": "present", "readiness": crr.READY}
            for page_id in crr.actual_page_ids(report_dir)
        ]
    return result
crr.assess_unit = assess_unit
""",
    ),
    "no-pages-found-means-not-applicable": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Infer NOT_APPLICABLE from an empty page list instead of earning it from the engine's own
# report.json. A workbook whose report failed to emit then reads as legitimately reference-free.
_orig = crr.assess_unit
def assess_unit(root, report_dir, *args, **kwargs):
    if not crr.actual_page_ids(report_dir):
        return crr.UnitResult(unit=report_dir.name[: -len(".Report")],
                              status=crr.STATUS_NOT_APPLICABLE, detail="no pages found")
    return _orig(root, report_dir, *args, **kwargs)
crr.assess_unit = assess_unit
""",
    ),
    "cannot-establish-exits-zero": (
        CAUGHT,
        """
import check_reference_readiness as crr
# The exit-code contract: an unassessable bundle must never green-light a start.
crr.EXIT_CANNOT_ESTABLISH = crr.EXIT_OK
""",
    ),
    "unverifiable-is-treated-as-ready": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Collapse the third state: evidence whose identity cannot be proven counts as proof.
crr.UNVERIFIABLE = crr.READY
""",
    ),
    "explained-and-unexplained-drops-collapse": (
        CAUGHT,
        """
import check_reference_readiness as crr
# Treat every missing page as engine-explained. The gate then stays silent on a real conversion
# gap -- the direction that matters, since the opposite only makes it noisy.
crr.drop_explanations = lambda handover: __import__("collections").defaultdict(lambda: "assumed")
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
# Collapse the cryptographic identity: name a page from the bare object name, so a dashboard and
# its same-named principal worksheet land on the SAME page id again.
crr.SourceObject.page_id = property(lambda self: crr.engine_page_id(self.name))
""",
    ),
    # --- discriminating controls -----------------------------------------------------------
    "control-cosmetic-reword-of-a-rendered-line": (
        SURVIVED,
        """
import check_reference_readiness as crr
# Pure presentation. If this is CAUGHT, the suite is asserting on incidental wording and its
# other detections prove less than they appear.
_orig = crr._render_page
crr._render_page = lambda page: _orig(page).replace(" -> ", " ==> ").replace("    - ", "  * ")
""",
    ),
    "control-absent-anchor": (
        INVALID,
        """
import check_reference_readiness as crr
# Names something that does not exist. The shared harness must report this as invalid rather than
# crediting the non-zero exit as a detection -- the exact false-green its docstring records.
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
        # `run()` raises when the injected plugin never imported, which is exactly the
        # absent-anchor case. Reporting it as INVALID is the point of the control.
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
        print(f"{flag} {actual:8s} (want {expected:8s})  {name:52s} -> {detail}")
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
