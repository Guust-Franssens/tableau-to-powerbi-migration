"""Tests for the two engine signals `read_handover.py` had no consumer for: #371 and #372.

Both issues are about COVERAGE rather than findings, so every test here is written against the same
one-sentence requirement: **absent evidence must never render, sort, or exit like verified
evidence.** A test that merely proves a value is printed does not defend that; each test below
therefore contrasts the absent case with the verified case, or asserts an exit code.

Fixture provenance, which is load-bearing:

* ``handover-2.339.0-evidence.json`` is the **unmodified** handover slice engine 2.339.0 wrote for
  ``Meridian Revenue by Region`` (copied byte-for-byte out of a real bundle). Its three
  ``viz_fidelity[]`` rows genuinely carry ``"evidence": "emitted+linted"``. Nothing here invents the
  handover shape.
* ``oracle-2.339.0-drifted.json`` / ``-pixel-exact.json`` / ``oracle-pre-2.332.0-axis-blind.json``
  carry a ``summary.placement`` block produced by calling the installed engine's own
  ``fidelity_oracle._placement_rollup`` - so ``by_axis`` is the engine's arithmetic, not a
  hand-typed guess at it. The drifted one encodes the shape upstream #169 was built to express: X
  pixel-perfect, Y drifting in BOTH directions (2 down / 1 up, signed mean +38.93 px).

⚠️ MEASURED FINDING, and the reason the #372 tests look the way they do: ``by_axis`` is **not** in
the handover. It is emitted by ``fidelity_oracle.py``, a separate opt-in tool, into its own report;
``migrate_estate.py`` never computes placement. Verified against engine 2.339.0 - ``by_axis`` occurs
in **zero** JSON files across the local corpus, and the estate ``report.json``'s
``summary.placement`` is ``null``. So the honest consumer finds an oracle report beside the bundle,
and reports NOT MEASURED otherwise - which is today's answer for every deterministic-tier bundle.

No network, no Power BI Desktop, no engine at test time: the engine was used to BUILD the fixtures,
never to run them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import read_handover as rh  # noqa: E402  # pylint: disable=wrong-import-position

REAL_SLICE = FIXTURES / "handover-2.339.0-evidence.json"
ORACLE_DRIFTED = FIXTURES / "oracle-2.339.0-drifted.json"
ORACLE_EXACT = FIXTURES / "oracle-2.339.0-pixel-exact.json"
ORACLE_AXIS_BLIND = FIXTURES / "oracle-pre-2.332.0-axis-blind.json"

WB_NAME = "Meridian Revenue by Region"


def real_workbook() -> dict:
    """The real 2.339.0 workbook payload, fresh each call so a test cannot mutate its neighbour."""
    return json.loads(REAL_SLICE.read_text(encoding="utf-8"))["workbook"]


def with_evidence(*values: str | None) -> dict:
    """The real payload with its `viz_fidelity[]` evidence values replaced.

    Derived from the REAL slice rather than hand-built, so every other key a renderer touches
    (`status`, `tier`, `reason`, `worksheet`, `visual_type`) is the engine's own. `None` DELETES the
    key, which is how a pre-2.335.0 bundle actually looks - not `"evidence": null`.
    """
    wb = real_workbook()
    rows = wb["viz_fidelity"]
    assert len(rows) == len(values), "the real slice has 3 rows; pass exactly that many values"
    for row, value in zip(rows, values):
        if value is None:
            row.pop("evidence", None)
        else:
            row["evidence"] = value
    return wb


def bundle(tmp_path: Path, wb: dict, oracle: Path | None = None, oracle_name: str | None = None) -> Path:
    """A bundle laid out the way the engine lays one out: `<bundle>/handover/<workbook>.json`."""
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True, exist_ok=True)
    payload = {"estate": {"tool": "migrate_estate"}, "workbook": wb}
    (root / "handover" / f"{wb.get('name') or WB_NAME}.json").write_text(json.dumps(payload), encoding="utf-8")
    if oracle is not None:
        (root / (oracle_name or f"{wb.get('name') or WB_NAME}.json")).write_text(
            oracle.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return root


def cites_oracle(stdout: str) -> bool:
    """True when the drift numbers carry a citation, in either the full or the compressed form.

    Deliberately form-agnostic: which form `render_default` picks depends on the byte budget, and a
    test that pinned one form would be asserting the budget rather than the citation. The FORMS
    themselves are pinned directly on `_layout_drift_lines` below.
    """
    return "measured from:" in stdout or ".json]" in stdout


def run_cli(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPTS / "read_handover.py"), *args]
    return subprocess.run(cmd, capture_output=True, check=False, text=True, encoding="utf-8")


# =============================================================================================
# #371 - viz_fidelity[].evidence
# =============================================================================================


def test_the_real_2339_slice_actually_carries_evidence():
    """Ground truth. If the engine ever stops emitting this, every test below is theatre, so pin the
    fixture's real content rather than trusting the issue text that described it."""
    rows = real_workbook()["viz_fidelity"]
    assert [r["evidence"] for r in rows] == ["emitted+linted"] * 3
    status, counts = rh.fidelity_evidence_status(real_workbook())
    assert status == rh.FIDELITY_EVIDENCE_PRESENT
    assert counts == {"emitted+linted": 3}


