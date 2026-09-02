"""Mutation harness for #423: prove the capture-leg, timeout and multi-batch tests can FAIL.

    python tests/mutation_capture_legs.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest, through the shared
``tests/mutation_harness.py``. Verdicts come from pytest's own lifecycle records, never from scraping
the terminal summary: a non-zero exit alone means nothing (``pytest tests/nope.py`` exits 4 having
run nothing), a collection error looks exactly like a named test error, and a dying xdist worker
emits ``FAILED`` for a test that never executed.

⚠️ **Every mutation names the TEST NODE IDs that must observe it, and is run and baselined against
only those.** That is review round 1's finding 6, and it was not a theoretical risk: mutations run
under ``-x``, so a whole-file target credits a mutation to whichever test in the file fails first.
``merge-reads-only-the-last-batch`` was reported CAUGHT by a neighbour while its own documented
anchor SURVIVED -- ``1 passed`` -- because that anchor's fixture already contained the whole answer
in its final batch. The advertised proof did not exist. Three changes make that unrepeatable:

* the mutation table maps name -> node IDs, so the claim is data rather than prose;
* :func:`verify_anchors` refuses to run if any anchor names a test pytest does not collect -- an
  anchor that selects nothing would be WORSE than a file, because pytest exits 4 for an unmatched
  node ID and an exit-code scorer would read that as a detection;
* the baseline is per-anchor, not per-file, so a green file cannot vouch for a test that never ran.

Two discriminating controls run alongside the real mutations, because a harness that reports
everything CAUGHT is indistinguishable from one that is not working:

* ``control-cosmetic-*`` changes something no assertion depends on. It MUST **SURVIVE**. A suite that
  "catches" it is asserting on incidental prose.
* ``control-absent-anchor-*`` patches a symbol that does not exist. It MUST be **INVALID** -- the
  mutation never applied, so any verdict about it would be fabricated. The first run of the shared
  harness reported 22/22 caught for exactly this reason: an import error exits non-zero, and a naive
  scorer reads that as a detection.

The vacuity modes this file was written against, all measured in this repository:

* an assertion inside a branch the fixture never enters -- so several mutations here target the
  BRANCH SELECTOR (``_SHARED_ROOT_CAUSE``, ``_VIEW_HEALTH_FAILURES``) rather than the branch body;
* a test that passes because the mutation broke something else -- which is why every mutation below
  is a *plausible alternative implementation*, not a crash;
* a fixture that already contains the answer, so no amount of reading less could change it -- the
  finding-6 shape, now covered by the anchor's own docstring and by a partial-retry fixture.

Several behaviours carry an OVER-correction mutation as well as the defect one
(``a-tie-is-reported-even-when-it-decided-nothing``, ``default-budget-collapses-to-the-floor``,
``salvage-deadline-applied-to-a-healthy-view``), because a rule that fires on everything is as
useless as one that never fires, and only a second mutation can tell the two apart.
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
SOCKET = "tests/test_tableau_http_deadline.py"
ORACLE = "tests/test_capture_tableau_oracle.py"

# name -> (the test NODE IDs that must observe it, the patch injected as a pytest plugin at startup)
#
# ⚠️ Node IDs, not files, and that is review round 1's finding 6. Mutations run under ``-x``, so a
# whole-file target credits a mutation to whichever test fails FIRST -- and the failing test may have
# nothing to do with the behaviour under test. Measured on this very file:
# ``merge-reads-only-the-last-batch`` was reported CAUGHT by
# ``test_the_two_legs_may_come_from_DIFFERENT_batches`` while its own documented anchor,
# ``test_a_later_batch_that_finally_succeeded_is_promoted``, SURVIVED -- 1 passed. The advertised
# proof did not exist. Anchoring makes the mutation-to-test mapping a checkable fact, and
# ``verify_anchors`` fails the run if any anchor names a test that is not collected.
MUTATIONS: dict[str, tuple[tuple[str, ...], str]] = {
    # ---------------------------------------------------------------- the defect itself
    "restore-the-early-return": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_failed_data_leg_no_longer_skips_the_render",
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_the_salvaged_render_is_a_real_status_not_a_placeholder",
        ),
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
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_salvage_render_gets_one_attempt_and_no_retry_budget",
        ),
        """
