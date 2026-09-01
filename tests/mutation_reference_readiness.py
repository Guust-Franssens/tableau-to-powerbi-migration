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

Every mutation names its ANCHOR, and that is the whole point of this file
------------------------------------------------------------------------
Round-2 finding 6: the previous version ran each mutation against the WHOLE test file under ``-x``
and credited whichever test failed first. That is not attribution - two unrelated mutations were both
credited to ``test_colliding_page_ids_cannot_be_attributed`` simply because it ran early, and the
harness would have stayed green if their real anchors regressed while an unrelated test failed first.
The per-anchor verification existed only in a session transcript, not in the repo.

So each entry declares:

* ``anchor``   -- the pytest node that must CATCH it, run ALONE. This is the committed claim.
* ``controls`` -- nodes that must SURVIVE it, run alone. Without these, "caught" cannot be
  distinguished from "the mutation broke everything", which is the failure mode that makes a
  mutation score meaningless.

Anchors are RESOLVED across the suites, and that guard is load-bearing
-----------------------------------------------------------------------
:func:`resolve_node` finds an anchor's file rather than hard-coding one, and **raises** when a name
exists in zero suites or in more than one. That is not tidiness; it catches a specific and measured
false-green.

The shared ``mutation_harness.py`` records the original form: its first run reported **22 of 22
caught**, and every one was an *import error* exiting non-zero before a single test executed. The
same shape recurs whenever a mutation goes stale - it patches a symbol that has been renamed or
deleted, and either does nothing (SURVIVED against its own anchor) or raises ``AttributeError``
/``NameError`` inside an unrelated test (CAUGHT against its own CONTROL). Both are the harness
scoring the *state of the mutation* rather than the state of the code.

Measured, when the test suite was split to match the module split, this guard flagged four entries on
its first run and **all four were stale rather than wrong**:

* one patched ``Evidence.match_names``, since renamed to ``.candidate()`` - a no-op that SURVIVED;
* one referenced the deleted ``KIND_ASSERTED`` and raised, scoring CAUGHT against its own control;
* one compared against ``reference_evidence.AMBIGUOUS`` while the gate had moved to
  ``object_identity.AMBIGUOUS`` - which exposed a real defect in the *shipped* code, not the test:
  the module split had left **two constants of the same name with different values**, one dead and
  shadowing the other's meaning;
* one used ``oid`` without importing it, so a ``NameError`` was being scored as a detection.

The lesson worth keeping: **a mutation harness is not self-validating.** It reports on code it
patches by name, so any rename silently converts a real check into a no-op. Resolving anchors and
failing on an unknown one is what makes that visible by construction, instead of leaving a green run
that proves nothing.

Two entries are whole-suite controls rather than fail-closed properties: a cosmetic reword MUST
survive (otherwise the suite asserts on incidental wording), and an absent anchor MUST be reported
INVALID rather than credited as a detection - the exact false-green described above.

Exit 0 only when every anchor caught its mutation and every control survived it.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
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

#: Every suite an anchor may live in. The tests were split to match the module split, so an anchor's
#: file is RESOLVED rather than hard-coded - and a name that exists in none of them, or in more than
#: one, is a hard error. An anchor that silently stopped existing would make this harness green while
#: proving nothing, which is the exact attribution defect round-2 finding 6 was about.
TARGETS = (
    "tests/test_check_reference_readiness.py",
    "tests/test_reference_evidence.py",
    "tests/test_object_identity.py",
    "tests/test_identity_normalization.py",
)
#: `mutation_harness.run` passes its target as ONE argv element, so a whole-suite control names the
#: gate suite - the only one that exercises `render`, and the one the absent-anchor guard applies to.
WHOLE_SUITE = TARGETS[0]

CAUGHT = "CAUGHT"
SURVIVED = "SURVIVED"
INVALID = "INVALID"


@dataclass(frozen=True)
class Mutation:
    """One patch, the test that must catch it, and the tests that must not."""

    code: str
    anchor: str | None = None
    controls: tuple[str, ...] = ()
    whole_suite: str | None = None


