"""
purpose: mutation-test the page-check behaviours in scripts/check_unit.py - break each rule in the
         production code and prove a specific test fails. Evidence that the tests can fail, kept in
         the repo rather than in a review transcript.
usage:   python tests/mutate_check_unit.py [--list] [--only <substring>]
         exit 0 when every mutation is CAUGHT and the negative control SURVIVES.

Why this exists: a covering test that cannot fail is credited as coverage and protects nothing. Two
things this harness refuses to do, both of which have produced false 100% scores in this repo:

* A mutation counts as CAUGHT only on a genuine pytest ``N failed``. A collection error, an import
  error or any other non-zero exit is reported BROKEN - never caught.
* A no-op NEGATIVE CONTROL is run through the identical path and must SURVIVE, which proves the
  harness can report a survivor at all.

Each mutation names the anchor tests it must kill, and is run against each anchor alone.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "scripts" / "check_unit.py"
# Both suites are collected and ``-k`` picks the anchor out of them: the _slug census gate lives in
# its own file but is a test OF this production file, so its mutations belong in this campaign.
SUITES = (REPO_ROOT / "tests" / "test_check_unit.py", REPO_ROOT / "tests" / "test_slug_call_site_census.py")

NEGATIVE_CONTROL = "NEGATIVE CONTROL"

# (label, exact snippet to replace, replacement, anchor tests that must fail)
MUTATIONS: list[tuple[str, str, str, list[str]]] = [
    (
        "expected pages: orphan worksheets dropped from the candidate set",
        '    orphans = [page for page in (worksheets or []) if page["id"] not in placed]',
        "    orphans = []",
        ["test_expected_pages_counts_orphan_worksheets_not_just_dashboards"],
    ),
    (
        "expected pages: zone walk stops recursing, so placed sheets look like orphans",
        '        _zone_worksheet_ids(zone.get("children"), found)',
        "        return",
        ["test_expected_pages_finds_placed_worksheets_at_any_zone_depth"],
    ),
    (
        "spec validation: a malformed required collection is skipped, not refused",
        "    if not isinstance(items, list):\n        return None, (",
        "    if False:  # pragma: no cover\n        return None, (",
        ["test_malformed_required_collection_is_unassessable_not_a_smaller_denominator"],
    ),
    (
        "spec validation: an unidentifiable entry is skipped, not refused",
        "            return None, f\"migration-spec.json '{collection}' entry #{index} has no usable name\"",
        "            continue",
        ["test_a_spec_entry_with_no_usable_identity_is_unassessable"],
    ),
    (
        "spec validation: an id-only entry is dropped instead of kept in the denominator",
        '        name = item.get("name") or item.get("title") or item.get("id")',
        '        name = item.get("name") or item.get("title")',
        ["test_an_id_only_spec_entry_stays_in_the_denominator"],
    ),
    (
        "spec validation: an unwalkable zone tree is ignored",
        "        if zones is not None and not isinstance(zones, (dict, list)):",
        "        if False:  # pragma: no cover",
        ["test_an_unwalkable_zone_tree_is_unassessable"],
    ),
    (
        "spec validation: a missing required array is tolerated",
        '    missing = [name for name in ("dashboards", "worksheets") if name not in payload]',
        "    missing = []",
        ["test_spec_declaring_no_pages_cannot_be_graded"],
    ),
    (
        "APPROVAL: engine evidence accepts an omission, no signature needed",
        "        elif signature == SIGNATURE_UNIQUE and not ambiguous:\n            disposition = OMISSION_SIGNED",
        '        elif (signature == SIGNATURE_UNIQUE or page["declared_reason"]) and not ambiguous:\n'
        "            disposition = OMISSION_SIGNED",
        ["test_engine_evidence_explains_an_omission_but_does_not_accept_it"],
    ),
    (
        "APPROVAL: a signature stops accepting an omission",
        "        elif signature == SIGNATURE_UNIQUE and not ambiguous:\n            disposition = OMISSION_SIGNED",
        "        elif False:  # pragma: no cover\n            disposition = OMISSION_SIGNED",
        ["test_a_signed_omission_with_engine_evidence_passes_and_counts_as_a_compromise"],
    ),
    (
        "APPROVAL: the oracle denominator drops engine-declared omissions too",
        '        if row["disposition"] in OMISSIONS_OWING_NOTHING',
        '        if row["disposition"] in OMISSIONS_OWING_NOTHING | {OMISSION_DECLARED}',
        ["test_oracle_coverage_still_expects_a_page_the_engine_merely_declared"],
    ),
    (
        "APPROVAL: the oracle denominator keeps a signed omission",
        '    accepted = _oracle_excluded_omissions(expectation, load_exemptions(target)["entries"])',
        "    accepted = []",
        ["test_oracle_coverage_drops_a_signed_omission_from_the_denominator"],
    ),
    (
        "KIND: evidence is indexed under both kinds, so a worksheet row settles a dashboard",
        "        index.add(identity, text)",
        "        index.add(identity, text)\n"
        "        for kind in oid.IDENTIFIABLE_KINDS:\n"
        "            crossed = oid.ObjectIdentity.from_engine(kind, identity.name)\n"
        "            if crossed is not None and crossed != identity:\n"
        "                index.add(crossed, text)",
        ["test_a_worksheet_row_can_never_explain_a_same_named_dashboard"],
    ),
    (
        "KIND: a candidate's own kind is ignored when resolving evidence",
        '    return oid.ObjectIdentity.from_engine(str(page.get("kind") or ""), page.get("name"))',
        '    return oid.ObjectIdentity.from_engine(oid.KIND_WORKSHEET, page.get("name"))',
        ["test_a_worksheet_row_can_never_explain_a_same_named_dashboard"],
    ),
    (
        "EVIDENCE: visual_type is no longer required, so a non-object scope row becomes evidence",
        '    if row.get("tier") != DROP_EVIDENCE_TIER or row.get("visual_type") != DROP_EVIDENCE_VISUAL_TYPE:',
        '    if row.get("tier") != DROP_EVIDENCE_TIER:',
        ["test_a_filter_scope_row_cannot_explain_a_same_named_worksheet"],
    ),
    (
        "EVIDENCE: tier is no longer required, so a degraded row explains a drop",
        '    if row.get("tier") != DROP_EVIDENCE_TIER or row.get("visual_type") != DROP_EVIDENCE_VISUAL_TYPE:',
        '    if row.get("visual_type") != DROP_EVIDENCE_VISUAL_TYPE:',
        ["test_a_row_with_no_tier_at_all_leaves_the_omission_unexplained"],
    ),
    (
        "EVIDENCE: workbook binding removed, so any slice explains any unit",
        "        bound.append((name, workbook if exact or loose else None))",
        "        bound.append((name, workbook))",
        ["test_another_workbooks_row_cannot_explain_this_units_omission"],
    ),
    (
        "EVIDENCE: read from pbip_warnings instead of viz_fidelity",
        '        _collect_drop_rows(workbook.get("viz_fidelity"), index, described)',
        "        _collect_drop_rows(\n"
        '            [{"worksheet": "B", "tier": "empty", "visual_type": "unsupported", "reason": w} '
        'for w in (workbook.get("pbip_warnings") or [])]\n'
        '            or workbook.get("viz_fidelity"),\n'
        "            index,\n"
        "            described,\n"
        "        )",
        ["test_drop_evidence_comes_from_viz_fidelity_not_pbip_warnings"],
    ),
    (
        "EVIDENCE: an ambiguous resolution is read as its first match",
        "    if resolution.outcome != oid.UNIQUE:\n        return None",
        "    if resolution.outcome == oid.ABSENT:\n        return None\n    return resolution.matches[0]",
        ["test_two_identical_evidence_rows_resolve_to_nothing_rather_than_the_first"],
    ),
    (
        "PAGES: an EMITTED candidate is still treated as an omission",
        "    absent = [page for page in candidates if id(page) not in satisfied]",
        "    absent = list(candidates)",
        ["test_an_emitted_page_is_never_an_omission"],
    ),
    (
        "PAGES: a zero-visual page counts as a rebuilt page",
        '    rendered = [page for page in actual if page.get("visuals", 0) > 0]',
        "    rendered = list(actual)",
        ["test_a_page_with_no_visuals_does_not_certify_a_candidate_as_rebuilt"],
    ),
    (
        "PAGES: visual count is fabricated instead of measured",
        '    return sum(1 for _ in visuals_root.rglob("visual.json"))',
        "    return 1",
        ["test_actual_pages_counts_zero_visuals_for_a_page_with_none"],
    ),
    (
        "PLACEHOLDER: identity drops the display-name clause",
        '    return page.get("name") == ENGINE_PLACEHOLDER_PAGE_NAME and str(page.get("id", "")).startswith(\n'
        "        ENGINE_PLACEHOLDER_PAGE_ID_PREFIX\n"
        "    )",
        '    return str(page.get("id", "")).startswith(ENGINE_PLACEHOLDER_PAGE_ID_PREFIX)',
        ["test_a_retitled_placeholder_id_is_still_a_blank_page"],
    ),
    (
        "PLACEHOLDER: identity drops the id-prefix clause",
        '    return page.get("name") == ENGINE_PLACEHOLDER_PAGE_NAME and str(page.get("id", "")).startswith(\n'
        "        ENGINE_PLACEHOLDER_PAGE_ID_PREFIX\n"
        "    )",
        '    return page.get("name") == ENGINE_PLACEHOLDER_PAGE_NAME',
        ["test_a_blank_page_titled_like_the_placeholder_is_still_blank"],
    ),
    (
        "PLACEHOLDER: a declared crash-guard page is reported as a blank page",
        "    placeholders = [page for page in blankish if _is_engine_placeholder_page(page)]",
        "    placeholders = []",
        ["test_engine_crash_guard_placeholder_is_not_a_blank_page"],
    ),
    (
        "BLANK: a page that renders nothing no longer fails the gate",
        '        if not unsigned and not unaccounted_extra and not blank and not expectation["contested_names"]',
        '        if not unsigned and not unaccounted_extra and not expectation["contested_names"]',
        ["test_a_blank_page_alone_fails_the_gate_even_when_every_page_is_paired"],
    ),
    (
        "ATTRIBUTION: an omission is named by position rather than by content",
        "    absent = [page for page in candidates if id(page) not in satisfied]",
        "    absent = candidates[-1:]",
        ["test_a_missing_page_is_named_by_content_not_by_position"],
    ),
    (
        "ATTRIBUTION: an extra page is named by position rather than by content",
        "        if claim.outcome == oid.ABSENT:\n            unmatched.append(page)",
        "        if True:\n            unmatched.append(page)",
        ["test_an_extra_page_is_named_by_content_not_by_position"],
    ),
    (
        "SIGNATURE: an exemption applies even to a page that is present",
        '    for page in expectation["omissions"]:\n        signature = signatures.signature_for(page)',
        '    for page in expectation["candidates"]:\n        signature = signatures.signature_for(page)',
        ["test_an_exemption_naming_a_present_page_accepts_nothing"],
    ),
    (
        "SIGNATURE: rename ambiguity no longer suspends a name-only signature (parity side)",
        "        elif signature == SIGNATURE_UNIQUE and not ambiguous:",
        "        elif signature == SIGNATURE_UNIQUE:",
        ["test_a_rename_makes_attribution_ambiguous_and_suspends_every_signature"],
    ),
    (
        "SIGNATURE: rename ambiguity no longer suspends a name-only signature (oracle side)",
        '    return bool(unaccounted) or bool(expectation["contested_names"])',
        "    return False",
        ["test_a_rename_suspends_signatures_on_the_oracle_denominator_too"],
    ),
    (
        "SIGNATURE: an unaccounted rendered page no longer creates ambiguity",
        '    return bool(unaccounted) or bool(expectation["contested_names"])',
        '    return bool(expectation["contested_names"])',
        ["test_a_rename_makes_attribution_ambiguous_and_suspends_every_signature"],
    ),
    (
        "SIGNATURE: an extra: signature cannot resolve the ambiguity",
        '    unaccounted = [page for page in expectation["unmatched_rendered"] if id(page) not in '
        "signatures.accounted_extra]",
        '    unaccounted = list(expectation["unmatched_rendered"])',
        ["test_declaring_the_renamed_page_resolves_the_ambiguity"],
    ),
    (
        "COMPROMISE: a signature that accepted nothing is still counted",
        '    unapplied = sum(len(check.get("unapplied_exemptions") or []) for check in report["checks"])',
        "    unapplied = 0",
        ["test_an_exemption_naming_a_present_page_accepts_nothing"],
    ),
    (
        "oracle: circular denominator restored (expected or actual)",
        '    if not expectation["assessable"]:\n        return _oracle_not_assessable(',
        '    if not expectation["assessable"]:\n'
        '        expectation = {**expectation, "assessable": True, "candidates": expectation["actual"], '
        '"omissions": [], "attribution_ambiguous": False}\n'
        '    if not expectation["assessable"]:\n        return _oracle_not_assessable(',
        [
            "test_oracle_coverage_without_an_expected_set_is_blocking_not_a_pass",
            "test_unassessable_oracle_coverage_fails_the_whole_run_closed",
        ],
    ),
    (
        "SHARED TYPE: pairing falls back to a raw name, so two candidates share one page",
        '        elif claim.outcome == oid.AMBIGUOUS or rendered_names.count(page["name"]) != 1:\n'
        "            contested.add(claim.name)",
        "        elif False:  # pragma: no cover\n            contested.add(claim.name)",
        ["test_two_same_named_candidates_cannot_share_one_rendered_page"],
    ),
    (
        "SHARED TYPE: typed oracle evidence still uses a bare-name claim",
        "    record, refusal = evidence.evidence_for(page)\n"
        "    if record is not None:\n"
        "        identity = _candidate_identity(page)\n"
        "        typed_unique = identity is not None and index.resolve(identity).outcome == oid.UNIQUE\n"
        "        contested = not typed_unique\n"
        "        if contested:\n"
        "            record = None\n"
        "    else:\n"
        '        contested = _claim(index, page["name"]).outcome != oid.UNIQUE',
        '    contested = _claim(index, page["name"]).outcome != oid.UNIQUE\n'
        "    record, refusal = evidence.evidence_for(page)",
        ["test_typed_oracle_records_cover_same_named_dashboard_and_worksheet"],
    ),
    (
        "SHARED TYPE: a bare-name signature is applied without resolving it",
        "        if len(matched) == 1:\n            applied[_page_key(next(iter(matched.values())))] = item",
        "        if matched:\n            applied[_page_key(next(iter(matched.values())))] = item",
        ["test_one_name_only_signature_cannot_sign_two_omissions"],
    ),
    (
        "SHARED TYPE: an id signature no longer resolves a contested name",
        '        matched = {id(page): page for page in expectation["candidates"] if item in {page["id"], '
        'page["name"]}}',
        '        matched = {id(page): page for page in expectation["candidates"] if item == page["name"]}',
        ["test_a_signature_naming_the_page_id_resolves_a_contested_name"],
    ),
    (
        "PATHS: handover roots de-duplicated before they are resolved",
        "        resolved = candidate.resolve()\n        if resolved not in roots:\n            roots.append(resolved)",
        "        if candidate not in roots:\n            roots.append(candidate)",
        ["test_evidence_is_found_from_a_relative_target_path"],
    ),
    (
        "SOURCE-EMPTY: a blank source worksheet is still reported as a rebuild gap",
        '        if page.get("source_empty"):\n            disposition = OMISSION_SOURCE_EMPTY',
        "        if False:  # pragma: no cover\n            disposition = OMISSION_SOURCE_EMPTY",
        ["test_a_source_empty_worksheet_owes_no_page"],
    ),
    (
        "SOURCE-EMPTY: a populated shelf is treated as blank",
        "    if not isinstance(encodings, dict) or set(encodings) != encoding_keys or any(encodings.values()):",
        "    if not isinstance(encodings, dict) or set(encodings) != encoding_keys:",
        ["test_a_worksheet_with_any_encoding_still_owes_a_page"],
    ),
    (
        "SOURCE-EMPTY: a non-object encodings value is treated as blank",
        "    if not isinstance(encodings, dict) or set(encodings) != encoding_keys or any(encodings.values()):",
        "    if set(encodings) != encoding_keys or any(encodings.values()):",
        ["test_a_worksheet_whose_encodings_are_not_an_object_is_not_called_empty"],
    ),
    (
        "SOURCE-EMPTY: a filter no longer counts as content",
        "    return not any(item[key] for key in worksheet_keys - NON_CONTENT_WORKSHEET_KEYS)",
        '    return not any(item[key] for key in worksheet_keys - NON_CONTENT_WORKSHEET_KEYS - {"filters"})',
        ["test_a_source_empty_sheet_that_still_has_a_filter_owes_a_page"],
    ),
    (
        "SIGNATURE: a plain item is matched through the lossy slug",
        '        matched = {id(page): page for page in expectation["candidates"] if item in {page["id"], '
        'page["name"]}}',
        '        matched = {id(page): page for page in expectation["candidates"] if _slug(item) in '
        '{_slug(page["id"]), _slug(page["name"])}}',
        ["test_a_punctuation_variant_name_is_not_the_same_signature"],
    ),
    (
        "SIGNATURE: an extra: item is matched through the lossy slug",
        '        matched = {id(page): page for page in expectation["rendered"] if page["name"] == item}',
        '        matched = {id(page): page for page in expectation["rendered"] if _slug(page["name"]) == _slug(item)}',
        ["test_an_extra_signature_matches_the_page_name_exactly"],
    ),
    (
        "SPEC: a reused page id is accepted, so one signature signs two pages",
        '    repeated = oid.duplicates([page["id"] for page in candidates])',
        "    repeated = []",
        ["test_a_spec_that_reuses_a_page_id_cannot_be_graded"],
    ),
    (
        "SOURCE-EMPTY: a visible title is no longer content",
        "    return not any(item[key] for key in worksheet_keys - NON_CONTENT_WORKSHEET_KEYS)",
        '    return not any(item[key] for key in worksheet_keys - NON_CONTENT_WORKSHEET_KEYS - {"title_text"})',
        ["test_a_visible_title_is_content_even_with_every_shelf_empty"],
    ),
    (
        "SOURCE-EMPTY: an incomplete worksheet structure is accepted as proof",
        "    if set(item) != worksheet_keys:\n        return False",
        "    if False:  # pragma: no cover\n        return False",
        ["test_a_worksheet_missing_one_schema_key_is_not_proof_of_emptiness"],
    ),
    (
        "SOURCE-EMPTY: an incomplete encodings structure is accepted as proof",
        "    if not isinstance(encodings, dict) or set(encodings) != encoding_keys or any(encodings.values()):",
        "    if not isinstance(encodings, dict) or any(encodings.values()):",
        ["test_a_complete_worksheet_with_partial_encodings_is_not_proof_of_emptiness"],
    ),
    (
        "SOURCE-EMPTY: the spec version is no longer checked before classifying",
        '    shape = _source_content_shape() if payload.get("migration_spec_version") == SOURCE_EMPTY_SPEC_VERSION '
        "else None",
        "    shape = _source_content_shape()",
        ["test_an_unrecognised_spec_version_cannot_be_classified_as_empty"],
    ),
    (
        "SOURCE-EMPTY: the channel set is hard-coded instead of read from the schema",
        '    worksheet = ((schema.get("properties") or {}).get("worksheets") or {}).get("items") or {}',
        '    worksheet = {"properties": {"encodings": {"properties": {"rows": {}}}, "id": {}, "name": {}}}',
        ["test_a_source_empty_worksheet_owes_no_page"],
    ),
    (
        "ORACLE: the denominator stops honouring a source-empty omission",
        '        if row["disposition"] in OMISSIONS_OWING_NOTHING',
        '        if row["disposition"] in {OMISSION_SIGNED}',
        ["test_a_source_empty_page_owes_no_oracle_evidence_either"],
    ),
    (
        "ORACLE: an emptied denominator drops the reason it emptied",
        "            excluded=accepted,",
        "            excluded=None,",
        ["test_an_emptied_oracle_denominator_still_names_why_it_emptied"],
    ),
    (
        "GLOBALITY: a signature is resolved per page instead of once globally",
        '        matched = {id(page): page for page in expectation["candidates"] if item in {page["id"], '
        'page["name"]}}',
        '        matched = {id(page): page for page in expectation["candidates"][:1] '
        'if item in {page["id"], page["name"]}}',
        ["test_one_item_matching_two_objects_across_namespaces_signs_neither"],
    ),
    (
        "GLOBALITY: matches are counted by row rather than by distinct object",
        '        matched = {id(page): page for page in expectation["candidates"] if item in {page["id"], '
        'page["name"]}}',
        "        matched = dict(\n"
        "            enumerate(\n"
        '                [page for page in expectation["candidates"] if item == page["id"]]\n'
        '                + [page for page in expectation["candidates"] if item == page["name"]]\n'
        "            )\n"
        "        )",
        ["test_an_item_matching_one_object_through_both_namespaces_still_applies"],
    ),
    (
        "GLOBALITY: an extra: item accepts every page sharing its name",
        '        matched = {id(page): page for page in expectation["rendered"] if page["name"] == item}\n'
        "        if len(matched) == 1:\n"
        "            accounted.add(next(iter(matched)))",
        '        matched = {id(page): page for page in expectation["rendered"] if page["name"] == item}\n'
        "        if matched:\n"
        "            accounted.update(matched)",
        ["test_one_extra_signature_cannot_account_for_two_same_named_pages"],
    ),
    (
        "IDENTITY->STR: the oracle denominator removes by display name",
        "    excluded_keys = {_page_key(page) for page in accepted}\n"
        '    pages = [page for page in expectation["candidates"] if _page_key(page) not in excluded_keys]',
        '    excluded_keys = {page["name"] for page in accepted}\n'
        '    pages = [page for page in expectation["candidates"] if page["name"] not in excluded_keys]',
        ["test_the_oracle_denominator_removes_by_identity_not_by_display_name"],
    ),
    (
        "IDENTITY->STR: stale detection compares omitted pages by display name",
        '    omitted = {_page_key(page) for page in expectation["omissions"]}',
        '    omitted = {page["name"] for page in expectation["omissions"]}',
        ["test_a_stale_signature_is_reported_even_when_a_same_named_page_is_omitted"],
    ),
    (
        "IDENTITY->STR: the two halves compute ambiguity separately again",
        "    ambiguous = _attribution_ambiguous(expectation, signatures := _resolve_signatures(expectation, entries))",
        "    signatures = _resolve_signatures(expectation, entries)\n"
        '    ambiguous = bool(expectation["unmatched_rendered"]) or bool(expectation["contested_names"])',
        ["test_both_halves_agree_on_whether_attribution_is_ambiguous"],
    ),
    (
        "BINDING: the handover side of the uniqueness guard is dropped",
        "        loose = handover_index.unique(name) is not None and stem_index.unique(name) is not None",
        "        loose = stem_index.unique(name) is not None",
        ["test_two_workbooks_whose_names_slug_alike_do_not_bind_interchangeably"],
    ),
    (
        "BINDING: the target-artifact side of the uniqueness guard is dropped",
        "        loose = handover_index.unique(name) is not None and stem_index.unique(name) is not None",
        "        loose = handover_index.unique(name) is not None",
        ["test_the_normalized_index_is_what_bindable_workbooks_consults"],
    ),
    (
        "BINDING: the lossy workbook fallback is removed entirely",
        "        loose = handover_index.unique(name) is not None and stem_index.unique(name) is not None",
        "        loose = False",
        ["test_a_filesystem_sanitised_workbook_name_still_binds_when_unambiguous"],
    ),
    (
        "BINDING: two distinct artifact stems are indexed as ONE candidate",
        "    for stem in sorted(stems):\n        index.add(stem, stem)",
        "    for stem in sorted(stems):\n        index.add_spelling(stem, 'any')",
        ["test_the_normalized_index_is_what_bindable_workbooks_consults"],
    ),
    (
        "INDEX: unique() picks the first candidate instead of refusing a collision",
        "        return matches[0] if len(matches) == 1 else None",
        "        return matches[0] if matches else None",
        ["test_the_index_refuses_zero_and_refuses_many"],
    ),
    (
        "INDEX: unique() refuses everything, including a legitimate single candidate",
        "        return matches[0] if len(matches) == 1 else None",
        "        return None",
        ["test_a_filesystem_sanitised_workbook_name_still_binds_when_unambiguous"],
    ),
    (
        "INDEX: add() overwrites, so a collision is never observable",
        "        self.__buckets.setdefault(_slug(name), []).append(value)",
        "        self.__buckets[_slug(name)] = [value]",
        ["test_the_index_refuses_zero_and_refuses_many"],
    ),
    (
        "INDEX: add_spelling() inflates one candidate into several",
        "        bucket = self.__buckets.setdefault(_slug(name), [])\n        if value not in bucket:\n"
        "            bucket.append(value)",
        "        bucket = self.__buckets.setdefault(_slug(name), [])\n        bucket.append(value)",
        ["test_add_spelling_does_not_inflate_a_single_candidate"],
    ),
    (
        "EXEMPTIONS: exact matching is removed, so only the lossy key decides",
        "        exact = [key for key, name, aliases in findings if item == name or item in aliases]",
        "        exact = []",
        ["test_one_scaffold_signature_cannot_exempt_two_findings"],
    ),
    (
        "EXEMPTIONS: every lossy spelling resolves to the FIRST finding",
        "                lossy.add_spelling(value, key)",
        "                lossy.add_spelling(value, findings[0][0])",
        ["test_a_scaffold_signature_matching_two_findings_only_by_slug_applies_to_neither"],
    ),
    (
        "EXEMPTIONS: an ambiguous entry is silently dropped instead of reported",
        "        elif lossy.count(item):\n            contested.append(item)",
        "        elif False:\n            contested.append(item)",
        ["test_a_scaffold_signature_matching_two_findings_only_by_slug_applies_to_neither"],
    ),
    (
        "EXEMPTIONS: the lossy fallback is removed entirely",
        "        elif (match := lossy.unique(item)) is not None:",
        "        elif False:",
        ["test_a_scaffold_signature_still_applies_when_it_names_exactly_one_finding"],
    ),
    (
        "ORACLE KIND: view_type is discarded, so a worksheet certifies a dashboard",
        '    return value if value in {"dashboard", "worksheet"} else None',
        '    return "dashboard"',
        ["test_a_worksheet_typed_record_cannot_certify_a_same_named_dashboard"],
    ),
    (
        "ORACLE KIND: 'unknown' is accepted as if it were a kind",
        '    return value if value in {"dashboard", "worksheet"} else None',
        "    return value if isinstance(value, str) and value else None",
        ["test_a_record_whose_kind_is_unestablished_certifies_nothing"],
    ),
    (
        "ORACLE KIND: a kind-less record is admitted anyway",
        "        if record.kind is None:\n            kindless += 1\n            continue",
        "        if record.kind is None:\n            kindless += 1",
        ["test_a_record_whose_kind_is_unestablished_certifies_nothing"],
    ),
    (
        "ORACLE KIND: the page lookup ignores the record's kind",
        '        exact = self.by_exact.get((str(page.get("kind")), page["name"]), [])',
        '        exact = next((v for k, v in self.by_exact.items() if k[1] == page["name"]), [])',
        ["test_a_worksheet_typed_record_cannot_certify_a_same_named_dashboard"],
    ),
    (
        "ORACLE KIND: a reference entry stops being a dashboard by construction",
        '                    kind="dashboard",',
        "                    kind=None,",
        ["test_reference_manifest_entries_are_dashboards_by_construction"],
    ),
    (
        "ORACLE IDENTITY: producer records are collapsed to one per key again",
        "        by_exact.setdefault((str(record.kind), record.name), []).append(record)",
        "        by_exact[(str(record.kind), record.name)] = [record]",
        ["test_two_producer_records_naming_one_page_satisfy_nothing"],
    ),
    (
        # ⚠️ Round-3 B-B DELETED the lossy sanitised-spelling rescue this used to aim at, so the old
        # snippet (`unit_index.unique(name) or producer_index.unique(name)`) could never apply again.
        # Re-aimed at the property rather than the vanished code: putting a name rescue BACK must be
        # caught. A display name is decoration, and two producers slugging alike prove it.
        "ORACLE WORKBOOK: a lossy display name is allowed to admit again",
        "    for record in records:\n        verdict = _attribute_record(unit_ids, record)\n        if not verdict.admitted:",
        "    for record in records:\n        verdict = _attribute_record(unit_ids, record)\n"
        "        if verdict.route == oid.WB_FOREIGN and verdict.axis == oid.WB_NAME:\n"
        '            verdict = oid.Attribution(oid.WB_LUID, "lossy name rescue", axis=oid.WB_NAME)\n'
        "        if not verdict.admitted:",
        ["test_two_producing_workbooks_slugging_alike_are_both_refused"],
    ),
    (
        # The LUID twin of "the unit stops hashing its own source" below. A unit that establishes no
        # LUID cannot call anything foreign - the refusal silently downgrades to `unattributed`.
        "ORACLE WORKBOOK: the unit stops establishing its own LUID, so nothing is ever foreign",
        "    luid = oid.agreed_luid(*_unit_source_claims(target)[1])",
        "    luid = None",
        ["test_oracle_evidence_from_a_different_workbook_satisfies_nothing"],
    ),
    (
        # Issue #450: `_declared_workbook` read a `workbook` key no capture producer writes, so on a
        # real 360-view capture EVERY record arrived ownerless and was admitted anyway.
        # ⚠️ The anchor was `test_the_workbook_name_a_real_capture_writes_is_read`, which does not
        # exist - so this reported BROKEN (189 deselected, 0 run), not CAUGHT. Re-anchored on the
        # test that actually asserts the field is read: without `workbook_name`, the record is
        # ownerless and `name_only_evidence` is empty rather than naming it.
        "ORACLE WORKBOOK: the fields a real capture writes are ignored again (#450)",
        '        name=entry.get("workbook") or outer.get("workbook") or entry.get("workbook_name")'
        ' or outer.get("workbook_name"),',
        '        name=entry.get("workbook") or outer.get("workbook"),',
        ["test_a_display_name_alone_never_certifies_however_real_the_capture"],
    ),
    (
        # ⚠️ Re-aimed for the same reason as the first entry: the rescue this guarded
        # (`verdict.route != oid.WB_FOREIGN or verdict.axis != oid.WB_NAME or not name`) is deleted,
        # so the snippet was unappliable. The fail-open direction it defended is still worth a
        # mutant, so the mutant now REINSTATES it - one axis up, where the LUIDs themselves disagree.
        "ORACLE WORKBOOK: a lossy name rescues a LUID disagreement (#450, the fail-open direction)",
        "    compared = (oid.WB_CONFLICT, oid.WB_FOREIGN)\n    return next(",
        "    compared = (oid.WB_CONFLICT, oid.WB_FOREIGN)\n"
        "    if any(identity.name and record.workbook.name == identity.name for identity in unit_ids):\n"
        '        return oid.Attribution(oid.WB_LUID, "lossy name rescue", axis=oid.WB_NAME)\n'
        "    return next(",
        ["test_a_foreign_luid_is_refused_even_when_the_workbook_name_matches_exactly"],
    ),
    (
        "ORACLE WORKBOOK: unattributed evidence is no longer disclosed in the grade",
        '        grade += (\n            f" (\u26a0\ufe0f {evidence.unattributed} record(s) establish no producing workbook and certify "',
        '        grade += (\n            f" ("',
        ["test_a_record_whose_workbook_cannot_be_established_certifies_nothing"],
    ),
    (
        "ORACLE KIND: kind-less evidence is discarded SILENTLY",
        "    if evidence.kindless:\n        grade += (",
        "    if False:\n        grade += (",
        ["test_a_record_whose_kind_is_unestablished_certifies_nothing"],
    ),
    (
        "ORACLE WORKBOOK: a unit-local reference manifest skips the guard again (round-2 blocker 3)",
        "        if not verdict.admitted:",
        "        if not verdict.admitted and record.workbook.established:",
        ["test_a_unit_local_reference_manifest_is_not_certified_by_its_location"],
    ),
    (
        "ORACLE WORKBOOK: the unit stops hashing its own source, so a recorded sha is unchecked",
        "    sha = _unit_source_sha256(target)",
        "    sha = None",
        ["test_a_unit_local_reference_manifest_certifies_when_its_recorded_sha_is_this_source"],
    ),
    (
        # ⚠️ There is deliberately NO mutant here for `Path(name).stem` vs `persisted_stem(name)`.
        # On Windows `WindowsPath` accepts BOTH separators, so no fixture string exists that `Path`
        # mishandles and the fix handles - the mutant is unkillable on this host BY CONSTRUCTION, and
        # an entry that can only ever report CAUGHT on Linux is a vacuity, not a guard. The property
        # is pinned by `test_the_recorded_path_parse_does_not_use_the_running_hosts_flavour`, which
        # exercises PureWindowsPath and PurePosixPath explicitly on one host, and by
        # `test_a_windows_recorded_source_id_still_yields_its_luid`, which fails on POSIX CI if the
        # call site regresses. `mutation_reference_readiness.py` carries the matching mutant.
        "ORACLE DISCOVERY: the ancestor walk stops one level short of the run root",
        "    return evidence_dirs(unit, ORACLE_DIR_NAMES, also=[target])",
        "    return _resolved_unique([base / name for base in (unit, target) for name in ORACLE_DIR_NAMES])",
        ["test_a_non_packaged_unit_still_reads_an_ancestors_oracle_capture"],
    ),
    (
        "SLUG CENSUS: a brand-new lossy call site outside the index",
        "def _page_key(page: dict[str, Any]) -> tuple[str, str, str]:",
        "def _unadjudicated_new_site(text: str) -> str:\n"
        "    return _slug(text)\n"
        "\n"
        "\n"
        "def _page_key(page: dict[str, Any]) -> tuple[str, str, str]:",
        ["test_every_slug_call_site_is_inside_the_index_or_allowlisted"],
    ),
    (
        "SLUG CENSUS: the reporting-only map is PROMOTED into a disposition",
        '        elif page["declared_reason"]:\n            disposition = OMISSION_DECLARED',
        '        elif page["declared_reason"] or expectation["explanations"]["described"]:\n'
        "            disposition = OMISSION_DECLARED",
        ["test_reporting_only_evidence_does_not_change_any_verdict"],
    ),
    (
        "SLUG CENSUS: the index grows a public accessor that leaks the bucket",
        "    def count(self, name: str) -> int:",
        "    def bucket(self, name: str) -> list[_T]:\n"
        "        return self.__buckets.get(_slug(name), [])\n"
        "\n"
        "    def count(self, name: str) -> int:",
        ["test_the_index_exposes_no_way_to_read_a_bucket"],
    ),
    (
        "oracle discovery: canonical oracle/ name removed",
        'ORACLE_DIR_NAMES = ("_oracle", "oracle")',
        'ORACLE_DIR_NAMES = ("_oracle",)',
        ["test_oracle_capture_is_discovered_under_the_canonical_run_layout"],
    ),
    (
        "oracle discovery: documented _oracle/ name removed",
        'ORACLE_DIR_NAMES = ("_oracle", "oracle")',
        'ORACLE_DIR_NAMES = ("oracle",)',
        ["test_underscore_oracle_directory_is_still_discovered"],
    ),
    (
        # ⚠️ The "loose workbook attribution is no longer tracked" mutant that used to sit here is
        # DELETED, not repaired. Its target (`loosely_attributed.append(rescued)`) and its anchor
        # (`test_a_loosely_attributed_workbook_carries_a_caveat_naming_it`) both went with round-3's
        # removal of the lossy rescue, so it could only ever report ANCHOR-SNIPPET x0. A mutation
        # that cannot apply is not coverage, and re-aiming it would defend a caveat whose gap is
        # closed - the same reason the #438 KIND caveat and its two mutants were retired together.
        "DISCLOSURE: the caveats never reach the rendered output",
        '        lines.extend(f"                    {caveat}" for caveat in check.get("known_gap_caveats", []))',
        "        pass",
        ["test_the_caveats_reach_the_rendered_cli_output"],
    ),
    (
        f"{NEGATIVE_CONTROL}: a no-op comment edit must SURVIVE",
        "# Where a dropped page's declared reason is read from.",
        "# Harness negative control - a comment edit that changes no behaviour.",
        [
            "test_engine_evidence_explains_an_omission_but_does_not_accept_it",
            "test_a_worksheet_row_can_never_explain_a_same_named_dashboard",
        ],
    ),
]

_FAILED = re.compile(r"(\d+) failed")
_ERROR = re.compile(r"(\d+) errors?\b|^(?:ERROR|INTERNALERROR)\b", re.IGNORECASE | re.MULTILINE)


def run_one_anchor(name: str) -> tuple[str, str]:
    """Run one anchor test; return (verdict, note).

    ⚠️ ``encoding="utf-8"`` is load-bearing on Windows. ``text=True`` alone decodes the child's stdout
    with the console codepage (cp1252 here), which raises ``UnicodeDecodeError`` the moment a failing
    assertion prints a non-Latin-1 character - and the gate's own ``⚠️`` caveats do exactly that. The
    harness then reported BROKEN with no output, which is at least fail-closed rather than a false
    CAUGHT, but it made every emoji-carrying assertion structurally unmutatable.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *[str(suite) for suite in SUITES], "-q", "-k", name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=REPO_ROOT,
    )
    tail = f"{proc.stdout or ''}\n{proc.stderr or ''}"[-4000:]
    last = tail.strip().splitlines()[-1] if tail.strip() else "no output"
    if _ERROR.search(tail):
        return "BROKEN", last
    failed = _FAILED.search(tail)
    if failed:
        return f"CAUGHT ({failed.group(1)} failed)", ""
    if proc.returncode != 0:
        return "BROKEN", last
    return "SURVIVED", last