import capture_tableau_oracle as o
# Decoupling without a cost guard: the salvage render re-asks a view that has just spent a full
# budget failing, doubling the wall clock on exactly the views that are already slowest.
o.SALVAGE_RETRY = o.RetryPolicy(max_attempts=5, budget_sec=1e6)
""",
    ),
    "cap-every-render-at-one-attempt": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_render_after_a_SUCCESSFUL_data_leg_keeps_the_full_session_policy",
        ),
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
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_the_first_failed_salvage_render_stops_the_rest_and_records_them",
        ),
        """
import capture_tableau_oracle as o
# Every requested tier is asked even after a sibling drawn from the same VizQL render failed --
# metered calls spent to learn the same thing, and the wall-clock bound stops being one timeout.
o._VIEW_HEALTH_FAILURES = frozenset()
""",
    ),
    "short-circuit-on-a-version-gate-too": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_version_gate_does_not_stop_the_remaining_salvage_legs",
        ),
        """
import capture_tableau_oracle as o
# Treats a CONFIGURATION fault as evidence the view is unwell, which loses the PNG on every
# pre-3.29 site that also asked for SVG.
o._VIEW_HEALTH_FAILURES = o._VIEW_HEALTH_FAILURES | {"unsupported_api_version", "format_mismatch"}
""",
    ),
    "attempt-renders-after-a-credential-block": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_credential_block_still_skips_the_renders_and_they_inherit_its_status",
        ),
        """
import capture_tableau_oracle as o
# Drops the carve-out: a credential-blocked view now spends render calls that cannot succeed, and
# its legs earn independent failures -- which flips the run from exit 2 to exit 3.
o._SHARED_ROOT_CAUSE = frozenset()
""",
    ),
    "stamp-skipped-credential-legs-as-failed": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_credential_only_run_still_exits_2_after_decoupling",
        ),
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
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_requested_leg_is_never_absent_so_absent_means_not_requested",
        ),
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
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_view_whose_render_SUCCEEDED_is_not_unestablished",
        ),
        """
import tableau_oracle_manifest as m
m.render_unestablished = lambda records, requested: [
    {"view_luid": r.get("view_luid"), "view_name": r.get("view_name"), "renders": {}} for r in records
]
""",
    ),
    "census-ignores-a-successful-tier": (
        ("tests/test_capture_tableau_oracle_leg_decoupling.py::test_one_ok_tier_is_enough_to_establish_a_reference",),
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
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_no_render_requested_means_nothing_is_unestablished",
        ),
        """
import tableau_oracle_manifest as m
_orig = m.render_unestablished
m.render_unestablished = lambda records, requested: _orig(records, requested or frozenset({"png"}))
""",
    ),
    # --------------------------------------------------------------------- --rest-timeout
    "ignore-the-rest-timeout": (
        ("tests/test_capture_tableau_oracle_leg_decoupling.py::test_the_request_timeout_reaches_the_transport",),
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
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_the_cli_accepts_a_raised_timeout_and_the_budget_follows_it",
        ),
        """
import capture_tableau_oracle as o
import tableau_capture_policy as p
# The budget stops tracking the timeout: raise --rest-timeout past 360s and the deadline is already
# spent when the first timeout returns, so it grants ZERO retries at any --max-attempts.
#
# ⚠️ Patched on BOTH modules. `build_retry_policy` now lives in `tableau_capture_policy` and calls
# ITS module-global, so patching only the `capture_tableau_oracle` re-export left the real code
# untouched and this mutation SURVIVED after the split -- caught by running it against its own
# anchor, which is the whole argument for per-anchor mutation runs.
p.default_retry_budget = lambda timeout_sec: p.DEFAULT_RETRY_BUDGET_SEC
o.default_retry_budget = p.default_retry_budget
""",
    ),
    "clamp-an-explicit-budget": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_an_explicit_budget_is_honoured_even_when_the_timeout_moves",
        ),
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
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_the_floor_warning_names_the_timeout_actually_in_force",
        ),
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
        ("tests/test_group_oracle_multi_batch.py::test_a_later_batch_that_finally_succeeded_is_promoted",),
        """
