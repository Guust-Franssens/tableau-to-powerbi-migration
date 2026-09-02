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

Each mutation names the single anchor test it must kill, and is run against that anchor alone.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "scripts" / "check_unit.py"
SUITE = REPO_ROOT / "tests" / "test_check_unit.py"

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
        "        if signed and not ambiguous:\n            disposition = OMISSION_SIGNED",
        '        if (signed or page["declared_reason"]) and not ambiguous:\n            disposition = OMISSION_SIGNED',
        ["test_engine_evidence_explains_an_omission_but_does_not_accept_it"],
    ),
    (
        "APPROVAL: a signature stops accepting an omission",
        "        if signed and not ambiguous:\n            disposition = OMISSION_SIGNED",
        "        if False:  # pragma: no cover\n            disposition = OMISSION_SIGNED",
        ["test_a_signed_omission_with_engine_evidence_passes_and_counts_as_a_compromise"],
    ),
    (
        "APPROVAL: the oracle denominator drops engine-declared omissions too",
        '    return [page for page in expectation["omissions"] '
        'if _exempted(entries, "page-parity", page["name"], {page["id"]})]',
        '    return [page for page in expectation["omissions"] '
        'if page["declared_reason"] or _exempted(entries, "page-parity", page["name"], {page["id"]})]',
        ["test_oracle_coverage_still_expects_a_page_the_engine_merely_declared"],
    ),
    (
        "APPROVAL: the oracle denominator keeps a signed omission",
        '    accepted = _signed_omissions(expectation, load_exemptions(target)["entries"])',
        "    accepted = []",
        ["test_oracle_coverage_drops_a_signed_omission_from_the_denominator"],
    ),
    (
        "KIND: evidence is indexed under both kinds, so a worksheet row settles a dashboard",
        "        index.add_identity(identity, text)",
        "        index.add_identity(identity, text)\n"
        "        for kind in oid.IDENTIFIABLE_KINDS:\n"
        "            crossed = oid.ObjectIdentity.from_engine(kind, identity.name)\n"
        "            if crossed is not None and crossed != identity:\n"
        "                index.add_identity(crossed, text)",
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
        "        if _slug(str(name)) not in keys:",
        "        if False:  # pragma: no cover",
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
        '    absent = [page for page in candidates if rendered_names.count(page["name"]) != 1]',
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
        "    status = STATUS_PASS if not unsigned and not unaccounted_extra and not blank "
        "else STATUS_PRECONDITION_FAILED",
        "    status = STATUS_PASS if not unsigned and not unaccounted_extra else STATUS_PRECONDITION_FAILED",
        ["test_a_blank_page_alone_fails_the_gate_even_when_every_page_is_paired"],
    ),
    (
        "ATTRIBUTION: an omission is named by position rather than by content",
        '    absent = [page for page in candidates if rendered_names.count(page["name"]) != 1]',
        "    absent = candidates[-1:]",
        ["test_a_missing_page_is_named_by_content_not_by_position"],
    ),
    (
        "ATTRIBUTION: an extra page is named by position rather than by content",
        '    unmatched = [page for page in rendered if page["name"] not in candidate_names]',
        "    unmatched = rendered[-1:]",
        ["test_an_extra_page_is_named_by_content_not_by_position"],
    ),
    (
        "SIGNATURE: an exemption applies even to a page that is present",
        '    for page in expectation["omissions"]:\n'
        '        signed = _exempted(entries, "page-parity", page["name"], {page["id"]})',
        '    for page in expectation["candidates"]:\n'
        '        signed = _exempted(entries, "page-parity", page["name"], {page["id"]})',
        ["test_an_exemption_naming_a_present_page_accepts_nothing"],
    ),
    (
        "SIGNATURE: rename ambiguity no longer suspends a name-only signature (parity side)",
        "        if signed and not ambiguous:",
        "        if signed:",
        ["test_a_rename_makes_attribution_ambiguous_and_suspends_every_signature"],
    ),
    (
        "SIGNATURE: rename ambiguity no longer suspends a name-only signature (oracle side)",
        '    if expectation["attribution_ambiguous"]:\n        return []',
        "    if False:  # pragma: no cover\n        return []",
        ["test_a_rename_suspends_signatures_on_the_oracle_denominator_too"],
    ),
    (
        "SIGNATURE: an unaccounted rendered page no longer creates ambiguity",
        '        if not _exempted(entries, "page-parity", f"extra:{page[\'name\']}")',
        "        if False  # pragma: no cover",
        ["test_a_rename_makes_attribution_ambiguous_and_suspends_every_signature"],
    ),
    (
        "SIGNATURE: an extra: signature cannot resolve the ambiguity",
        "    unaccounted_extra = [\n"
        "        page\n"
        '        for page in expectation["unmatched_rendered"]\n'
        '        if not _exempted(entries, "page-parity", f"extra:{page[\'name\']}")\n'
        "    ]",
        '    unaccounted_extra = list(expectation["unmatched_rendered"])',
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


def run_anchor(names: list[str]) -> tuple[str, str]:
    """Run only the anchor tests; return (verdict, note)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-q", "-k", " or ".join(names)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    tail = (proc.stdout or "")[-4000:]
    failed = _FAILED.search(tail)
    if failed:
        return f"CAUGHT ({failed.group(1)} failed)", ""
    if proc.returncode != 0 or "error" in tail.lower():
        return "BROKEN", tail.strip().splitlines()[-1] if tail.strip() else "no output"
    return "SURVIVED", tail.strip().splitlines()[-1] if tail.strip() else "no output"


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
    ok = all(
        verdict == "SURVIVED" if NEGATIVE_CONTROL in label else verdict.startswith("CAUGHT")
        for label, verdict, _note in results
    )
    caught = sum(1 for _label, verdict, _note in results if verdict.startswith("CAUGHT"))
    controls = sum(1 for label, _verdict, _note in results if NEGATIVE_CONTROL in label)
    print()
    print(
        f"{caught}/{len(results) - controls} mutations caught; "
        f"negative control {'SURVIVED' if ok else 'DID NOT behave as required'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
