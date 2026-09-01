"""Mutation harness for #423: prove the capture-leg, timeout and multi-batch tests can FAIL.

    python tests/mutation_capture_legs.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest, through the shared
``tests/mutation_harness.py``. Verdicts come from pytest's own lifecycle records, never from scraping
the terminal summary: a non-zero exit alone means nothing (``pytest tests/nope.py`` exits 4 having
run nothing), a collection error looks exactly like a named test error, and a dying xdist worker
emits ``FAILED`` for a test that never executed.

Two discriminating controls run alongside the real mutations, because a harness that reports
everything CAUGHT is indistinguishable from one that is not working:

* ``control-cosmetic-*`` changes something no assertion depends on. It MUST **SURVIVE**. A suite that
  "catches" it is asserting on incidental prose.
* ``control-absent-anchor-*`` patches a symbol that does not exist. It MUST be **INVALID** -- the
  mutation never applied, so any verdict about it would be fabricated. The first run of the shared
  harness reported 22/22 caught for exactly this reason: an import error exits non-zero, and a naive
  scorer reads that as a detection.

The vacuity modes this file was written against, both measured in this repository:

* an assertion inside a branch the fixture never enters -- so several mutations here target the
  BRANCH SELECTOR (``_SHARED_ROOT_CAUSE``, ``_VIEW_HEALTH_FAILURES``) rather than the branch body;
* a test that passes because the mutation broke something else -- which is why every mutation below
  is a *plausible alternative implementation*, not a crash.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from mutation_harness import (  # noqa: E402  # pylint: disable=wrong-import-position
    PY,
    last_line,
    observed_mutation,
    run,
    sanitized_env,
    session_ended_abnormally,
    session_is_trustworthy,
)

LEGS = "tests/test_capture_tableau_oracle_leg_decoupling.py"
BATCH = "tests/test_group_oracle_multi_batch.py"

# name -> (target suite, the patch injected as a pytest plugin at interpreter start)
MUTATIONS: dict[str, tuple[str, str]] = {
    # ---------------------------------------------------------------- the defect itself
    "restore-the-early-return": (
        LEGS,
        """
import capture_tableau_oracle as o
_orig = o._capture_renders
def renders(session, record, wants, targets):
    # THE #423 defect, verbatim: `if record["data"]["status"] != "ok": return record`.
    if record["data"]["status"] != "ok":
        return
    return _orig(session, record, wants, targets)
o._capture_renders = renders
""",
    ),
    "salvage-uses-the-full-retry-policy": (
        LEGS,
        """
import capture_tableau_oracle as o
# Decoupling without a cost guard: the salvage render re-asks a view that has just spent a full
# budget failing, doubling the wall clock on exactly the views that are already slowest.
o.SALVAGE_RETRY = o.RetryPolicy(max_attempts=5, budget_sec=1e6)
""",
    ),
    "cap-every-render-at-one-attempt": (
        LEGS,
        """
import capture_tableau_oracle as o
# The plausible-but-wrong cost guard: cap ALL renders, not just salvage ones. Cheap, and it silently
# removes retries from healthy views whose data leg succeeded.
_orig = o._capture_render
def render(session, view_luid, path, kind, options):
    return _orig(session, view_luid, path, kind, o._RenderOptions(options.api, o.SALVAGE_RETRY))
o._capture_render = render
""",
    ),
    "never-short-circuit-the-remaining-salvage-legs": (
        LEGS,
        """
import capture_tableau_oracle as o
# Every requested tier is asked even after a sibling drawn from the same VizQL render failed --
# metered calls spent to learn the same thing, and the wall-clock bound stops being one timeout.
o._VIEW_HEALTH_FAILURES = frozenset()
""",
    ),
    "short-circuit-on-a-version-gate-too": (
        LEGS,
        """
import capture_tableau_oracle as o
# Treats a CONFIGURATION fault as evidence the view is unwell, which loses the PNG on every
# pre-3.29 site that also asked for SVG.
o._VIEW_HEALTH_FAILURES = o._VIEW_HEALTH_FAILURES | {"unsupported_api_version", "format_mismatch"}
""",
    ),
    "attempt-renders-after-a-credential-block": (
        LEGS,
        """
