"""Mutation harness for #471: prove the zero-row-capture tests can FAIL.

    python tests/mutation_oracle_empty.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest, through the shared
``tests/mutation_harness.py``. Verdicts come from pytest's own lifecycle records, never from scraping
the terminal summary: a non-zero exit alone means nothing (``pytest tests/nope.py`` exits 4 having
run nothing), a collection error looks exactly like a named test error, and a dying xdist worker
emits ``FAILED`` for a test that never executed. The shared harness's own first run scored 22/22
CAUGHT where every one was a false positive -- an import error exits non-zero -- which is why the
absent-anchor control below exists and must score INVALID rather than CAUGHT.

⚠️ **Every mutation names the TEST NODE IDs that must observe it**, and is baselined and run against
only those. A whole-file target would credit a mutation to whichever test in the file fails first
under ``-x``, which measurably produced an advertised proof that did not exist (#423 review round 1).

What this campaign is guarding against, specifically. The feature is four separable behaviours that a
single broad assertion could vouch for without observing any of them:

* the empty views are **named**, not merely counted (the defect);
* the classification is **honest** -- a header with no rows is a real query, a payload with no header
  at all is UNCLASSIFIABLE, and the byte count is never the discriminator;
* the fact rides on the **record** and survives every downstream slice;
* none of it moves the **exit code**, which is the regression risk, because `status` drives both the
  code and the blocked/failed partitions.

So the mutations come in matched pairs wherever a rule could fail in two directions: a census that
names nothing and one that names everything; a classifier collapsed onto each of its two values; a
guard that reads an absent row count as a zero and one that counts a failed leg as empty. A rule that
fires on everything is as useless as one that never fires, and only the second mutation can tell them
apart.
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

ORACLE = "tests/test_capture_tableau_oracle.py"
GROUP = "tests/test_group_oracle_by_workbook.py"
PACKAGE = "tests/test_package_unit.py"

# name -> (the test NODE IDs that must observe it, the patch injected as a pytest plugin at startup)
MUTATIONS: dict[str, tuple[tuple[str, ...], str]] = {
    # ------------------------------------------------------- the defect itself: counted, not named
    "the-list-is-reduced-to-its-length": (
        (f"{ORACLE}::test_an_empty_capture_is_named_not_merely_counted",),
        """
import tableau_oracle_manifest as m
# Exactly the master behaviour: the empty views are computed and only their COUNT escapes.
m.data_empty_views = lambda records: []
""",
    ),
    "the-census-names-every-view": (
        (f"{ORACLE}::test_a_capture_with_rows_is_never_named_empty",),
        """
import tableau_oracle_manifest as m
# The over-correction. A list that names all 94 views is a view count wearing a different name, and
# a reviewer learns to skip it within a week.
m.data_empty_views = lambda records: [
    {
        "view_luid": r.get("view_luid"),
        "view_name": r.get("view_name"),
        "workbook_name": r.get("workbook_name"),
        "view_type": r.get("view_type"),
        "classification": m.EMPTY_QUERY_NO_ROWS,
    }
    for r in records
]
""",
    ),
    # ------------------------------------------------------------------- the classification, both ways
    "every-empty-capture-is-called-a-real-query": (
        (f"{ORACLE}::test_a_payload_with_no_header_at_all_reads_as_CANNOT_CLASSIFY",),
        """
import tableau_oracle_manifest as m
_orig = m.empty_classification
def classify(record):
    # Collapses the honest "cannot classify" onto the confident bucket -- which is what overstated
    # the field finding 14 -> 12 by counting two glossary sheets as capture defects.
    out = _orig(record)
    return m.EMPTY_QUERY_NO_ROWS if out else out
m.empty_classification = classify
""",
    ),
    "every-empty-capture-is-called-unclassifiable": (
        (f"{ORACLE}::test_a_header_with_no_rows_is_classified_as_a_real_query",),
        """
import tableau_oracle_manifest as m
_orig = m.empty_classification
def classify(record):
    # The opposite collapse: refusing to classify anything. A CSV that NAMES its columns proves a
    # query ran, and throwing that away makes the whole list unactionable.
    out = _orig(record)
    return m.EMPTY_CANNOT_CLASSIFY if out else out
m.empty_classification = classify
""",
    ),
    "the-byte-count-becomes-the-discriminator": (
        (f"{ORACLE}::test_the_byte_count_is_not_consulted_as_the_discriminator",),
        """
