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
        "drop evidence: any warned row explains a drop (tier ignored)",
        "        if tier == DROP_EVIDENCE_TIER:",
        '        if row.get("status") == "warned":',
        ["test_a_degraded_warning_asserts_a_rendered_visual_and_excuses_nothing"],
    ),
    (
        "drop evidence: a row with no tier at all is accepted",
        "        if tier == DROP_EVIDENCE_TIER:",
        "        if tier in (DROP_EVIDENCE_TIER, None):",
        ["test_a_row_with_no_tier_at_all_leaves_the_drop_unexplained"],
    ),
    (
        "drop evidence: workbook binding removed, so any slice explains any unit",
        "        if _slug(str(name)) not in keys:",
        "        if False:  # pragma: no cover",
        ["test_another_workbooks_warning_cannot_excuse_this_units_missing_page"],
    ),
    (
        "drop evidence: explanations read from pbip_warnings instead of viz_fidelity",
        '        _collect_drop_rows(workbook.get("viz_fidelity"), reasons, ambiguous)',
        '        _collect_drop_rows([{"worksheet": "B", "tier": "empty", "reason": w} '
        'for w in (workbook.get("pbip_warnings") or [])] or workbook.get("viz_fidelity"), reasons, ambiguous)',
        ["test_drop_reasons_come_from_viz_fidelity_not_pbip_warnings"],
    ),
    (
        "drop evidence: an EMITTED page is still treated as a drop",
        '    absent = [page for page in candidates if _slug(page["name"]) not in rendered_slugs]',
        "    absent = list(candidates)",
        ["test_warned_but_emitted_page_is_not_treated_as_a_drop"],
    ),
    (
        "drop evidence: declared drops no longer subtracted from the expectation",
        '    explained = [page for page in expectation["explained_drops"] if page not in dropped]',
        "    explained = []",
        ["test_engine_declared_drop_is_explained_and_does_not_fail_parity"],
    ),
    (
        "drop evidence: an unreadable handover reads as if reasons had been consulted",
        '        "available": bool(bound),',
        '        "available": True,',
        ["test_missing_handover_says_drop_reasons_were_unavailable"],
    ),
    (
        "oracle: circular denominator restored (expected or actual)",
        '    if not expectation["assessable"]:\n        return _oracle_not_assessable(',
        '    if not expectation["assessable"]:\n'
        '        expectation = {**expectation, "assessable": True, "candidates": expectation["actual"], '
        '"explained_drops": []}\n'
        '    if not expectation["assessable"]:\n        return _oracle_not_assessable(',
        [
            "test_oracle_coverage_without_an_expected_set_is_blocking_not_a_pass",
            "test_unassessable_oracle_coverage_fails_the_whole_run_closed",
        ],
    ),
    (
        "oracle: denominator stops excluding declared drops",
        '    pages = [page for page in expectation["candidates"] if _slug(page["name"]) not in explained]',
        '    pages = list(expectation["candidates"])',
        ["test_oracle_coverage_excludes_engine_declared_drops_from_the_denominator"],
    ),
    (
        "pages: a zero-visual page counts as a rebuilt page",
        '    rendered = [page for page in actual if page.get("visuals", 0) > 0]',
        "    rendered = list(actual)",
        ["test_a_page_with_no_visuals_does_not_certify_a_candidate_as_rebuilt"],
    ),
    (
        "pages: visual count is fabricated instead of measured",
        '    return sum(1 for _ in visuals_root.rglob("visual.json"))',
        "    return 1",
        ["test_actual_pages_counts_zero_visuals_for_a_page_with_none"],
    ),
    (
        "placeholder: identity drops the zero-visual clause",
        '        page.get("visuals", 0) == 0\n        and page.get("name") == ENGINE_PLACEHOLDER_PAGE_NAME',
        '        page.get("name") == ENGINE_PLACEHOLDER_PAGE_NAME',
        ["test_a_placeholder_id_holding_real_visuals_is_a_rebuilt_page"],
    ),
    (
        "placeholder: identity drops the display-name clause",
        '        and page.get("name") == ENGINE_PLACEHOLDER_PAGE_NAME\n',
        "",
        ["test_a_blank_page_that_is_not_the_engine_placeholder_is_reported"],
    ),
    (
        "placeholder: identity drops the id-prefix clause",
        '        and str(page.get("id", "")).startswith(ENGINE_PLACEHOLDER_PAGE_ID_PREFIX)\n',
        "",
        ["test_a_real_page_titled_like_the_placeholder_is_still_a_page"],
    ),
    (
        "placeholder: a declared crash-guard page counts as an extra page",
        "    placeholders = [page for page in actual if _is_engine_placeholder_page(page)]",
        "    placeholders = []",
        ["test_engine_crash_guard_placeholder_is_not_an_extra_page"],
    ),
    (
        "blank pages: a page that renders nothing no longer fails the gate",
        "        STATUS_PASS if not unexempted_missing and not unexempted_extra and not blank "
        "else STATUS_PRECONDITION_FAILED",
        "        STATUS_PASS if not unexempted_missing and not unexempted_extra else STATUS_PRECONDITION_FAILED",
        ["test_a_blank_page_alone_fails_the_gate_even_when_the_counts_balance"],
    ),
    (
        "attribution: a shortfall is attributed by position, so exemptions excuse the wrong page",
        '    missing = [page for page in effective_expected if _slug(page["name"]) not in rendered_slugs] '
        "if shortfall else []",
        "    missing = effective_expected[-shortfall:] if shortfall else []",
        ["test_a_missing_page_is_named_by_content_not_by_position"],
    ),
    (
        "attribution: an exemption applies even to a page that is present",
        '        [page for page in signed if _slug(page["name"]) not in rendered_slugs],',
        "        list(signed),",
        ["test_an_exemption_excuses_the_page_it_names_and_no_other"],
    ),
    (
        "attribution: a surplus is attributed by position, so an extra page is misnamed",
        '    unmatched = [page for page in rendered if _slug(page["name"]) not in expected_slugs] if surplus else []',
        "    unmatched = rendered[-surplus:] if surplus else []",
        ["test_an_extra_page_is_named_by_content_not_by_position"],
    ),
    (
        "attribution: name matching replaces the count, so a renamed page fails",
        "    shortfall = max(0, len(effective_expected) - len(rendered))",
        "    shortfall = 1",
        ["test_a_placeholder_id_holding_real_visuals_is_a_rebuilt_page"],
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
            "test_a_degraded_warning_asserts_a_rendered_visual_and_excuses_nothing",
            "test_engine_crash_guard_placeholder_is_not_an_extra_page",
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