import capture_tableau_oracle as o
# Drops the carve-out: a credential-blocked view now spends render calls that cannot succeed, and
# its legs earn independent failures -- which flips the run from exit 2 to exit 3.
o._SHARED_ROOT_CAUSE = frozenset()
""",
    ),
    "stamp-skipped-credential-legs-as-failed": (
        LEGS,
        """
import capture_tableau_oracle as o
_orig = o._capture_renders
def renders(session, record, wants, targets):
    out = _orig(session, record, wants, targets)
    for leg in ("image", "svg", "pdf"):
        entry = record.get(leg)
        if entry and entry.get("attempted") is False and entry.get("status") == "source_credential":
            entry["status"] = "failed"
    return out
o._capture_renders = renders
""",
    ),
    "omit-the-not-attempted-record": (
        LEGS,
        """
import capture_tableau_oracle as o
_orig = o._capture_renders
def renders(session, record, wants, targets):
    out = _orig(session, record, wants, targets)
    # Back to an ABSENT key for anything not actually attempted -- the collapse #423 is about.
    for leg in ("image", "svg", "pdf"):
        if (record.get(leg) or {}).get("attempted") is False:
            record.pop(leg)
    return out
o._capture_renders = renders
""",
    ),
    # ------------------------------------------------------------- the UNESTABLISHED census
    "census-counts-every-view": (
        LEGS,
        """
import tableau_oracle_manifest as m
m.render_unestablished = lambda records, requested: [
    {"view_luid": r.get("view_luid"), "view_name": r.get("view_name"), "renders": {}} for r in records
]
""",
    ),
    "census-ignores-a-successful-tier": (
        LEGS,
        """
import tableau_oracle_manifest as m
_orig = m.render_unestablished
def census(records, requested):
    if not requested:
        return []
    out = []
    for record in records:
        legs = {k: (record.get(m._LEG_KEY[k]) or {}).get("status") for k in sorted(requested)}
        # ALL tiers must be ok, rather than any -- so PNG obtained with SVG version-gated out reads
        # as a gap, and the field fires on a reference set that is perfectly usable.
        if all(s == "ok" for s in legs.values()):
            continue
        out.append({"view_luid": record.get("view_luid"), "view_name": record.get("view_name"), "renders": legs})
    return out
m.render_unestablished = census
""",
    ),
    "census-fires-when-no-render-was-requested": (
        LEGS,
        """
import tableau_oracle_manifest as m
_orig = m.render_unestablished
m.render_unestablished = lambda records, requested: _orig(records, requested or frozenset({"png"}))
""",
    ),
    # --------------------------------------------------------------------- --rest-timeout
    "ignore-the-rest-timeout": (
        LEGS,
        """
import capture_tableau_oracle as o
_orig = o.TableauSession.__init__
def init(self, creds, retry=None, timeout_sec=None):
    _orig(self, creds, retry, timeout_sec)
    self.timeout_sec = o.REST_TIMEOUT_SEC
o.TableauSession.__init__ = init
""",
    ),
    "freeze-the-default-budget": (
        LEGS,
        """
import capture_tableau_oracle as o
# The budget stops tracking the timeout: raise --rest-timeout past 360s and the deadline is already
# spent when the first timeout returns, so it grants ZERO retries at any --max-attempts.
o.default_retry_budget = lambda timeout_sec: o.DEFAULT_RETRY_BUDGET_SEC
""",
    ),
    "clamp-an-explicit-budget": (
        LEGS,
        """
import capture_tableau_oracle as o
_orig = o.build_retry_policy
def build(max_attempts, budget_sec, timeout_sec=o.REST_TIMEOUT_SEC):
    # Silently overrides a deliberately tight budget for fast-failing transients. Surgical: an UNSET
    # budget still tracks the timeout, so only the "honoured, never clamped" rule is under test.
    if budget_sec is not None:
        budget_sec = max(budget_sec, o.retry_admission_floor(timeout_sec))
    return _orig(max_attempts, budget_sec, timeout_sec)
o.build_retry_policy = build
""",
    ),
    "warn-against-the-module-constant": (
        LEGS,
        """