def test_an_unexamined_visual_does_not_render_like_a_verified_one():
    """THE REQUIREMENT, stated as a contrast rather than as a presence check.

    Mutation killed: make `_evidence_summary_line` ignore which value it saw (e.g. always print the
    total as verified). Both lines become identical and this fails.
    """
    verified = rh._evidence_summary_line(with_evidence("emitted+linted", "emitted+linted", "emitted+linted"))
    unexamined = rh._evidence_summary_line(with_evidence("emitted", "emitted+linted", "emitted+linted"))
    assert verified != unexamined
    assert "3/3" in verified and "!!" not in verified
    assert "2/3" in unexamined and "??" in unexamined
    assert "emitted 1" in unexamined


def test_a_never_examined_visual_does_not_pass_the_gate():
    """`emitted` means the emitter ran and NOTHING inspected the artifact. Judged by exit code.

    Mutation killed: treat `emitted` as a pass (or as a blocker). Either collapse changes this code.
    """
    code, verdict = rh.evidence_gate(with_evidence("emitted", "emitted+linted", "emitted+linted"))
    assert code == rh.EXIT_NOT_VERIFIED
    assert code != rh.EXIT_OK and code != rh.EXIT_EVIDENCE_BLOCKED
    assert "NOT VERIFIED" in verdict


def test_a_lint_failed_visual_blocks():
    """Mutation killed: route `lint_failed` to the not-verified code. 1 and 3 are different answers."""
    code, verdict = rh.evidence_gate(with_evidence("lint_failed", "emitted+linted", "emitted+linted"))
    assert code == rh.EXIT_EVIDENCE_BLOCKED
    assert "BLOCKED" in verdict


def test_only_a_fully_linted_workbook_passes():
    code, verdict = rh.evidence_gate(real_workbook())
    assert code == rh.EXIT_OK
    assert "3 of 3" in verdict
    # ...and it still refuses to call that a render check.
    assert "NOT a render check" in verdict


def test_a_lint_failure_outranks_an_unexamined_visual():
    """A workbook with both must report the blocker, not the softer state."""
    code, _ = rh.evidence_gate(with_evidence("lint_failed", "emitted", "emitted+linted"))
    assert code == rh.EXIT_EVIDENCE_BLOCKED


def test_a_pre_2335_bundle_reports_unknown_rather_than_clean():
    """AC3 of #371. Deleting the key is how a real pre-2.335.0 slice looks.

    Mutation killed: default a missing `evidence` to `emitted+linted`, or to `emitted`. The first
    flips the exit code to 0; the second makes the status PRESENT and loses the NOT RECORDED wording.
    """
    wb = with_evidence(None, None, None)
    status, counts = rh.fidelity_evidence_status(wb)
    assert status == rh.FIDELITY_EVIDENCE_MISSING
    assert counts == {rh.EVIDENCE_UNKNOWN: 3}
    code, verdict = rh.evidence_gate(wb)
    assert code == rh.EXIT_NOT_VERIFIED
    assert "NOT VERIFIED" in verdict and "2.335.0" in verdict
    assert "NOT RECORDED" in rh._evidence_summary_line(wb)