import tableau_oracle_manifest as m
def classify(record):
    # The forbidden heuristic, implemented exactly as it is tempting to implement it: "0 bytes means
    # a glossary sheet, 2 bytes means blank data". It fits one site at n=14 and is not a documented
    # Tableau contract, so it would read as confident and be wrong at the next site.
    data = record.get("data") or {}
    if data.get("status") != "ok" or data.get("row_count") != 0:
        return None
    if data.get("columns"):
        return m.EMPTY_QUERY_NO_ROWS
    return m.EMPTY_CANNOT_CLASSIFY if data.get("bytes") == 0 else m.EMPTY_QUERY_NO_ROWS
m.empty_classification = classify
""",
    ),
    # --------------------------------------------------------------- the exit code must not move
    "an-empty-capture-is-promoted-to-a-failure": (
        (
            f"{ORACLE}::test_an_empty_capture_never_changes_the_code_a_run_would_otherwise_exit",
            f"{ORACLE}::test_an_empty_capture_keeps_status_ok_and_does_not_move_the_exit_code",
        ),
        """
import tableau_oracle_manifest as m
_orig = m.flag_empty
def flag(records):
    # "Just mark it failed" -- the regression this feature is most likely to cause. `status` drives
    # the exit code AND the blocked/failed partitions, so a legible run becomes a broken one and an
    # operator debugs the transport for a view whose filter is pointed at a day with no data.
    out = _orig(records)
    for record in out:
        if m.FLAG_DATA_EMPTY in (record.get("flags") or []):
            record["data"] = {**record["data"], "status": "failed"}
    return out
m.flag_empty = flag
""",
    ),
    # ------------------------------------------------------------------ per-view console visibility
    "the-per-view-line-stays-an-INFO": (
        (f"{ORACLE}::test_the_per_view_line_for_an_empty_capture_is_a_WARNING",),
        """
import tableau_oracle_manifest as m
from tableau_env import redacted_note
def progress(index, total, record, redactor=None):
    # The master implementation, verbatim: the zero is present, inside an INFO line, among 94 of
    # them. Technically visible; the reporting site found its 12 by opening PNGs by hand.
    data = record.get("data", {})
    name = redacted_note(record.get("view_name"), redactor, limit=34)
    status = data.get("status")
    if status == "ok":
        m.LOG.info("  %2d/%d  %-34s %5d rows  %6.1fs", index, total, name, data["row_count"], data["elapsed_sec"])
    elif status == "source_credential":
        m.LOG.warning("  %2d/%d  %-34s NEEDS CREDENTIAL: %s", index, total, name, data.get("detail"))
    else:
        m.LOG.warning("  %2d/%d  %-34s FAILED (%s): %s", index, total, name, status, data.get("detail"))
m.log_progress = progress
""",
    ),
    "every-per-view-line-becomes-a-WARNING": (
        (f"{ORACLE}::test_the_per_view_line_for_a_normal_capture_stays_an_INFO",),
        """
import tableau_oracle_manifest as m
_orig = m.log_progress
def progress(index, total, record, redactor=None):
    # The over-correction: a WARN on all 94 lines is a WARN nobody reads, which is the defect again
    # with a different level.
    real = m.LOG.info
    m.LOG.info = m.LOG.warning
    try:
        return _orig(index, total, record, redactor)
    finally:
        m.LOG.info = real
m.log_progress = progress
""",
    ),
    # ---------------------------------------------------------------------- the run-end block
    "the-run-end-block-is-silent": (
        (f"{ORACLE}::test_the_run_end_block_names_the_empty_views_and_what_they_cost",),
        """
import tableau_oracle_manifest as m
m._log_empty = lambda empty, redactor: None
""",
    ),
    "the-run-end-block-fires-on-a-clean-run": (
        (f"{ORACLE}::test_the_run_end_block_is_silent_when_nothing_is_empty",),
        """
import tableau_oracle_manifest as m
_orig = m._log_empty
def log(empty, redactor):
    # A diagnostic that fires when there is nothing to report is one an operator learns to skip --
    # and the guard that prevents it is a single `if not empty: return`.
    m.LOG.warning("0 view(s) captured ZERO DATA ROWS.")
    return _orig(empty, redactor)
