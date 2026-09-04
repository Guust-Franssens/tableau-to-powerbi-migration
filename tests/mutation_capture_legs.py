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
    anchors_that_missed,
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
COMPLETE = "tests/test_payload_completeness.py"

# ⚠️ PROBE: proof that the mutated BODY ran, not merely that a test went red.
#
# A mutation is only evidence if the replaced behaviour actually executed. A red test can also mean
# the patch crashed on import, or that a fixture happened to break -- this repository has measured
# both, and once scored 22/22 "caught" where every one was an import error. `observed_mutation()`
# answers "did a named test fail in its call phase", which is necessary and not sufficient.
#
# So every mutation added for review round 3 calls `_probe("<its own name>")` from INSIDE the branch
# whose behaviour it changes, and `PROBED` declares the tag that must appear. Absent tag -> the
# verdict is refused, however red the suite went. The prelude is prepended to EVERY mutation (it is
# inert if unused), so an older mutation is unaffected and can be probed later without ceremony.
PROBE = ROOT / "tests" / "_mutation_probe.txt"

PROBE_PRELUDE = f"""
from pathlib import Path as _ProbePath
_PROBE_FILE = _ProbePath(r"{PROBE}")


def _probe(tag):
    with _PROBE_FILE.open("a", encoding="utf-8") as _fh:
        _fh.write(tag + "\\n")

"""

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
    # ------------------------------------------- review round 6: PROVENANCE, not merely freshness
    #
    # ⚠️ Both of these were fail-OPEN, and both produced a manifest that reported itself entirely
    # healthy while carrying evidence that did not belong to what it claimed to describe.
    "batches-from-different-tenants-are-merged": (
        ("tests/test_group_oracle_multi_batch.py::test_batches_from_DIFFERENT_tenants_are_refused_rather_than_merged",),
        """
import group_oracle_by_workbook as g
def refuse(batches):
    # Cross-tenant evidence mixing: two sites sharing only a workbook CAPTION folded into one
    # manifest that declared tenant B's server/site while its views carried tenant A's artifacts.
    return None
g._refuse_incompatible_sources = refuse
""",
    ),
    "an-unrecorded-source-merges-with-anything": (
        (
            "tests/test_group_oracle_multi_batch.py::test_a_manifest_that_records_NO_source_cannot_be_merged_with_one_that_does",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._refuse_incompatible_sources
def refuse(batches):
    # Treat an absent server/site as a WILDCARD rather than as its own identity -- which lets
    # precisely the batch we know least about merge with anything, the wrong direction for a guard
    # whose failure mode is presenting one customer's data as another's.
    known = [b for b in batches if g._source_identity(b.manifest) is not None]
    return _orig(known) if known else None
g._refuse_incompatible_sources = refuse
""",
    ),
    "a-stale-render-is-promoted-across-revisions": (
        (
            "tests/test_group_oracle_multi_batch.py::test_an_older_render_of_a_DIFFERENT_revision_is_not_promoted_beside_newer_data",
        ),
        """
import group_oracle_by_workbook as g
def revision(view):
    # Collapse every revision to one value, so any batch can supply any leg -- the pre-fix
    # behaviour. An old `image: ok` then lands beside new data under the NEW revision's
    # `updated_at`, and the manifest reports data_ok=1, image_ok=1, render_unestablished=0.
    return "same"
g._revision = revision
""",
    ),
    "an-unknown-revision-counts-as-a-match": (
        (
            "tests/test_group_oracle_multi_batch.py::test_a_view_whose_revision_is_UNKNOWN_does_not_promote_across_batches",
        ),
        """
import group_oracle_by_workbook as g
def revision(view):
    # The subtler half: keep the gate but let a MISSING `updated_at` satisfy it. "We cannot tell"
    # silently becomes "they are the same", which is the inference the gate exists to refuse -- and
    # a server that omits `updatedAt` is a real shape, not a hypothetical one.
    return view.get("updated_at") or "unknown"
g._revision = revision
""",
    ),
    "a-refused-stale-leg-is-dropped-instead-of-marked": (
        (
            "tests/test_group_oracle_multi_batch.py::test_a_refused_stale_render_is_RECORDED_rather_than_silently_dropped",
        ),
        """
import group_oracle_by_workbook as g
def blocked(refused, kind, roots):
    # Refuse the promotion and say nothing. The merge is then CORRECT and unreadable: "an older
    # revision has a render for this view" is the fact that decides whether to re-capture, and a
    # silent absence turns a known gap into an unknown one.
    return None
g._first_blocked_by_revision = blocked
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
# `close` alone. ⚠️ The original comment here explained this as "close does not interrupt a
# peer-blocked recv on Windows", which is FALSE as a general statement -- re-measured, close alone
# aborts a bare recv in 0.254s while shutdown alone does not. The real asymmetry is the stream: a
# `makefile()` read (how http.client takes the status line and headers) defers the underlying close
# through `SocketIO`'s refcount, so against a trickling peer shutdown aborts in 0.257s and close
# does not abort at all. Both calls are load-bearing, in phases that do not overlap; the bare-recv
# half is pinned by `abort-drops-close-so-a-bare-recv-runs-on`.
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
    "watchdog-armed-after-the-connection-sequence": (
        (
            "tests/test_tableau_http_deadline.py::test_a_proxy_that_trickles_its_CONNECT_response_is_bounded",
            "tests/test_tableau_http_deadline.py::test_the_watchdog_is_armed_before_TLS_negotiation",
        ),
        """
import tableau_http as h
def connect(self):
    # Round 4, finding 1: arm only once the whole connection sequence is done. With the corrected
    # HTTPS MRO that `super()` is `HTTPSConnection.connect`, so TCP setup, the proxy CONNECT
    # exchange and the entire TLS handshake run outside the watchdog.
    super(h._DeadlineHTTPConnection, self).connect()
    self._t2p_armed_sock = self.sock
    self._t2p_timer = h._arm_watchdog(self.sock, self._t2p_deadline)
h._DeadlineHTTPConnection.connect = connect
""",
    ),
    "watchdog-not-repointed-when-tls-replaces-the-socket": (
        ("tests/test_tableau_http_deadline.py::test_the_watchdog_is_armed_before_TLS_negotiation",),
        """
import tableau_http as h
def rearm(self):
    # Arming early WITHOUT re-pointing trades one blind phase for a later one: `wrap_socket`
    # detaches the raw socket, so the early watchdog is inert from the handshake onwards -- which
    # is where the status line, the headers and the body are read.
    return None
h._DeadlineHTTPConnection._rearm_if_the_socket_was_replaced = rearm
""",
    ),
    "abort-drops-shutdown-so-a-trickling-stream-runs-on": (
        ("tests/test_tableau_http_deadline.py::test_the_abort_covers_a_bare_recv_not_only_a_makefile_stream",),
        """
import socket
import tableau_http as h
def abort(sock):
    # ⚠️ A NEGATIVE-CONTROL pairing, not a duplicate: `watchdog-closes-instead-of-shutting-down`
    # drops `shutdown` and is killed by the makefile-stream anchor; this drops `close` and must be
    # killed by the bare-recv anchor. Either alone would let half of `_abort_socket` be deleted
    # unnoticed, because every other socket test in the file reads through `makefile()`.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
h._abort_socket = abort
""",
    ),
    # ------------------------------------------------- review round 6: the truncation fail-OPEN
    #
    # ⚠️ These four are one defect at two layers, and the layers are NOT redundant. The transport
    # catches a peer that fell short of its own `Content-Length`; the capture catches a payload that
    # is structurally incomplete however it was framed. Removing either alone must be caught, which
    # is exactly what an earlier draft got wrong: with only the capture-layer anchor, deleting the
    # capture check SURVIVED, because the transport had already refused the body and the guard was
    # never reached.
    "eof-ignores-an-outstanding-content-length": (
        # ⚠️ ONE anchor. The end-to-end companion
        # `test_a_render_the_peer_TRUNCATED_is_never_recorded_as_evidence` was declared here too
        # and does NOT observe this mutation -- measured with each anchor run alone (#480 round 4):
        # `observed=True exit=1` for the transport anchor, `observed=False exit=0 (1 passed)` for
        # the end-to-end one. It is the same non-redundancy the comment above describes, in the
        # other direction: remove the TRANSPORT check and the truncated body is still refused by
        # the FORMAT check (8 bytes with a valid magic number is not a complete PNG), so the
        # end-to-end record still reads `status != ok` and the anchor passes. Both anchors ran in
        # one `-x` invocation until round 4, so the first one's failure ended the run and this was
        # never visible.
        (
            "tests/test_tableau_http_deadline.py::test_the_truncation_is_caught_by_the_TRANSPORT_not_only_by_the_format_check",
        ),
        """
import http.client
import tableau_http as h
_orig = h._read_bounded
def read_bounded(stream, deadline, timeout):
    # THE blocker, verbatim: at EOF, do not ask whether the peer still owes bytes. `read1` does not
    # raise `IncompleteRead` on a premature close -- it calls `_close_conn()` and returns b"" -- so
    # 8 bytes of a declared 1024 were returned as HTTP 200 and persisted as a complete PNG.
    try:
        return _orig(stream, deadline, timeout)
    except http.client.IncompleteRead as exc:
        return exc.partial
h._read_bounded = read_bounded
""",
    ),
    "renders-credited-on-a-magic-number-alone": (
        (
            "tests/test_tableau_http_deadline.py::test_a_STRUCTURALLY_incomplete_render_is_refused_even_when_the_transport_is_satisfied",
            "tests/test_payload_completeness.py::test_a_png_cut_short_at_any_offset_is_refused",
        ),
        """
import tableau_payload_facts as f
def complete(kind, payload):
    # The pre-fix check: a leading signature IS the verdict. Eight bytes of PNG magic then satisfy
    # `format_matches`, get written to disk, and are recorded `status: ok` with a SHA-256 beside
    # them -- while `render_unestablished` reports 0 for a view with no usable reference at all.
    return True, ""
f.payload_is_complete = complete
""",
    ),
    "completeness-skipped-at-the-capture-seam": (
        (
            "tests/test_tableau_http_deadline.py::test_a_STRUCTURALLY_incomplete_render_is_refused_even_when_the_transport_is_satisfied",
        ),
        """
import capture_tableau_oracle as o
_orig = o.payload_is_complete
def complete(kind, payload):
    # The other half: the checker is correct and nobody calls it. Distinct from the mutation above
    # because a future refactor could keep `payload_is_complete` perfect and drop the call site --
    # and `_capture_render` is the ONLY place that writes the file and stamps `status: ok`.
    return True, ""
o.payload_is_complete = complete
""",
    ),
    "svg-completeness-trusts-the-root-element-not-the-parse": (
        ("tests/test_payload_completeness.py::test_an_svg_cut_short_is_refused_although_its_root_element_is_perfect",),
        """
import tableau_payload_facts as f
import tableau_render_capability as c
def svg_complete(payload):
    # A truncated SVG still opens with a flawless `<svg ...>`, so a root-element check cannot see
    # that the document never ends. Only parsing to the last byte can.
    return (True, "") if c.looks_like_svg(payload) else (False, "expected an <svg> root")
f._COMPLETENESS_CHECKS["svg"] = svg_complete
""",
    ),
    # ------------------------------------- review round 3: a MARKER SCAN standing in for a parser
    #
    # ⚠️ One defect class, found three times in three formats. Each mutation restores one instance of
    # it, and each is killed by a test written against the CLASS rather than the instance -- which is
    # the point: fixing them one at a time is what produced three review rounds.
    "pdf-completeness-searches-a-tail-window": (
        (
            "tests/test_payload_completeness.py::test_a_pdf_truncated_inside_a_LATER_revision_is_refused",
            "tests/test_payload_completeness.py::test_no_completeness_check_can_be_satisfied_by_bytes_that_are_not_the_end",
        ),
        """
import tableau_payload_facts as f
def pdf_complete(payload):
    # THE round-3 blocker, verbatim: accept any `%%EOF` within the last 2 KiB. A PDF carries one per
    # incremental revision, so a download cut during a later revision still holds an earlier marker
    # -- measured 105 bytes from the end -- and an independent parser then reads the PRIOR revision.
    if not payload.startswith(b"%PDF-"):
        return False, "the PDF header is missing"
    if b"%%EOF" not in payload[-2048:]:
        return False, "the PDF has no %%EOF trailer in its final 2048 byte(s), so it was cut short"
    return True, ""
f._COMPLETENESS_CHECKS["pdf"] = pdf_complete
""",
    ),
    "pdf-startxref-is-never-resolved": (
        ("tests/test_payload_completeness.py::test_a_pdf_whose_startxref_does_not_resolve_is_refused",),
        """
import tableau_payload_facts as f
def pdf_complete(payload):
    # The half-fix: require `%%EOF` to END the file, but never follow the pointer before it. A file
    # whose final startxref is missing, non-numeric, or aimed outside itself then reads as complete.
    if not payload.startswith(b"%PDF-"):
        return False, "the PDF header is missing"
    if not payload.rstrip().endswith(b"%%EOF"):
        return False, "the PDF does not END with %%EOF, so it was cut short"
    return True, ""
f._COMPLETENESS_CHECKS["pdf"] = pdf_complete
""",
    ),
    "svg-entity-scan-bounded-by-the-first-svg": (
        # ⚠️ ONE anchor. `test_no_external_entity_can_reach_the_network` was declared beside it and
        # cannot observe this mutation, because its fixture puts the `<!DOCTYPE ... <!ENTITY ...>`
        # BEFORE the first `<svg` -- exactly where a scan bounded by the first `<svg` still looks.
        # Measured with each anchor alone (#480 round 4): `observed=True exit=1` for the
        # WHEREVER_it_sits anchor, `observed=False exit=0 (1 passed)` for the network one. The
        # anchor that pins this behaviour is the one that moves the declaration; the SSRF anchor
        # pins a different property and keeps its own mutation.
        ("tests/test_payload_completeness.py::test_an_entity_declaration_is_refused_WHEREVER_it_sits",),
        """
import re
from xml.etree import ElementTree
import tableau_payload_facts as f
_ENTITY = re.compile(rb"<!ENTITY", re.I)
def svg_complete(payload):
    # The round-3 SECURITY blocker: bound the entity scan by the first raw `<svg`. An attacker moves
    # that boundary with `<!-- <svg -->` or `<?d <svg ?>`, the real DTD falls outside it, and expat
    # expands the entity -- measured 1000 characters -- while the artifact is credited as evidence.
    root_at = payload.find(b"<svg")
    prolog = payload[: root_at if root_at >= 0 else 65536]
    if _ENTITY.search(prolog):
        return False, "the SVG declares XML entities, which this parser refuses to expand"
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        return False, f"the SVG is not well-formed XML (expat error {exc.code} at line 1)"
    except (LookupError, ValueError):
        return False, "the SVG declares an encoding this parser cannot decode"
    if root.tag.rsplit("}", 1)[-1] != "svg":
        return False, "the SVG document parses but its root element is not <svg>"
    return True, ""
f._COMPLETENESS_CHECKS["svg"] = svg_complete
""",
    ),
    "svg-encoding-family-is-not-caught": (
        (
            "tests/test_payload_completeness.py::test_an_encoding_the_parser_cannot_decode_becomes_a_verdict_not_a_crash",
        ),
        """
from xml.parsers import expat
import tableau_payload_facts as f
class _Refused(Exception):
    pass
def svg_complete(payload):
    # The round-3 MEDIUM, verbatim: handle the parser's OWN error family and nothing else. Measured,
    # `LookupError` (carrying the declared encoding verbatim) and `ValueError: multi-byte encodings
    # are not supported` are NOT `ExpatError`, so both escape and crash the capture instead of
    # becoming a non-ok leg.
    #
    # ⚠️ Re-implemented rather than wrapped: an earlier version of this mutation delegated to the
    # FIXED function, which catches the family internally, so the exception never reached the
    # wrapper's `except` and the mutation SURVIVED while claiming to restore the defect.
    root = []
    def start_element(name, attributes):
        if not root:
            root.append(name)
    def entity_declaration(name, is_param, value, base, system_id, public_id, notation):
        raise _Refused
    def external_entity_reference(context, base, system_id, public_id):
        raise _Refused
    parser = expat.ParserCreate()
    parser.StartElementHandler = start_element
    parser.EntityDeclHandler = entity_declaration
    parser.ExternalEntityRefHandler = external_entity_reference
    try:
        parser.Parse(payload, True)
    except _Refused:
        return False, "the SVG declares XML entities, which this parser refuses to expand"
    except expat.ExpatError as exc:
        return False, f"the SVG is not well-formed XML (expat error {exc.code} at line {exc.lineno})"
    if not root:
        return False, "the SVG document contains no element at all"
    if root[0].rpartition(":")[2] != "svg":
        return False, "the SVG document parses but its root element is not <svg>"
    return True, ""
f._COMPLETENESS_CHECKS["svg"] = svg_complete
""",
    ),
    "svg-parse-is-not-required-to-reach-the-end": (
        # ⚠️ ONE anchor. The cross-format invariant
        # `test_no_completeness_check_can_be_satisfied_by_bytes_that_are_not_the_end` was declared
        # beside it and cannot observe this mutation: it APPENDS junk to a complete payload, and
        # `Parse(payload, False)` still raises on bytes after the root element, so the refusal
        # stands. Two thirds of its parametrizations are png/pdf, which never reach the SVG path at
        # all. Measured with each anchor alone (#480 round 4): `observed=True exit=1` for the
        # cut-short anchor, `observed=False exit=0 (12 passed)` for the invariant. Truncation and
        # trailing junk are different claims; only the first one pins `isfinal=True`.
        ("tests/test_payload_completeness.py::test_an_svg_cut_short_is_refused_although_its_root_element_is_perfect",),
        """
from xml.parsers import expat
import tableau_payload_facts as f
def svg_complete(payload):
    # The subtlest form: parse, but not to the END. `Parse(payload, False)` leaves the document open,
    # so a truncation is indistinguishable from a well-formed prefix and junk appended after the root
    # is never reached. Entity refusal still works, so this passes every OTHER svg test in the suite.
    root = []
    def start_element(name, attributes):
        if not root:
            root.append(name)
    def entity_declaration(name, is_param, value, base, system_id, public_id, notation):
        raise ValueError("entity")
    parser = expat.ParserCreate()
    parser.StartElementHandler = start_element
    parser.EntityDeclHandler = entity_declaration
    try:
        parser.Parse(payload, False)
    except ValueError:
        return False, "the SVG declares XML entities, which this parser refuses to expand"
    except expat.ExpatError as exc:
        return False, f"the SVG is not well-formed XML (expat error {exc.code} at line {exc.lineno})"
    except LookupError:
        return False, "the SVG declares an encoding this parser cannot decode"
    if not root:
        return False, "the SVG document contains no element at all"
    if root[0].rpartition(":")[2] != "svg":
        return False, "the SVG document parses but its root element is not <svg>"
    return True, ""
f._COMPLETENESS_CHECKS["svg"] = svg_complete
""",
    ),
    "tls-handshake-runs-before-the-watchdog-can-reach-it": (
        (
            "tests/test_tableau_http_deadline.py::test_a_trickling_TLS_handshake_is_bounded_by_the_deadline",
            "tests/test_tableau_http_deadline.py::test_the_watchdog_is_armed_before_TLS_negotiation",
        ),
        """
import tableau_http as h
def wrap_socket(self, sock, server_hostname=None):
    # The pre-fix behaviour: handshake INSIDE `wrap_socket`, while the raw socket the watchdog
    # points at has already been detached (`fileno() == -1`) and the SSLSocket does not yet exist.
    # The only bound left is the socket timeout, which RESTARTS at the handshake rather than
    # counting down the remaining budget -- measured 0.167s over a 0.16s deadline.
    return self._context.wrap_socket(sock, server_hostname=server_hostname)
h._DeferredHandshakeContext.wrap_socket = wrap_socket
""",
    ),
    "tls-handshake-keeps-a-full-per-phase-timeout": (
        ("tests/test_tableau_http_deadline.py::test_the_handshake_timeout_is_narrowed_to_the_REMAINING_budget",),
        """
import tableau_http as h
def connect(self):
    # Defer the handshake CORRECTLY -- the SSLSocket exists and the watchdog is re-pointed -- and
    # then hand it whatever per-phase timeout the socket already carries instead of what is left of
    # the budget. The abort path looks right and the ceiling is silently the old one: a per-phase
    # timeout that RESTARTS at the handshake is what put a 0.16s deadline 0.167s over.
    if self._t2p_deadline is None:
        return super(h._DeadlineHTTPSConnection, self).connect()
    real_context = self._context
    self._context = h._DeferredHandshakeContext(real_context)
    try:
        super(h._DeadlineHTTPSConnection, self).connect()
    finally:
        self._context = real_context
    self.sock.do_handshake()
h._DeadlineHTTPSConnection.connect = connect
""",
    ),
    # ------------------------------------ review round 3: "CANNOT ESTABLISH" IS ITS OWN STATE.
    #
    # ⚠️ Every mutation below calls `_probe(...)` from inside the branch it changes. Its tag must
    # appear in the probe file or the verdict is refused, however red the suite went -- a red test
    # proves a test failed, never that the mutated body ran.
    "two-unknown-sources-compare-equal": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_TWO_anonymous_captures_do_not_merge_just_because_both_are_anonymous",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._source_identity
def identity(manifest):
    # THE blocker, verbatim: map a missing identity onto ("", "") so two anonymous manifests produce
    # ONE identity, the cardinality check sees no disagreement, and they merge.
    found = _orig(manifest)
    if found is None:
        _probe("two-unknown-sources-compare-equal")
        return "", ""
    return found
g._source_identity = identity
""",
    ),
    "an-empty-site-is-treated-as-unrecorded": (
        ("tests/test_group_oracle_multi_batch.py::test_a_recorded_but_EMPTY_site_is_the_default_site_not_an_absence",),
        """
import group_oracle_by_workbook as g
_orig = g._source_identity
def identity(manifest):
    # The OVER-correction, and the reason the guard tests field PRESENCE rather than truthiness:
    # `""` is Tableau Server's documented DEFAULT SITE, so treating it as an absence refuses every
    # legitimate Default-site merge. A rule that fires on everything is as useless as one that never
    # fires, and only this second mutation can tell the two apart.
    found = _orig(manifest)
    if found is not None and not found[1]:
        _probe("an-empty-site-is-treated-as-unrecorded")
        return None
    return found
g._source_identity = identity
""",
    ),
    "the-two-source-refusals-are-collapsed-into-one-type": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_an_unestablished_source_is_NOT_reported_as_a_tenant_disagreement",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._refuse_incompatible_sources
def refuse(batches):
    # "These are two tenants" and "we cannot tell whether these are two tenants" are different
    # answers. One shared exception type still blocks, so every exit-code assertion keeps passing --
    # and the fail-open collapse could return unnoticed, because no test could name which guard fired.
    try:
        return _orig(batches)
    except g.UnestablishedBatchSource as exc:
        _probe("the-two-source-refusals-are-collapsed-into-one-type")
        raise g.IncompatibleBatchSources(str(exc)) from None
g._refuse_incompatible_sources = refuse
""",
    ),
    # ------------------------------------ review round 3: PROMOTION IS RECONCILED, NOT LAYERED.
    "a-refused-artifact-is-left-on-disk": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_a_REFUSED_old_revision_artifact_does_not_stay_in_reference_images",
        ),
        """
import group_oracle_by_workbook as g
def reconcile(destination, written, previous, *, dry_run):
    # THE blocker: copy the winners and reconcile nothing. The manifest still correctly reports the
    # stale render as unpromoted -- and the old-revision PNG stays in reference/images/, so a
    # consumer listing the DIRECTORY gets evidence the merge explicitly rejected.
    _probe("a-refused-artifact-is-left-on-disk")
    return [], []
g._reconcile_destination = reconcile
""",
    ),
    "reconciliation-deletes-what-it-cannot-attribute": (
        ("tests/test_group_oracle_multi_batch.py::test_reconciliation_removes_only_files_a_PREVIOUS_grouping_named",),
        """
import group_oracle_by_workbook as g
from pathlib import Path
def reconcile(destination, written, previous, *, dry_run):
    # The OVER-correction: "replace" read as "delete anything I did not write". reference/{images,data}/
    # is this script's tree, but not everything in it is ours -- silently removing a hand-dropped
    # reference is a worse failure than the one being fixed.
    _probe("reconciliation-deletes-what-it-cannot-attribute")
    on_disk = set()
    for _kind, sub in g.RENDER_LEGS:
        folder = destination / sub
        if folder.is_dir():
            on_disk |= {f"{sub}/{c.name}" for c in folder.iterdir() if c.is_file()}
    extra = sorted(on_disk - written)
    if not dry_run:
        for relative in extra:
            (destination / relative).unlink(missing_ok=True)
    return extra, []
g._reconcile_destination = reconcile
""",
    ),
    "unattributed-files-are-accepted-silently": (
        ("tests/test_group_oracle_multi_batch.py::test_reconciliation_removes_only_files_a_PREVIOUS_grouping_named",),
        """
import group_oracle_by_workbook as g
_orig = g._reconcile_destination
def reconcile(destination, written, previous, *, dry_run):
    # Keep the removal, drop the report. The dangerous half is quiet: a folder holding bytes no
    # manifest accounts for reads as a clean promotion, and the exit code says everything landed.
    _probe("unattributed-files-are-accepted-silently")
    removed, _unattributed = _orig(destination, written, previous, dry_run=dry_run)
    return removed, []
g._reconcile_destination = reconcile
""",
    ),
    "a-dry-run-reconciles-anyway": (
        ("tests/test_group_oracle_multi_batch.py::test_a_dry_run_reconciles_NOTHING",),
        """
import group_oracle_by_workbook as g
_orig = g._reconcile_destination
def reconcile(destination, written, previous, *, dry_run):
    # Deleting IS writing. A dry run that removes files is not a dry run, and this is the one
    # mutation whose damage is invisible in the manifest it produces.
    _probe("a-dry-run-reconciles-anyway")
    return _orig(destination, written, previous, dry_run=False)
g._reconcile_destination = reconcile
""",
    ),
    # ------------------------------------ review round 3: WORKBOOKS ARE KEYED BY LUID.
    "views-are-bucketed-by-workbook-NAME": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_two_DIFFERENT_workbooks_normalizing_onto_one_folder_are_both_refused",
        ),
        """
import group_oracle_by_workbook as g
def group(manifest):
    # THE blocker, verbatim: bucket by display name. Two distinct LUIDs whose names normalize onto
    # one key merge into one bucket, and nothing downstream can tell they were ever two workbooks.
    _probe("views-are-bucketed-by-workbook-NAME")
    buckets = {}
    for view in manifest.get("views", []):
        buckets.setdefault(view.get("workbook_name") or "", []).append(view)
    return buckets
g.group_views = group
""",
    ),
    "a-destination-claimed-twice-is-written-anyway": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_two_DIFFERENT_workbooks_normalizing_onto_one_folder_are_both_refused",
        ),
        """
import group_oracle_by_workbook as g
def contested(resolved):
    # Keep the LUID keying and lose the collision detection: each folder reports exactly one
    # claimant, so both workbooks are written in sequence and the second manifest overwrites the
    # first while both workbooks' files remain. Exit 0, no warning.
    _probe("a-destination-claimed-twice-is-written-anyway")
    return {item.folder: [item.luid] for item in resolved}
g._contested = contested
""",
    ),
    "every-shared-parent-counts-as-a-collision": (
        ("tests/test_group_oracle_multi_batch.py::test_two_workbooks_with_their_OWN_folders_still_group",),
        """
import group_oracle_by_workbook as g
def contested(resolved):
    # The OVER-correction: refuse any capture carrying more than one workbook. It passes the
    # collision test and breaks every ordinary estate -- which is why the negative control exists.
    _probe("every-shared-parent-counts-as-a-collision")
    everyone = [item.luid for item in resolved]
    return {item.folder: everyone for item in resolved}
g._contested = contested
""",
    ),
    "a-missing-workbook-luid-falls-back-to-the-name": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_a_view_with_NO_workbook_luid_is_refused_rather_than_bucketed_by_name",
        ),
        """
import group_oracle_by_workbook as g
def group(manifest):
    # The subtle half: keep LUID keying but let a MISSING LUID fall back to the display name. "We
    # cannot tell which workbook this is" silently becomes an identity again, which is the whole
    # defect class -- an unassessable input collapsing into the clean bucket.
    buckets = {}
    for view in manifest.get("views", []):
        key = view.get("workbook_luid")
        if not key:
            _probe("a-missing-workbook-luid-falls-back-to-the-name")
            key = view.get("workbook_name") or ""
        buckets.setdefault(key, []).append(view)
    return buckets
g.group_views = group
""",
    ),
    # ------------------------------------ review round 3: EVERY BATCH ON DISK.
    "an-unlisted-batch-beside-a-given-one-is-skipped": (
        ("tests/test_group_oracle_multi_batch.py::test_a_batch_on_disk_that_was_not_passed_is_REFUSED_not_skipped",),
        """
import group_oracle_by_workbook as g
def siblings(listed, excluded):
    # THE moved boundary: read exactly the arguments the operator remembered. The third retry whose
    # PNG finally landed stays unread, and the merged manifest reports the earlier failure as current.
    _probe("an-unlisted-batch-beside-a-given-one-is-skipped")
    return None
g._refuse_unlisted_siblings = siblings
""",
    ),
    "any-sibling-directory-blocks-the-listed-mode": (
        ("tests/test_group_oracle_multi_batch.py::test_a_non_batch_sibling_does_not_block_the_LISTED_mode",),
        """
import group_oracle_by_workbook as g
def siblings(listed, excluded):
    # The OVER-correction: refuse on ANY sibling directory rather than on an unlisted capture BATCH.
    # `_runs/<run>/oracle` sits beside assessment/, bundle/ and scratch/, so this makes the listed
    # mode unusable on the layout this repo actually writes.
    _probe("any-sibling-directory-blocks-the-listed-mode")
    for path in {p.resolve().parent for p in listed}:
        for child in (c for c in path.iterdir() if c.is_dir()):
            if child.resolve() not in {p.resolve() for p in listed} and child.resolve() not in excluded:
                raise g.UnlistedBatchOnDisk(str(child))
g._refuse_unlisted_siblings = siblings
""",
    ),
    "discovery-skips-what-it-cannot-classify": (
        ("tests/test_group_oracle_multi_batch.py::test_a_directory_under_the_root_that_is_NOT_a_batch_blocks",),
        """
import group_oracle_by_workbook as g
def discover(root, excluded):
    # The boundary moved a THIRD time: from "every argument you typed" to "every directory I happened
    # to recognise". A half-written capture is then silently absent, which is the same defect in a
    # third costume -- and the only difference from the shipped rule is a `continue` instead of a raise.
    _probe("discovery-skips-what-it-cannot-classify")
    found = []
    root_is_batch = g.is_capture_batch(root)
    if root_is_batch:
        found.append(root)
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if child.resolve() in excluded:
            continue
        if root_is_batch and child.name in g.CAPTURE_SUBDIRS:
            continue
        if g.is_capture_batch(child):
            found.append(child)
    return found
g.discover_batches = discover
""",
    ),
    "any-oracle_manifest_json-counts-as-a-capture-batch": (
        ("tests/test_group_oracle_multi_batch.py::test_a_GROUPED_manifest_is_not_mistaken_for_a_capture_batch",),
        """
import group_oracle_by_workbook as g
def is_batch(directory):
    # Match on the FILENAME rather than the schema. `migrations/workbooks/<slug>/reference/` carries a
    # file with exactly that name -- this script's own OUTPUT -- so discovery would feed output into
    # input, and a per-workbook subset would be merged as though it were a capture.
    _probe("any-oracle_manifest_json-counts-as-a-capture-batch")
    return (directory / g.MANIFEST_NAME).is_file()
g.is_capture_batch = is_batch
""",
    ),
    "exclude-is-accepted-and-ignored": (
        ("tests/test_group_oracle_multi_batch.py::test_exclude_is_the_ONE_auditable_escape_and_is_recorded",),
        """
import group_oracle_by_workbook as g
_orig = g.resolve_batch_dirs
def resolve(oracle, oracle_root, exclude):
    # The flag parses, the refusal still fires, and nothing an operator excluded is actually excluded.
    # The auditable escape becomes an unusable one -- fail-CLOSED, but it makes the guard un-shippable.
    _probe("exclude-is-accepted-and-ignored")
    return _orig(oracle, oracle_root, ())
g.resolve_batch_dirs = resolve
""",
    ),
    # ------------------------------------ review round 3, finding 4: THE EXIT CODE.
    "a-stale-refusal-does-not-reach-the-exit-code": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_a_REFUSED_old_revision_artifact_does_not_stay_in_reference_images",
        ),
        """
import group_oracle_by_workbook as g
def incomplete(outcomes, manifest):
    # The shipped behaviour before finding 4: warn about the refused cross-revision leg, persist the
    # count, and return 0. A gate reading only the exit code is told everything landed.
    _probe("a-stale-refusal-does-not-reach-the-exit-code")
    return bool(any(outcomes[b] for b in g.OUTCOME_BUCKETS if b != "grouped"))
g._incomplete = incomplete
""",
    ),
    "every-run-reports-incomplete": (
        ("tests/test_group_oracle_multi_batch.py::test_an_UNCHANGED_re_run_removes_nothing",),
        """
import group_oracle_by_workbook as g
def incomplete(outcomes, manifest):
    # The OVER-correction. An exit code that is always 1 carries no information, and would pass every
    # single "must not return 0" assertion in this file.
    _probe("every-run-reports-incomplete")
    return True
g._incomplete = incomplete
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
def report(inputs, outcomes):
    out = _orig(inputs, outcomes)
    g.LOG.info("cosmetic extra line, asserted on by nothing")
    return out
g._write_grouping_report = report
""",
    ),
    # ------------------------- review round 7: AN UNASSESSABLE MANIFEST IS NOT A CLEAN MERGE.
    # Both mutations below restore a measured fail-open verbatim; the two after them are the
    # matching OVER-corrections, because a validator that refuses legitimate captures would block
    # the customer multi-batch merge this whole change exists to deliver.
    "a-manifest-is-parsed-but-not-validated": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_an_empty_json_object_is_refused_rather_than_grouped_as_zero_workbooks",
            "tests/test_group_oracle_multi_batch.py"
            "::test_a_manifest_with_no_views_key_is_refused_even_when_its_schema_is_right",
            "tests/test_group_oracle_multi_batch.py::test_a_JSON_LIST_manifest_exits_2_rather_than_CRASHING_at_1",
        ),
        """
import json
from pathlib import Path
import group_oracle_by_workbook as g
def load(oracle_dir):
    # THE defect, verbatim: reading is accepting. `{}` and a schema-carrying manifest with no `views`
    # then report a CLEAN merge (exit 0, "0 grouped"), and `[]` dies on `.get` inside merge_batches
    # at exit 1 -- the "grouped what it could" code, for an input that was never grouped at all.
    _probe("a-manifest-is-parsed-but-not-validated")
    path = oracle_dir / g.MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no {g.MANIFEST_NAME} in {oracle_dir}")
    return json.loads(path.read_text(encoding="utf-8"))
g.load_manifest = load
""",
    ),
    "a-missing-view_luid-collapses-onto-the-empty-string": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_TWO_views_with_no_view_luid_are_refused_rather_than_COLLAPSED_into_one",
            "tests/test_group_oracle_multi_batch.py::test_ONE_anonymous_view_beside_an_identified_one_is_refused_too",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._validate_manifest