def test_a_partially_annotated_bundle_counts_the_missing_rows_as_unknown():
    """The engine annotates all-or-nothing, but a hand-edited or merged slice may not.

    Mutation killed: drop rows with no `evidence` from the distribution. Total would fall to 2 and
    the gate would see a complete set.
    """
    wb = with_evidence("emitted+linted", None, "emitted+linted")
    status, counts = rh.fidelity_evidence_status(wb)
    assert status == rh.FIDELITY_EVIDENCE_PRESENT
    assert counts == {"emitted+linted": 2, rh.EVIDENCE_UNKNOWN: 1}
    assert rh.evidence_gate(wb)[0] == rh.EXIT_NOT_VERIFIED


def test_an_unrecognised_future_value_is_not_verified():
    """Fail-closed on a value this consumer has never heard of.

    Mutation killed: treat "anything that is not lint_failed" as a pass. A future
    `emitted+rendered` would then silently inherit a green from a consumer that cannot judge it.
    """
    wb = with_evidence("emitted+rendered", "emitted+linted", "emitted+linted")
    code, verdict = rh.evidence_gate(wb)
    assert code == rh.EXIT_NOT_VERIFIED
    assert "emitted+rendered 1" in verdict


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, rh.FIDELITY_EVIDENCE_NONE),
        ({"viz_fidelity": []}, rh.FIDELITY_EVIDENCE_NONE),
        ({"viz_fidelity": {}}, rh.FIDELITY_EVIDENCE_INVALID),
        ({"viz_fidelity": "rebuilt"}, rh.FIDELITY_EVIDENCE_INVALID),
    ],
)
def test_absent_or_malformed_viz_fidelity_never_reports_as_verified(payload, expected):
    """Mutation killed: return NONE for a malformed shape, or let either state exit 0."""
    status, _ = rh.fidelity_evidence_status(payload)
    assert status == expected
    assert rh.evidence_gate(payload)[0] == rh.EXIT_NOT_VERIFIED


def test_the_fidelity_view_prints_evidence_beside_status(tmp_path):
    """`status: warned/rebuilt` records that the EMITTER ran; the two must be read together.

    Mutation killed: revert `_fidelity_row` to status-only. The value disappears from the view.
    """
    root = bundle(tmp_path, real_workbook())
    proc = run_cli(str(root), "--fidelity")
    assert proc.returncode == rh.EXIT_OK
    assert proc.stdout.count("emitted+linted") >= 3
    assert "EVIDENCE (engine >= 2.335.0): 3/3 visual(s) examined" in proc.stdout


def test_the_fidelity_view_spells_out_what_emitted_means(tmp_path):
    """A bare count of `emitted` is not legible; the legend is what makes it a finding.

    Mutation killed: drop `_evidence_legend` from `_fidelity_counts`.
    """
    root = bundle(tmp_path, with_evidence("emitted", "emitted", "emitted+linted"))
    proc = run_cli(str(root), "--fidelity")
    assert "NEVER EXAMINED" in proc.stdout
    assert "1/3 visual(s) examined" in proc.stdout


def test_the_gate_is_opt_in(tmp_path):
    """Every existing caller keeps getting 0 on a rendered queue.

    Mutation killed: make the gate unconditional. A reader that starts failing pipelines because it
    learned to have an opinion is a breaking change, not a feature.
    """
    root = bundle(tmp_path, with_evidence("lint_failed", "emitted", "emitted"))
    assert run_cli(str(root)).returncode == rh.EXIT_OK
    assert run_cli(str(root), "--gate-evidence").returncode == rh.EXIT_EVIDENCE_BLOCKED


def test_the_gate_exit_codes_end_to_end(tmp_path):
    """The three states, through the real CLI, judged by exit code and nothing else."""
    cases = [
        (("emitted+linted", "emitted+linted", "emitted+linted"), rh.EXIT_OK),
        (("emitted", "emitted+linted", "emitted+linted"), rh.EXIT_NOT_VERIFIED),
        (("lint_failed", "emitted+linted", "emitted+linted"), rh.EXIT_EVIDENCE_BLOCKED),
        ((None, None, None), rh.EXIT_NOT_VERIFIED),
    ]
    for i, (values, expected) in enumerate(cases):
        root = bundle(tmp_path / f"case{i}", with_evidence(*values))
        assert run_cli(str(root), "--gate-evidence").returncode == expected, values