MUTATIONS: dict[str, Mutation] = {
    # --- the exit contract itself ------------------------------------------------------------
    "reinstate-warn-only": Mutation(
        code="""
import argparse
import check_reference_readiness as crr
# Round-1 finding 1: parsed, and returning EXIT_OK before FINDINGS or CANNOT_ESTABLISH are mapped.
# Measured returning 0 while the gate's own output said "CANNOT_ESTABLISH is NOT a pass".
_orig = crr.main
def main(argv=None):
    argv = [a for a in (argv or []) if a != "--warn-only"]
    _orig(argv)
    return crr.EXIT_OK
crr.main = main
""",
        anchor="test_there_is_no_flag_that_can_soften_the_verdict",
        controls=("test_orphan_worksheets_are_expected_pages",),
    ),
    "cannot-establish-exits-zero": Mutation(
        code="""
import check_reference_readiness as crr
crr.EXIT_CANNOT_ESTABLISH = crr.EXIT_OK
""",
        anchor="test_the_status_and_exit_vocabulary_is_pinned_to_its_literal_values",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    "unverifiable-is-treated-as-ready": Mutation(
        code="""
import check_reference_readiness as crr
crr.UNVERIFIABLE = crr.READY
""",
        anchor="test_the_status_and_exit_vocabulary_is_pinned_to_its_literal_values",
        controls=("test_orphan_worksheets_are_expected_pages",),
    ),
    "oracle-grade-equals-validation-grade": Mutation(
        code="""
import reference_evidence as ev
import check_reference_readiness as crr
# Round-1 finding 8a: this SURVIVED the whole suite, because the literal pin omitted GRADE_ORACLE
# and the only oracle assertion compared against the same mutable constant.
ev.GRADE_ORACLE = ev.GRADE_VALIDATION
crr.GRADE_ORACLE = ev.GRADE_VALIDATION
""",
        anchor="test_the_status_and_exit_vocabulary_is_pinned_to_its_literal_values",
        controls=("test_a_page_with_no_evidence_at_all_is_blind_not_unverifiable",),
    ),
    # --- NOT_APPLICABLE must be earned --------------------------------------------------------
    "a-vanished-report-is-not-applicable": Mutation(
        code="""
import check_reference_readiness as crr
crr._units_without_reports = lambda engine_report, reports: []
def _empty_bundle_unit(root, engine_report):
    return crr.UnitResult(unit=root.name, status=crr.STATUS_NOT_APPLICABLE, detail="models only")
crr._empty_bundle_unit = _empty_bundle_unit
""",
        anchor="test_a_workbook_whose_report_never_shipped_is_a_finding",
        controls=("test_a_datasource_only_unit_is_not_applicable",),
    ),
    "no-pages-found-means-not-applicable": Mutation(
        code="""
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
        anchor="test_not_applicable_is_earned_from_the_engine_report_not_from_an_empty_page_list",
        controls=("test_a_datasource_only_unit_is_not_applicable",),
    ),
    # --- evidence must be USABLE ---------------------------------------------------------------
    "existence-is-evidence": Mutation(
        code="""
import reference_evidence as ev
ev.render_facts = lambda path, recorded: (None, None) if path.is_file() else "missing"
""",
        anchor="test_a_zero_byte_render_is_rejected_not_promoted",
        controls=("test_a_page_with_no_evidence_at_all_is_blind_not_unverifiable",),
    ),
    "shallow-png-header-check": Mutation(
        code="""
import struct
import reference_evidence as ev
# Round-2 finding 1: the pre-fix parse - signature, IHDR marker, 8 dimension bytes. A 24-byte blob
# passed while Pillow called the same bytes truncated.
def _png_size(blob):
    if len(blob) < 24 or blob[:8] != b"\\x89PNG\\r\\n\\x1a\\n" or blob[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", blob[16:24])
    return int(w), int(h)
ev._png_size = _png_size
""",
        anchor="test_a_24_byte_blob_is_not_a_png",
        controls=("test_the_192px_embedded_thumbnail_route_still_counts",),
    ),
    "recorded-integrity-is-ignored": Mutation(
        code="""
import reference_evidence as ev
# The measured shape: zeroed hashes and 1x1 dimensions still returned READY 3/3.
ev._integrity_mismatch = lambda blob, size, recorded: None
""",
        anchor="test_a_swapped_image_no_longer_counts",
        controls=("test_the_192px_embedded_thumbnail_route_still_counts",),
    ),
    "a-missing-recorded-hash-is-accepted": Mutation(
        code="""
import reference_evidence as ev
_orig = ev._integrity_mismatch
def _integrity_mismatch(blob, size, recorded):
    return None if not recorded.sha256 else _orig(blob, size, recorded)
ev._integrity_mismatch = _integrity_mismatch
""",
        anchor="test_a_manifest_with_no_recorded_hash_cannot_be_trusted",
        controls=("test_a_swapped_image_no_longer_counts",),
    ),
    "empty-capabilities-grade-as-layout": Mutation(
        code="""
import reference_evidence as ev
_orig = ev.provider_grade
def provider_grade(provider, capabilities):
    if not isinstance(capabilities, list) or not capabilities:
        return ev.CAP_LAYOUT
    return _orig(provider, capabilities)
ev.provider_grade = provider_grade
""",
        anchor="test_empty_capabilities_are_rejected_not_graded_unknown",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    "the-legibility-floor-is-removed": Mutation(
        code="""
import reference_evidence as ev
ev.MIN_RENDER_EDGE = 0
""",
        anchor="test_an_illegibly_small_render_is_rejected",
        controls=("test_the_192px_embedded_thumbnail_route_still_counts",),
    ),
    # --- grade must be capped by the producer ---------------------------------------------------
    "a-provider-may-grade-itself": Mutation(
        code="""
import reference_evidence as ev
# Round-2 finding 2: grade from the self-reported list, with no provider ceiling.
def provider_grade(provider, capabilities):
    if not isinstance(capabilities, list) or not capabilities:
        return "!no usable capability grade"
    caps = {c for c in capabilities if isinstance(c, str)}
    if not caps <= ev.ALLOWED_CAPABILITIES:
        return "!capabilities outside the known vocabulary"
    if provider == "oracle_capture":
        return ev.GRADE_ORACLE
    return ev.GRADE_VALIDATION if ev.CAP_VALIDATION in caps else "/".join(sorted(caps))
ev.provider_grade = provider_grade
""",
        anchor="test_a_low_grade_provider_cannot_promote_itself",
        controls=("test_validation_grade_is_reported_when_present",),
    ),
    "an-unknown-provider-gets-a-default-ceiling": Mutation(
        code="""
import reference_evidence as ev
_orig = ev.PROVIDER_CEILING
class _Permissive(dict):
    def get(self, key, default=None):
        return dict.get(self, key, ev.ALLOWED_CAPABILITIES)
ev.PROVIDER_CEILING = _Permissive(_orig)
""",
        anchor="test_an_unrecognised_provider_can_claim_nothing",
        controls=("test_validation_grade_is_reported_when_present",),
    ),
    "the-manual-name-prefix-is-not-stripped": Mutation(
        code="""
import reference_evidence as ev
import object_identity as oid
# `collect_manual` globs `tableau-*.png` and names each record from the file stem, so the prefix is
# imposed by the glob rather than chosen by the operator. Not stripping it makes the route match
# nothing at all.
ev.Evidence.candidate = lambda self: oid.Candidate(names=(self.name,), kind=self.kind)
""",
        anchor="test_validation_grade_is_reported_when_present",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    # --- evidence must be ATTRIBUTABLE ----------------------------------------------------------
    "evidence-matches-any-workbook": Mutation(
        code="""
import reference_evidence as ev
ev.Evidence.is_for = lambda self, unit: True
""",
        anchor="test_evidence_for_another_workbook_does_not_satisfy_this_one",
        controls=("test_a_page_with_no_evidence_at_all_is_blind_not_unverifiable",),
    ),
    "missing-workbook-identity-is-accepted": Mutation(
        code="""
import reference_evidence as ev
_orig = ev.Evidence.build.__func__
def build(cls, **kwargs):
    if not (kwargs.get("workbook_sha") or kwargs.get("workbook_luid") or kwargs.get("workbook_name")):
        kwargs["workbook_name"] = "WB"
    return _orig(cls, **kwargs)
ev.Evidence.build = classmethod(build)
""",
        anchor="test_evidence_with_no_workbook_identity_is_rejected",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    "a-name-only-provenance-luid-is-trusted": Mutation(
        code="""
import check_reference_readiness as crr
# Round-2 finding 3: origin.match was never consulted, so a LUID whose bytes DIFFER from the
# server's - which stamp_tableau_provenance.py says will not reproduce - made evidence ready.
def _provenance_luid(root, source_sha):
    payload = crr.json_object(root / "source-provenance.json")
    for record in (payload or {}).get("inputs") or []:
        origin = record.get("origin") or {}
        luid = origin.get("workbook_luid")
        if isinstance(luid, str) and luid:
            return luid
    return None
crr._provenance_luid = _provenance_luid
""",
        anchor="test_a_name_only_provenance_luid_is_not_trusted",
        controls=("test_a_sha256_confirmed_provenance_luid_is_trusted",),
    ),
    "a-luid-record-discards-its-workbook-name": Mutation(
        code="""
import reference_evidence as ev
_orig = ev._oracle_workbook_ids
def _oracle_workbook_ids(record):
    luid, name = _orig(record)
    return (luid, None) if luid else (None, name)
ev._oracle_workbook_ids = _oracle_workbook_ids
""",
        anchor="test_a_record_carrying_both_luid_and_name_can_still_fall_back_to_the_name",
        controls=("test_a_sha256_confirmed_provenance_luid_is_trusted",),
    ),
    # --- completeness needs a readable, unique mapping --------------------------------------------
    "unreadable-page-json-falls-back-to-the-directory-name": Mutation(
        code="""
import check_reference_readiness as crr
def page_map(report_dir):
    root = report_dir / "definition" / "pages"
    found = {}
    if root.is_dir():
        for page_json in sorted(root.rglob("page.json")):
            payload = crr.json_object(page_json) or {}
            found[str(payload.get("name") or page_json.parent.name)] = str(payload.get("displayName") or "")
    return found, []
crr.page_map = page_map
""",
        anchor="test_an_unreadable_page_definition_is_not_a_page",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    "pages-json-is-checked-only-when-well-formed": Mutation(
        code="""
import check_reference_readiness as crr
# Round-2 finding 4: the cross-check ran only when pageOrder happened to be a list.
_orig = crr.page_map
def page_map(report_dir):
    found, problems = _orig(report_dir)
    return found, [p for p in problems if "pages.json is missing" not in p]
crr.page_map = page_map
""",
        anchor="test_an_unreadable_pages_json_is_not_a_valid_mapping",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    "colliding-page-ids-are-not-detected": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr._expectation
def _expectation(unit, report_dir, source):
    objects = crr.source_objects(source)
    return objects if objects else _orig(unit, report_dir, source)
crr._expectation = _expectation
""",
        anchor="test_colliding_page_ids_cannot_be_attributed",
        controls=("test_orphan_worksheets_are_expected_pages",),
    ),
    # --- scope, normalization and drop-explanation joins -------------------------------------------
    "worksheet-render-satisfies-dashboard-page": Mutation(
        code="""
import object_identity as oid
import check_reference_readiness as crr
def match_evidence(obj, evidence):
    named = [e for e in evidence if any(oid.normalize(n) == oid.normalize(obj.name) for n in e.candidate().names)]
    return (named[0], []) if named else (None, [])
crr.match_evidence = match_evidence
""",
        anchor="test_a_worksheet_render_does_not_make_a_dashboard_page_ready",
        controls=(
            "test_a_worksheet_render_does_satisfy_a_worksheet_page",
            "test_a_dashboard_and_a_same_named_worksheet_get_different_page_ids",
        ),
    ),
    "unknown-scope-counts-as-a-match": Mutation(
        code="""
import object_identity as oid
import check_reference_readiness as crr
# Let a record whose producer declared no object type satisfy a page anyway.
def match_evidence(obj, evidence):
    named = [e for e in evidence if any(oid.normalize(n) == oid.normalize(obj.name) for n in e.candidate().names)]
    ok = [e for e in named if e.kind in (obj.kind, oid.KIND_UNKNOWN)]
    return (ok[0], []) if ok else (None, named)
crr.match_evidence = match_evidence
""",
        anchor="test_an_oracle_record_typed_unknown_cannot_satisfy_a_page",
        controls=("test_oracle_evidence_for_this_workbook_does_count",),
    ),
    "drop-explanations-normalize-the-name": Mutation(
        code="""
import object_identity as oid
import check_reference_readiness as crr
# Round-2 finding 5: normalization collapses case and repeated whitespace, so two objects with
# DIFFERENT page ids shared one key and one warning excused the wrong page.
_orig = crr.drop_explanations
class _Loose(dict):
    def __contains__(self, key):
        kind, name = key
        return any(k == kind and oid.normalize(n) == oid.normalize(name) for k, n in self)
    def __getitem__(self, key):
        kind, name = key
        for (k, n), v in self.items():
            if k == kind and oid.normalize(n) == oid.normalize(name):
                return v
        raise KeyError(key)
crr.drop_explanations = lambda handover: _Loose(_orig(handover))
""",
        anchor="test_a_drop_warning_matches_the_exact_object_name_only",
        controls=("test_an_exact_drop_warning_still_explains_its_own_object",),
    ),
    "normalized-name-collisions-are-resolved-not-refused": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr._expectation
def _expectation(unit, report_dir, source):
    result = _orig(unit, report_dir, source)
    if isinstance(result, crr.UnitResult) and "differ only by case" in result.detail:
        return crr.source_objects(source)
    return result
crr._expectation = _expectation
""",
        anchor="test_names_differing_only_by_whitespace_cannot_be_attributed",
        controls=("test_orphan_worksheets_are_expected_pages",),
    ),
    "ambiguous-evidence-picks-the-first": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr.match_evidence
def match_evidence(obj, evidence):
    match, named = _orig(obj, evidence)
    if match is crr.AMBIGUOUS:
        ok = [e for e in named if e.kind == obj.kind]
        return (ok[0] if ok else None), ([] if ok else named)
    return match, named
crr.match_evidence = match_evidence
""",
        anchor="test_two_evidence_records_sharing_a_normalized_name_are_ambiguous",
        controls=("test_a_single_differently_spelled_evidence_record_still_matches",),
    ),
    "drop-explanations-key-on-name-alone": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr.drop_explanations
def drop_explanations(handover):
    byname = {name: reason for (_kind, name), reason in _orig(handover).items()}
    return {(k, name): reason for name, reason in byname.items()
            for k in (crr.KIND_DASHBOARD, crr.KIND_WORKSHEET)}
crr.drop_explanations = drop_explanations
""",
        anchor="test_a_worksheet_warning_cannot_excuse_a_missing_dashboard",
        controls=("test_a_dashboard_scope_warning_does_explain_a_missing_dashboard",),
    ),
    "flat-pbip-warnings-explain-any-drop": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr.drop_explanations
class _Any(dict):
    def __init__(self, base, fallback):
        super().__init__(base)
        self._fallback = fallback
    def __contains__(self, key):
        return dict.__contains__(self, key) or self._fallback is not None
    def __getitem__(self, key):
        return dict.__getitem__(self, key) if dict.__contains__(self, key) else self._fallback
def drop_explanations(handover):
    workbook = (handover or {}).get("workbook") or {}
    flat = [w for w in (workbook.get("pbip_warnings") or [])
            if isinstance(w, str) and any(m in w for m in crr.DELIBERATE_DROP_MARKERS)]
    return _Any(_orig(handover), flat[0] if flat else None)
crr.drop_explanations = drop_explanations
""",
        anchor="test_a_flat_pbip_warning_cannot_explain_any_drop",
        controls=("test_a_dashboard_scope_warning_does_explain_a_missing_dashboard",),
    ),
    "every-dropped-page-is-a-finding": Mutation(
        code="""
import check_reference_readiness as crr
crr.DELIBERATE_DROP_MARKERS = ()
""",
        anchor="test_a_page_the_engine_dropped_with_a_reason_is_accounted_for",
        controls=("test_a_page_the_engine_dropped_silently_is_a_finding",),
    ),
    # --- expectation must not be circular ----------------------------------------------------------
    "expectation-falls-back-to-the-pages-that-were-built": Mutation(
        code="""
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
        anchor="test_the_expectation_never_falls_back_to_the_pages_that_were_built",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    "orphan-worksheets-are-not-expected": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr.source_objects
def source_objects(path):
    objects = _orig(path)
    return None if objects is None else [o for o in objects if o.kind == crr.KIND_DASHBOARD]
crr.source_objects = source_objects
""",
        anchor="test_orphan_worksheets_are_expected_pages",
        controls=("test_a_datasource_only_unit_is_not_applicable",),
    ),
    "page-id-drops-the-scope-prefix": Mutation(
        code="""
import check_reference_readiness as crr
crr.SourceObject.page_id = property(lambda self: crr.engine_page_id(self.name))
""",
        anchor="test_a_dashboard_and_a_same_named_worksheet_get_different_page_ids",
        controls=("test_a_datasource_only_unit_is_not_applicable",),
    ),
    # --- grade bar --------------------------------------------------------------------------------
    "require-validation-grade-does-not-change-page-readiness": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr._page_row
def _page_row(obj, page_status, evidence, reason, require_validation_grade):
    return _orig(obj, page_status, evidence, reason, False)
crr._page_row = _page_row
""",
        anchor="test_require_validation_grade_changes_page_readiness_not_just_the_unit",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    "one-validation-grade-page-silences-the-ceiling": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr._merge
def _merge(root, units, evidence, rejected):
    report = _orig(root, units, evidence, rejected)
    report["all_evidence_validation_grade"] = crr.GRADE_VALIDATION in report["grades_present"]
    return report
crr._merge = _merge
""",
        anchor="test_one_validation_grade_page_does_not_silence_the_ceiling_for_the_rest",
        controls=("test_validation_grade_is_reported_when_present",),
    ),
    # --- round 3: grade may never widen kind, and evidence must be exclusive ---------------------
    "a-grade-promotes-a-record-kind": Mutation(
        code="""
import reference_evidence as ev
import object_identity as oid
# Round-3 finding 1: the repair that made the manual route reachable re-created the founding defect.
# A validation-grade manual record was promoted to a kind matching BOTH dashboards and worksheets.
_orig = ev.Evidence.candidate
def candidate(self):
    base = _orig(self)
    if self.provider == "manual" and self.grade == ev.GRADE_VALIDATION:
        return oid.Candidate(names=base.names, kind=oid.KIND_WORKSHEET)
    return base
ev.Evidence.candidate = candidate
""",
        anchor="test_a_grade_can_never_widen_an_evidence_kind",
        controls=("test_validation_grade_is_reported_when_present",),
    ),
    "one-render-may-satisfy-several-pages": Mutation(
        code="""
import check_reference_readiness as crr
# The alias created a second name with no uniqueness check, so one image made two distinct
# worksheets ready. Identity is not enough on its own - evidence must be EXCLUSIVE.
crr._enforce_exclusivity = lambda rows: None
""",
        anchor="test_one_render_cannot_make_two_pages_ready",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    "untyped-evidence-is-silently-dropped": Mutation(
        code="""
import check_reference_readiness as crr
_orig = crr.render
crr.render = lambda report, *, verbose=False: _orig(report, verbose=verbose).replace("UNTYPED EVIDENCE", "")
""",
        anchor="test_untyped_evidence_is_reported_with_the_route_to_make_it_usable",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    # --- round 3: lossy normalization on engine-to-engine joins ----------------------------------
    "engine-workbook-names-are-normalized-into-sets": Mutation(
        code="""
import check_reference_readiness as crr
import object_identity as oid
# Round-3 finding 2: `_unit_names` returned normalized SETS, so two genuinely distinct workbooks
# collapsed to one key and the collision was permanently discarded.
_orig = crr._unit_names
def _unit_names(engine_report):
    workbooks, datasources = _orig(engine_report)
    return sorted({oid.normalize(n) for n in workbooks}), sorted({oid.normalize(n) for n in datasources})
crr._unit_names = _unit_names
_shipped = crr._units_without_reports
def _units_without_reports(engine_report, reports):
    shipped = {oid.normalize(p.name[: -len(".Report")]) for p in reports}
    workbooks, _ = _unit_names(engine_report)
    return [crr.UnitResult(unit=n, status=crr.STATUS_FINDINGS, detail="no report ships for it")
            for n in sorted(set(workbooks) - shipped)]
crr._units_without_reports = _units_without_reports
""",
        anchor="test_two_engine_workbooks_differing_only_by_whitespace_are_both_required",
        controls=("test_a_workbook_whose_report_never_shipped_is_a_finding",),
    ),
    "a-duplicated-engine-workbook-is-deduplicated": Mutation(
        code="""
import check_reference_readiness as crr
import object_identity as oid
oid.duplicates = lambda names: []
crr.oid.duplicates = lambda names: []
""",
        anchor="test_a_duplicated_engine_workbook_name_cannot_be_attributed",
        controls=("test_a_workbook_whose_report_never_shipped_is_a_finding",),
    ),
    "the-datasource-classification-normalizes": Mutation(
        code="""
import check_reference_readiness as crr
import object_identity as oid
_orig = crr._datasource_only
def _datasource_only(unit, report_dir, engine_report):
    workbooks, datasources = crr._unit_names(engine_report)
    norm = {oid.normalize(n) for n in datasources}
    if engine_report is not None and oid.normalize(unit) in norm and unit not in workbooks:
        return crr.UnitResult(unit=unit, status=crr.STATUS_NOT_APPLICABLE,
                              detail="datasource-only unit", report_dir=str(report_dir))
    return _orig(unit, report_dir, engine_report)
crr._datasource_only = _datasource_only
""",
        anchor="test_a_datasource_classification_uses_the_exact_name",
        controls=("test_a_datasource_only_unit_is_not_applicable",),
    ),
    "source-asset-selection-normalizes-the-stem": Mutation(
        code="""
from pathlib import Path
import check_reference_readiness as crr
import object_identity as oid
_orig = crr.resolve_source
def resolve_source(root, unit, handover, explicit):
    found = _orig(root, unit, handover, explicit)
    if found is not None:
        return found
    manifest = crr.json_object(root / "input_manifest.json")
    for asset in (manifest or {}).get("assets") or []:
        name = str(asset.get("name") or "")
        if Path(name).stem and oid.normalize(Path(name).stem) == oid.normalize(unit):
            candidate = root.parent / "assets" / name
            if candidate.is_file():
                return candidate
    return None
crr.resolve_source = resolve_source
""",
        anchor="test_source_asset_selection_uses_the_exact_stem",
        controls=("test_a_worksheet_render_does_satisfy_a_worksheet_page",),
    ),
    # --- round 3: the abstraction's own guarantees ------------------------------------------------
    "a-resolution-can-be-read-without-being-unique": Mutation(
        code="""
import object_identity as oid
oid.Resolution.value = lambda self: self.matches[0] if self.matches else None
""",
        anchor="test_reading_an_ambiguous_resolution_raises_rather_than_picking",
        controls=("test_collisions_and_duplicates_preserve_multiplicity",),
    ),
    "an-unknown-kind-can-become-an-identity": Mutation(
        code="""
import object_identity as oid
oid.ObjectIdentity.__post_init__ = lambda self: None
oid.ObjectIdentity.from_engine = classmethod(
    lambda cls, kind, name: cls(kind=kind, name=name) if isinstance(name, str) and name.strip() else None
)
""",
        anchor="test_from_engine_returns_none_where_the_constructor_raises",
        controls=("test_collisions_and_duplicates_preserve_multiplicity",),
    ),
    "an-index-overwrites-instead-of-appending": Mutation(
        code="""
import object_identity as oid
# Multiplicity is what makes a collision visible; overwriting deletes it silently.
def _store(self, key, candidate, value):
    self._by_key[key] = [(candidate, value)]
oid._Index._store = _store
""",
        anchor="test_reading_an_ambiguous_resolution_raises_rather_than_picking",
        controls=("test_an_engine_index_has_no_normalized_layer_to_fall_back_to",),
    ),
    # --- round 4: the quarantine rule that closes the residual risk --------------------------------
    "the-quarantine-rule-sees-no-violations": Mutation(
        code="""
import check_identity_normalization as rule
# The rule's own fail-open: a detector that reports nothing passes the real-repo assertion while
# proving nothing at all.
rule.scan_source = lambda path, source: []
""",
        anchor="test_a_module_alias_call_is_caught",
        controls=("test_the_repository_has_no_lossy_join_outside_the_identity_module",),
    ),
    "the-quarantine-rule-matches-on-the-name-alone": Mutation(
        code="""
import ast
import check_identity_normalization as rule
# Drop the per-file alias resolution and match any callee spelled `normalize`. This is the BROAD
# rule the reviewer warned against: it still catches real violations, so only the false-positive
# controls can tell it apart from the narrow one - and a rule that cries wolf gets switched off.
def scan_source(path, source):
    if path.name == rule.OWNER_FILE:
        return []
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == rule.GUARDED_FUNCTION:
                found.append(rule.Violation(path.name, node.lineno, rule._expression(node)))
    return found
rule.scan_source = scan_source
""",
        anchor="test_another_modules_own_normalize_is_not_reported",
        controls=("test_a_module_alias_call_is_caught",),
    ),
    "the-quarantine-rule-skips-unparseable-files": Mutation(
        code="""
import check_identity_normalization as rule
_orig = rule.scan_source
def scan_source(path, source):
    try:
        return _orig(path, source)
    except Exception:
        return []
import ast
_parse = ast.parse
def parse(src, *a, **kw):
    return _parse(src, *a, **kw)
rule.scan_source = lambda path, source: [] if _unparseable(source) else _orig(path, source)
def _unparseable(source):
    try:
        ast.parse(source)
        return False
    except SyntaxError:
        return True
""",
        anchor="test_an_unparseable_file_is_reported_rather_than_skipped",
        controls=("test_a_module_alias_call_is_caught",),
    ),
    "the-quarantine-rule-refuses-without-naming-the-fix": Mutation(
        code="""
import check_identity_normalization as rule
rule.FIX = "do not do this"
""",
        anchor="test_the_rendered_verdict_names_the_fix_not_just_the_refusal",
        controls=("test_a_module_alias_call_is_caught",),
    ),
    "the-quarantine-rule-ignores-string-literals-by-scanning-text": Mutation(
        code="""
import check_identity_normalization as rule
# Textual scanning instead of AST: catches real calls, but also every mutation SOURCE string in this
# very harness, so the rule would fight the tests that defend the invariant.
def scan_source(path, source):
    if path.name == rule.OWNER_FILE:
        return []
    hits = []
    for number, line in enumerate(source.splitlines(), start=1):
        if f".{rule.GUARDED_FUNCTION}(" in line or line.strip().startswith(f"{rule.GUARDED_FUNCTION}("):
            hits.append(rule.Violation(path.name, number, f"{rule.GUARDED_FUNCTION}()"))
    return hits
rule.scan_source = scan_source
""",
        anchor="test_an_occurrence_inside_a_string_literal_is_not_reported",
        controls=("test_a_local_function_called_normalize_is_not_reported",),
    ),
    "evidence-attribution-normalizes-the-workbook-name": Mutation(
        code="""
import object_identity as oid
import reference_evidence as ev
# Found by WRITING the quarantine rule, not by reasoning: `Evidence.is_for` compared workbook names
# through the lossy function, so two workbooks differing only by whitespace would have swapped
# captures. A layer nobody had enumerated.
def is_for(self, unit):
    if self.workbook_sha is not None:
        return self.workbook_sha.casefold() == unit.source_sha256.casefold()
    if self.workbook_luid and unit.workbook_luid:
        return self.workbook_luid.casefold() == unit.workbook_luid.casefold()
    return bool(self.workbook_name) and oid.normalize(self.workbook_name) == oid.normalize(unit.name)
ev.Evidence.is_for = is_for
""",
        anchor="test_evidence_attribution_uses_the_exact_workbook_name",
        controls=("test_oracle_evidence_for_this_workbook_does_count",),
    ),
    # --- round 4: absence is not prohibition - every "cannot" must RAISE -------------------------
    "the-identity-constructor-does-not-validate": Mutation(
        code="""
import object_identity as oid
# Measured: a frozen dataclass has a PUBLIC constructor, so ObjectIdentity(KIND_UNKNOWN, "Ops") built
# fine while from_engine refused it. Removing __post_init__ restores exactly that.
oid.ObjectIdentity.__post_init__ = lambda self: None
""",
        anchor="test_the_public_constructor_refuses_an_unidentifiable_kind",
        controls=("test_collisions_and_duplicates_preserve_multiplicity",),
    ),
    "a-resolution-is-truthy-instead-of-raising": Mutation(
        code="""
import object_identity as oid
# The reasoning error round 4 corrected: OMITTING __bool__ does not prevent truth-testing, it makes
# the object truthy. Deleting it is therefore the real regression, not an equivalent.
del oid.Resolution.__bool__
""",
        anchor="test_truth_testing_a_resolution_raises",
        controls=("test_reading_an_ambiguous_resolution_raises_rather_than_picking",),
    ),
    "the-matches-are-public-again": Mutation(
        code="""
import object_identity as oid
# Re-expose the raw collection, which is all `resolution.matches[0]` ever needed.
oid.Resolution.matches = property(lambda self: tuple(value for _c, value in self._matches))
""",
        anchor="test_the_matches_are_not_reachable_as_a_public_collection",
        controls=("test_reading_an_ambiguous_resolution_raises_rather_than_picking",),
    ),
    "a-candidate-index-accepts-an-engine-identity": Mutation(
        code="""
import object_identity as oid
# Measured: a normalized index accepted an ObjectIdentity and then uniquely resolved a DIFFERENT
# engine name through the lossy key.
_orig = oid.CandidateIndex.add
def add(self, candidate, value):
    if isinstance(candidate, oid.ObjectIdentity):
        candidate = oid.Candidate(names=(candidate.name,), kind=candidate.kind)
    return _orig(self, candidate, value)
oid.CandidateIndex.add = add
""",
        anchor="test_a_candidate_index_refuses_an_object_identity_by_type",
        controls=("test_a_candidate_index_resolves_a_spelling_difference_but_refuses_a_collision",),
    ),
    "an-engine-index-gains-a-normalized-key": Mutation(
        code="""
import object_identity as oid
_orig = oid.EngineIndex.resolve
def resolve(self, identity):
    found = _orig(self, identity)
    if found.outcome != oid.ABSENT:
        return found
    hits = [entry for (kind, key), entries in self._by_key.items()
            if kind == identity.kind and oid.normalize(key) == oid.normalize(identity.name)
            for entry in entries]
    return oid.Resolution(identity=identity, _matches=tuple(hits))
oid.EngineIndex.resolve = resolve
""",
        anchor="test_an_engine_index_has_no_normalized_layer_to_fall_back_to",
        controls=("test_a_candidate_index_resolves_a_spelling_difference_but_refuses_a_collision",),
    ),
    "a-lookalike-carries-the-evidence-again": Mutation(
        code="""
import object_identity as oid
# The previous helper returned a bool and so worked as a resolution predicate. Giving Lookalike a
# `.value` is the same bypass, one field along.
_orig = oid.name_lookalikes
def name_lookalikes(identity, candidates):
    found = _orig(identity, candidates)
    for item in found:
        object.__setattr__(item, "value", candidates[0])
    return found
oid.name_lookalikes = name_lookalikes
""",
        anchor="test_name_lookalikes_cannot_be_used_to_select_evidence",
        controls=("test_collisions_and_duplicates_preserve_multiplicity",),
    ),
    # --- round 4: exclusivity by FILE identity, across all units -----------------------------------
    "exclusivity-compares-path-text": Mutation(
        code="""
import check_reference_readiness as crr
# Measured: comparing `evidence_path` strings left both pages ready when the same physical PNG was
# referenced under two spellings that `Path.samefile()` calls identical.
crr._render_key = lambda path: path
""",
        anchor="test_the_same_file_under_two_spellings_is_still_one_render",
        controls=("test_one_render_cannot_make_two_pages_ready",),
    ),
    "exclusivity-runs-inside-each-unit": Mutation(
        code="""
import check_reference_readiness as crr
# Measured: run per unit, the same render satisfied one page in EACH of two units and the bundle
# reported READY 2/2. This restores exactly that scoping - each unit folds its own rows, and the
# bundle-wide pass is a no-op.
_enforce = crr._enforce_exclusivity
_result = crr._readiness_result
def _readiness_result(unit, report_dir, source, rows):
    _enforce(rows)
    return _result(unit, report_dir, source, rows)
crr._readiness_result = _readiness_result
crr._enforce_exclusivity = lambda rows: None
""",
        anchor="test_one_render_cannot_satisfy_a_page_in_each_of_two_units",
        controls=("test_one_render_cannot_make_two_pages_ready",),
    ),
    # --- whole-suite discriminating controls ------------------------------------------------------
    "control-cosmetic-reword-of-a-rendered-line": Mutation(
        code="""
import check_reference_readiness as crr
# Pure presentation. If this is CAUGHT, the suite is asserting on incidental wording.
_orig = crr._render_page
crr._render_page = lambda page: _orig(page).replace(" -> ", " ==> ").replace("    - ", "  * ")
""",
        whole_suite=SURVIVED,
    ),
    "control-absent-anchor": Mutation(
        code="""
import check_reference_readiness as crr
# Names something that does not exist. Must be reported invalid, never credited as a detection.
crr.no_such_function_exists.disabled = True
""",
        whole_suite=INVALID,
    ),
}


@dataclass
class Failure:
    """One expectation that did not hold."""

    mutation: str
    node: str
    want: str
    got: str
    detail: str = field(default="")

    def __str__(self) -> str:
        return f"{self.mutation} / {self.node}: want {self.want}, got {self.got} ({self.detail})"


def resolve_node(name: str) -> str:
    """The full pytest node id for an anchor, or a hard error if it is not uniquely findable."""
    hits = [target for target in TARGETS if f"def {name}(" in (ROOT / target).read_text(encoding="utf-8")]
    if len(hits) != 1:
        raise SystemExit(f"anchor {name!r} found in {len(hits)} suite(s), expected exactly 1: {hits}")
    return f"{hits[0]}::{name}"


def baseline_is_clean() -> bool:
    """A mutation is only evidence against a clean baseline."""
    proc = subprocess.run(
        [PY, "-m", "pytest", *TARGETS, "-q", "--no-header", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=sanitized_env(),
    )
    print(f"BASELINE {len(TARGETS)} suite(s) exit={proc.returncode}")
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
    return proc.returncode == 0


def verdict_for(name: str, code: str, target: str) -> tuple[str, str]:
    """Score one mutation against one pytest target, using the shared harness's lifecycle record."""
    try:
        _, exit_code, detail, outcomes = run(name, code, target)
    except SystemExit as exc:
        # `run()` raises when the injected plugin never imported - the absent-anchor case.
        return INVALID, str(exc)
    if observed_mutation(outcomes):
        note = detail if not session_ended_abnormally(outcomes) else f"{detail} [abnormal exit {exit_code}]"
        return CAUGHT, note
    if session_is_trustworthy(outcomes):
        return SURVIVED, detail
    return INVALID, f"no verdict (exit {exit_code}, {detail})"


def check(name: str, mutation: Mutation) -> list[Failure]:
    """Run one mutation against its anchor and controls, or against the whole suite."""
    failures: list[Failure] = []
    if mutation.whole_suite is not None:
        got, detail = verdict_for(name, mutation.code, WHOLE_SUITE)
        flag = "ok " if got == mutation.whole_suite else "BAD"
        print(f"{flag} {got:8s} (want {mutation.whole_suite:8s})  {name:56s} <whole suite>")
        if got != mutation.whole_suite:
            failures.append(Failure(name, "<whole suite>", mutation.whole_suite, got, detail))
        return failures
    expectations = [(mutation.anchor or "", CAUGHT), *((node, SURVIVED) for node in mutation.controls)]
    for node, want in expectations:
        got, detail = verdict_for(name, mutation.code, resolve_node(node))
        flag = "ok " if got == want else "BAD"
        print(f"{flag} {got:8s} (want {want:8s})  {name:56s} {node}")
        if got != want:
            failures.append(Failure(name, node, want, got, detail))
    return failures


def main() -> int:
    """Run every mutation against its committed anchor, and fail on any mismatch."""
    if not baseline_is_clean():
        print("\nHARNESS ERROR: baseline is not clean, so no mutation verdict is trustworthy.")
        return 2
    print()
    failures = [failure for name, mutation in MUTATIONS.items() for failure in check(name, mutation)]
    checks = sum(1 if m.whole_suite else 1 + len(m.controls) for m in MUTATIONS.values())
    print()
    if failures:
        print("MUTATION EXPECTATIONS NOT MET:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"All {len(MUTATIONS)} mutations matched their expectations ({checks} anchor/control checks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