import group_oracle_by_workbook as g
_orig = g.load_batches
g.load_batches = lambda dirs: _orig(dirs)[-1:]
""",
    ),
    "merge-ignores-the-on-disk-check": (
        (
            "tests/test_group_oracle_multi_batch.py::test_a_newer_batch_whose_file_is_GONE_does_not_displace_an_older_one_that_has_it",
        ),
        """
import group_oracle_by_workbook as g
# A manifest entry is treated as evidence: a newer `ok` whose bytes are gone displaces an older
# batch that still has them, making the merged set worse than either input.
g._leg_is_promotable = lambda entry, root: entry.get("status") == "ok" and bool(entry.get("path"))
""",
    ),
    "merge-uses-argv-order-not-captured-at": (
        ("tests/test_group_oracle_multi_batch.py::test_argument_order_does_not_decide_the_winner_when_timestamps_do",),
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
        ("tests/test_group_oracle_multi_batch.py::test_a_per_view_timestamp_outranks_the_batch_manifest_timestamp",),
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
        ("tests/test_group_oracle_multi_batch.py::test_a_view_no_batch_could_render_stays_visibly_unestablished",),
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
        ("tests/test_group_oracle_multi_batch.py::test_an_undated_batch_is_reported_rather_than_dated_by_argv",),
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
        ("tests/test_group_oracle_multi_batch.py::test_every_promoted_artifact_names_the_batch_it_came_from",),
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
        (
            "tests/test_group_oracle_multi_batch.py::test_the_workbook_manifest_says_which_views_have_no_establishable_render",
        ),
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
        (
            "tests/test_group_oracle_multi_batch.py::test_an_artifact_the_grouping_could_not_place_counts_as_unestablished",
        ),
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
    # ------------------------------------------------- review round 1: render intent, ties, salvage
    "intent-copied-from-the-newest-batch-only": (
        ("tests/test_group_oracle_multi_batch.py::test_a_later_DATA_ONLY_batch_does_not_erase_a_known_render_gap",),
        """
import group_oracle_by_workbook as g
_orig = g._merge_render_intent
def intent(batches, views):
    out = _orig(batches, views)
    # The round-1 finding-2 defect: intent taken from the newest batch, so a later data-only run
    # rewrites `requested_renders` to [] and every known render gap reads as "never requested".
    newest = max(batches, key=lambda b: (b.captured_at, b.order))
    out["requested_renders"] = sorted(newest.manifest.get("requested_renders") or [])
    return out
g._merge_render_intent = intent
""",
    ),
    "leg-fallback-takes-the-newest-VIEW-not-the-newest-record": (
        ("tests/test_group_oracle_multi_batch.py::test_a_later_DATA_ONLY_batch_does_not_erase_a_known_render_gap",),
        """
import group_oracle_by_workbook as g
_orig = g._resolve_leg
def resolve(candidates, kind, roots):
    winner, ties = _orig(candidates, kind, roots)
    # The other half of finding 2: when nothing is promotable, fall back to the newest VIEW rather
    # than the newest view that HAS a record for this leg -- so a data-only batch erases the older
    # batch's failed image instead of preserving it.
    if winner is not None and not g._leg_is_promotable(winner[1].get(kind) or {}, roots[winner[0].label]):
        newest = candidates[0]
        return (newest if isinstance(newest[1].get(kind), dict) else None), ties
    return winner, ties