def test_the_json_form_names_verified_never_leaving_it_to_be_derived(tmp_path):
    """Mutation killed: emit only `counts` and let the consumer decide what counts as verified -
    which is precisely the decision #371 exists to make once, here."""
    out = tmp_path / "out.json"
    root = bundle(tmp_path, with_evidence("emitted", "lint_failed", "emitted+linted"))
    assert run_cli(str(root), "--json", str(out)).returncode == rh.EXIT_OK
    block = json.loads(out.read_text(encoding="utf-8"))["viz_fidelity_evidence"]
    assert block["status"] == rh.FIDELITY_EVIDENCE_PRESENT
    assert block["verified"] == 1
    assert block["never_examined"] == 1
    assert block["lint_failed"] == 1
    assert block["total"] == 3
    assert block["gate_exit_code"] == rh.EXIT_EVIDENCE_BLOCKED


def test_the_estate_list_names_which_workbook_has_unexamined_visuals(tmp_path):
    """ "38 of 40 examined" estate-wide hides that both gaps sit in one report.

    Mutation killed: report only the estate total and drop the per-workbook lines.
    """
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    clean = real_workbook()
    clean["name"] = "Clean Workbook"
    dirty = with_evidence("emitted", "emitted", "emitted+linted")
    dirty["name"] = "Unexamined Workbook"
    for wb in (clean, dirty):
        (root / "handover" / f"{wb['name']}.json").write_text(
            json.dumps({"estate": {}, "workbook": wb}), encoding="utf-8"
        )
    proc = run_cli(str(root), "--list")
    assert proc.returncode == rh.EXIT_OK
    assert "Unexamined Workbook" in proc.stdout
    assert "1/3 examined" in proc.stdout
    # The clean one is NOT listed as a gap - otherwise the list is noise rather than triage.
    assert "Clean Workbook" not in proc.stdout.split("Visual evidence:")[1].split("\n\n")[0]


# =============================================================================================
# #372 - summary.placement.by_axis
# =============================================================================================


def test_by_axis_is_absent_from_the_real_2339_handover():
    """CONTRADICTS the premise that the handover reader can just read `by_axis` off the slice.

    This is the measured finding behind the whole #372 design: the block lives in the fidelity
    oracle's report, not in anything `migrate_estate.py` writes. If a future engine inlines it, this
    test fails and the consumer should be simplified - which is exactly the signal wanted.
    """
    assert "by_axis" not in REAL_SLICE.read_text(encoding="utf-8")
    status, payload = rh.layout_drift_status(real_workbook())
    assert status == rh.LAYOUT_DRIFT_NOT_MEASURED
    assert payload == {}


def test_not_measured_is_reported_as_such_and_not_as_zero_drift():
    """AC2/AC3 of #372. Absence of the block is not absence of drift.

    Mutation killed: return a zeroed `by_axis` when there is none, or stay silent. The first makes
    the line read "pixel-exact"; the second removes it from the default view entirely.
    """
    line = rh._layout_drift_summary_line(real_workbook())
    assert "NOT MEASURED" in line
    assert "pixel-exact" not in line
    assert line != rh._layout_drift_summary_line({}, json.loads(ORACLE_EXACT.read_text(encoding="utf-8")))


def test_measured_no_drift_is_distinguishable_from_never_measured():
    """AC3 of #372, stated as the contrast it actually is."""
    oracle = json.loads(ORACLE_EXACT.read_text(encoding="utf-8"))
    measured, _ = rh.layout_drift_status({}, oracle)
    never, _ = rh.layout_drift_status({}, None)
    assert measured == rh.LAYOUT_DRIFT_EXACT
    assert never == rh.LAYOUT_DRIFT_NOT_MEASURED
    assert measured != never
    assert "MEASURED, pixel-exact" in rh._layout_drift_summary_line({}, oracle)


def test_the_worst_axis_its_sign_and_its_exact_count_are_reported():
    """AC1 of #372, against a `by_axis` block the ENGINE computed.

    The fixture encodes upstream #169's reported shape: X pixel-perfect, Y drifting both ways.

    Mutation killed: rank the worst axis by mean instead of worst, collapse the sign to an absolute
    value, or drop the direction counts. The corpus evidence in #372 is explicit that collapsing the
    sign is what made the previous metric useless.
    """
    oracle = json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8"))
    status, payload = rh.layout_drift_status({}, oracle)
    assert status == rh.LAYOUT_DRIFT_PRESENT
    assert payload["worst"][0] == "y"
    line = rh._layout_drift_summary_line({}, oracle)
    assert "worst Y" in line
    assert "0/3 exact" in line
    assert "+38.93px" in line
    assert "(2 down / 1 up)" in line
    # BOTH axes, always - one axis alone reads as "the other is fine".
    assert "X 3/3 exact" in line