m._log_empty = log
""",
    ),
    "the-run-end-block-prints-the-raw-view-name": (
        (f"{ORACLE}::test_a_view_name_is_redacted_before_the_empty_diagnostic_prints_it",),
        """
import tableau_oracle_manifest as m
def log(empty, redactor):
    # ⚠️ A view NAME is response data: a reflected SESSION TOKEN has arrived as one from an
    # authenticated metadata call, which the export seam never sees. Skipping `redacted_note` here
    # puts it in the console, which CI keeps.
    if not empty:
        return
    m.LOG.warning("%d view(s) captured ZERO DATA ROWS.", len(empty))
    for entry in empty:
        m.LOG.warning("  - %s: %s", entry.get("view_name"), entry.get("classification"))
m._log_empty = log
""",
    ),
    # ------------------------------------------------------------------------ the per-view flag
    "the-flag-is-never-written": (
        (f"{ORACLE}::test_the_empty_fact_rides_on_the_view_record_as_a_flag",),
        """
import tableau_oracle_manifest as m
# The manifest still counts and names them, but nothing rides on the record -- so every downstream
# slice (a per-workbook subset, a packaged unit) loses the fact.
m.flag_empty = lambda records: records
""",
    ),
    "flagging-mutates-the-caller-s-records": (
        (f"{ORACLE}::test_flagging_does_not_mutate_the_caller_s_records",),
        """
import tableau_oracle_manifest as m
def flag(records):
    # In place, which is the obvious implementation and is wrong: `write_manifest` is handed the live
    # list the capture loop built and does not own it.
    for record in records:
        classification = m.empty_classification(record)
        if classification:
            record["flags"] = [m.FLAG_DATA_EMPTY, classification]
    return records
m.flag_empty = flag
""",
    ),
    # -------------------------------------------------------------- what counts as "empty" at all
    "an-absent-row-count-is-read-as-a-zero": (
        (f"{ORACLE}::test_an_older_record_with_no_row_count_is_not_claimed_empty",),
        """
import tableau_oracle_manifest as m
def classify(record):
    # `if data.get("row_count")` instead of `!= 0` -- one character of difference, and an OLDER
    # manifest that never measured emptiness is now reported as evidentially empty.
    data = record.get("data") or {}
    if data.get("status") != "ok" or data.get("row_count"):
        return None
    return m.EMPTY_QUERY_NO_ROWS if data.get("columns") else m.EMPTY_CANNOT_CLASSIFY
m.empty_classification = classify
""",
    ),
    "a-failed-leg-is-also-counted-empty": (
        (f"{ORACLE}::test_a_failed_data_leg_is_not_ALSO_reported_as_empty",),
        """
import tableau_oracle_manifest as m
def classify(record):
    # Drops the status guard and defaults the missing count to zero, so one root cause is counted
    # twice: a credential-blocked view is reported as blocked AND as an empty capture.
    data = record.get("data") or {}
    if data.get("row_count", 0) != 0:
        return None
    return m.EMPTY_QUERY_NO_ROWS if data.get("columns") else m.EMPTY_CANNOT_CLASSIFY
m.empty_classification = classify
""",
    ),
    # ------------------------------------------------------------------ the downstream consumers
    "the-capture-stops-recording-the-csv-header": (
        (f"{ORACLE}::test_a_REAL_empty_export_travels_the_whole_path_from_capture_to_manifest",),
        """
import capture_tableau_oracle as o
import tableau_payload_facts as f
_orig = f.summarise_csv
def summarise(payload):
    # The CHAIN, not the verdict layer: `_capture_data` merges `summarise_csv`, and `columns` is the
    # one recorded fact the classification reads. Drop it and every empty capture is unclassifiable,
    # which no test built from a hand-written record could observe.
    out = dict(_orig(payload))
    out["columns"] = []
    return out
o.summarise_csv = summarise
""",
    ),
    "the-workbook-subset-counts-but-does-not-name": (
        (f"{GROUP}::test_a_zero_row_view_is_counted_AND_named_in_the_workbook_manifest",),
        """
import group_oracle_by_workbook as g
_orig = g.subset_manifest
def subset(manifest, workbook, views):
    # The capture-wide defect one folder down: the per-workbook reader is exactly the reviewer who
    # needs to know WHICH page carries no evidence.
    out = _orig(manifest, workbook, views)
    out.pop("data_empty_views", None)
    return out
