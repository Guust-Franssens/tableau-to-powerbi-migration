"""Mutation harness for the #417 credential-modal DECISION SEAM.

    python tests/mutation_credential_seam.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest, exactly like
``tests/mutation_harness.py``, whose scorer it imports rather than reimplementing. That import is the
point: a sibling agent wrote its own scorer, found a stricter rule, and had to retract it after the
rule produced a **false INVALID**. There is one scorer in this repo.

⚠️ **These mutations edit SOURCE FILES on disk, not module attributes, and that is forced by the
subject.** The seam's collector path is ``probe_desktop_credential.ps1`` -> a **child python
process** running ``decide_dialog.py``. A ``monkeypatch``/plugin mutation applied inside the pytest
process is invisible to that child, so an attribute-level mutation would SURVIVE every collector test
for a reason that has nothing to do with coverage - the single most expensive kind of false result.
Each file is restored in a ``finally``, and :func:`main` re-verifies every file byte-for-byte at the
end, because a harness that can leave the tree mutated is worse than no harness.

Each mutation is anchored to the test that must catch it. Anchoring rather than running the whole
file is deliberate twice over: the file's ``serial`` tests spawn real GUI windows on the operator's
desktop, and ``run()`` has no marker filter; and a mutation test's question is "can the covering test
fail", not "can any test fail".

Three discriminating controls are included, because a harness with only positive cases cannot tell a
working detector from a broken one:

* ``control-cosmetic-comment`` mutates a COMMENT. It must **SURVIVE** - if it is reported CAUGHT the
  suite is failing for a reason unrelated to the mutation.
* ``control-absent-anchor`` names a string that is not in the file. It must be reported **INVALID**
  before pytest is ever started - the measured vacuity mode where a stale symbol crashes and scores
  CAUGHT.
* ``control-cosmetic-docstring`` mutates prose inside a docstring, against the anchor with the
  largest assertion surface. Must SURVIVE.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
import mutation_harness  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".github" / "skills" / "pbip-model-refresh"
DETECTOR = SKILL / "scripts" / "_credential_modal.py"
DECIDER = SKILL / "scripts" / "decide_dialog.py"
COLLECTOR = SKILL / "scripts" / "probe_desktop_credential.ps1"
SUITE = ".github/skills/pbip-model-refresh/tests/test_credential_modal_detection.py"

# name -> (file, old source text, new source text, anchor test, expected verdict)
MUTATIONS: dict[str, tuple[Path, str, str, str, str]] = {
    # --- #417: only a real Boolean True may authorise suppression --------------------------------
    "harvest-complete-accepts-truthy": (
        DECIDER,
        'harvest_complete=raw.get("HarvestComplete") is True,',
        'harvest_complete=bool(raw.get("HarvestComplete")),',
        "test_only_a_real_boolean_true_can_authorise_suppression",
        "CAUGHT",
    ),
    "harvest-complete-accepts-anything": (
        DECIDER,
        'harvest_complete=raw.get("HarvestComplete") is True,',
        "harvest_complete=True,",
        "test_only_a_real_boolean_true_can_authorise_suppression",
        "CAUGHT",
    ),
    "an-incomplete-harvest-is-still-benign": (
        DETECTOR,
        "        if window.harvest_complete is False:\n"
        "            return _finding(DIALOG_KIND_BENIGN_UNVERIFIED, window, benign_hit)\n",
        "",
        "test_benign_content_from_a_truncated_harvest_is_not_benign",
        "CAUGHT",
    ),
    # --- #417 review finding 2: a caption substring must not convict ------------------------------
    "the-join-reads-the-caption-too": (
        DETECTOR,
        "    body = tuple(text for text in content if canonical_text(text) not in interactive)\n"
        "    if len(body) > 1:\n"
        "        prose = tuple(text for text in all_texts if canonical_text(text) not in interactive)\n"
        '        pairs.append((" ".join(body), " ".join(prose)))',
        "    body = tuple(text for text in content if canonical_text(text) not in interactive)\n"
        "    if len(body) > 1:\n"
        "        prose = tuple(text for text in all_texts if canonical_text(text) not in interactive)\n"
        '        pairs.append((" ".join(prose), " ".join(prose)))',
        # ⚠️ NOT `test_a_harmless_caption_substring_does_not_fabricate_a_credential_wall`. Measured
        # here on the first run: that test's fixture leaves ONE non-interactive body element, so
        # `len(body) > 1` is false and the mutated line is never executed - the mutation SURVIVED for
        # a reason with nothing to do with coverage. That is the repo's third known vacuity mode ("an
        # assertion inside a branch the fixture never enters"), caught by the harness rather than by
        # reading, and it is why an anchor has to be chosen against the branch and not the topic.
        "test_a_join_the_body_cannot_carry_on_its_own_never_convicts",
        "CAUGHT",
    ),
    "the-join-swallows-interactive-elements": (
        DETECTOR,
        "    body = tuple(text for text in content if canonical_text(text) not in interactive)",
        "    body = tuple(content)",
        "test_an_interposed_button_does_not_break_the_split_signature",
        "CAUGHT",
    ),
    "the-caption-is-searched-element-by-element": (
        DETECTOR,
        "    pairs = [(text, text) for text in content]",
        "    pairs = [(text, text) for text in all_texts]",
        "test_a_harmless_caption_substring_does_not_fabricate_a_credential_wall",
        "CAUGHT",
    ),
    "no-prose-join-at-all": (
        DETECTOR,
        "    if len(body) > 1:",
        "    if False:  # noqa: SIM108",
        "test_a_credential_signature_split_across_wpf_elements_still_convicts",
        "CAUGHT",
    ),
    "the-join-drops-the-caption-from-its-evidence": (
        DETECTOR,
        '        pairs.append((" ".join(body), " ".join(prose)))',
        '        pairs.append((" ".join(body), " ".join(body)))',
        "test_a_credential_signature_split_across_wpf_elements_still_convicts",
        "CAUGHT",
    ),
    # --- #417 review: modality must not exonerate an identified blocking prompt --------------------
    "modality-outranks-an-identified-prompt": (
        DETECTOR,
        "        and (not is_proven_non_blocking(window) or is_identified_human_blocker(window))",
        "        and not is_proven_non_blocking(window)",
        "test_a_native_query_approval_outranks_an_enabled_owner",
        "CAUGHT",
    ),
    "identification-also-overrides-modality-for-credentials": (
        DETECTOR,
        "    blocking = blocking_prompt_signature()\n"
        "    return any(blocking.search(matched) for matched, _ in credential_search_texts(window))",
        "    blocking = blocking_prompt_signature()\n"
        "    credential = credential_signature()\n"
        "    return any(\n"
        "        blocking.search(matched) or credential.search(matched)\n"
        "        for matched, _ in credential_search_texts(window)\n"
        "    )",
        "test_the_credential_prepass_reads_windows_that_classification_skips",
        "CAUGHT",
    ),
    # --- #417: every reportable kind has operator guidance, and it crosses the seam ----------------
    "benign-unverified-has-no-guidance": (
        DETECTOR,
        "    DIALOG_KIND_BENIGN_UNVERIFIED: (\n"
        '        "its content reads as refresh progress, but the read of that window did not finish - "\n'
        '        "benign-LOOKING is not benign, and what was missed is unknown; look at the Desktop screen"\n'
        "    ),\n",
        "",
        "test_every_finding_kind_has_operator_guidance",
        "CAUGHT",
    ),
    "guidance-never-crosses-the-seam": (
        DECIDER,
        '        "guidance": None if finding is None else dialog_guidance(finding),',
        '        "guidance": None,',
        "test_the_operator_guidance_crosses_the_seam_with_the_verdict",
        "CAUGHT",
    ),
    "guidance-names-a-sign-on-problem": (
        DETECTOR,
        '        "its content reads as refresh progress, but the read of that window did not finish - "',
        '        "its content reads as refresh progress, but authentication may still be pending - "',
        "test_the_guidance_is_marker_free_so_it_cannot_be_relabelled_as_a_sign_on_problem",
        "CAUGHT",
    ),
    # --- #417: the collector must not decide anything ---------------------------------------------
    "the-collector-drops-the-in-flight-flag": (
        COLLECTOR,
        "    if ($RefreshInFlight) { $argv += '--in-flight' }",
        "    if ($false) { $argv += '--in-flight' }",
        "test_a_refresh_progress_dialog_during_our_own_refresh_is_ignored_entirely",
        "CAUGHT",
    ),
    "the-collector-invents-a-clean-verdict-when-the-decider-is-silent": (
        COLLECTOR,
        "        Verdict = 'DIALOG_UNREADABLE'; Kind = 'unreadable'; ExitCode = 3\n"
        '        Evidence = "decider produced no verdict: $raw"',
        '        Verdict = $null; Kind = $null; ExitCode = 0\n        Evidence = "decider produced no verdict: $raw"',
        "test_a_decider_that_answers_nothing_is_indeterminate_never_clean",
        "CAUGHT",
    ),
    # --- blind review 2026-09-01, HIGH 2: canonical caption identity ------------------------------
    "caption-identity-is-case-sensitive-again": (
        DETECTOR,
        "    content = tuple(text for text in all_texts if canonical_text(text) != canonical_title)",
        "    content = tuple(text for text in all_texts if text != title)",
        # The anchor is the ORACLE case, because that is the only test that asserts the CORRECT answer
        # rather than agreement between the two arms - the differential passed this defect with zero
        # mismatches, both paths agreeing on exit 0.
        "test_the_seam_answers_a_known_oracle_CORRECTLY_not_merely_consistently",
        "CAUGHT",
    ),
    "caption-identity-folds-case-but-not-normalisation": (
        DETECTOR,
        '    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())',
        "    return text.casefold()",
        "test_the_seam_answers_a_known_oracle_CORRECTLY_not_merely_consistently",
        "CAUGHT",
    ),
    "the-join-matches-interactive-labels-case-sensitively": (
        DETECTOR,
        "    interactive = {canonical_text(text) for text in normalize_texts(window.interactive_texts)}",
        "    interactive = set(normalize_texts(window.interactive_texts))",
        "test_the_seam_answers_a_known_oracle_CORRECTLY_not_merely_consistently",
        "CAUGHT",
    ),
    # --- blind review 2026-09-01, HIGH 1: the window schema ---------------------------------------
    "no-schema-validation-at-all": (
        DECIDER,
        "    validate_window(raw)\n",
        "",
        "test_a_window_record_the_decider_cannot_trust_is_never_a_clean_verdict",
        "CAUGHT",
    ),
    "unknown-fields-are-ignored-not-rejected": (
        DECIDER,
        "    unknown = sorted(set(raw) - set(WINDOW_FIELDS) - TOLERATED_FIELDS)\n    if unknown:",
        "    unknown = []\n    if unknown:",
        "test_a_window_record_the_decider_cannot_trust_is_never_a_clean_verdict",
        "CAUGHT",
    ),
    "missing-fields-are-ignored-not-rejected": (
        DECIDER,
        "    missing = sorted(set(WINDOW_FIELDS) - set(raw))\n    if missing:",
        "    missing = []\n    if missing:",
        "test_a_window_record_the_decider_cannot_trust_is_never_a_clean_verdict",
        "CAUGHT",
    ),
    "type-checks-use-isinstance-so-a-bool-is-a-width": (
        DECIDER,
        "    return any(type(value) is expected for expected in allowed)  # pylint: disable=unidiomatic-typecheck",
        "    return isinstance(value, allowed)",
        "test_a_window_record_the_decider_cannot_trust_is_never_a_clean_verdict",
        "CAUGHT",
    ),
    "text-elements-are-not-checked": (
        DECIDER,
        '    for name in ("Texts", "InteractiveTexts"):',
        "    for name in ():",
        "test_a_window_record_the_decider_cannot_trust_is_never_a_clean_verdict",
        "CAUGHT",
    ),
    "validation-runs-after-the-candidates-branch": (
        DECIDER,
        "        windows = [_window_from(item) for item in raw]",
        "        windows = (\n"
        "            [\n"
        "                DesktopWindow(\n"
        '                    title=str(item.get("Title") or ""),\n'
        '                    class_name=str(item.get("ClassName") or ""),\n'
        '                    width=int(item.get("Width") or 0),\n'
        '                    height=int(item.get("Height") or 0),\n'
        '                    texts=tuple(str(t) for t in (item.get("Texts") or []) if t),\n'
        '                    hwnd=int(item.get("Hwnd") or 0),\n'
        '                    owner_hwnd=int(item.get("OwnerHwnd") or 0),\n'
        "                )\n"
        "                for item in raw\n"
        "            ]\n"
        "            if args.candidates_only\n"
        "            else [_window_from(item) for item in raw]\n"
        "        )",
        # A faithful reconstruction of the measured pre-fix behaviour on that branch, rather than
        # "delete the validate call" - which was the first attempt and SURVIVED, because `_window_from`
        # then raised KeyError on the same record and produced the identical exit-3 answer. Defence in
        # depth is real here; expressing the mutation as the ORIGINAL defect is what makes it visible.
        "test_the_candidates_only_pass_fails_closed_too",
        "CAUGHT",
    ),
    "the-schema-carve-out-grows-to-swallow-a-real-field": (
        DECIDER,
        'TOLERATED_FIELDS = frozenset({"HarvestComplete"})',
        'TOLERATED_FIELDS = frozenset({"HarvestComplete", "Width"})',
        "test_a_malformed_self_report_is_unverified_not_unreadable",
        "CAUGHT",
    ),
    "the-collector-stops-sending-OwnerHwnd": (
        COLLECTOR,
        "    OwnerHwnd        = $Window.OwnerHwnd\n",
        "",
        "test_the_collector_emits_exactly_the_fields_the_decider_requires",
        "CAUGHT",
    ),
    # --- discriminating controls ------------------------------------------------------------------
    "control-cosmetic-comment": (
        DETECTOR,
        "# Per-kind operator guidance.",
        "# Per-kind operator guidance (cosmetic mutation-control edit).",
        "test_every_finding_kind_has_operator_guidance",
        "SURVIVED",
    ),
    "control-cosmetic-docstring": (
        DECIDER,
        '"""Read collected windows, print one JSON verdict line, and return its exit code."""',
        '"""Read collected windows and print one JSON verdict line (cosmetic mutation-control edit)."""',
        "test_the_seam_leaves_no_verdict_mismatch_between_the_two_entry_paths",
        "SURVIVED",
    ),
    "control-absent-anchor": (
        DETECTOR,
        "def get_dialog_classification_that_was_deleted_in_417(",
        "def something_else(",
        "test_every_finding_kind_has_operator_guidance",
        "INVALID",
    ),
}


def verdict_for(name: str, path: Path, old: str, new: str, anchor: str) -> tuple[str, str]:
    """Apply one SOURCE mutation, run its anchor, and score it with the SHARED harness.

    Returns ``(verdict, detail)``. ``INVALID`` means the mutation never applied, which is a harness
    result and not evidence about the tests - the measured vacuity mode this repo has already been
    bitten by is a stale symbol scoring CAUGHT.
    """
    original = path.read_text(encoding="utf-8")
    occurrences = original.count(old)
    if occurrences != 1:
        return "INVALID", f"anchor text appears {occurrences}x in {path.name} - the mutation never applied"
    try:
        path.write_text(original.replace(old, new), encoding="utf-8")
        _, returncode, detail, outcomes = mutation_harness.run(name, "pass\n", f"{SUITE}::{anchor}")
    finally:
        path.write_text(original, encoding="utf-8")
    if mutation_harness.observed_mutation(outcomes):
        return "CAUGHT", detail
    if mutation_harness.session_is_trustworthy(outcomes):
        return "SURVIVED", detail
    return "INVALID", f"pytest never reached a verdict (exit {returncode}, {detail})"


def main() -> int:
    """Run every mutation, compare against its expected verdict, and prove the tree is restored."""
    before = {path: path.read_bytes() for path in (DETECTOR, DECIDER, COLLECTOR)}

    anchors = sorted({anchor for _, _, _, anchor, _ in MUTATIONS.values()})
    baseline = mutation_harness.subprocess.run(
        [mutation_harness.PY, "-m", "pytest", *[f"{SUITE}::{anchor}" for anchor in anchors]]
        + ["-q", "--no-header", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=mutation_harness.sanitized_env(),
    )
    print(f"BASELINE {len(anchors)} anchors  exit={baseline.returncode}  {mutation_harness.last_line(baseline)}")
    if baseline.returncode != 0:
        # A mutation is only evidence against a clean baseline: an already-failing anchor would be
        # credited to every mutation that names it.
        print("\nHARNESS ERROR: the anchor baseline is not clean, so no verdict is trustworthy.")
        return 2
    print()

    wrong: list[str] = []
    for name, (path, old, new, anchor, expected) in MUTATIONS.items():
        verdict, detail = verdict_for(name, path, old, new, anchor)
        flag = "ok " if verdict == expected else "!! "
        if verdict != expected:
            wrong.append(f"{name}: expected {expected}, got {verdict} ({detail})")
        print(f"{flag}{verdict:9s} {name:52s} -> {anchor}")

    print()
    unrestored = [path.name for path, blob in before.items() if path.read_bytes() != blob]
    if unrestored:
        print(f"HARNESS ERROR: files left mutated on disk: {unrestored}")
        return 2
    print("all mutated files restored byte-for-byte")
    if wrong:
        print("\nUNEXPECTED VERDICTS:")
        for item in wrong:
            print(f"  {item}")
        return 1
    print(f"{len(MUTATIONS)} mutations, every verdict as expected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