def test_the_sign_survives_a_mirrored_population():
    """Two populations with IDENTICAL magnitudes and opposite directions must not read the same.

    Mutation killed: report `mean_abs_px` in place of `mean_signed_px`. Both lines become equal.
    """
    oracle = json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8"))
    mirrored = json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8"))
    stats = mirrored["summary"]["placement"]["by_axis"]["y"]
    stats["mean_signed_px"] = -stats["mean_signed_px"]
    stats["positive"], stats["negative"] = stats["negative"], stats["positive"]
    assert rh._layout_drift_summary_line({}, oracle) != rh._layout_drift_summary_line({}, mirrored)
    assert "-38.93px" in rh._layout_drift_summary_line({}, mirrored)
    assert "(1 down / 2 up)" in rh._layout_drift_summary_line({}, mirrored)


def test_an_asymmetric_drift_names_the_axis_that_actually_drifted():
    """Transpose the fixture's axes; the verdict must follow the data, not the alphabet.

    Mutation killed: hard-code `y` as the worst axis, which the drifted fixture alone cannot catch.
    """
    swapped = json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8"))
    by_axis = swapped["summary"]["placement"]["by_axis"]
    by_axis["x"], by_axis["y"] = by_axis["y"], by_axis["x"]
    status, payload = rh.layout_drift_status({}, swapped)
    assert status == rh.LAYOUT_DRIFT_PRESENT
    assert payload["worst"][0] == "x"
    line = rh._layout_drift_summary_line({}, swapped)
    assert "worst X" in line
    # X's direction words differ from Y's; using Y's would be a silent mislabel.
    assert "(2 right / 1 left)" in line


def test_a_pre_2332_oracle_report_reports_per_axis_unknown_not_zero():
    """AC2 of #372: a report from before the block existed is UNKNOWN, never "no drift".

    Mutation killed: treat a missing `by_axis` as an empty measurement. The status collapses to
    EXACT and the workbook reads as pixel-perfect on the strength of a field nobody emitted.
    """
    oracle = json.loads(ORACLE_AXIS_BLIND.read_text(encoding="utf-8"))
    assert "by_axis" not in oracle["summary"]["placement"]
    status, payload = rh.layout_drift_status({}, oracle)
    assert status == rh.LAYOUT_DRIFT_AXIS_BLIND
    line = rh._layout_drift_summary_line({}, oracle)
    assert "PER-AXIS UNKNOWN" in line
    # The axis-blind number it DOES have is still surfaced, so the reader is not left with nothing.
    assert "108.0px" in line
    assert payload["placement"]["verdict"] == "drifted"


@pytest.mark.parametrize("by_axis", [[], "x", 5])
def test_a_malformed_by_axis_is_invalid_not_measured(by_axis):
    """Mutation killed: `or {}` a malformed block into an empty one, which reads as no drift."""
    oracle = {"kind": rh.ORACLE_KIND, "summary": {"placement": {"by_axis": by_axis}}}
    status, _ = rh.layout_drift_status({}, oracle)
    assert status == rh.LAYOUT_DRIFT_INVALID
    assert "INVALID SHAPE" in rh._layout_drift_summary_line({}, oracle)


def test_the_oracle_is_found_by_kind_not_by_filename(tmp_path):
    """`fidelity_oracle.py --out` takes an arbitrary path, so there is no filename to key on.

    Mutation killed: match on a filename convention. The report below is called `anything.json`.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED, oracle_name="anything.json")
    proc = run_cli(str(root))
    assert proc.returncode == rh.EXIT_OK
    assert "worst Y" in proc.stdout
    assert "anything.json" in proc.stdout  # provenance is cited


def test_the_cited_path_keeps_its_filename_when_clipped():
    """A path clipped from the RIGHT loses the only part that identifies the report.

    Mutation killed: clip with `_clip` (head-preserving) instead of `_clip_tail`.
    """
    long_path = "C:\\" + ("a" * 200) + "\\oracle-report.json"
    clipped = rh._clip_tail(long_path)
    assert clipped.endswith("oracle-report.json")
    assert len(clipped) <= 88


def test_a_name_matched_oracle_wins_over_a_stranger(tmp_path):
    """Two reports, one named for this workbook: attributing the other one's pixels here would be a
    confident wrong answer, which is worse than no answer.

    Mutation killed: take the first candidate found.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_EXACT, oracle_name=f"{WB_NAME}.json")
    (root / "aaa-other-workbook.json").write_text(ORACLE_DRIFTED.read_text(encoding="utf-8"), encoding="utf-8")
    proc = run_cli(str(root))
    assert "MEASURED, pixel-exact" in proc.stdout
    assert "worst Y" not in proc.stdout