g.subset_manifest = subset
""",
    ),
    "the-workbook-subset-keeps-its-own-copy-of-the-rule": (
        (f"{GROUP}::test_the_workbook_subset_uses_the_SAME_empty_predicate_as_the_capture",),
        """
import group_oracle_by_workbook as g
_orig = g.subset_manifest
def subset(manifest, workbook, views):
    # The second copy of "row_count == 0" this change deleted. It disagrees with the capture on an
    # older record that never recorded one -- reading an absence as a zero.
    out = _orig(manifest, workbook, views)
    ok = [v for v in views if ((v.get("data") or {}).get("status")) == "ok"]
    out["data_empty"] = len([v for v in ok if (v.get("data") or {}).get("row_count", 0) == 0])
    return out
g.subset_manifest = subset
""",
    ),
    "the-packager-drops-the-flag": (
        (f"{PACKAGE}::test_the_per_view_empty_flag_survives_packaging",),
        """
import manifest_scope
# The allowlist DROPS an unenumerated key rather than raising, so this is silent: a packaged unit
# ships a zero-row capture with nothing anywhere saying so. That is moving the failure boundary,
# not removing it.
manifest_scope.ORACLE_VIEW_ALLOW.pop("flags", None)
""",
    ),
    # ------------------------------------------------------- #480: the THIRD state must not be silent
    #
    # This PR's own review found it had fixed "a zero-row capture reads as ok" and introduced "an
    # UNASSESSABLE capture reads as ok". These mutations exist so that boundary cannot move back: each
    # one is a plausible implementation that returns to reporting an unmeasured view as a clean one.
    "an-absent-row-count-goes-back-to-reading-as-CLEAN": (
        (f"{ORACLE}::test_a_row_count_that_was_never_recorded_is_UNASSESSABLE_not_clean",),
        """
import tableau_oracle_manifest as m
# Exactly the round-1 behaviour: the predicate exists, and answers None for everything -- so every
# consumer reads "not empty" as "fine" and the view is reported successful, evidence-complete,
# unflagged and unnamed. Identical to a capture that returned 900,000 rows.
m.unassessable_reason = lambda record: None
""",
    ),
    "every-view-is-called-unassessable": (
        (f"{ORACLE}::test_a_capture_with_rows_is_never_called_unassessable",),
        """
import tableau_oracle_manifest as m
# The over-correction, and the matched pair the empty census already has: a list naming all 94 views
# is a view count wearing a different name, and it also drives `captured_complete` to zero on a
# perfectly good run.
m.unassessable_reason = lambda record: m.UNASSESSABLE_NO_ROW_COUNT
""",
    ),
    "an-unassessable-view-is-still-counted-evidence-complete": (
        (f"{ORACLE}::test_a_row_count_that_was_never_recorded_is_UNASSESSABLE_not_clean",),
        """
import tableau_oracle_manifest as m
_orig = m._partition
def partition(records, requested=frozenset()):
    # The narrowest possible regression, and the one most likely to be written by accident: the flag
    # and the named list are kept, and `complete` stops consulting them. `captured_complete` is what
    # a run REPORTS as captured, so this alone puts an unmeasured view back in the clean bucket.
    sets = _orig(records, requested)
    sets["complete"] = [
        r for r in records
        if r.get("data", {}).get("status") == "ok"
        and all(s == "ok" for s in m._render_statuses(r, requested))
    ]
    return sets
m._partition = partition
""",
    ),
    "an-unassessable-capture-is-promoted-to-a-failure": (
        (f"{ORACLE}::test_an_unassessable_capture_never_moves_the_exit_code",),
        """
import tableau_oracle_manifest as m
_orig = m.flag_empty
def flag(records):
    # The opposite over-correction to the one above, and the same regression the empty half is
    # guarded against: `status` drives the exit code AND the blocked/failed partitions, so a run
    # whose only fault is that we cannot VOUCH for a body becomes a failed run.
    out = _orig(records)
    for record in out:
        if m.FLAG_DATA_UNASSESSABLE in (record.get("flags") or []):
            record["data"] = {**record["data"], "status": "failed"}
    return out
m.flag_empty = flag
""",
    ),
    "an-absent-row-count-is-flagged-but-never-named": (
        (f"{ORACLE}::test_the_run_end_block_names_the_unassessable_views_and_what_they_cost",),
        """