g._resolve_leg = resolve
""",
    ),
    "reference_required-taken-from-the-newest-batch": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_a_batch_that_required_a_reference_is_not_overruled_by_one_that_did_not",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._merge_render_intent
def intent(batches, views):
    out = _orig(batches, views)
    newest = max(batches, key=lambda b: (b.captured_at, b.order))
    out["reference_required"] = bool(newest.manifest.get("reference_required"))
    out["reference_missing"] = bool(out["reference_required"] and not out.get("reference_missing") is False)
    return out
g._merge_render_intent = intent
""",
    ),
    "ties-reported-as-captured_at": (
        ("tests/test_group_oracle_multi_batch.py::test_equal_timestamps_are_reported_as_a_tie_not_as_captured_at",),
        """
import group_oracle_by_workbook as g
_orig = g._resolve_leg
def resolve(candidates, kind, roots):
    winner, _ties = _orig(candidates, kind, roots)
    # Finding 5: equal timestamps decide nothing, but the basis still claimed `captured_at`.
    return winner, []
g._resolve_leg = resolve
""",
    ),
    "a-tie-is-reported-even-when-it-decided-nothing": (
        ("tests/test_group_oracle_multi_batch.py::test_a_tie_that_decides_NOTHING_is_not_reported",),
        """
import group_oracle_by_workbook as g
_orig = g._resolve_leg
def resolve(candidates, kind, roots):
    winner, _ties = _orig(candidates, kind, roots)
    # The opposite over-reaction: flag every shared timestamp, including ones evidence separated,
    # until the field fires on ordinary runs and nobody reads it.
    if winner is None:
        return winner, []
    same = [b.label for b, v in candidates if g._stamp(b, v) == g._stamp(*winner)]
    return winner, (same if len(same) > 1 else [])
g._resolve_leg = resolve
""",
    ),
    "reauthenticate-on-the-final-attempt": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py"
            "::test_a_lost_session_on_the_final_attempt_does_not_reauthenticate",
        ),
        """
import capture_tableau_oracle as o
_orig_sign_in = o.TableauSession.sign_in
_orig_export = o.TableauSession.export
def export(self, path, *, api=None, retry=None):
    # Finding 3a: re-authenticate whenever the session is lost, even on the policy's final attempt,
    # where the new token cannot be used by this export. Modelled by signing in on the way out of a
    # session_lost failure, which is what the un-guarded branch effectively did.
    try:
        return _orig_export(self, path, api=api, retry=retry)
    except o.ExportFailed as exc:
        if exc.kind == "session_lost":
            self.sign_in()
        raise
o.TableauSession.export = export
""",
    ),
    "no-shared-salvage-deadline": (
        ("tests/test_capture_tableau_oracle_leg_decoupling.py::test_three_slow_salvage_legs_share_ONE_deadline",),
        """
import capture_tableau_oracle as o
# Finding 3b: attempts are bounded per leg but the SEQUENCE is not, so three legs that each fail
# after a full request timeout cost three timeouts against a stated one-timeout bound.
o._salvage_exhausted = lambda deadline, timeout: ""
""",
    ),
    "salvage-deadline-admits-a-leg-that-cannot-finish": (
        ("tests/test_capture_tableau_oracle_leg_decoupling.py::test_three_slow_salvage_legs_share_ONE_deadline",),
        """
import capture_tableau_oracle as o
_orig = o._salvage_exhausted
def exhausted(deadline, timeout):
    # The subtler version: admit while the deadline has not PASSED, rather than while a whole
    # request still fits. A leg starting at deadline-epsilon then blocks for a full timeout, so the
    # real ceiling is budget + timeout and creeps with every tier.
    return "" if o.time.monotonic() < deadline else _orig(deadline, timeout)
o._salvage_exhausted = exhausted
""",
    ),
    "salvage-deadline-applied-to-a-healthy-view": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py"
            "::test_the_salvage_deadline_does_not_apply_when_the_data_leg_SUCCEEDED",
        ),
        """
import capture_tableau_oracle as o
_orig = o._capture_renders
def renders(session, record, wants, targets):
    # Over-reach: apply the salvage ceiling to every view, so a healthy slow render loses its tiers.
    record = dict(record) if False else record
    saved = record["data"]["status"]
    record["data"]["status"] = "transient"
    try:
        return _orig(session, record, wants, targets)
    finally:
        record["data"]["status"] = saved
o._capture_renders = renders
""",
    ),
    "default-budget-is-a-flat-2x": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py"
            "::test_the_default_budget_is_never_below_the_admission_floor",
            "tests/test_capture_tableau_oracle_leg_decoupling.py"
            "::test_a_sub_second_timeout_actually_retries_a_full_timeout_failure",
        ),
        """
import capture_tableau_oracle as o
import tableau_capture_policy as p
# Finding 4: 2x is below the admission floor for any sub-second timeout, so a full-timeout failure
# gets ZERO retries at the DEFAULT budget -- the footgun the default exists to prevent. Patched on
# both modules because the arithmetic test reads the re-export and the loop test reaches the owner.
p.default_retry_budget = lambda timeout_sec: 2.0 * timeout_sec
o.default_retry_budget = p.default_retry_budget
""",
    ),
    "default-budget-collapses-to-the-floor": (
        ("tests/test_capture_tableau_oracle_leg_decoupling.py::test_the_ratio_still_dominates_at_realistic_timeouts",),
        """
import capture_tableau_oracle as o
import tableau_capture_policy as p
# The opposite over-correction: taking the floor everywhere shrinks every real budget from 360s to
# 181s while still clearing the floor, so the arithmetic test alone cannot see it.
p.default_retry_budget = p.retry_admission_floor
o.default_retry_budget = p.default_retry_budget
""",
    ),
    # ------------------------------------------------- review round 2: a real bound, and real identity
    "no-end-to-end-deadline-on-a-salvage-leg": (
        ("tests/test_tableau_http_deadline.py::test_a_trickling_salvage_sequence_is_bounded_end_to_end",),
        """
import capture_tableau_oracle as o
_orig = o._capture_render
def render(session, view_luid, path, kind, options):
    # Round 2's finding 1: admission alone, with no end-to-end deadline. `urllib`'s timeout bounds one
    # socket OPERATION, so a trickling response outlives it and one admitted leg exceeds the whole
    # budget by itself. The virtual-clock tests cannot see this; only a real socket can.
    return _orig(session, view_luid, path, kind, o._RenderOptions(options.api, options.retry, None))
o._capture_render = render
""",
    ),
    "deadline-checked-with-read-not-read1": (
        ("tests/test_tableau_http_deadline.py::test_read_bounded_abandons_a_stream_that_outlives_its_deadline",),
        """
import tableau_http as h
_orig = h._read_bounded
def read_bounded(stream, deadline, timeout):
    # The subtler half: keep the deadline but read with `read`, which blocks until the whole chunk
    # has arrived -- so a small trickling body is delivered in ONE call and the clock is never
    # consulted. Measured before the fix: 0.970s against a 0.3s deadline, i.e. the deadline did
    # nothing while looking entirely present in the code.
    if deadline is None:
        return stream.read()
    if hasattr(stream, "read1"):
        stream = _NoRead1(stream)
    return _orig(stream, deadline, timeout)
class _NoRead1:
    def __init__(self, inner):
        self._inner = inner
    def read(self, *args):
        return self._inner.read(*args)
h._read_bounded = read_bounded
""",
    ),
    "abandoned-body-returned-as-a-success": (
        ("tests/test_tableau_http_deadline.py::test_read_bounded_never_returns_a_partial_body",),
        """
import tableau_http as h
_orig = h._read_bounded
def read_bounded(stream, deadline, timeout):
    # Worse than the unbounded read, because it is silent: return whatever arrived before the
    # deadline as though the body were complete, so a truncated CSV is recorded as a 200.
    try:
        return _orig(stream, deadline, timeout)
    except TimeoutError:
        return b""
h._read_bounded = read_bounded
""",
    ),
    "deadline-covers-only-the-success-path": (
        ("tests/test_tableau_http_deadline.py::test_the_error_body_path_is_bounded_without_any_socket",),
        """
import tableau_http as h
_orig = h._read_bounded
_depth = {"n": 0}
def read_bounded(stream, deadline, timeout):
    # `HTTPError.read()` is a separate code path, reached from inside an `except` clause. A deadline
    # applied only to the success path leaves the transport unbounded on every 4xx/5xx.
    import http.client, urllib.error
    if isinstance(stream, urllib.error.HTTPError):
        return stream.read()
    return _orig(stream, deadline, timeout)
h._read_bounded = read_bounded
""",
    ),
    "a-deadline-on-every-request-not-just-salvage": (
        ("tests/test_tableau_http_deadline.py::test_no_deadline_leaves_every_other_caller_byte_for_byte_unchanged",),
        """
import tableau_http as h
import time
_orig = h._read_bounded
def read_bounded(stream, deadline, timeout):
    # Over-reach: apply a deadline even when the caller asked for none, which truncates the DATA leg
    # -- a real export streaming legitimately slowly -- and silently shrinks every capture.
    return _orig(stream, deadline if deadline is not None else time.monotonic() + timeout, timeout)
h._read_bounded = read_bounded
""",
    ),
    "batch-label-is-the-directory-name": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_two_captures_with_the_same_directory_NAME_stay_distinguishable",
        ),
        """
import group_oracle_by_workbook as g
# Round 2's finding 2: two captures at run1/oracle and run2/oracle collapse into one label, one
# `roots` entry, and indistinguishable provenance.
g._batch_labels = lambda dirs: [d.name for d in dirs]
""",
    ),
    "batch-labels-disambiguated-by-index": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_a_disambiguated_label_says_WHICH_capture_not_merely_that_they_differ",
        ),
        """
import group_oracle_by_workbook as g
def labels(dirs):
    # Unique, and useless as provenance: `oracle`, `oracle-2` says only THAT they differ, never which
    # directory a reader should open.
    seen, out = {}, []
    for d in dirs:
        seen[d.name] = seen.get(d.name, 0) + 1
        out.append(d.name if seen[d.name] == 1 else f"{d.name}-{seen[d.name]}")
    return out
g._batch_labels = labels
""",
    ),
    "every-label-prefixed-whether-or-not-it-collides": (
        ("tests/test_group_oracle_multi_batch.py::test_unique_names_are_left_alone",),
        """
import group_oracle_by_workbook as g
_orig = g._batch_labels
# The opposite over-reach: prefix everything, churning `source_batch` for every existing capture.
g._batch_labels = lambda dirs: ["/".join(d.resolve().parts[-2:]) for d in dirs]
""",
    ),
    "the-same-capture-twice-is-silently-deduplicated": (
        ("tests/test_group_oracle_multi_batch.py::test_the_same_capture_given_twice_is_refused_not_deduplicated",),
        """
import group_oracle_by_workbook as g
_orig = g.load_batches
def load(dirs):
    seen, unique = set(), []
    for d in dirs:
        key = str(d.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return _orig(unique)
g.load_batches = load
""",
    ),
    # ------------------------------------- review round 3: the whole request, and an exact keyword
    "deadline-covers-only-the-body": (
        ("tests/test_tableau_http_deadline.py::test_slow_HEADERS_are_bounded_by_the_deadline_too",),
        """
import urllib.request
import tableau_http as h
# Round 3's finding 1: enforce the deadline only AFTER urlopen() returns, so connection, status line
# and headers run under the per-socket-operation timeout alone -- which a server trickling its
# HEADERS never trips. Measured before the fix: 1.378s against a 0.20s ceiling.
h._open = lambda req, timeout, deadline: urllib.request.urlopen(req, timeout=timeout)
""",
    ),
    "watchdog-closes-instead-of-shutting-down": (
        ("tests/test_tableau_http_deadline.py::test_slow_HEADERS_are_bounded_by_the_deadline_too",),
        """
import tableau_http as h
# The subtler half, and it is platform behaviour rather than logic: `close()` decrements a handle and
# does NOT interrupt a peer-blocked recv on Windows. Measured with close(): the timer fired, the
# header read still ran to completion, 0.878s against a 0.15s deadline. Only shutdown(SHUT_RDWR)
# tears the connection down in both directions.
def abort(sock):
    try:
        sock.close()
    except OSError:
        pass
h._abort_socket = abort
""",
    ),
    "no-pre-request-deadline-check": (
        ("tests/test_tableau_http_deadline.py::test_a_deadline_already_passed_never_opens_a_connection",),
        """
import urllib.request
import tableau_http as h
_orig = h._open
def open_(req, timeout, deadline):
    # Drop the "already spent" refusal, so a request with no budget left still opens a socket and
    # does network work before being aborted.
    if deadline is not None and deadline - h.time.monotonic() <= 0:
        opener = urllib.request.build_opener(h._DeadlineHTTPHandler(deadline), h._DeadlineHTTPSHandler(deadline))
        return opener.open(req, timeout=timeout)
    return _orig(req, timeout, deadline)
h._open = open_
""",
    ),
    "a-deadline-on-every-request-lifecycle-too": (
        ("tests/test_tableau_http_deadline.py::test_slow_headers_are_UNBOUNDED_without_the_deadline",),
        """
import tableau_http as h
import time
_orig = h._open
# Over-reach: bound the lifecycle even when the caller asked for no deadline, which truncates the
# DATA leg -- a real export streaming legitimately slowly.
h._open = lambda req, timeout, deadline: _orig(req, timeout, deadline if deadline is not None else time.monotonic() + timeout)
""",
    ),
    "double-gate-matches-keywords-by-substring": (
        ("tests/test_capture_tableau_oracle.py::test_the_double_gate_rejects_a_merely_similar_keyword",),
        """
import ast
# Round 3's finding 2: derive the right keyword names and then compare them by SUBSTRING, so a
# double declaring `api_version` / `deadline_seconds` satisfies the gate for `api` / `deadline` while
# raising TypeError on the first real call. Modelled by making the control's parse report the
# confusable names as if they were the real ones.
_orig = ast.parse
def parse(source, *args, **kwargs):
    return _orig(source.replace("api_version", "api").replace("deadline_seconds", "deadline"), *args, **kwargs)
ast.parse = parse
""",
    ),
    "eof-at-the-deadline-is-a-complete-body": (
        ("tests/test_tableau_http_deadline.py::test_an_eof_at_the_deadline_is_not_treated_as_a_complete_body",),
        """
import tableau_http as h
_orig = h._read_bounded
def read_bounded(stream, deadline, timeout):
    # The platform divergence CI found: `shutdown(SHUT_RDWR)` raises on Windows but yields a clean
    # EOF on Linux, so an aborted trickle is read as a COMPLETE body and reported HTTP 200. Silent
    # corruption, and green on the machine the fix was written on.
    try:
        return _orig(stream, deadline, timeout)
    except TimeoutError as exc:
        if "cannot be assumed" in str(exc):
            return b""
        raise
h._read_bounded = read_bounded
""",
    ),
    "abort-uses-shutdown-but-eof-is-still-trusted": (
        ("tests/test_tableau_http_deadline.py::test_a_body_that_genuinely_finishes_in_time_still_returns",),
        """
import time
import tableau_http as h
_orig = h._read_bounded
def read_bounded(stream, deadline, timeout):
    # The opposite over-correction: refuse EVERY eof once a deadline is in force, which passes the
    # abort test and breaks every real capture whose body simply finished.
    if deadline is None:
        return _orig(stream, deadline, timeout)
    result = _orig(stream, deadline, timeout)
    raise TimeoutError("a complete body cannot be assumed here")
h._read_bounded = read_bounded
""",
    ),
    # -------------------------------------------------------------- discriminating controls
    "control-cosmetic-log-wording": (
        (
            "tests/test_capture_tableau_oracle_leg_decoupling.py::test_the_manifest_counts_and_names_the_views_with_no_establishable_render",
        ),
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
        ("tests/test_group_oracle_multi_batch.py::test_the_grouping_report_names_every_batch_it_merged",),
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
        ("tests/test_capture_tableau_oracle_leg_decoupling.py::test_a_failed_data_leg_no_longer_skips_the_render",),
        """
import capture_tableau_oracle as o
o._this_symbol_does_not_exist.attribute = 1
""",
    ),
    "control-absent-anchor-batch": (
        ("tests/test_group_oracle_multi_batch.py::test_a_later_batch_that_finally_succeeded_is_promoted",),
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


def verify_anchors() -> list[str]:
    """Every declared anchor must name a test pytest actually collects.

    ⚠️ The whole point of anchoring is that the mutation-to-test mapping is CHECKABLE. An anchor that
    silently names nothing would be worse than a file target: pytest exits 4 for an unmatched node ID,
    and a scorer that reads a non-zero exit as a detection would report the mutation CAUGHT by a test
    that never ran. This is the same false-green shape the shared harness's own docstring records.
    """
    collected: set[str] = set()
    for suite in (LEGS, BATCH, SOCKET, ORACLE):
        proc = subprocess.run(
            [PY, "-m", "pytest", suite, "--collect-only", "-q", "--no-header", "--color=no"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=sanitized_env(),
        )
        collected.update(line.strip() for line in proc.stdout.splitlines() if "::" in line)

    def selects(anchor: str) -> bool:
        # A PARAMETRIZED test collects as `path::name[param]`, and `path::name` is the node ID that
        # selects every one of them -- which is what an anchor for a parametrized behaviour should
        # say. Caught on this file's first anchored run, and it is the reason this check exists: the
        # bare name looked wrong to an exact-match verifier while being exactly right to pytest.
        return anchor in collected or any(node.startswith(f"{anchor}[") for node in collected)

    return sorted(
        f"{name} -> {anchor}"
        for name, (anchors, _code) in MUTATIONS.items()
        for anchor in anchors
        if not selects(anchor)
    )


def baseline(anchors: tuple[str, ...]) -> tuple[int, str]:
    """A mutation is only evidence against a clean baseline -- of ITS OWN anchors, not of a file.

    Baselining the whole file would readmit exactly what anchoring removes: a green file says nothing
    about whether the anchors were even collected, and under ``-x`` a mutation could be credited to a
    neighbour the baseline had covered.
    """
    proc = subprocess.run(
        [PY, "-m", "pytest", *anchors, "-q", "--no-header", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=sanitized_env(),
    )
    return proc.returncode, last_line(proc)


def classify(name: str, code: str, target: tuple[str, ...]) -> tuple[str, str]:
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
    """Run every mutation against its own anchors, and fail unless each scored what it declared."""
    missing = verify_anchors()
    if missing:
        print("HARNESS ERROR: an anchor names a test pytest does not collect, so it proves nothing:")
        for item in missing:
            print(f"  {item}")
        return 2

    dirty = []
    for anchors in sorted({anchors for anchors, _code in MUTATIONS.values()}):
        code, summary = baseline(anchors)
        print(f"BASELINE exit={code}  {summary:34s} {' + '.join(a.split('::')[-1] for a in anchors)}")
        if code != 0:
            dirty.append(anchors)
    if dirty:
        print("\nHARNESS ERROR: an anchor baseline is not clean, so no verdict on it is trustworthy:", dirty)
        return 2

    print()
    wrong = []
    for name, (anchors, code) in MUTATIONS.items():
        verdict, detail = classify(name, code, anchors)
        expected = EXPECTED[name]
        ok = verdict.rstrip("*") == expected
        print(f"{verdict:13s} {'' if ok else f'(EXPECTED {expected}) '}{name:50s} -> {detail}")
        if not ok:
            wrong.append(f"{name}: expected {expected}, got {verdict}")
    print()
    if wrong:
        print("MUTATIONS THAT DID NOT SCORE AS DECLARED:")
        for item in wrong:
            print(f"  {item}")
        return 1
    print(
        f"all {len(MUTATIONS)} mutations scored as declared, each against its OWN anchor(s) "
        f"({sum(1 for v in EXPECTED.values() if v == 'CAUGHT')} caught, "
        f"{sum(1 for v in EXPECTED.values() if v == 'SURVIVED')} cosmetic controls survived, "
        f"{sum(1 for v in EXPECTED.values() if v == 'INVALID')} absent-anchor controls invalid)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