def test_an_ambiguous_set_of_oracle_reports_is_refused(tmp_path):
    """Mutation killed: fall back to "any candidate" when none names the workbook."""
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED, oracle_name="one.json")
    (root / "two.json").write_text(ORACLE_EXACT.read_text(encoding="utf-8"), encoding="utf-8")
    proc = run_cli(str(root))
    assert "NOT MEASURED" in proc.stdout
    assert "measured from" not in proc.stdout


def test_an_explicit_oracle_flag_refuses_a_file_that_is_not_an_oracle_report(tmp_path):
    """Pointing `--oracle` at the handover slice by mistake must fail loudly, not silently report
    NOT MEASURED - the user asserted the file exists, so silence would hide a typo.

    Mutation killed: swallow the mismatch and return `(None, None)`. Exit becomes 0.
    """
    root = bundle(tmp_path, real_workbook())
    proc = run_cli(str(root), "--oracle", str(REAL_SLICE))
    assert proc.returncode == rh.EXIT_USAGE
    assert "not a fidelity-oracle report" in proc.stderr


def test_an_explicit_oracle_flag_beats_auto_discovery(tmp_path):
    root = bundle(tmp_path, real_workbook(), ORACLE_EXACT, oracle_name=f"{WB_NAME}.json")
    proc = run_cli(str(root), "--oracle", str(ORACLE_DRIFTED))
    assert "worst Y" in proc.stdout
    assert "pixel-exact" not in proc.stdout


def test_the_oracle_is_found_in_a_fidelity_subfolder(tmp_path):
    root = bundle(tmp_path, real_workbook())
    (root / "fidelity").mkdir()
    (root / "fidelity" / "report.json").write_text(ORACLE_DRIFTED.read_text(encoding="utf-8"), encoding="utf-8")
    assert "worst Y" in run_cli(str(root)).stdout


def test_the_json_form_carries_the_drift_status_even_when_unmeasured(tmp_path):
    """A consumer that never reads the numbers still cannot mistake absence for a clean measurement.

    Mutation killed: omit `layout_drift` when there is no oracle, or set `measured: true` regardless.
    """
    out = tmp_path / "out.json"
    root = bundle(tmp_path, real_workbook())
    assert run_cli(str(root), "--json", str(out)).returncode == rh.EXIT_OK
    block = json.loads(out.read_text(encoding="utf-8"))["layout_drift"]
    assert block["status"] == rh.LAYOUT_DRIFT_NOT_MEASURED
    assert block["measured"] is False
    assert block["by_axis"] is None
    assert block["worst_axis"] is None


def test_the_json_form_carries_the_full_by_axis_block_when_measured(tmp_path):
    out = tmp_path / "out.json"
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED)
    assert run_cli(str(root), "--json", str(out)).returncode == rh.EXIT_OK
    block = json.loads(out.read_text(encoding="utf-8"))["layout_drift"]
    assert block["measured"] is True
    assert block["worst_axis"] == "y"
    assert block["worst_axis_stats"]["mean_signed_px"] == 38.93
    assert set(block["by_axis"]) == {"x", "y"}
    assert block["measured_from"].endswith(".json")


