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

Each mutation names the anchor tests it must kill and is run against each anchor alone, so a "caught"
verdict cannot be borrowed from an unrelated failure.
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
DETECTOR = REPO_ROOT / "scripts" / "host_paths.py"
CEILINGS = REPO_ROOT / "scripts" / "check_path_ceiling.py"
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
        "    if isinstance(payload, str) and discloses_host_location(payload):"
        '\n        return REDACTED, [prefix or "."]',
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
        # #480 round 4 moved this predicate into `_declares_non_relative` so `retained_path` meets
        # the same check as `path`; the snippet follows it rather than being retired.
        #
        # ⚠️ Round 7 RE-ANCHORED it. `_declares_unsafe_path` now also asks a CONTAINMENT question,
        # and `tmp_path` on Windows sits under the runner's own profile - so the old anchor's
        # absolute path is refused by the new layer with the identical "non-relative" diagnosis and
        # the mutation SURVIVED against it. What only the parse half answers is a NON-PROFILE
        # location (`<drive>:\builds\...`), which is what the new anchor drives. Strictly stronger:
        # it isolates this branch instead of sharing an input with the guard beside it.
        #
        # ⚠️ Round 9 RE-ANCHORED it AGAIN, for the same reason one layer up, and the pattern is the
        # point: `discloses_host_location` refuses every ROOTED location, build drives included, so
        # a build drive no longer isolates anything either. What is left to this branch alone is a
        # DRIVE-RELATIVE path (`<drive>:secret.png` - a drive with no root separator), which no
        # grammar of absolute locations can see because nothing about it is rooted. Each wider layer
        # costs one re-isolation; skipping it leaves the branch unfalsifiable and green.
        '    return candidate.is_absolute() or bool(candidate.drive) or declared.startswith(("\\\\\\\\", "/"))',
        "    return False  # noqa",
        ["test_a_DRIVE_RELATIVE_path_is_refused_by_the_PARSE_half_alone"],
    ),
    (
        # ⚠️ REPLACES "capture path: echo the declared path back into the packaged manifest" (#480
        # round 4). That mutation is now genuinely unobservable: `_contain_declared_paths` runs on
        # every returned leg, so re-assigning `rewritten["path"] = leg["path"]` is scrubbed straight
        # back to `REFUSED_PATH` and no test can see it. Keeping it would have reported a SURVIVOR
        # that is really defence in depth. The sole mitigation is now the scrub itself, so that is
        # what this mutation removes -- and it is a strictly stronger claim, because the scrub also
        # covers the field name (`retained_path`) that walked around the old guard entirely.
        #
        # ⚠️ Round 5 RE-ANCHORED it. The document sweep in `_scope_oracle_manifest` now contains the
        # same values a second time, so the two former anchors -- which read the packaged MANIFEST --
        # survive this mutation on the strength of that sweep alone. What only the leg guard produces
        # is the DIAGNOSIS: neutered, `_copy_leg` returns no reason, so no `ORACLE_OMISSION` line is
        # written and the operator cannot tell a hostile manifest from a silent one.
        "capture path: ship any declared string, however non-relative",
        PACKAGER,
        "    contained, refused = _contain_unsafe_strings(leg)",
        "    contained, refused = leg, []  # noqa",
        ["test_a_refused_nested_string_is_DIAGNOSED_not_silently_scrubbed"],
    ),
    # ---- round-5 finding: containment must be recursive, and cover the whole document -----------
    (
        # Round 3 guarded a NAME, round 4 guarded a top-level VALUE, and a string one level down --
        # inside an allowlisted `SCALAR_LIST` -- walked around both. Removing the container descent
        # restores exactly that: `views` itself is a list, so nothing under it is visited at all.
        "containment: stop descending into lists and tuples, so a nested string ships verbatim",
        PACKAGER,
        "    if isinstance(value, (list, tuple)):",
        "    if False:  # noqa",
        [
            "test_a_string_INSIDE_a_retained_container_cannot_ship_a_host_path",
            "test_a_VIEW_LEVEL_string_cannot_ship_a_host_path_either",
            "test_the_containment_walk_reaches_EVERY_depth_and_container",
        ],
    ),
    (
        # The other half of the round-5 boundary: the four copied legs are not the document. With the
        # sweep gone, `views[].flags[]` and `views[].view_name` meet no check on any path.
        #
        # ⚠️ Round 7 RE-ANCHORED the SNIPPET, not the claim: the sweep moved to after `stamp_scope`
        # so that nothing is appended to the document behind it, and it now sweeps `stamped`.
        "oracle manifest: guard only the copied legs, not the packaged document",
        PACKAGER,
        "    stamped, refused = _contain_unsafe_strings(stamped)",
        "    refused = []  # noqa",
        ["test_a_VIEW_LEVEL_string_cannot_ship_a_host_path_either"],
    ),
    (
        # The name is untrusted too, and it leaves the manifest for `handover.md` and
        # `package-manifest.json`, which the document sweep never sees.
        #
        # ⚠️ Round 6 RE-ANCHORED the snippet, not the claim: containment moved from the naming pair
        # to the whole view, at the source, so `naming, _ = _contain_unsafe_strings([...])` no longer
        # exists to mutate. Reading the RAW view back is the strictly larger version of the same
        # edit, and it still has to kill the round-5 name anchor.
        "packaging: read the RAW view, uncontained, as rounds 3-5 did",
        PACKAGER,
        "        view = _contain_unsafe_strings(raw_view)[0]",
        "        view = raw_view  # noqa",
        [
            "test_a_refused_object_NAME_does_not_leak_into_the_other_two_artifacts",
            "test_the_ORACLE_RESULT_itself_is_contained_not_only_the_scoped_manifest",
        ],
    ),
    # ---- round-6 finding: contain the oracle RESULT at its source, not at a fifth consumer -------
    (
        # The precise round-5 behaviour, restored: the NAME contained and nothing else. It proves the
        # new anchors need whole-view containment rather than one more field, which is exactly what
        # rounds 3, 4 and 5 each supplied and each was followed by. The round-5 anchors SURVIVE this
        # by design - `oracle-manifest.json` is still swept - and that is the finding in one line.
        "packaging: contain only the object NAME, as round 5 did, leaving the LUID and type raw",
        PACKAGER,
        "        view = _contain_unsafe_strings(raw_view)[0]",
        '        view = {**raw_view, "view_name": _contain_unsafe_strings(raw_view.get("view_name"))[0]}  # noqa',
        [
            "test_the_ORACLE_RESULT_itself_is_contained_not_only_the_scoped_manifest",
            "test_a_refused_view_LUID_does_not_reach_an_OMISSION_row",
        ],
    ),
    (
        # Moving containment to the source is only safe because the walk is IDEMPOTENT: a later
        # reader must still be able to see that a value was refused. Neutered, the sentinel reads as
        # an ordinary string, `_copy_leg` stops diagnosing and `scope.refused_fields` empties - the
        # operator loses both the WHY and the WHERE while the value stays contained, which is the
        # silent-scrub failure round 5 named.
        "containment: treat an already-refused string as ordinary, retiring the diagnosis",
        PACKAGER,
        '        if value == REFUSED_PATH:\n            return value, [prefix or "."]',
        "        if False:  # noqa\n            pass",
        [
            "test_a_refused_nested_string_is_DIAGNOSED_not_silently_scrubbed",
            "test_a_VIEW_LEVEL_string_cannot_ship_a_host_path_either",
        ],
    ),
    (
        # The consumer enumeration's second document: `source-provenance.json` feeds
        # `shippable_provenance`, `render_handover` and `workbook_identity`'s conflict sentence, and
        # nothing downstream sweeps any of them. The sentence is why an artifact-walking assertion is
        # not enough on its own - the leak stops being a string VALUE the moment it is quoted.
        "packaging: read source-provenance.json raw, so the identity quotes it verbatim",
        PACKAGER,
        "    entries = _contain_unsafe_strings(entries)[0]",
        "    entries = list(entries)  # noqa",
        ["test_the_WORKBOOK_IDENTITY_document_is_contained_at_its_own_intake_too"],
    ),
    (
        "project: report one dropped path per ROW instead of one per field",
        MECHANISM,
        "        return kept_rows, sorted(set(dropped))",
        "        return kept_rows, dropped",
        ["test_one_unknown_field_across_many_rows_is_reported_once"],
    ),
    # ---- round-7 finding B1: the predicate asked "IS this a path", which a prefix defeats ---------
    (
        # The exact round-7 predicate, restored: a parse of the whole string, so `HTTP 503: ` in
        # front of a host path -- which is how `classify_export_error` writes `retry_reasons[]` --
        # makes it answer False. The positive-control anchor rides along deliberately: it must NOT
        # fail here, and if a future edit makes the predicate broader instead of narrower it is the
        # test that says so.
        "containment: ask 'IS this a path' again, so a host path wrapped in prose ships",
        PACKAGER,
        "    return (\n"
        "        _declares_non_relative(declared) or discloses_host_location(declared) "
        'or ".." in PureWindowsPath(declared).parts\n'
        "    )",
        '    return _declares_non_relative(declared) or ".." in PureWindowsPath(declared).parts  # noqa',
        ["test_a_host_path_WRAPPED_IN_PROSE_is_refused_not_only_a_bare_one"],
    ),
    (
        # The same sub-class on the handover slice's own guard. Round 7's `HOST_PATH_RE` was anchored
        # and is now deleted, so this restores the escape by narrowing the CONSUMER instead: one
        # detector, three consumers, and narrowing any one of them reopens it one artifact over.
        "handover: redact only what a PROFILE regex sees, so a build root in prose ships whole",
        MECHANISM,
        "    if isinstance(payload, str) and discloses_host_location(payload):",
        "    if isinstance(payload, str) and discloses_host_path(payload):  # noqa",
        ["test_a_host_path_WRAPPED_IN_PROSE_is_redacted_in_the_HANDOVER_slice_too"],
    ),
    # ---- round-9 leak 1: the predicate matched a SPELLING, not the property ----------------------
    (
        # The round-8 detector, restored at the DETECTOR rather than at a consumer: the shipping
        # question narrows back to "is there a user profile in this text". Everything else in the
        # pipeline is untouched, so what fails is exactly the class round 9 measured escaping -- a
        # build drive, a UNC share, a POSIX root, and a real profile path re-spelled as an
        # administrative share or as percent-encoding.
        "detector: narrow the shipping question back to PROFILE paths only, as round 8 did",
        DETECTOR,
        "    if HOST_PROFILE_PATH_RE.search(text):\n        return True\n    return any(",
        "    return HOST_PROFILE_PATH_RE.search(text) is not None\n    return any(  # noqa",
        [
            "test_a_NON_PROFILE_absolute_WRAPPED_IN_PROSE_is_refused_in_EVERY_SPELLING",
            "test_a_host_path_WRAPPED_IN_PROSE_is_redacted_in_the_HANDOVER_slice_too",
        ],
    ),
    (
        # The normalisation half on its own. The grammar stays wide; only the alphabet-folding is
        # removed, and that is enough on its own -- the grammar is written against ONE separator, so
        # with nothing folding the alphabet first every Windows spelling walks out along with the
        # percent-encoded one. That is the round-8 failure mode in one line: a wide grammar over an
        # unnormalised string is still a spelling test.
        "detector: ask the grammar without folding the alphabet first",
        DETECTOR,
        "for found in _HOST_LOCATION_RE.finditer(_normalised(text))",
        "for found in _HOST_LOCATION_RE.finditer(text)  # noqa",
        ["test_a_NON_PROFILE_absolute_WRAPPED_IN_PROSE_is_refused_in_EVERY_SPELLING"],
    ),
    # ---- round-9 leak 2: the handover slice's KEYS were never contained --------------------------
    (
        # Round 8's handover walk, restored: VALUES cleaned, KEYS carried raw. The slice ships WHOLE
        # and is the agent's work queue, so a key is exactly as untrusted as a value -- which the
        # packager's own manifest walk had already concluded one artifact and one round earlier.
        "handover: clean VALUES but carry raw dictionary KEYS, as round 8 did",
        MECHANISM,
        "            safe_key, key_redacted = _redacted_key(key, out)",
        "            safe_key, key_redacted = key, False  # noqa",
        [
            "test_an_untrusted_handover_KEY_is_redacted_like_a_value",
            "test_the_handover_KEY_walk_reports_the_REDACTED_key_and_stays_idempotent",
        ],
    ),
    (
        # Redaction without collision disambiguation: two unsafe keys both land on one sentinel and
        # `dict` keeps the last, so the agent's work queue silently loses a field. Containment that
        # destroys data is not containment, which is why `tableau_env.scrub_tree` disambiguates and
        # why both of its heirs copy the property rather than the code.
        "handover: redact colliding keys onto ONE sentinel, losing every field but the last",
        MECHANISM,
        "    unique, suffix = REDACTED, 2\n    while unique in taken:\n"
        '        unique, suffix = f"{REDACTED}#{suffix}", suffix + 1\n    return unique, True',
        "    return REDACTED, True  # noqa",
        ["test_the_handover_KEY_walk_reports_the_REDACTED_key_and_stays_idempotent"],
    ),
    # ---- round-7 finding B2: keys are untrusted too, and nothing may be appended after the sweep --
    (
        # The values-only walk, restored. The key then survives into `project()`, which names the
        # field it just refused using that key and ships it in `scope.dropped_fields`.
        "containment: clean VALUES but preserve raw dictionary KEYS, as round 7 did",
        PACKAGER,
        "            safe_key, key_refused = _contain_unsafe_key(key, kept)",
        "            safe_key, key_refused = key, False  # noqa",
        [
            "test_an_untrusted_dictionary_KEY_is_contained_like_a_value",
            "test_the_containment_walk_contains_KEYS_and_reports_the_CONTAINED_key",
        ],
    ),
    (
        # The diagnostic half: even with keys contained at the view intake, a TOP-LEVEL manifest key
        # reaches `project()` raw. Building the dropped path from it puts the disclosure back into
        # the record of the catch -- `tableau_env.scrub_tree`'s third property, restated.
        "project: name a dropped field with its RAW, untrusted key",
        MECHANISM,
        "    return REDACTED if discloses_host_path(text) else text",
        "    return text  # noqa",
        ["test_NOTHING_is_appended_to_the_oracle_manifest_after_its_last_containment_pass"],
    ),
    (
        # The STRUCTURAL claim, isolated. Not "delete the sweep" -- that would be caught by half the
        # suite and would prove nothing about ordering. This restores the round-7 ORDER exactly: the
        # document is swept, and then `scope` is appended to it. Everything else stays contained, so
        # only the ordering anchor can fail.
        "oracle manifest: sweep the document and THEN append `scope`, as round 7 did",
        PACKAGER,
        "    stamped, refused = _contain_unsafe_strings(stamped)",
        '    _late = stamped.pop("scope")\n'
        "    stamped, refused = _contain_unsafe_strings(stamped)\n"
        '    stamped["scope"] = _late  # noqa',
        ["test_NOTHING_is_appended_to_the_oracle_manifest_after_its_last_containment_pass"],
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
        "        replace_dir(staging, final, verify=None if discard_edits else partial(_refuse_if_edited, unit))",
        "        shutil.copytree(staging, final, dirs_exist_ok=True)",
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
        "values as greppable `<text>` elements, except where labels render as paths - zero text is not zero\ncontent.",
        "values as greppable `<text>` elements, and both are renders of the same object.",
        ["test_the_readme_keeps_the_png_and_svg_legs_distinct_with_the_zero_text_caveat"],
    ),
    # ---- the 2026-09-03 cold-run findings, each a prose or shipping guard ----------------------
    (
        "README: demote the CSV oracle back below the image legs",
        PACKAGER,
        "**`oracle/*/data/*.csv` is the NUMERIC oracle** - exact labels and figures, "
        "no OCR and no judgement. Read it first.",
        "it also carries this unit's exported numbers.",
        ["test_the_readme_leads_with_the_csv_numeric_oracle_before_any_image"],
    ),
    (
        "README: drop the page-pairing contract an agent otherwise reads check_unit.py for",
        PACKAGER,
        "A page counts as REBUILT only when its `displayName` EXACTLY equals an expected object's name AND it\n"
        "ships at least one visual; one that pairs by name with no visual is reported `blank` and FAILS. The\n"
        "expected set is every dashboard PLUS every worksheet not placed on one.",
        "Both gates grade this unit against the pages it is expected to carry.",
        ["test_the_readme_states_the_page_pairing_contract"],
    ),
    (
        "README: soften the zero-visual page from a FAILURE to an omission",
        PACKAGER,
        "ships at least one visual; one that pairs by name with no visual is reported `blank` and FAILS. The",
        "ships at least one visual; one that pairs by name with no visual is simply not credited. The",
        ["test_the_readme_states_the_page_pairing_contract"],
    ),
    (
        "README: narrow the expected page set back to dashboards only",
        PACKAGER,
        "expected set is every dashboard PLUS every worksheet not placed on one.",
        "expected set is every dashboard in the workbook.",
        ["test_the_readme_states_the_page_pairing_contract"],
    ),
    (
        "README: put the bare unit NAME back on the documented gate commands",
        PACKAGER,
        "    python scripts/check_reference_readiness.py <path-to-this-folder>\n"
        "    python scripts/check_unit.py <path-to-this-folder>",
        "    python scripts/check_reference_readiness.py {unit}\n    python scripts/check_unit.py {unit}",
        ["test_every_command_the_readme_prints_produces_a_verdict_not_a_usage_error"],
    ),
    (
        "README: delete the provenance ceiling an agent cannot lift from inside the package",
        PACKAGER,
        "## UNFIXABLE FROM THIS PACKAGE",
        "## Notes",
        ["test_the_readme_names_the_provenance_ceiling_it_cannot_lift"],
    ),
    (
        "README: send the agent back to the bundle to edit (issue #460's silent-discard shape)",
        PACKAGER,
        "| `fabric/` | the engine WORKING COPY - **edit here**, and when you work from a package THIS tree "
        "is canonical; `<bundle>/pbip/` never promotes over it. Re-running `package_unit.py` into this "
        "folder REFUSES (exit 3) rather than discarding what you changed - `--discard-package-edits` "
        "overrides. Declared-edit tooling (`declare_generated_edit.py`, `--tamper`) is bundle-only.",
        "| `fabric/` | a copy of the engine working copy; edit `<bundle>/pbip/` instead.",
        [
            "test_AGENTS_md_and_the_package_readme_agree_on_where_an_agent_edits",
            "test_declared_edit_tooling_is_scoped_to_bundle_work_in_BOTH_documents",
        ],
    ),
    (
        "README: drop the bundle-only scope from the declared-edit tooling note",
        PACKAGER,
        " Declared-edit tooling (`declare_generated_edit.py`, `--tamper`) is bundle-only.",
        "",
        ["test_declared_edit_tooling_is_scoped_to_bundle_work_in_BOTH_documents"],
    ),
    (
        "packager: stop shipping the spec contract, leaving the README to describe it",
        PACKAGER,
        "    shutil.copy2(SPEC_SCHEMA, dest / SPEC_SCHEMA.name)\n    return SPEC_SCHEMA.name, None",
        "    return None, None",
        ["test_the_package_ships_the_spec_schema_it_tells_an_agent_to_obey"],
    ),
    # ---- issue #461: the package carrying its own rows ----------------------------------------
    (
        "packager: skip localization, leaving every partition pointing into the bundle",
        PACKAGER,
        "    data_sources = _localize_data_sources(dest, final, model_name)",
        '    data_sources = {"parameter": None, "shipped": [], "omissions": [], "bytes": 0}',
        [
            "test_no_packaged_tmdl_points_at_an_absolute_path_OUTSIDE_the_package",
            "test_the_rows_the_model_imports_are_shipped_and_the_partition_reads_them",
        ],
    ),
    (
        "packager: rewrite the partitions but ship none of the bytes",
        PACKAGER,
        "    shutil.copy2(readable, target)",
        "    pass",
        ["test_the_rows_the_model_imports_are_shipped_and_the_partition_reads_them"],
    ),
    (
        # ⚠️ Replaces "write the parameter from the STAGING dir", which this merge made INERT. That
        # mutation swapped `final` for `dest` in the value; the placeholder-root design now takes
        # only the SEPARATOR from that argument, and two local paths have the same separator, so
        # the swap changed nothing and the anchor test could no longer fail. This targets what
        # actually protects the same property today: the value must never be a build-time path.
        "packager: bake the BUILD-TIME package path into the data-folder parameter (the pre-token shape)",
        PACKAGER,
        '    return f"{PACKAGE_ROOT_TOKEN}{sep}{DATA_DIR}{sep}"',
        '    return f"{final}{sep}{DATA_DIR}{sep}"',
        ["test_the_data_folder_parameter_names_a_PLACEHOLDER_not_the_machine_that_built_it"],
    ),
    (
        "packager: drop the size ceiling, so an unbounded source is copied unnoticed",
        PACKAGER,
        "    if size > MAX_DATA_BYTES:",
        "    if False:  # noqa",
        ["test_an_oversized_source_is_refused_by_the_ceiling_rather_than_copied"],
    ),
    (
        "packager: skip an unshippable source SILENTLY instead of recording why",
        PACKAGER,
        '                record["omissions"].append({"file": _leaf(source), "reason": refusal})',
        "                pass",
        ["test_a_source_that_cannot_be_shipped_is_a_LOUD_omission_not_a_silent_skip"],
    ),
    (
        "packager: resolve a packaged-name collision by overwriting the first source",
        PACKAGER,
        "        candidate = f\"{hashlib.sha256(source.encode('utf-8')).hexdigest()[:8]}/{name}\"",
        "        candidate = candidate",
        ["test_two_sources_sharing_a_file_name_do_not_overwrite_each_other"],
    ),
    (
        "README: drop the data/ row, so the shipped rows are unmentioned in the package map",
        PACKAGER,
        "| `data/` | the rows the model imports",
        "| `unmentioned/` | the rows the model imports",
        ["test_the_generated_readme_names_every_file_the_package_contains"],
    ),
    (
        "packager: scan only File.Contents, missing every datasource-only unit's folder parameter",
        PACKAGER,
        "    _localize_folder_parameters(documents, dest, final, record, taken, accounted)",
        "    pass",
        ["test_a_folder_PARAMETER_pointing_out_of_the_package_is_moved_with_its_files"],
    ),
    (
        "packager: treat ANY POSIX-absolute literal as a file path (8 of 9 are false positives)",
        PACKAGER,
        'return value.startswith("/") and bool(PurePosixPath(value).suffix)',
        'return value.startswith("/")',
        ["test_a_POSIX_literal_with_no_file_suffix_is_left_alone"],
    ),
    (
        "packager: PROBE a UNC literal, which blocks on SMB name resolution for minutes",
        PACKAGER,
        '        return None, "a UNC path is not probed, because resolving an absent host can block for minutes"',
        "        pass",
        ["test_a_UNC_literal_is_refused_WITHOUT_being_probed"],
    ),
    (
        "packager: report EVERY absolute path, condemning the package's own legitimate DataFolder",
        PACKAGER,
        "            if value not in accounted and _is_path_literal(value) and not _inside(final, value):",
        "            if value not in accounted and _is_path_literal(value):",
        [
            "test_an_absolute_path_under_the_packages_own_data_is_NOT_a_violation",
            "test_the_rows_the_model_imports_are_shipped_and_the_partition_reads_them",
        ],
    ),
    (
        f"{NEGATIVE_CONTROL}: a comment-only edit that must change no verdict",
        PACKAGER,
        "# --------------------------------------------------------------------------------------------\n# CLI",
        "# --------------------------------------------------------------------------------------------\n# CLI (negative control)",
        ["test_no_foreign_unit_survives_anywhere_in_the_packaged_report"],
    ),
    # ---- issue #476: the path budget, and the staging segment that was added at the deepest point -
    (
        "staging: put the unit name back in the staging segment (the pre-fix `.{unit}.staging`)",
        PACKAGER,
        '    return out_root / f".{_short_stem(unit)}"',
        '    return out_root / f".{unit}.staging"',
        [
            "test_staging_is_shallower_than_the_package_it_becomes",
            "test_the_staging_name_costs_a_CONSTANT_never_the_length_of_the_unit_name",
            "test_nothing_is_assembled_deeper_than_the_pre_fix_staging_tree",
        ],
    ),
    (
        "staging: stage every unit under ONE shared directory, so a second unit overwrites the first",
        PACKAGER,
        '    return out_root / f".{_short_stem(unit)}"',
        '    return out_root / ".staging"',
        ["test_two_units_do_not_stage_under_the_same_directory"],
    ),
    (
        "replace_dir: name the retired tree after the package it retires (10 characters deeper)",
        PACKAGER,
        '    return final.with_name(f".{_short_stem(final.name)}{_RETIRED_SUFFIX}")',
        '    return final.with_name(f".{final.name}.replaced")',
        ["test_the_retired_package_is_never_named_after_the_package_it_retires"],
    ),
    (
        "budget: package the unit anyway, reproducing the mid-assembly WinError 206",
        PACKAGER,
        "    budget = path_budget(bundle, unit, out_root, limits=limits, assets_dir=assets_dir)\n"
        "    if budget.refused:\n        raise PackagePathTooLong(budget)",
        "    budget = path_budget(bundle, unit, out_root, limits=limits, assets_dir=assets_dir)\n"
        "    if False:  # noqa\n        raise PackagePathTooLong(budget)",
        [
            "test_a_unit_whose_paths_exceed_the_ceiling_is_refused_BEFORE_anything_is_written",
            "test_the_refusal_names_the_path_its_length_the_ceiling_and_the_characters_to_reclaim",
        ],
    ),
    (
        "budget: measure only the FINAL tree, missing a unit that fits once renamed and not before",
        PACKAGER,
        "    return (final, staging_dir(out_root, unit), retired_dir(final))",
        "    return (final,)",
        [
            "test_the_budget_measures_the_STAGED_tree_too_not_only_the_final_one",
            "test_the_budget_measures_both_the_staged_and_the_final_tree",
        ],
    ),
    (
        "budget: let the batch run start, so the estate fails one unit at a time again",
        PACKAGER,
        "        parser.error(render_out_too_deep(too_deep, len(units)))",
        "        pass",
        [
            "test_main_refuses_a_too_deep_out_before_packaging_ANY_unit",
            "test_the_batch_refusal_names_every_offending_unit_and_one_number_that_fixes_them",
        ],
    ),
    (
        "message: name the path but not its length, the ceiling or the overage (WinError 206's own failing)",
        PACKAGER,
        'f"  deepest: {worst.length} UTF-16 units, {worst.length - worst.ceiling} over the "\n'
        '        f"{worst.ceiling}-character {kind} ceiling\\n"',
        'f"  deepest: this path is too long\\n"',
        ["test_the_refusal_names_the_path_its_length_the_ceiling_and_the_characters_to_reclaim"],
    ),
    (
        "message: report a reclaim figure that does not add up to the --out it measured",
        PACKAGER,
        'f"{budget.hard_budget} ({budget.out_root_length - budget.hard_budget} shorter) for this unit to fit."',
        'f"{budget.hard_budget} ({budget.hard_budget} shorter) for this unit to fit."',
        ["test_the_refusal_names_the_path_its_length_the_ceiling_and_the_characters_to_reclaim"],
    ),
    (
        "message: offer a negative --out length instead of saying no --out can fit the unit",
        PACKAGER,
        "        if budget.hard_budget >= 0",
        "        if budget.hard_budget >= -1000",
        ["test_a_unit_no_out_can_fit_says_so_instead_of_naming_an_impossible_directory"],
    ),
    # ---- blind-review B2: a staging name that could delete a finished package -------------------
    (
        "B2: allow a unit to be named exactly like another unit's staging directory",
        PACKAGER,
        "            is_reserved_packaging_name(unit),",
        "            False,  # noqa",
        [
            "test_a_unit_named_like_another_units_staging_directory_cannot_delete_it",
            "test_every_scratch_name_this_packager_creates_is_one_no_unit_may_be_called",
        ],
    ),
    (
        "B2: rmtree whatever occupies the staging path, named by this packager or not",
        PACKAGER,
        "    if not is_reserved_packaging_name(path.name):",
        "    if False:  # noqa",
        ["test_the_cleanup_refuses_to_delete_a_directory_this_packager_did_not_name"],
    ),
    (
        "B2: reserve a name shape the generators never produce, so the two halves drift apart",
        PACKAGER,
        'rf"^\\.[0-9a-f]{{{_STAGING_STEM_CHARS}}}{re.escape(_RETIRED_SUFFIX)}?$"',
        'r"^\\.reserved$"',
        [
            "test_every_scratch_name_this_packager_creates_is_one_no_unit_may_be_called",
            "test_a_unit_named_like_another_units_staging_directory_cannot_delete_it",
        ],
    ),
    # ---- blind-review B1: the projection that measured a subset and reported exit 0 -------------
    (
        "B1: stop projecting the source asset, whose filename the CUSTOMER chose",
        PACKAGER,
        '        tails.append((KIND_FILE, f"assets/{asset.name}"))',
        "        pass",
        ["test_the_source_assets_own_filename_is_projected_not_discovered_at_write_time"],
    ),
    (
        "B1: trust the projection, so an output nobody predicted ships unmeasured",
        PACKAGER,
        "        assert_assembled_fits(unit, staging, final, out_root, limits)",
        "        pass",
        ["test_an_output_the_projection_never_predicted_is_still_refused_before_the_swap"],
    ),
    (
        "B1: forget the RETIRED tree that shutil.rmtree walks on every repackage",
        PACKAGER,
        "    return (final, staging_dir(out_root, unit), retired_dir(final))",
        "    return (final, staging_dir(out_root, unit))",
        ["test_the_RETIRED_tree_is_measured_because_rmtree_WALKS_it"],
    ),
    (
        "B1: let the filesystem's own length refusal escape as a bare traceback again",
        PACKAGER,
        '    if getattr(error, "winerror", None) != _TOO_LONG_WINERROR and error.errno != errno.ENAMETOOLONG:\n'
        "        return None",
        "    if True:  # noqa\n        return None",
        ["test_a_length_refusal_from_the_FILESYSTEM_is_restated_with_a_path_and_a_remedy"],
    ),
    # ---- blind-review B3: whose ceiling is being applied to which path --------------------------
    (
        "B3: apply the WINDOWS ceilings to absolute paths on every host again",
        PACKAGER,
        "    limits = platform_limits() if limits is None else limits\n    return _budget(",
        "    limits = WINDOWS_LIMITS if limits is None else limits\n    return _budget(",
        ["test_a_long_POSIX_out_is_not_refused_by_a_ceiling_that_belongs_to_WINDOWS"],
    ),
    (
        "B3: drop the relocation half, so a POSIX host builds a package Windows can never open",
        PACKAGER,
        "        return bool(self.overruns or self.shipping)",
        "        return bool(self.overruns)",
        ["test_a_tail_no_WINDOWS_root_can_fit_is_refused_even_on_a_generous_host"],
    ),
    (
        "B3: swallow the shipping advisory, so a tight relocation budget travels unreported",
        PACKAGER,
        "    if not tight:\n        return None",
        "    if True:  # noqa\n        return None",
        ["test_a_package_that_barely_fits_a_WINDOWS_root_WARNS_and_still_ships"],
    ),
    # ---- the ceilings themselves: pinned in a suite this harness can score ----------------------
    (
        "ceilings: move DIR_CEILING to the value Desktop's own message names (247 -> 260)",
        CEILINGS,
        "DIR_CEILING = 247",
        "DIR_CEILING = 260",
        ["test_the_measured_desktop_ceilings_are_pinned_as_two_DISTINCT_literals"],
    ),
    (
        "ceilings: derive FILE_CEILING from DIR_CEILING instead of pinning it separately",
        CEILINGS,
        "FILE_CEILING = 259\nDIR_CEILING = 247",
        "FILE_CEILING = 247 + 13\nDIR_CEILING = 247",
        [
            "test_the_measured_desktop_ceilings_are_pinned_as_two_DISTINCT_literals",
            "test_the_packager_budgets_against_those_same_two_literals",
        ],
    ),
]

_FAILED = re.compile(r"(\d+) failed")
_ERROR = re.compile(r"(\d+) errors?\b|^(?:ERROR|INTERNALERROR)\b", re.IGNORECASE | re.MULTILINE)


def run_one_anchor(name: str) -> tuple[str, str]:
    """Run one anchor test; return (verdict, note).

    ``encoding="utf-8"`` is load-bearing on Windows: ``text=True`` alone decodes the child's stdout
    with the console codepage, and this file's own ``⚠️`` caveats appear in failing assertions.
    """
    proc = subprocess.run(  # noqa: S603
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