import capture_tableau_oracle as o
def build(max_attempts, budget_sec, timeout_sec=o.REST_TIMEOUT_SEC):
    # The POLICY is left correct on purpose; only the warning's numbers regress to the module
    # default, so the mutation isolates "the warning must name the timeout actually in force".
    if budget_sec is None:
        budget_sec = o.default_retry_budget(timeout_sec)
    if budget_sec < o.retry_admission_floor(timeout_sec):
        o.LOG.warning(
            "--retry-budget %.0fs is below the %.0fs needed to retry a failure that blocks for the "
            "full %.0fs request timeout",
            budget_sec, o.RETRY_ADMISSION_FLOOR_SEC, float(o.REST_TIMEOUT_SEC),
        )
    return o.RetryPolicy(max_attempts=max_attempts, budget_sec=budget_sec)
o.build_retry_policy = build
""",
    ),
    # ------------------------------------------------------------------- multi-batch promotion
    "merge-reads-only-the-last-batch": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g.load_batches
g.load_batches = lambda dirs: _orig(dirs)[-1:]
""",
    ),
    "merge-ignores-the-on-disk-check": (
        BATCH,
        """
import group_oracle_by_workbook as g
# A manifest entry is treated as evidence: a newer `ok` whose bytes are gone displaces an older
# batch that still has them, making the merged set worse than either input.
g._leg_is_promotable = lambda entry, root: entry.get("status") == "ok" and bool(entry.get("path"))
""",
    ),
    "merge-uses-argv-order-not-captured-at": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g._merge_one_view
def merge_one(candidates, roots):
    # Order the merge by the position on the command line, so the answer depends on typing habit
    # rather than on when each capture actually happened.
    return _orig(sorted(candidates, key=lambda pair: pair[0].order, reverse=True), roots)
g._merge_one_view = merge_one
""",
    ),
    "merge-ignores-the-per-view-timestamp": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g._merge_one_view
def merge_one(candidates, roots):
    # Dates every view by its BATCH manifest, so a long capture's views all look simultaneous.
    return _orig(sorted(candidates, key=lambda pair: (pair[0].captured_at, pair[0].order), reverse=True), roots)
g._merge_one_view = merge_one
""",
    ),
    "merge-drops-a-leg-no-batch-established": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g._merge_one_view
def merge_one(candidates, roots):
    merged = _orig(candidates, roots)
    for kind, _sub in g.RENDER_LEGS:
        if (merged.get(kind) or {}).get("status") not in (None, "ok"):
            merged.pop(kind, None)
    return merged
g._merge_one_view = merge_one
""",
    ),
    "merge-never-reports-the-basis": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g.merge_batches
def merge(batches):
    manifest, roots, _basis = _orig(batches)
    manifest["merge_order_basis"] = "captured_at"
    return manifest, roots, "captured_at"
g.merge_batches = merge
""",
    ),
    "source-batch-is-never-recorded": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g._merge_one_view
def merge_one(candidates, roots):
    merged = _orig(candidates, roots)
    merged.pop("source_batch", None)
    for kind, _sub in g.RENDER_LEGS:
        if isinstance(merged.get(kind), dict):
            merged[kind].pop("source_batch", None)
    return merged
g._merge_one_view = merge_one
""",
    ),
    "workbook-census-omitted": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g.subset_manifest
def subset(manifest, workbook, views):
    out = _orig(manifest, workbook, views)
    # The per-workbook manifest stops answering "which pages of THIS workbook have no reference".
    out.pop("render_unestablished", None)
    out.pop("render_unestablished_views", None)
    return out
g.subset_manifest = subset
""",
    ),
    "workbook-census-reads-the-capture-not-the-grouping": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g.subset_manifest
def subset(manifest, workbook, views):
    out = _orig(manifest, workbook, views)
    # Recomputes from the CAPTURE's statuses instead of the GROUPED ones, so a leg the capture
    # obtained but this grouping could not place still reads as covered.
    capture_views = [v for v in manifest.get("views", []) if v.get("workbook_name") == workbook]
    census = g.render_unestablished(capture_views, frozenset(manifest.get("requested_renders") or []))
    out["render_unestablished"] = len(census)
    out["render_unestablished_views"] = census
    return out
g.subset_manifest = subset
""",
    ),
    # -------------------------------------------------------------- discriminating controls
    "control-cosmetic-log-wording": (
        LEGS,
        """
import tableau_oracle_manifest as m
_orig = m._log_unestablished
def log(unestablished, redactor):
    m.LOG.warning("cosmetically reworded banner nobody asserts on")
    return _orig(unestablished, redactor)
m._log_unestablished = log
""",
    ),
    "control-cosmetic-batch-report-key": (
        BATCH,
        """