def validate(path, payload):
    # THE second defect: keep every SHAPE check and drop only the identity one, so the manifest is
    # well-formed by every other measure and `merge_batches` still buckets on `view_luid or ""` --
    # a coercion this mutation deliberately does NOT touch, because it is still in the shipped code
    # and is exactly what makes a missing identity lossy. Two anonymous views -> one bucket,
    # newest-wins, one view out, exit 0.
    _probe("a-missing-view_luid-collapses-onto-the-empty-string")
    try:
        return _orig(path, payload)
    except g.UnidentifiedCaptureView:
        return payload
g._validate_manifest = validate
""",
    ),
    "an-EMPTY-views-list-is-refused-as-damaged": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_an_EMPTY_views_list_is_a_true_statement_about_the_data_and_is_NOT_refused",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._validate_manifest
def validate(path, payload):
    # The OVER-correction, and the reason the guard tests the KEY rather than truthiness: a capture
    # whose filter selected nothing really does write `"views": []` and exits 0. Refusing it turns an
    # honest empty capture into a blocking error while catching nothing the shipped rule misses.
    _probe("an-EMPTY-views-list-is-refused-as-damaged")
    found = _orig(path, payload)
    if not found["views"]:
        raise g.MalformedCaptureManifest(f"{path} carries no views")
    return found
g._validate_manifest = validate
""",
    ),
    "a-missing-workbook_luid-is-refused-at-load-too": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_a_missing_WORKBOOK_luid_is_still_BUCKETED_and_reported_not_refused_at_load",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._validate_manifest