def test_the_provenance_line_is_inside_the_byte_cap(tmp_path):
    """REGRESSION, and a defect this file's own docstring calls out: `--max-bytes` is a STRICT cap
    on everything a run prints. The first version printed `measured from:` AFTER `_capped()`, so a
    1500-byte cap emitted 1632 bytes with no truncation banner and reported success.

    Mutation killed: move the provenance line back out of the budgeted body.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED)
    for cap in (rh.MIN_MAX_BYTES, 2000, 20_000):
        proc = run_cli(str(root), "--max-bytes", str(cap))
        assert proc.returncode == rh.EXIT_OK, cap
        assert len(proc.stdout.encode("utf-8")) <= cap, cap
        assert cites_oracle(proc.stdout), cap


def test_both_citation_forms_name_the_report_and_stay_bounded():
    """The two forms, pinned directly so the CLI tests above can stay form-agnostic.

    Mutation killed: emit the citation only in the full form. The terse pass would then silently
    strip provenance from every number it prints, exactly when the reader can least afford it.
    """
    oracle = rh.OracleSource(
        json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8")),
        Path("C:/" + "d" * 120) / (("z" * 90) + ".json"),
    )
    full = rh._layout_drift_lines({}, oracle, terse=False)
    terse = rh._layout_drift_lines({}, oracle, terse=True)
    assert len(full) == 2 and "measured from:" in full[1]
    assert full[1].rstrip().endswith(".json")  # clipped from the LEFT, filename intact
    assert len(terse) == 1 and terse[0].endswith("]")
    assert sum(len(x) for x in terse) < sum(len(x) for x in full)
    # Bounded whatever the path: a fallback that can itself overrun the cap is not a fallback.
    assert len(terse[0]) <= 100
    assert "z" * 90 not in terse[0]


def test_an_inlined_placement_block_would_be_preferred_over_the_oracle():
    """Forward compatibility, asserted rather than merely commented: if `migrate_estate.py` ever
    absorbs the rollup, the handover wins and the separate report stops being needed."""
    wb = real_workbook()
    wb["viz_placement"] = json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8"))["summary"]["placement"]
    status, payload = rh.layout_drift_status(wb, json.loads(ORACLE_EXACT.read_text(encoding="utf-8")))
    assert status == rh.LAYOUT_DRIFT_PRESENT
    assert payload["worst"][0] == "y"


# =============================================================================================
# Budget - the two new lines must not break the cap this module treats as a defect
# =============================================================================================


def test_the_new_lines_do_not_fire_the_hard_cap_at_the_minimum(tmp_path):
    """REGRESSION. The first version of these lines added ~185 bytes to `render_default`'s fixed
    tail and fired `HARD CAP` at `--max-bytes 1500` - which this module's own docstring calls a
    budgeting DEFECT rather than a pass.

    Mutation killed: remove the terse second pass from `render_default`.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED)
    for cap in (rh.MIN_MAX_BYTES, 1600, 2000, 20_000):
        proc = run_cli(str(root), "--max-bytes", str(cap))
        assert proc.returncode == rh.EXIT_OK, cap
        assert "HARD CAP" not in proc.stdout, cap
        assert len(proc.stdout.encode("utf-8")) <= cap, cap


def test_the_terse_form_compresses_but_never_drops_either_signal(tmp_path):
    """A budget too tight for the prose is still wide enough for the verdict AND its citation.

    Mutation killed: drop the lines (or the provenance) instead of shortening them when the cap is
    tight. Absence rendering as silence is the exact failure both issues are about.
    """
    wb = with_evidence("emitted", "emitted", "emitted+linted")
    root = bundle(tmp_path, wb, ORACLE_DRIFTED)
    proc = run_cli(str(root), "--max-bytes", str(rh.MIN_MAX_BYTES))
    assert proc.returncode == rh.EXIT_OK
    assert "HARD CAP" not in proc.stdout
    assert "evidence: 1/3 examined" in proc.stdout
    assert "worst Y" in proc.stdout
    assert "+38.93px" in proc.stdout
    assert cites_oracle(proc.stdout)  # the citation survives compression


def test_the_terse_form_stays_bounded_however_long_the_oracle_path_is(tmp_path):
    """Mutation killed: interpolate the raw filename into the terse line. A long report name would
    then push the compressed fallback back over the cap - a fallback that can overrun is not one.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED, oracle_name=("z" * 90) + ".json")
    proc = run_cli(str(root), "--max-bytes", str(rh.MIN_MAX_BYTES))
    assert proc.returncode == rh.EXIT_OK
    assert "HARD CAP" not in proc.stdout
    assert len(proc.stdout.encode("utf-8")) <= rh.MIN_MAX_BYTES
    assert "z" * 90 not in proc.stdout