import tableau_oracle_manifest as m
# The #471 defect in its new home: counted, flagged, and not NAMED. A count cannot tell a reviewer
# which page carries no evidence, which is the entire reason the empty census was written.
m.data_unassessable_views = lambda records: []
""",
    ),
    "the-unassessable-run-end-block-fires-on-a-clean-run": (
        (f"{ORACLE}::test_the_unassessable_run_end_block_is_silent_when_everything_was_measured",),
        """
import tableau_oracle_manifest as m
_orig = m._log_unassessable
def log(unassessable, redactor):
    # A diagnostic that fires when there is nothing to report is one an operator learns to skip.
    m.LOG.warning("0 view(s) captured data that could NOT BE ASSESSED.")
    return _orig(unassessable, redactor)
m._log_unassessable = log
""",
    ),
    "the-unassessable-run-end-block-prints-the-raw-view-name": (
        (f"{ORACLE}::test_a_view_name_is_redacted_before_the_unassessable_diagnostic_prints_it",),
        """
import tableau_oracle_manifest as m
def log(unassessable, redactor):
    # ⚠️ A view NAME is response data on this line too -- a reflected SESSION TOKEN has arrived as
    # one from an authenticated metadata call. A second console surface is a second leak.
    if not unassessable:
        return
    m.LOG.warning("%d view(s) captured data that could NOT BE ASSESSED.", len(unassessable))
    for entry in unassessable:
        m.LOG.warning("  - %s: %s", entry.get("view_name"), entry.get("reason"))
m._log_unassessable = log
""",
    ),
    "the-per-view-line-prints-an-invented-zero": (
        (f"{ORACLE}::test_the_per_view_line_for_an_unassessable_capture_is_a_WARNING_and_does_not_raise",),
        """
import tableau_oracle_manifest as m
from tableau_env import redacted_note
def progress(index, total, record, redactor=None):
    # `data.get("row_count", 0)` -- the fix that makes the KeyError go away and invents the
    # measurement instead. The line then reads "0 rows", which is the one thing this module refuses
    # to say about a capture that measured nothing.
    data = record.get("data", {})
    name = redacted_note(record.get("view_name"), redactor, limit=34)
    if data.get("status") == "ok":
        m.LOG.warning(
            "  %2d/%d  %-34s %5d rows  %6.1fs  <- UNASSESSABLE (%s)",
            index, total, name, data.get("row_count", 0), data.get("elapsed_sec", 0.0),
            m.unassessable_reason(record) or m.empty_classification(record),
        )
m.log_progress = progress
""",
    ),
    "a-bool-row-count-is-accepted-as-a-measurement": (
        (f"{ORACLE}::test_a_row_count_of_True_is_a_corrupt_record_not_a_measurement_of_one_row",),
        """
import tableau_oracle_manifest as m
def reason(record):
    # `isinstance(True, int)` is True in Python, so dropping the bool exclusion is a one-token edit
    # that silently accepts a corrupt record as a measurement of one row.
    data = record.get("data") or {}
    if data.get("status") != "ok":
        return None
    if isinstance(data.get("row_count"), int):
        return None
    certification = data.get("certification")
    return certification if certification in m.UNASSESSABLE_REASONS else m.UNASSESSABLE_NO_ROW_COUNT
m.unassessable_reason = reason
""",
    ),
    # -------------------------------------------- #480: an HTTP 200 is not evidence until certified
    "any-200-is-accepted-as-CSV": (
        (
            f"{ORACLE}::test_an_uncertifiable_200_is_never_recorded_as_rows",
            f"{ORACLE}::test_an_uncertifiable_200_is_never_classified_from_its_first_line",
        ),
        """
import tableau_payload_facts as f
import capture_tableau_oracle as o
# The master behaviour: every 200 certifies, whatever it declared and whatever is in it. That is how
# an HTML error page was recorded `columns: ["<html>"], row_count: 2` and an octet-stream body was
# DIAGNOSED `empty_query_no_rows`.
o.certify_csv = lambda payload, content_type: f.CSV_CERTIFIED
""",
    ),
    "the-declared-content-type-is-ignored": (
        (f"{ORACLE}::test_an_uncertifiable_200_is_never_recorded_as_rows",),
        """