def run_anchor(names: list[str]) -> tuple[str, str]:
    """Run each declared anchor independently; every one must observe the mutation."""
    outcomes = [(name, *run_one_anchor(name)) for name in names]
    broken = [(name, note) for name, verdict, note in outcomes if not verdict.startswith(("CAUGHT", "SURVIVED"))]
    if broken:
        return "BROKEN", "; ".join(f"{name}: {note}" for name, note in broken)

    caught = [name for name, verdict, _note in outcomes if verdict.startswith("CAUGHT")]
    missed = [name for name, verdict, _note in outcomes if verdict == "SURVIVED"]
    if len(caught) == len(outcomes):
        return (outcomes[0][1], outcomes[0][2]) if len(outcomes) == 1 else (f"CAUGHT ({len(caught)} anchors)", "")
    if caught:
        return "PARTIAL-ANCHOR", f"caught: {', '.join(caught)}; missed: {', '.join(missed)}"
    return "SURVIVED", "; ".join(f"{name}: {note}" for name, _verdict, note in outcomes)


def run_campaign(selected: list[tuple[str, str, str, list[str]]]) -> list[tuple[str, str, str]]:
    """Apply each mutation in turn against pristine source, run its anchor, and always restore."""
    original = TARGET.read_text(encoding="utf-8")
    results: list[tuple[str, str, str]] = []
    try:
        for label, old, new, names in selected:
            occurrences = original.count(old)
            if occurrences != 1:
                results.append((label, f"ANCHOR-SNIPPET x{occurrences}", "mutation could not be applied"))
            else:
                TARGET.write_text(original.replace(old, new), encoding="utf-8")
                verdict, note = run_anchor(names)
                results.append((label, verdict, note))
            print(f"{results[-1][1]:26s} {label}")
            if results[-1][2]:
                print(f"    {results[-1][2]}")
    finally:
        TARGET.write_text(original, encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    """Run the campaign and return 0 only when every mutation is caught and the control survives."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list the mutations without running them")
    parser.add_argument("--only", help="run only mutations whose label contains this substring")
    args = parser.parse_args(argv)

    selected = [row for row in MUTATIONS if not args.only or args.only.lower() in row[0].lower()]
    if args.list:
        for label, _old, _new, names in selected:
            print(f"{label}\n    anchor: {', '.join(names)}")
        return 0
    if not selected:
        print("no mutation matched --only", file=sys.stderr)
        return 64

    results = run_campaign(selected)
    controls = [(label, verdict) for label, verdict, _note in results if NEGATIVE_CONTROL in label]
    mutations = [(label, verdict) for label, verdict, _note in results if NEGATIVE_CONTROL not in label]
    caught = sum(1 for _label, verdict in mutations if verdict.startswith("CAUGHT"))
    broken = [label for label, verdict in mutations if not verdict.startswith(("CAUGHT", "SURVIVED"))]
    survived = [label for label, verdict in mutations if verdict == "SURVIVED"]
    control_ok = bool(controls) and all(verdict == "SURVIVED" for _label, verdict in controls)
    print()
    print(f"{caught}/{len(mutations)} mutations caught")
    # Report the three states SEPARATELY. Folding them into one sentence made this harness answer
    # "negative control DID NOT behave as required" when the control had in fact survived and the
    # real problem was two stale anchor snippets - a confident wrong answer from the instrument
    # written to prove the tests can fail.
    if broken:
        print(f"{len(broken)} mutation(s) COULD NOT BE APPLIED or BROKE/PARTIALLY matched: {'; '.join(broken)}")
    if survived:
        print(f"{len(survived)} mutation(s) SURVIVED (no test failed): {'; '.join(survived)}")
    if not controls:
        # A --only selection that excludes the control is UNVALIDATED, not failed: nothing has shown
        # the harness could report a survivor at all. Say which of the two it is.
        print("no negative control in this selection - result is UNVALIDATED, run the full campaign")
    else:
        print(f"negative control {'SURVIVED as required' if control_ok else 'DID NOT behave as required'}")
    return 0 if controls and control_ok and not broken and not survived else 1


if __name__ == "__main__":
    raise SystemExit(main())
