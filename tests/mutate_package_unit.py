"""
purpose: mutation-test the scoping and repackaging guards in scripts/package_unit.py - break each
         rule in the production code and prove a specific test fails. Evidence that the tests can
         fail, kept in the repo rather than in a review transcript.
usage:   python tests/mutate_package_unit.py [--list] [--only <substring>]
         exit 0 when every mutation is CAUGHT and the negative control SURVIVES.

Why this exists, concretely. Round-2 blind review of PR #451 found that
`test_the_readme_separates_the_png_and_svg_evidence_legs` **stayed green when the entire zero-text
caveat was deleted** - a guard credited as coverage that protected nothing. The round-1 claim that a
mutation campaign had been run could not be checked, because the harness lived in a scratch file and
was never committed. Both problems have the same fix: the campaign is a repo artifact, re-runnable
by anyone, scored the same way `mutate_check_unit.py` scores its own.

The contract is copied from `tests/mutate_check_unit.py` deliberately - it is the convention this
repo already trusts, and both of its refusals have caught false 100% scores here:

* a mutation counts as CAUGHT only on a genuine pytest ``N failed``. A collection error, an import
  error or any other non-zero exit is reported BROKEN - never caught. (A previous harness in this
  repo scored 22/22 where every one was an import error.)
* a no-op NEGATIVE CONTROL runs through the identical path and must SURVIVE, which proves the
  harness can report a survivor at all.

Each mutation names the single anchor test it must kill and is run against that anchor alone, so a
"caught" verdict cannot be borrowed from an unrelated failure.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGER = REPO_ROOT / "scripts" / "package_unit.py"
MECHANISM = REPO_ROOT / "scripts" / "manifest_scope.py"
SUITES = (REPO_ROOT / "tests" / "test_package_unit.py", REPO_ROOT / "tests" / "test_package_unit_gates.py")

NEGATIVE_CONTROL = "NEGATIVE CONTROL"

# (label, target file, exact snippet to replace, replacement, anchor tests that must fail)
MUTATIONS: list[tuple[str, Path, str, str, list[str]]] = [
    # ---- round-1 finding 1: the top-level leak -------------------------------------------------
    (
        "report: carry every unenumerated top-level field (the pre-fix denylist)",
        MECHANISM,
        "    scoped, dropped = project(narrowed, REPORT_ALLOW)",
        "    scoped, dropped = dict(narrowed), []",
        ["test_no_foreign_unit_survives_anywhere_in_the_packaged_report"],
    ),
    (
        "report: stop recording which fields were dropped",
        MECHANISM,
        '    return stamp_scope(scoped, unit, dropped, "report.json")',
        '    return stamp_scope(scoped, unit, [], "report.json")',
        ["test_a_dropped_nested_field_is_recorded_by_its_full_path"],
    ),
    # ---- round-2 blocker 1 / round-3 finding 1: how deep the allowlist reaches -----------------
    (
        "report: carry the engine's whole workbook row instead of just its name",
        MECHANISM,
        '    "workbooks": Rows(REPORT_ROW),',
        '    "workbooks": Rows({**REPORT_ROW, "model_facts": KEEP, "future_nested": KEEP}),',
        ["test_a_retained_row_does_not_smuggle_unknown_nested_fields"],
    ),
    (
        "KEEP stops being scalar-only, so a container ships its unknown grandchildren",
        MECHANISM,
        "        if not isinstance(payload, _SCALARS):\n            raise UnscopedStructure(",
        "        if False:  # noqa\n            raise UnscopedStructure(",
        ["test_a_container_at_a_scalar_leaf_fails_loudly_rather_than_shipping"],
    ),
    (
        "oracle legs stop being specified, so a leg's grandchildren ride along",
        MECHANISM,
        '    **{leg: ORACLE_LEG_SPEC for leg in ("image", "svg", "pdf", "data")},',
        '    **{leg: KEEP for leg in ("image", "svg", "pdf", "data")},',
        ["test_an_unknown_field_inside_a_RETAINED_container_cannot_ship"],
    ),
    # ---- round-3 finding 2: artifacts that never entered project() -----------------------------
    (
        "provenance: ship every entry that matched the sha, refused or not",
        MECHANISM,
        '    refused = not identity.get("luid")',
        "    refused = False",
        ["test_a_refused_attribution_ships_no_provenance_entry_at_all"],
    ),
    (
        "provenance: carry the whole origin, workbook_name and project included",
        MECHANISM,
        '            "origin": _fields("workbook_luid", "match"),',
        '            "origin": _fields("workbook_luid", "match", "workbook_name", "project", "future_source_path"),',
        ["test_the_provenance_carries_only_the_three_fields_the_gate_reads"],
    ),
    (
        "handover: keep every top-level key, estate section included",
        MECHANISM,
        "    kept = {key: value for key, value in payload.items() if key in HANDOVER_CONSUMED_KEYS}",
        "    kept = dict(payload)",
        ["test_the_handover_slice_drops_its_estate_section"],
    ),
    (
        "handover: stop redacting absolute host paths by value",
        MECHANISM,
        '    if isinstance(payload, str) and HOST_PATH_RE.match(payload):\n        return REDACTED, [prefix or "."]',
        '    if False:  # noqa\n        return REDACTED, [prefix or "."]',
        ["test_an_absolute_path_anywhere_in_the_handover_slice_is_redacted"],
    ),
    # ---- round-3 finding 3: the manifest is untrusted input ------------------------------------
    (
        "capture path: drop the containment check, restoring ../ traversal",
        PACKAGER,
        "    if not resolved.is_relative_to(root):",
        "    if False:  # noqa",
        ["test_a_relative_path_escaping_the_capture_root_is_refused"],
    ),
    (
        "capture path: accept an absolute path from the manifest",
        PACKAGER,
        '    if candidate.is_absolute() or candidate.drive or declared.startswith(("\\\\\\\\", "/")):',
        "    if False:  # noqa",
        ["test_an_absolute_path_is_refused_and_never_reaches_the_manifest"],
    ),
    (
        "capture path: echo the declared path back into the packaged manifest",
        PACKAGER,
        '        rewritten["path"] = REFUSED_PATH',
        '        rewritten["path"] = leg["path"]',
        ["test_an_absolute_path_is_refused_and_never_reaches_the_manifest"],
    ),
    (
        "project: report one dropped path per ROW instead of one per field",
        MECHANISM,
        "        return kept_rows, sorted(set(dropped))",
        "        return kept_rows, dropped",
        ["test_one_unknown_field_across_many_rows_is_reported_once"],
    ),
    # ---- over-trim: the opposite failure -------------------------------------------------------
    (
        "report: drop datasources[], costing a datasource unit its earned NOT_APPLICABLE",
        MECHANISM,
        'REPORT_UNIT_LISTS = ("workbooks", "datasources")',
        'REPORT_UNIT_LISTS = ("workbooks",)',
        ["test_a_scoped_estate_report_still_earns_a_datasource_unit_its_not_applicable"],
    ),
    (
        "report: narrow a collection only when the engine wrote it, so an absent one stays absent",
        MECHANISM,
        "    for collection in REPORT_UNIT_LISTS:\n        narrowed[collection] = [",
        "    for collection in [c for c in REPORT_UNIT_LISTS if c in payload]:\n        narrowed[collection] = [",
        ["test_the_scoped_report_still_declares_both_collections_as_lists"],
    ),
    # ---- round-2 blocker 2: the other shipped manifests ----------------------------------------
    (
        "oracle manifest: keep the estate's own aggregate counts instead of recomputing",
        PACKAGER,
        '    scoped["view_types"] = counts',
        '    scoped["view_types"] = (manifest or {}).get("view_types") or counts',
        ["test_the_oracle_manifest_recomputes_its_counts_from_the_packaged_views"],
    ),
    (
        "oracle manifest: carry every unenumerated key, including the foreign probe identity",
        PACKAGER,
        "    scoped, dropped = project(narrowed, ORACLE_MANIFEST_ALLOW)",
        "    scoped, dropped = dict(narrowed), []",
        ["test_the_oracle_manifest_drops_estate_run_stats_and_the_foreign_probe"],
    ),
    (
        "receipt: carry engine.root and engine.plugin_root installation paths",
        MECHANISM,
        '    "engine": _fields("version", "source", "canonical"),',
        '    "engine": KEEP,',
        ["test_the_receipt_keeps_the_engine_version_and_drops_installation_paths"],
    ),
    (
        "receipt: carry every unenumerated top-level field",
        MECHANISM,
        "    scoped, dropped = project(narrowed, RECEIPT_ALLOW)",
        "    scoped, dropped = dict(narrowed), []",
        ["test_no_shipped_manifest_carries_an_absolute_host_path"],
    ),
    (
        "receipt: let an absolute host path survive into the shipped package",
        MECHANISM,
        '    "engine": _fields("version", "source", "canonical"),',
        '    "engine": KEEP,',
        ["test_no_shipped_manifest_carries_an_absolute_host_path"],
    ),
    # ---- round-2 blocker 3: repackaging --------------------------------------------------------
    (
        "repackaging: merge into the existing package instead of replacing it",
        PACKAGER,
        "        replace_dir(staging, out_root / unit)",
        "        shutil.copytree(staging, out_root / unit, dirs_exist_ok=True)",
        ["test_repackaging_removes_evidence_the_new_input_no_longer_produces"],
    ),
    (
        "repackaging: leave stale files in every copied tree",
        PACKAGER,
        "    shutil.rmtree(retired, ignore_errors=True)\n    _rename_retrying(final, retired)",
        "    shutil.rmtree(retired, ignore_errors=True)\n    shutil.copytree(final, retired)",
        ["test_repackaging_removes_a_stale_file_from_every_copied_tree"],
    ),
    # ---- the prose guards, which round 2 proved were the weakest -------------------------------
    (
        "README: name the oracle kind directories in the plural",
        PACKAGER,
        "split `dashboard/` vs `worksheet/` vs `unknown/`",
        "split `dashboards/` vs `worksheets/` vs `unknown/`",
        ["test_the_readme_names_the_oracle_kinds_exactly_as_the_code_emits_them"],
    ),
    (
        "README: drop the report.json row from the package map",
        PACKAGER,
        "| `report.json` | **gate input, and readable.**",
        "| `omitted-row.json` | **gate input, and readable.**",
        ["test_the_generated_readme_names_every_file_the_package_contains"],
    ),
    (
        "README: delete the zero-text SVG caveat (survived round-1's assertion)",
        PACKAGER,
        "* ⚠️ but an SVG is not universally a data oracle: a chart whose labels render as paths carries\n"
        "  **zero** `<text>` elements. Measured on the same workbook, **four** of its worksheets do -\n"
        "  `Hired By Year`, `Terminated By Year`, `Age Groups` and `Education Levels`. Absence of text is not\n"
        "  absence of content - fall back to the PNG.",
        "* the SVG and the PNG are both renders of the same object.",
        ["test_the_readme_separates_the_png_and_svg_evidence_legs"],
    ),
    (
        f"{NEGATIVE_CONTROL}: a comment-only edit that must change no verdict",
        PACKAGER,
        "# --------------------------------------------------------------------------------------------\n# CLI",
        "# --------------------------------------------------------------------------------------------\n# CLI (negative control)",
        ["test_no_foreign_unit_survives_anywhere_in_the_packaged_report"],
    ),
]

_FAILED = re.compile(r"(\d+) failed")


def run_anchor(names: list[str]) -> tuple[str, str]:
    """Run only the anchor tests; return (verdict, note).

    ``encoding="utf-8"`` is load-bearing on Windows: ``text=True`` alone decodes the child's stdout
    with the console codepage, and this file's own ``⚠️`` caveats appear in failing assertions.
    """
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", *[str(suite) for suite in SUITES], "-q", "-k", " or ".join(names)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=REPO_ROOT,
    )
    tail = (proc.stdout or "")[-4000:]
    failed = _FAILED.search(tail)
    if failed:
        return f"CAUGHT ({failed.group(1)} failed)", ""
    last = tail.strip().splitlines()[-1] if tail.strip() else "no output"
    if proc.returncode != 0 or "error" in tail.lower():
        return "BROKEN", last
    return "SURVIVED", last


def run_campaign(selected: list[tuple[str, Path, str, str, list[str]]]) -> list[tuple[str, str, str]]:
    """Apply each mutation in turn against pristine source, run its anchor, and always restore.

    Every target file is snapshotted up front and restored in `finally`, so a mutation in one module
    cannot leak into the run of the next - the mechanism and the packager are two files now.
    """
    pristine = {path: path.read_text(encoding="utf-8") for path in {row[1] for row in selected}}
    results: list[tuple[str, str, str]] = []
    try:
        for label, target, old, new, names in selected:
            original = pristine[target]
            occurrences = original.count(old)
            if occurrences != 1:
                results.append((label, f"ANCHOR-SNIPPET x{occurrences}", f"in {target.name}: not applied"))
            else:
                target.write_text(original.replace(old, new), encoding="utf-8")
                verdict, note = run_anchor(names)
                results.append((label, verdict, note))
                target.write_text(original, encoding="utf-8")
            print(f"{results[-1][1]:26s} {label}")
            if results[-1][2]:
                print(f"    {results[-1][2]}")
    finally:
        for path, text in pristine.items():
            path.write_text(text, encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    """Run the campaign and return 0 only when every mutation is caught and the control survives."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list the mutations without running them")
    parser.add_argument("--only", help="run only mutations whose label contains this substring")
    args = parser.parse_args(argv)

    selected = [row for row in MUTATIONS if not args.only or args.only.lower() in row[0].lower()]
    if args.list:
        for label, target, _old, _new, names in selected:
            print(f"{label}\n    target: {target.name}\n    anchor: {', '.join(names)}")
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
    if broken:
        print(f"{len(broken)} mutation(s) COULD NOT BE APPLIED or BROKE the run: {'; '.join(broken)}")
    if survived:
        print(f"{len(survived)} mutation(s) SURVIVED (no test failed): {'; '.join(survived)}")
    if not controls:
        print("no negative control in this selection - result is UNVALIDATED, run the full campaign")
    else:
        print(f"negative control {'SURVIVED as required' if control_ok else 'DID NOT behave as required'}")
    return 0 if controls and control_ok and not broken and not survived else 1


if __name__ == "__main__":
    raise SystemExit(main())