import tableau_payload_facts as f
import capture_tableau_oracle as o
_orig = f.certify_csv
# Structure only -- "the payload is decisive", which is right for a PNG and wrong for a CSV, because
# a CSV has no signature. `not CSV at all` parses as a one-column table and would be certified.
o.certify_csv = lambda payload, content_type: _orig(payload, None)
""",
    ),
    "an-uncertified-body-is-still-written-to-disk-as-data": (
        (f"{ORACLE}::test_an_uncertifiable_200_is_never_recorded_as_rows",),
        """
import capture_tableau_oracle as o
_orig = o._capture_data
def capture(session, view_luid, out_dir, stem):
    # Refuses the SHAPE but keeps the file, which is the half-fix: `data/<luid>.csv` then exists,
    # named as data, holding an error page -- and a consumer that lists the folder counts it.
    record = _orig(session, view_luid, out_dir, stem)
    if record.get("status") == "format_mismatch":
        path = out_dir / "data" / (stem + ".csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"whatever arrived")
        record["path"] = "data/" + stem + ".csv"
    return record
o._capture_data = capture
""",
    ),
    "a-refused-body-is-recorded-status-ok": (
        (f"{ORACLE}::test_an_uncertifiable_200_is_never_classified_from_its_first_line",),
        """
import capture_tableau_oracle as o
_orig = o._capture_data
def capture(session, view_luid, out_dir, stem):
    # "The HTTP call succeeded, so status is ok" applied one step too far. It is true of an
    # unassessable body and false of a refused one: `status` drives the exit code, so this makes an
    # uncertifiable capture exit 0 with the failure recorded nowhere a caller reads.
    record = _orig(session, view_luid, out_dir, stem)
    if record.get("status") == "format_mismatch":
        record["status"] = "ok"
    return record
o._capture_data = capture
""",
    ),
    "an-absent-content-type-is-waved-through-as-certified": (
        (f"{ORACLE}::test_a_200_with_no_Content_Type_is_kept_but_reported_UNASSESSABLE",),
        """
import tableau_payload_facts as f
import capture_tableau_oracle as o
_orig = f.certify_csv
def certify(payload, content_type):
    # The tempting leniency: "a proxy stripped the header, the body looks fine, call it CSV". It is
    # the fail-open this whole change exists to close -- nothing establishes those bytes as data.
    out = _orig(payload, content_type)
    return f.CSV_CERTIFIED if out == f.CSV_CONTENT_TYPE_ABSENT else out
f.certify_csv = certify
# ⚠️ BOTH bindings, and measured: `capture_tableau_oracle` does `from tableau_payload_facts import
# certify_csv`, so patching only the defining module works ONLY while this plugin happens to run
# before the consumer is imported. Under a runner that imports the consumer first the mutation
# silently did nothing and its anchor PASSED -- a survived mutation scored as caught by import order.
o.certify_csv = certify
""",
    ),
    "the-certifier-refuses-a-real-CSV-too": (
        (f"{ORACLE}::test_a_real_CSV_declared_as_CSV_is_still_certified_and_still_counted",),
        """
import tableau_payload_facts as f
import capture_tableau_oracle as o
# The negative direction, and the reason the positive control exists: a gate that refuses everything
# passes every "must not be recorded as rows" test in this file and breaks every real capture.
o.certify_csv = lambda payload, content_type: f.CSV_CONTENT_TYPE_NOT_CSV
""",
    ),
    "an-empty-body-stops-being-certifiable": (
        (f"{ORACLE}::test_certify_csv_verdicts",),
        """
import tableau_payload_facts as f
_orig = f.certify_csv
def certify(payload, content_type):
    # #471's own two fixtures are a 0-byte body and a bare CRLF. Refusing them would make the
    # zero-row diagnostic unreachable -- the new gate silently deleting the old feature.
    if not payload.strip():
        return f.CSV_MALFORMED
    return _orig(payload, content_type)
f.certify_csv = certify
""",
    ),
    "the-packager-drops-the-certification": (
        (f"{PACKAGE}::test_a_packaged_view_whose_row_count_was_never_recorded_says_so",),
        """
import manifest_scope
# The allowlist DROPS an unenumerated key rather than raising, so this is silent: a packaged unit
# ships `status: ok` with no row count and nothing saying the body was never established as CSV.
manifest_scope.ORACLE_LEG_ALLOW.pop("certification", None)
manifest_scope.ORACLE_LEG_SPEC.pop("certification", None)
""",
    ),
    "the-packager-carries-the-flag-but-never-derives-it": (
        (f"{PACKAGE}::test_a_packaged_view_from_an_OLDER_capture_is_flagged_by_the_packager_itself",),
        """
import package_unit as p
import tableau_oracle_manifest as m
_orig = p._scope_oracle_manifest
def scope(manifest, packaged, objects, unit):
    # The half-fix: carry a flag the capture already wrote, and derive nothing. Correct for a
    # manifest written by a current run, and silent for every older one -- which is the input the
    # review actually used, and where "flags absent" reads as a clean capture.
    return _orig(manifest, [dict(v) for v in packaged], objects, unit)
p._scope_oracle_manifest = scope
m.flag_empty = lambda records: records
""",
    ),
    "the-packager-flags-every-view-it-ships": (
        (f"{PACKAGE}::test_the_packager_does_not_flag_a_view_whose_rows_were_measured",),
        """
import tableau_oracle_manifest as m
_orig = m.flag_empty
def flag(records):
    # The matched over-correction: a flag on all of them is a view list wearing a diagnostic, and a
    # reader learns to ignore the field within a week.
    return [{**r, "flags": [m.FLAG_DATA_UNASSESSABLE, m.UNASSESSABLE_NO_ROW_COUNT]} for r in records]
m.flag_empty = flag
""",
    ),
    "the-workbook-subset-drops-the-unassessable-pair": (
        (f"{GROUP}::test_a_view_with_no_row_count_is_counted_AND_named_UNASSESSABLE_in_the_workbook_manifest",),
        """
import group_oracle_by_workbook as g
_orig = g.subset_manifest
def subset(manifest, workbook, views):
    # The per-workbook reader is the one who acts on this, and a subset reporting `data_ok: 2` with
    # nothing beside it says "two good captures" about a view nothing measured.
    out = _orig(manifest, workbook, views)
    out.pop("data_unassessable", None)
    out.pop("data_unassessable_views", None)
    return out
g.subset_manifest = subset
""",
    ),
    # ------------------------------------------------------------- discriminating controls
    "control-cosmetic-unassessable-banner-wording": (
        (f"{ORACLE}::test_a_row_count_that_was_never_recorded_is_UNASSESSABLE_not_clean",),
        """
import tableau_oracle_manifest as m
_orig = m._log_unassessable
def log(unassessable, redactor):
    m.LOG.warning("cosmetically reworded unassessable banner nobody asserts on")
    return _orig(unassessable, redactor)
m._log_unassessable = log
""",
    ),
    "control-cosmetic-empty-banner-wording": (
        (f"{ORACLE}::test_an_empty_capture_is_named_not_merely_counted",),
        """
import tableau_oracle_manifest as m
_orig = m._log_empty
def log(empty, redactor):
    m.LOG.warning("cosmetically reworded banner nobody asserts on")
    return _orig(empty, redactor)
m._log_empty = log
""",
    ),
    "control-absent-anchor-empty": (
        (f"{ORACLE}::test_an_empty_capture_is_named_not_merely_counted",),
        """
import tableau_oracle_manifest as m
m._this_symbol_does_not_exist.attribute = 1
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

    ⚠️ An anchor that silently names nothing would be worse than a file target: pytest exits 4 for an
    unmatched node ID, and a scorer that reads a non-zero exit as a detection would report the
    mutation CAUGHT by a test that never ran.
    """
    collected: set[str] = set()
    for suite in (ORACLE, GROUP, PACKAGE):
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
        # selects every one of them -- which is what an anchor for a parametrized behaviour says.
        return anchor in collected or any(node.startswith(f"{anchor}[") for node in collected)

    return sorted(
        f"{name} -> {anchor}"
        for name, (anchors, _code) in MUTATIONS.items()
        for anchor in anchors
        if not selects(anchor)
    )


def baseline(anchors: tuple[str, ...]) -> tuple[int, str]:
    """A mutation is only evidence against a clean baseline -- of ITS OWN anchors, not of a file."""
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
        # ⚠️ The shared harness only raises SystemExit for the literal string "Error importing
        # plugin"; an AttributeError inside the plugin exits 1 with no lifecycle record instead, and
        # scoring that as a detection is the exact false-green the shared harness records.
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