def validate(path, payload):
    # The plausible OVER-correction: "both LUIDs are identity, so refuse both." It reads as symmetry
    # and it deletes a whole reporting path -- the `unidentified` outcome bucket, its REFUSAL_NO_LUID
    # record and its exit-1 verdict all become unreachable. The two fields differ in CONSEQUENCE: a
    # missing workbook_luid loses nothing, a missing view_luid destroys the record.
    _probe("a-missing-workbook_luid-is-refused-at-load-too")
    found = _orig(path, payload)
    anonymous = [i for i, v in enumerate(found["views"]) if not v.get("workbook_luid")]
    if anonymous:
        raise g.UnidentifiedCaptureView(f"{path} holds {len(anonymous)} view(s) with no workbook_luid")
    return found
g._validate_manifest = validate
""",
    ),
    "the-shape-and-identity-refusals-are-collapsed-into-one-type": (
        (
            "tests/test_group_oracle_multi_batch.py"
            "::test_the_identity_refusal_is_its_OWN_type_not_a_generic_malformed_manifest",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._validate_manifest
def validate(path, payload):
    # "This file is not a capture manifest" and "this capture cannot say which view it captured" are
    # different answers. One shared type still exits 2, so every exit-code assertion keeps passing --
    # and no test could then name which guard fired, which is how the weaker one rots back open.
    _probe("the-shape-and-identity-refusals-are-collapsed-into-one-type")
    try:
        return _orig(path, payload)
    except g.UnidentifiedCaptureView as exc:
        raise g.MalformedCaptureManifest(str(exc)) from None
g._validate_manifest = validate
""",
    ),
    # ------------------------- review round 8: AN IDENTITY IS A TYPE, NOT A TRUTHINESS. The third
    # round of the same fail-open class, so the mutations below break the DECLARED TABLE rather than
    # any single predicate -- and the two over-corrections after them are the shapes a table makes
    # easy to get wrong: refusing `null` (which the real writer emits) and requiring a leg `path`
    # (which no failed capture has).
    "view_luid-is-tested-for-TRUTHINESS-not-type": (
        (
            "tests/test_group_oracle_multi_batch.py::test_a_view_luid_that_is_not_a_NON_EMPTY_STRING_is_refused_at_2",
            "tests/test_group_oracle_multi_batch.py"
            "::test_a_wrong_TYPED_view_luid_is_the_identity_refusal_not_a_generic_shape_one",
        ),
        """
import group_oracle_by_workbook as g
def identity(value):
    # THE defect, verbatim: `not view.get("view_luid")` answered "is there something there" instead
    # of "is it an identity". `123` is truthy and hashable, so it bucketed, merged and reported a
    # CLEAN run (measured: exit 0, "1 grouped"); `{...}` and `[...]` are truthy and unhashable and
    # crashed at exit 1. Every shape check above this line still passes, which is why the manifest
    # reads as well-formed right up to the fold.
    _probe("view_luid-is-tested-for-TRUTHINESS-not-type")
    return bool(value)
g._is_view_identity = identity
""",
    ),
    "the-consumed-leg-structures-are-not-typed": (
        (
            "tests/test_group_oracle_multi_batch.py::test_a_render_leg_that_is_a_SCALAR_is_refused_rather_than_crashing",
            "tests/test_group_oracle_multi_batch.py"
            "::test_an_OBJECT_valued_leg_path_is_refused_rather_than_joined_onto_a_Path",
        ),
        """
import group_oracle_by_workbook as g
def views(path, records):
    # The second half of the finding: identity typed, legs still trusted. `image: "junk"` died on
    # `.get` inside `_leg_is_promotable` and an object-valued `image.path` died on `root / path` --
    # both at exit 1, the "grouped what it could" code, for input that was never grouped.
    _probe("the-consumed-leg-structures-are-not-typed")
    return None
g._refuse_untyped_views = views
""",
    ),
    "the-type-table-is-declared-but-never-applied": (
        ("tests/test_group_oracle_multi_batch.py::test_EVERY_field_in_the_type_table_is_actually_enforced",),
        """
import group_oracle_by_workbook as g
def untyped(path, where, mapping, specs):
    # The failure mode a DECLARATIVE fix has that a predicate does not: a table that reads as
    # complete and is enforced nowhere. Every field name still appears in the source, so the census
    # test and every docstring stay green while nothing is actually checked.
    _probe("the-type-table-is-declared-but-never-applied")
    return None
g._refuse_untyped = untyped
""",
    ),
    "a-list-field-is-typed-only-as-a-LIST": (
        (
            "tests/test_group_oracle_multi_batch.py::test_a_LIST_field_is_typed_by_its_ELEMENTS_not_only_by_being_a_list",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._refuse_untyped
def untyped(path, where, mapping, specs):
    # The half-measure: check the container, trust the contents. `sorted(["png", 7])` raises, so
    # `requested_renders` with one wrong element still reaches exit 1 through the render-intent union.
    _probe("a-list-field-is-typed-only-as-a-LIST")
    return _orig(path, where, mapping, tuple(g._Typed(s.name, s.kind) for s in specs))
g._refuse_untyped = untyped
""",
    ),
    "JSON-null-is-treated-as-a-type-error": (
        ("tests/test_group_oracle_multi_batch.py::test_the_writers_OWN_nulls_are_not_type_errors",),
        """
import group_oracle_by_workbook as g
def untyped(path, where, mapping, specs):
    # THE over-correction, and it would refuse real captures rather than damaged ones: the writer
    # emits `"workbook_luid": workbook.get("id")`, so JSON null is what a REST response that omitted
    # the field produces. "Present but null" and "absent" are one statement, and only one of them
    # would still be accepted here.
    _probe("JSON-null-is-treated-as-a-type-error")
    for spec in specs:
        value = mapping.get(spec.name)
        if value is not None and not isinstance(value, spec.kind):
            raise g.MalformedCaptureManifest(f"{path}: {where} carries {spec.name!r} wrongly typed")
        if spec.name in mapping and mapping[spec.name] is None:
            raise g.MalformedCaptureManifest(f"{path}: {where} carries {spec.name!r} as null")
g._refuse_untyped = untyped
""",
    ),
    "a-leg-is-required-to-carry-a-path": (
        ("tests/test_group_oracle_multi_batch.py::test_a_FAILED_leg_carrying_no_path_is_still_a_legitimate_record",),
        """
import group_oracle_by_workbook as g
_orig = g._refuse_untyped_views
def views(path, records):
    # The second over-correction: typing a field slides easily into requiring it. A capture that
    # failed writes `{"status": "transient", "error": ..., "detail": ...}` with no `path` at all, so
    # this refuses every unsuccessful capture ever taken -- the exact evidence the grouping exists to
    # keep visible.
    _probe("a-leg-is-required-to-carry-a-path")
    _orig(path, records)
    for index, view in enumerate(records):
        for kind, _sub in g.RENDER_LEGS:
            leg = view.get(kind)
            if isinstance(leg, dict) and "path" not in leg:
                raise g.MalformedCaptureManifest(f"{path}: view {index}'s {kind} leg carries no path")
g._refuse_untyped_views = views
""",
    ),
    "control-untyped-field-nobody-reads": (
        (
            "tests/test_group_oracle_multi_batch.py::test_a_legitimate_multi_batch_merge_is_UNAFFECTED_by_the_new_validation",
        ),
        """
import group_oracle_by_workbook as g
_orig = g._refuse_untyped_views
def views(path, records):
    # NEGATIVE CONTROL: type a field this script never reads (`view_name`, carried but not consumed).
    # It must SURVIVE. If it were caught, the suite would be asserting on the validator's incidental
    # reach rather than on the fields the merge actually depends on -- and every future table entry
    # would then need a matching fixture edit for reasons nothing explains.
    _orig(path, records)
    for view in records:
        if view.get("view_name") is not None and not isinstance(view.get("view_name"), str):
            raise g.MalformedCaptureManifest(f"{path}: view_name is not a string")
g._refuse_untyped_views = views
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

# name -> the tag its mutated body must write. Only the round-3 mutations declare one, because only
# those were written with a `_probe(...)` call inside the branch they change; an older mutation has
# no tag and is scored exactly as before. A declared tag that does NOT appear refuses the verdict --
# which is stronger than "a test went red", the check that once reported 22/22 caught on 22 import
# errors. A NEGATIVE probe is asserted too, for the absent-anchor controls: they must not run.
#
# ⚠️ ENUMERATED, never derived from the code. Deriving it (`"_probe(" in code`) would make the
# harness's own guard fail open in the most likely way it will ever be attacked: delete the
# `_probe(...)` line while editing a mutation and the requirement deletes itself with it, silently
# returning that mutation to red-suite-only scoring. :func:`verify_probes` makes that a hard error in
# BOTH directions instead -- a probed mutation that lost its call, and a call nobody declared.
PROBED = frozenset(
    {
        "two-unknown-sources-compare-equal",
        "an-empty-site-is-treated-as-unrecorded",
        "the-two-source-refusals-are-collapsed-into-one-type",
        "a-refused-artifact-is-left-on-disk",
        "reconciliation-deletes-what-it-cannot-attribute",
        "unattributed-files-are-accepted-silently",
        "a-dry-run-reconciles-anyway",
        "views-are-bucketed-by-workbook-NAME",
        "a-destination-claimed-twice-is-written-anyway",
        "every-shared-parent-counts-as-a-collision",
        "a-missing-workbook-luid-falls-back-to-the-name",
        "an-unlisted-batch-beside-a-given-one-is-skipped",
        "any-sibling-directory-blocks-the-listed-mode",
        "discovery-skips-what-it-cannot-classify",
        "any-oracle_manifest_json-counts-as-a-capture-batch",
        "exclude-is-accepted-and-ignored",
        "a-stale-refusal-does-not-reach-the-exit-code",
        "every-run-reports-incomplete",
        "a-manifest-is-parsed-but-not-validated",
        "a-missing-view_luid-collapses-onto-the-empty-string",
        "an-EMPTY-views-list-is-refused-as-damaged",
        "a-missing-workbook_luid-is-refused-at-load-too",
        "the-shape-and-identity-refusals-are-collapsed-into-one-type",
        "view_luid-is-tested-for-TRUTHINESS-not-type",
        "the-consumed-leg-structures-are-not-typed",
        "the-type-table-is-declared-but-never-applied",
        "a-list-field-is-typed-only-as-a-LIST",
        "JSON-null-is-treated-as-a-type-error",
        "a-leg-is-required-to-carry-a-path",
    }
)


def verify_probes() -> list[str]:
    """The declared probe set and the mutations that actually call ``_probe`` must be the SAME set.

    Both directions are failures, and the first is the one that matters: a mutation listed here whose
    ``_probe(...)`` call has been edited away would pass every other check while quietly losing the
    proof that its body ran.
    """
    calls = {name for name, (_anchors, code) in MUTATIONS.items() if "_probe(" in code}
    unknown = sorted(PROBED - set(MUTATIONS))
    return (
        [f"{name}: declared probed but never calls _probe()" for name in sorted(PROBED & set(MUTATIONS) - calls)]
        + [f"{name}: calls _probe() but is not declared in PROBED" for name in sorted(calls - PROBED)]
        + [f"{name}: declared probed but is not a mutation" for name in unknown]
    )


def probe_tags() -> set[str]:
    """Every tag written by the mutation that just ran, then reset for the next one."""
    if not PROBE.is_file():
        return set()
    tags = {line.strip() for line in PROBE.read_text(encoding="utf-8").splitlines() if line.strip()}
    PROBE.unlink(missing_ok=True)
    return tags


def verify_anchors() -> list[str]:
    """Every declared anchor must name a test pytest actually collects.

    ⚠️ The whole point of anchoring is that the mutation-to-test mapping is CHECKABLE. An anchor that
    silently names nothing would be worse than a file target: pytest exits 4 for an unmatched node ID,
    and a scorer that reads a non-zero exit as a detection would report the mutation CAUGHT by a test
    that never ran. This is the same false-green shape the shared harness's own docstring records.
    """
    collected: set[str] = set()
    for suite in (LEGS, BATCH, SOCKET, ORACLE, COMPLETE):
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
    PROBE.unlink(missing_ok=True)
    try:
        _label, returncode, detail, outcomes = run(name, PROBE_PRELUDE + code, target)
    except SystemExit as exc:
        # The shared harness refuses to score a mutation whose plugin failed to import. That is
        # exactly the verdict an absent-anchor control is meant to earn.
        return "INVALID", f"{exc} [probe: {sorted(probe_tags()) or 'nothing ran'}]"
    tags = probe_tags()
    expected_tag = name if name in PROBED else None
    if expected_tag is not None and expected_tag not in tags:
        # ⚠️ The point of the probe. A red suite proves a test failed; it does not prove the replaced
        # body ever executed, and this repository has scored 22 import errors as 22 detections.
        return "NO-PROBE", f"{detail} [mutated body never ran: probe tags {sorted(tags) or 'none'}]"
    if expected_tag is None and tags:
        return "HARNESS-ERROR", f"an unprobed mutation wrote probe tags {sorted(tags)}"
    if observed_mutation(outcomes):
        verdict = "CAUGHT" if outcomes["call_failed"] else "CAUGHT*"
        if session_ended_abnormally(outcomes):
            detail = f"{detail} [session ended abnormally: exit {returncode}]"
        return verdict, detail
    if session_is_trustworthy(outcomes):
        return "SURVIVED", detail
    if anchors_that_missed(outcomes):
        # #480 round 4. One declared anchor observed the mutation and another did not. Before every
        # anchor got its own pytest invocation this scored CAUGHT on the first anchor's failure, so
        # a mutation could be credited to an anchor that never ran. It is a finding about the
        # anchor list, not an instrument fault, so it is not folded into HARNESS-ERROR.
        return "PARTIAL-ANCHOR", detail
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
    broken = verify_probes()
    if broken:
        print("HARNESS ERROR: the declared probe set and the mutations that call _probe() disagree:")
        for item in broken:
            print(f"  {item}")
        return 2

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
        f"all {len(MUTATIONS)} mutations scored as declared, each anchor run in its OWN pytest "
        f"invocation and EVERY declared anchor required to observe the mutation "
        f"({sum(1 for v in EXPECTED.values() if v == 'CAUGHT')} caught, "
        f"{sum(1 for v in EXPECTED.values() if v == 'SURVIVED')} cosmetic controls survived, "
        f"{sum(1 for v in EXPECTED.values() if v == 'INVALID')} absent-anchor controls invalid); "
        f"{len(PROBED)} of them additionally PROVED the mutated body ran"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