import group_oracle_by_workbook as g
_orig = g._write_grouping_report
def report(batches, migrations_root, basis, outcomes, *, dry_run):
    out = _orig(batches, migrations_root, basis, outcomes, dry_run=dry_run)
    g.LOG.info("cosmetic extra line, asserted on by nothing")
    return out
g._write_grouping_report = report
""",
    ),
    "control-absent-anchor-legs": (
        LEGS,
        """
import capture_tableau_oracle as o
o._this_symbol_does_not_exist.attribute = 1
""",
    ),
    "control-absent-anchor-batch": (
        BATCH,
        """
import group_oracle_by_workbook as g
g._also_not_a_real_symbol.attribute = 1
""",
    ),
}

# What each mutation MUST score for the run to be trustworthy. Declared, so a control that starts
# being "caught" -- the signature of a suite asserting on incidental prose -- fails the harness
# instead of quietly inflating the caught count.
EXPECTED = {
    name: ("INVALID" if "absent-anchor" in name else "SURVIVED" if "control-" in name else "CAUGHT")
    for name in MUTATIONS
}


def baseline(target: str) -> int:
    """A mutation is only evidence against a clean baseline."""
    proc = subprocess.run(
        [PY, "-m", "pytest", target, "-q", "--no-header", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=sanitized_env(),
    )
    print(f"BASELINE {target:56s} exit={proc.returncode}  {last_line(proc)}")
    return proc.returncode


def classify(name: str, code: str, target: str) -> tuple[str, str]:
    """Score one mutation as CAUGHT / SURVIVED / INVALID / HARNESS-ERROR, with a detail line."""
    try:
        _label, returncode, detail, outcomes = run(name, code, target)
    except SystemExit as exc:
        # The shared harness refuses to score a mutation whose plugin failed to import. That is
        # exactly the verdict an absent-anchor control is meant to earn.
        return "INVALID", str(exc)
    if observed_mutation(outcomes):
        verdict = "CAUGHT" if outcomes["call_failed"] else "CAUGHT*"
        if session_ended_abnormally(outcomes):
            detail = f"{detail} [session ended abnormally: exit {returncode}]"
        return verdict, detail
    if session_is_trustworthy(outcomes):
        return "SURVIVED", detail
    if not outcomes.get("recorded") and not outcomes.get("session_finished"):
        # pytest never started, so the patch never ran and NO verdict about the suite is possible.
        # Distinguished from HARNESS-ERROR deliberately: a real mutation landing here is reported as
        # INVALID and fails its declared CAUGHT, so nothing is masked -- while an absent-anchor
        # control lands here BY DESIGN and pins that this path is reachable and correctly labelled.
        # ⚠️ The shared harness only raises SystemExit for the literal string "Error importing
        # plugin"; an AttributeError inside the plugin exits 1 with no lifecycle record instead, and
        # scoring that as a detection is the exact false-green the shared harness's own docstring
        # records (22/22 "caught" on its first run).
        return "INVALID", f"mutation never applied - {detail}"
    return "HARNESS-ERROR", f"exit {returncode}, {detail}"


def main() -> int:
    """Run every mutation and fail unless each scored what it was declared to score."""
    dirty = [target for target in (LEGS, BATCH) if baseline(target) != 0]
    if dirty:
        print("\nHARNESS ERROR: baseline is not clean, so no mutation verdict is trustworthy:", dirty)
        return 2
    print()
    wrong = []
    for name, (target, code) in MUTATIONS.items():
        verdict, detail = classify(name, code, target)
        expected = EXPECTED[name]
        ok = verdict.rstrip("*") == expected
        print(f"{verdict:13s} {'' if ok else f'(EXPECTED {expected}) '}{name:48s} -> {detail}")
        if not ok:
            wrong.append(f"{name}: expected {expected}, got {verdict}")
    print()
    if wrong:
        print("MUTATIONS THAT DID NOT SCORE AS DECLARED:")
        for item in wrong:
            print(f"  {item}")
        return 1
    print(
        f"all {len(MUTATIONS)} mutations scored as declared "
        f"({sum(1 for v in EXPECTED.values() if v == 'CAUGHT')} caught, "
        f"{sum(1 for v in EXPECTED.values() if v == 'SURVIVED')} cosmetic controls survived, "
        f"{sum(1 for v in EXPECTED.values() if v == 'INVALID')} absent-anchor controls invalid)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
