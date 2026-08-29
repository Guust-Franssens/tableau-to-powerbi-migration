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
ORACLE_EDGE_DRIFT = FIXTURES / "oracle-2.339.0-edge-size-drift.json"

WB_NAME = "Meridian Revenue by Region"

# A minimal axis record that IS assessable, used as the baseline the malformed cases mutate one
# field of. Taken from the shape the engine's `_axis_rollup` actually returns.
_AXIS_OK = {
    "evaluated": 2,
    "exact": 2,
    "median_abs_px": 0.0,
    "mean_abs_px": 0.0,
    "worst_abs_px": 0.0,
    "mean_signed_px": 0.0,
    "positive": 0,
    "negative": 0,
}


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


def _blen(text: str) -> int:
    """Byte length, the unit `--max-bytes` is actually enforced in."""
    return len(text.encode("utf-8"))


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
    assert "NOT ASSESSABLE" in rh._layout_drift_summary_line({}, oracle)


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
    assert sum(_blen(x) for x in terse) < sum(_blen(x) for x in full)
    # Bounded whatever the path: a fallback that can itself overrun the cap is not a fallback.
    # Measured in BYTES, because that is the unit `--max-bytes` is enforced in.
    assert _blen(terse[0]) <= rh.TERSE_DRIFT_LINE_MAX_BYTES
    assert _blen(full[1]) <= 88 + len("           measured from: ")
    assert "z" * 90 not in terse[0]


def test_the_terse_line_is_bounded_even_when_the_VERDICT_alone_overflows():
    """The bound must hold for ANY input, not just well-behaved data.

    The citation-side clip cannot help here: an extra axis with a long name (a future engine could
    add one) or an extreme pixel value pushes the VERDICT itself past the ceiling before any
    provenance is appended. A bound enforced only on the citation is a bound that holds for the
    inputs you thought of.

    Mutation killed: remove the final `_clip_head` clamp from `_layout_drift_lines`.
    """
    long_axis = "diagonal_" + ("v" * 40)
    axes = {
        "x": _AXIS_OK,
        "y": _AXIS_OK,
        long_axis: {**_AXIS_OK, "exact": 0, "worst_abs_px": 12345678901234.5, "mean_signed_px": -98765432109876.5},
    }
    oracle = {"kind": rh.ORACLE_KIND, "summary": {"placement": {"verdict": "drifted", "by_axis": axes}}}
    verdict = rh._layout_drift_summary_line({}, oracle, terse=True)
    assert _blen(verdict) > rh.TERSE_DRIFT_LINE_MAX_BYTES, "fixture too tame to exercise the clamp"

    for path in (None, Path("report.json")):
        line = rh._layout_drift_lines({}, rh.OracleSource(oracle, path), terse=True)
        assert len(line) == 1
        assert _blen(line[0]) <= rh.TERSE_DRIFT_LINE_MAX_BYTES, path
        assert "drift" in line[0], "the verdict's meaning lives at the FRONT and must survive"
        assert "\ufffd" not in line[0]


def test_the_terse_citation_is_dropped_only_when_there_is_no_room_for_one():
    """Ordering of sacrifice, stated as a test: the VERDICT is the signal and the citation is its
    provenance, so when the ceiling cannot hold both, the citation goes - never the verdict.

    ⚠️ The fixture is TUNED to land in the guard's observable window (0 <= room < 4) and ASSERTS
    that it did. The first version used a fixture with plenty of room, so the branch it names never
    ran and the mutation escaped: a conditional assertion inside an untaken branch is a test that
    cannot fail, which is the thing this whole review pass is about.

    Mutation killed: append a citation regardless of whether there is room for a meaningful one.
    Without the guard a 3-byte allowance yields `[eport.json]` - a citation that looks like a
    filename and names a file which does not exist. A wrong citation is worse than none.
    """
    axis = "y" + "Q" * 30  # tuned so the terse verdict is 66 bytes -> room == 3
    axes = {
        "x": _AXIS_OK,
        "y": _AXIS_OK,
        axis: {**_AXIS_OK, "exact": 0, "worst_abs_px": 1.0, "mean_signed_px": -1.0},
    }
    oracle = {"kind": rh.ORACLE_KIND, "summary": {"placement": {"verdict": "drifted", "by_axis": axes}}}
    verdict = rh._layout_drift_summary_line({}, oracle, terse=True)
    room = rh.TERSE_DRIFT_LINE_MAX_BYTES - _blen(verdict) - len(" []")
    assert 0 <= room < 4, f"fixture must land in the guard's window; room={room}"

    line = rh._layout_drift_lines({}, rh.OracleSource(oracle, Path("report.json")), terse=True)[0]
    assert line == verdict, "with no room for a real citation, the verdict must survive UNTOUCHED"
    assert _blen(line) <= rh.TERSE_DRIFT_LINE_MAX_BYTES
    assert "[" not in line, "a truncated citation naming a nonexistent file is worse than none"


def test_a_citation_is_still_emitted_whenever_there_IS_room():
    """The other side of the same guard, so "drop it" cannot quietly become the default.

    Mutation killed: raise the room threshold until the citation is never emitted at all.
    """
    oracle = rh.OracleSource(json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8")), Path("report.json"))
    line = rh._layout_drift_lines({}, oracle, terse=True)[0]
    assert line.endswith("]") and ".json]" in line

    """Forward compatibility, asserted rather than merely commented: if `migrate_estate.py` ever
    absorbs the rollup, the handover wins and the separate report stops being needed."""
    wb = real_workbook()
    wb["viz_placement"] = json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8"))["summary"]["placement"]
    status, payload = rh.layout_drift_status(wb, json.loads(ORACLE_EXACT.read_text(encoding="utf-8")))
    assert status == rh.LAYOUT_DRIFT_PRESENT
    assert payload["worst"][0] == "y"


# =============================================================================================
# Review pass: malformed or unassessable input must never collapse into the CLEAN bucket
#
# One root cause wearing five faces. It is the same failure #371 exists to prevent - "an unexamined
# visual is indistinguishable from a verified one" - reappearing at the level of this module's own
# parsing. Wherever the reader cannot assess something, that is its own state, never a pass.
# =============================================================================================


def test_an_unreadable_row_stays_in_the_denominator():
    """HIGH 1. Filtering non-object rows out before counting made the denominator describe the rows
    the parser could read, not the visuals the engine emitted.

    Measured on the first version: ``[{"evidence": "emitted+linted"}, 42]`` reported
    *"1 of 1 visual(s) emitted+linted - structural coverage complete"* and exited **0**. Two
    visuals in, one out, and the one nobody could read vanished into a clean bill of health.

    Mutation killed: drop non-dict rows before counting, or bucket them as ``emitted+linted``.
    """
    wb = {"viz_fidelity": [{"evidence": "emitted+linted"}, 42]}
    status, counts = rh.fidelity_evidence_status(wb)
    assert status == rh.FIDELITY_EVIDENCE_PRESENT
    assert sum(counts.values()) == 2, "the denominator must be every ROW, not every readable row"
    assert counts[rh.EVIDENCE_UNREADABLE] == 1
    code, verdict = rh.evidence_gate(wb)
    assert code == rh.EXIT_NOT_VERIFIED
    assert "1 of 2" in verdict
    assert "1 of 1" not in verdict, "a 2-row input can never report a 1-row denominator"


@pytest.mark.parametrize("junk", [42, "rebuilt", None, [], ["nested"]])
def test_no_shape_of_unreadable_row_can_produce_a_pass(junk):
    """The same hole probed across every JSON type a row could arrive as.

    Mutation killed: special-case one type (e.g. skip ``None``) and let it fall out of the count.
    """
    wb = {"viz_fidelity": [{"evidence": "emitted+linted"}, junk]}
    _, counts = rh.fidelity_evidence_status(wb)
    assert sum(counts.values()) == 2
    assert rh.evidence_gate(wb)[0] == rh.EXIT_NOT_VERIFIED


def test_an_all_unreadable_workbook_is_not_a_pass():
    wb = {"viz_fidelity": [1, 2, 3]}
    status, counts = rh.fidelity_evidence_status(wb)
    assert status == rh.FIDELITY_EVIDENCE_PRESENT
    assert counts == {rh.EVIDENCE_UNREADABLE: 3}
    assert rh.evidence_gate(wb)[0] == rh.EXIT_NOT_VERIFIED


def test_a_mixed_estate_never_claims_coverage_it_did_not_assess(tmp_path):
    """HIGH 1, estate half. A workbook with no ``viz_fidelity`` (or an unreadable one) contributes
    nothing to the totals, so it was skipped silently - while still appearing in the ordinary
    workbook list below. The estate then read "1/1 examined": complete coverage, next to two
    workbooks nobody assessed at all.

    Mutation killed: `continue` past NONE/INVALID workbooks without naming them.
    """
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    payloads = {
        "Clean": {"viz_fidelity": [{"evidence": "emitted+linted", "worksheet": "w"}]},
        "Unreadable": {"viz_fidelity": {}},
        "NoRows": {},
    }
    for name, extra in payloads.items():
        wb = {"name": name, **extra}
        (root / "handover" / f"{name}.json").write_text(json.dumps({"estate": {}, "workbook": wb}), encoding="utf-8")
    proc = run_cli(str(root), "--list")
    assert proc.returncode == rh.EXIT_OK
    assert "NOT ASSESSABLE" in proc.stdout
    assert "2 workbook(s) NOT ASSESSABLE" in proc.stdout
    for name in ("Unreadable", "NoRows"):
        assert f"?? {name}" in proc.stdout, f"{name} must be NAMED, not merely counted"
    assert "NOT in the total above" in proc.stdout, "the total must disclaim what it excludes"


def test_the_engine_verdict_overrules_a_by_axis_that_looks_exact():
    """HIGH 2. ``by_axis`` measures ``left``/``top`` ONLY; the rollup around it also weighs the far
    edges and SIZE. A visual whose origin is perfect but whose size is wrong is therefore invisible
    to the axis records - and that is the exact customer shape in #372 (visuals compressed to
    ~34-48% of intended height, origins in the right place).

    The fixture is the ENGINE's own ``_placement_rollup`` output: both axes report ``2/2 exact`` at
    ``0.0px``, while the enclosing verdict is ``drifted`` with ``worst_max_edge_px: 240.0``. Reading
    only ``by_axis`` reported ``pixel_exact`` with ``measured: true``.

    Mutation killed: decide exactness from the axis records alone.
    """
    oracle = json.loads(ORACLE_EDGE_DRIFT.read_text(encoding="utf-8"))
    placement = oracle["summary"]["placement"]
    # Pin the trap in the fixture itself, so a regenerated fixture that lost it fails loudly.
    assert placement["verdict"] == "drifted"
    assert all(a["exact"] == a["evaluated"] for a in placement["by_axis"].values())
    assert all(a["worst_abs_px"] == 0.0 for a in placement["by_axis"].values())

    status, _ = rh.layout_drift_status({}, oracle)
    assert status == rh.LAYOUT_DRIFT_EDGE_ONLY
    assert status != rh.LAYOUT_DRIFT_EXACT
    line = rh._layout_drift_summary_line({}, oracle)
    assert "EDGE or SIZE drift" in line
    assert "240.0px" in line
    assert "!!" in line, "a size defect the axes cannot see must be loud, not quiet"


def test_the_edge_only_state_is_distinct_in_json():
    """A machine consumer must be able to tell the three measured states apart without prose."""
    edge = rh._layout_drift_json({}, rh.OracleSource(json.loads(ORACLE_EDGE_DRIFT.read_text(encoding="utf-8"))))
    exact = rh._layout_drift_json({}, rh.OracleSource(json.loads(ORACLE_EXACT.read_text(encoding="utf-8"))))
    assert edge["status"] != exact["status"]
    assert edge["measured"] is True and exact["measured"] is True
    assert edge["origins_pixel_exact"] is False, "origins are exact but the VISUAL is not"
    assert exact["origins_pixel_exact"] is True
    assert edge["edge_or_size_drift"] is True and exact["edge_or_size_drift"] is False
    assert edge["enclosing_verdict"] == "drifted"


@pytest.mark.parametrize(
    "by_axis, why",
    [
        ({"x": {}, "y": {}}, "empty records: _num(missing) == _num(missing) is None == None -> True"),
        ({"x": _AXIS_OK, "y": "bad"}, "one axis unreadable; reading only the other says 'fine'"),
        ({"x": _AXIS_OK}, "a single axis is a malformed rollup - the engine emits both or neither"),
        ({"y": _AXIS_OK}, "same, mirrored"),
        ({"x": _AXIS_OK, "y": {**_AXIS_OK, "evaluated": float("nan")}}, "non-finite count"),
        ({"x": _AXIS_OK, "y": {**_AXIS_OK, "evaluated": float("inf")}}, "infinite count"),
        ({"x": _AXIS_OK, "y": {**_AXIS_OK, "exact": 5}}, "exact exceeds evaluated"),
        ({"x": _AXIS_OK, "y": {**_AXIS_OK, "evaluated": -1, "exact": -1}}, "negative counts"),
        ({"x": _AXIS_OK, "y": {**_AXIS_OK, "worst_abs_px": -3.0}}, "negative magnitude"),
        ({"x": _AXIS_OK, "y": {**_AXIS_OK, "exact": True, "evaluated": True}}, "bools posing as counts"),
        ({"x": _AXIS_OK, "y": {**_AXIS_OK, "mean_signed_px": "0"}}, "a string posing as a number"),
    ],
)
def test_an_unassessable_axis_record_is_never_exact(by_axis, why):
    """HIGH 2, second half. ``None == None`` is ``True``, so a MISSING count satisfied
    ``exact == evaluated`` and reported ``pixel_exact`` with ``measured: true``.

    Mutation killed: compare counts without validating them first; or accept one axis.
    """
    oracle = {"kind": rh.ORACLE_KIND, "summary": {"placement": {"verdict": "pixel-exact", "by_axis": by_axis}}}
    status, _ = rh.layout_drift_status({}, oracle)
    assert status == rh.LAYOUT_DRIFT_INVALID, why
    assert status != rh.LAYOUT_DRIFT_EXACT
    assert rh._layout_drift_json({}, rh.OracleSource(oracle))["measured"] is False
    assert "NOT ASSESSABLE" in rh._layout_drift_summary_line({}, oracle)


def test_valid_axes_with_no_enclosing_verdict_are_not_called_exact():
    """Origins exact, but nothing corroborates the edges or the size: that is unassessable, not a
    pass. A rollup without a verdict is not one the engine wrote.

    Mutation killed: default a missing enclosing verdict to pixel-exact.
    """
    oracle = {"kind": rh.ORACLE_KIND, "summary": {"placement": {"by_axis": {"x": _AXIS_OK, "y": _AXIS_OK}}}}
    assert rh.layout_drift_status({}, oracle)[0] == rh.LAYOUT_DRIFT_INVALID


def test_a_lone_oracle_is_not_attributed_across_a_multi_workbook_bundle(tmp_path):
    """HIGH 3. The singleton fallback accepted an arbitrarily-named report unconditionally, so in a
    bundle where the oracle ran for A only, B was handed A's measurements - and a pixel-exact A made
    an entirely unmeasured B read as verified.

    Mutation killed: accept a lone unmatched candidate regardless of how many workbooks exist.
    """
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    for name in ("Workbook A", "Workbook B"):
        (root / "handover" / f"{name}.json").write_text(
            json.dumps({"estate": {}, "workbook": {"name": name, "viz_fidelity": []}}), encoding="utf-8"
        )
    (root / "measured-run.json").write_text(ORACLE_EXACT.read_text(encoding="utf-8"), encoding="utf-8")

    proc = run_cli(str(root), "--workbook", "Workbook B")
    assert proc.returncode == rh.EXIT_OK
    assert "NOT MEASURED" in proc.stdout
    assert "pixel-exact" not in proc.stdout, "another workbook's measurement must not land here"
    assert not cites_oracle(proc.stdout)


def test_a_single_workbook_target_still_accepts_an_arbitrarily_named_report(tmp_path):
    """The narrowing must not become a refusal to work: with one workbook there is nothing to
    confuse it with, which is the case the singleton fallback legitimately serves.

    Mutation killed: refuse the singleton unconditionally.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED, oracle_name="measured-run.json")
    proc = run_cli(str(root))
    assert "worst Y" in proc.stdout
    assert cites_oracle(proc.stdout)


def test_a_name_matched_report_still_wins_in_a_multi_workbook_bundle(tmp_path):
    """...and the workbook that WAS measured must still get its numbers."""
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    for name in ("Workbook A", "Workbook B"):
        (root / "handover" / f"{name}.json").write_text(
            json.dumps({"estate": {}, "workbook": {"name": name, "viz_fidelity": []}}), encoding="utf-8"
        )
    (root / "Workbook A.json").write_text(ORACLE_DRIFTED.read_text(encoding="utf-8"), encoding="utf-8")
    assert "worst Y" in run_cli(str(root), "--workbook", "Workbook A").stdout
    assert "NOT MEASURED" in run_cli(str(root), "--workbook", "Workbook B").stdout


def test_a_corrupt_slice_does_not_shrink_the_census_into_the_singleton_path(tmp_path):
    """HIGH (round 2). The singleton-oracle guard was gated on the number of PARSED workbooks, and
    `load_workbooks` silently skips a slice it cannot read - so a two-slice bundle with ONE corrupt
    slice counted 1, satisfied the guard, and handed the readable workbook the other one's
    measurements.

    Measured before the fix: ``HANDOVER_SLICES=2  LOADED=1`` ->
    ``drift: MEASURED, pixel-exact [measured-run.json]``, exit 0.

    This is the round-1 HIGH 1 defect recurring one level up: a parse failure silently shrinks a
    denominator and unlocks a permissive path.

    Mutation killed: gate on ``len(found)`` (or on ``census.workbooks``) instead of the census.
    """
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    (root / "handover" / "Workbook A.json").write_text(
        json.dumps({"estate": {}, "workbook": {"name": "Workbook A", "viz_fidelity": []}}), encoding="utf-8"
    )
    (root / "handover" / "Workbook B.json").write_text("{ this is not valid json", encoding="utf-8")
    (root / "measured-run.json").write_text(ORACLE_EXACT.read_text(encoding="utf-8"), encoding="utf-8")

    census = rh.load_workbook_census(root)
    assert census.declared == 2, "the CORRUPT slice must still be declared"
    assert len(census.workbooks) == 1
    assert census.unreadable == 1
    assert census.provably_single is False

    proc = run_cli(str(root))
    assert proc.returncode == rh.EXIT_OK
    assert "NOT MEASURED" in proc.stdout
    assert "pixel-exact" not in proc.stdout, "another workbook's measurement must not land here"
    assert not cites_oracle(proc.stdout)


def test_a_malformed_estate_entry_does_not_shrink_the_census(tmp_path):
    """The same undercount through the other input shape: `report.json`'s `workbooks[]` with a
    non-object entry. The DECLARED list is the census; parse results reconcile against it.

    Mutation killed: count only the entries that survive the `isinstance(wb, dict)` filter.
    """
    root = tmp_path / "bundle"
    root.mkdir(parents=True)
    (root / "report.json").write_text(
        json.dumps({"workbooks": [{"name": "Workbook A", "viz_fidelity": []}, 42]}), encoding="utf-8"
    )
    (root / "measured-run.json").write_text(ORACLE_EXACT.read_text(encoding="utf-8"), encoding="utf-8")

    census = rh.load_workbook_census(root)
    assert (census.declared, len(census.workbooks), census.unreadable) == (2, 1, 1)
    assert census.provably_single is False

    proc = run_cli(str(root))
    assert "NOT MEASURED" in proc.stdout
    assert "pixel-exact" not in proc.stdout


def test_a_genuine_single_workbook_target_is_not_over_refused(tmp_path):
    """The narrowing must not swallow the case the fallback legitimately serves.

    Mutation killed: refuse the singleton whenever ANY json in the bundle fails to yield a workbook
    - which would over-refuse on every bundle that has an oracle report or a report.json beside the
    slices.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED, oracle_name="measured-run.json")
    census = rh.load_workbook_census(root)
    assert census.provably_single is True
    proc = run_cli(str(root))
    assert "worst Y" in proc.stdout
    assert cites_oracle(proc.stdout)


def test_a_stray_non_workbook_json_beside_the_slices_is_not_unreadable(tmp_path):
    """A file that was never a workbook is neither declared nor unreadable.

    The oracle report itself commonly sits next to the slices, and counting it as a failed workbook
    would disable the fallback on exactly the bundles that have something to attribute.

    Mutation killed: treat every non-workbook JSON as an unreadable workbook.
    """
    root = bundle(tmp_path, real_workbook())
    (root / "handover" / "an-oracle-report.json").write_text(
        ORACLE_DRIFTED.read_text(encoding="utf-8"), encoding="utf-8"
    )
    census = rh.load_workbook_census(root)
    assert (census.declared, census.unreadable) == (1, 0)
    assert census.provably_single is True
    assert "worst Y" in run_cli(str(root)).stdout


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"workbook": {"name": "A"}}, (1, 1, 0)),
        ({"workbooks": [{"name": "A"}, {"name": "B"}]}, (2, 2, 0)),
        ({"workbooks": [{"name": "A"}, 42, None]}, (3, 1, 2)),
        ({"workbooks": []}, (0, 0, 0)),
        ({"workbook": 42}, (1, 0, 1)),
        ({"workbooks": "nope"}, (1, 0, 1)),
        ({"kind": "tableau-fabric-structural-fidelity"}, (0, 0, 0)),
        ({}, (0, 0, 0)),
        ([1, 2], (0, 0, 0)),
    ],
)
def test_the_census_classifies_every_payload_shape(payload, expected):
    """Declared / parsed / unreadable, enumerated. A payload that DECLARES a workbook key it cannot
    honour is unreadable; one that never claimed to be a workbook is a stray.

    Mutation killed: collapse "declared but unreadable" into "stray", which is precisely the
    undercount.
    """
    workbooks, declared, unreadable = rh._census_from_payload(payload, Path("x.json"))
    assert (declared, len(workbooks), unreadable) == expected


def test_slice_failures_survive_the_fallthrough_to_report_json(tmp_path):
    """When NO slice parses, the reader falls through to `report.json` - and must not forget that
    slices existed and could not be read.

    Mutation killed: build the report.json census fresh, discarding the pending slice failures.
    """
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    (root / "handover" / "broken.json").write_text("{{{", encoding="utf-8")
    (root / "report.json").write_text(
        json.dumps({"workbooks": [{"name": "Only One", "viz_fidelity": []}]}), encoding="utf-8"
    )
    (root / "measured-run.json").write_text(ORACLE_EXACT.read_text(encoding="utf-8"), encoding="utf-8")

    census = rh.load_workbook_census(root)
    assert census.unreadable == 1, "the unreadable slice must survive the fallthrough"
    assert census.provably_single is False
    assert "NOT MEASURED" in run_cli(str(root)).stdout


def test_an_unreadable_input_is_announced_on_stderr(tmp_path):
    """Silently skipping a corrupt slice is the same failure as silently skipping an unreadable
    row: the reader looks complete because what it could not read left no trace.

    stderr, so the warning is outside the `--max-bytes` budget and cannot be truncated away.

    Mutation killed: drop the warning, or emit it on stdout where the cap can cut it.
    """
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    (root / "handover" / "Good.json").write_text(
        json.dumps({"estate": {}, "workbook": {"name": "Good", "viz_fidelity": []}}), encoding="utf-8"
    )
    (root / "handover" / "Broken.json").write_text("nope", encoding="utf-8")
    proc = run_cli(str(root), "--workbook", "Good", "--max-bytes", str(rh.MIN_MAX_BYTES))
    assert proc.returncode == rh.EXIT_OK
    assert "could not be read" in proc.stderr
    assert "Broken.json" in proc.stderr
    assert "1 of 2" in proc.stderr
    assert len(proc.stdout.encode("utf-8")) <= rh.MIN_MAX_BYTES


def test_load_workbooks_still_returns_only_the_list(tmp_path):
    """Backwards compatibility: the census is additive, not a breaking change to the loader."""
    root = bundle(tmp_path, real_workbook())
    found = rh.load_workbooks(root)
    assert isinstance(found, list) and len(found) == 1
    assert found[0][0] == WB_NAME
    assert found == rh.load_workbook_census(root).workbooks


def test_find_oracle_report_refuses_an_unmatched_singleton_without_a_census(tmp_path):
    """`census=None` means provenance unknown, and unknown is not permission.

    Mutation killed: default the census to a permissive "single workbook" when the caller omits it.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED, oracle_name="measured-run.json")
    source = root / "handover" / f"{WB_NAME}.json"
    assert rh.find_oracle_report(root, source, WB_NAME).payload is None
    assert rh.find_oracle_report(root, source, WB_NAME, None, rh.load_workbook_census(root)).payload is not None


@pytest.mark.parametrize(
    "declared, workbooks, unreadable, expected, why",
    [
        (1, 1, 0, True, "one declared, one parsed, nothing lost"),
        (2, 1, 1, False, "the classic: a corrupt slice shrank the parsed count"),
        (1, 1, 1, False, "unreadable is never compatible with a proof"),
        (0, 1, 0, False, "a workbook nothing declared is not a provable census"),
        (2, 2, 0, False, "two workbooks"),
        (0, 0, 0, False, "nothing at all"),
        # The clause that today looks redundant, pinned as an INDEPENDENT requirement. It is the
        # only one that refuses a census where the declared list is larger than the parsed list
        # WITHOUT anything being marked unreadable - the shape a future loader would produce the
        # moment it deduplicates (two slices naming the same workbook -> declared 2, parsed 1,
        # unreadable 0). `provably_single` must not become true by arithmetic coincidence.
        (2, 1, 0, False, "declared > parsed with nothing flagged unreadable is NOT a proof"),
    ],
)
def test_provably_single_requires_all_three_conditions(declared, workbooks, unreadable, expected, why):
    """Mutation killed: drop any one of `declared == 1`, `unreadable == 0`, `len(workbooks) == 1`.

    Each is asserted against a census that isolates it, so no clause survives on the strength of
    another one happening to imply it on today's inputs.
    """
    census = rh.WorkbookCensus([(f"W{i}", {}, Path(f"w{i}.json")) for i in range(workbooks)], declared, unreadable, ())
    assert census.provably_single is expected, why


def test_the_loader_keeps_declared_equal_to_parsed_plus_unreadable(tmp_path):
    """The invariant that makes `declared == 1` look redundant, asserted rather than assumed - so a
    future loader path that breaks it is caught here instead of silently widening the guard."""
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    (root / "handover" / "Good.json").write_text(
        json.dumps({"estate": {}, "workbook": {"name": "Good"}}), encoding="utf-8"
    )
    (root / "handover" / "Broken.json").write_text("{{{", encoding="utf-8")
    (root / "handover" / "Stray.json").write_text(json.dumps({"kind": "something-else"}), encoding="utf-8")
    census = rh.load_workbook_census(root)
    assert census.declared == len(census.workbooks) + census.unreadable
    assert (census.declared, len(census.workbooks), census.unreadable) == (2, 1, 1)


def test_the_estate_evidence_section_is_budgeted(tmp_path):
    """MEDIUM 4. One unconditional line per workbook is unbounded in the estate's width. At
    ``MIN_MAX_BYTES`` a 30-workbook estate fired ``HARD CAP``, cut 18 of the 30 signals with no
    omitted-count line, and exited 0 - a dropped signal under budget pressure is a silent
    false-clean, which is the failure this section exists to prevent.

    Mutation killed: remove the budget/reserve and emit every workbook line unconditionally.
    """
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    for i in range(30):
        wb = {"name": f"Workbook-{i:02d}", "viz_fidelity": [{"evidence": "emitted", "worksheet": "w"}]}
        (root / "handover" / f"{wb['name']}.json").write_text(
            json.dumps({"estate": {}, "workbook": wb}), encoding="utf-8"
        )
    proc = run_cli(str(root), "--list", "--max-bytes", str(rh.MIN_MAX_BYTES))
    assert proc.returncode == rh.EXIT_OK
    assert "HARD CAP" not in proc.stdout
    assert len(proc.stdout.encode("utf-8")) <= rh.MIN_MAX_BYTES
    assert "Visual evidence: 0/30" in proc.stdout, "the estate total must survive any cap"
    assert "not named here" in proc.stdout, "everything cut must be COUNTED"


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

    ⚠️ ASCII ONLY here, on purpose, and it is NOT sufficient by itself: where one character is one
    byte, a character-counting clip looks byte-bounded. The multibyte case below is the one that
    can actually fail, and it did.
    """
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED, oracle_name=("z" * 90) + ".json")
    proc = run_cli(str(root), "--max-bytes", str(rh.MIN_MAX_BYTES))
    assert proc.returncode == rh.EXIT_OK
    assert "HARD CAP" not in proc.stdout
    assert len(proc.stdout.encode("utf-8")) <= rh.MIN_MAX_BYTES
    assert "z" * 90 not in proc.stdout


@pytest.mark.parametrize(
    "glyph, width",
    [("\u732b", 3), ("\U0001f4c8", 4)],  # CJK ideograph (3 bytes), emoji (4 bytes)
)
def test_the_terse_citation_is_bounded_in_BYTES_not_characters(tmp_path, glyph, width):
    """`--max-bytes` counts BYTES; the first version of the terse citation clipped CHARACTERS.

    Measured on the shipped code before this fix: a 90-glyph CJK filename produced a 69-character
    terse line that was 101 BYTES - a 46% overrun of a bound the code believed it was enforcing -
    and pushed the whole render past the cap. The ASCII test above could not see it because there
    character count and byte count coincide.

    Mutation killed: clip the citation with a character-counting helper.

    The glyph count is derived from the byte width rather than fixed, because this test writes a
    REAL file and Linux caps a filename component at 255 BYTES: 90 emoji is 360 and raised
    `OSError [Errno 36]` on CI while passing on Windows, where the cap counts UTF-16 units. The
    original 90 was itself a character count applied to a byte limit - the exact confusion this
    test exists to catch. 240 bytes still overruns `TERSE_DRIFT_LINE_MAX_BYTES` (72) by 3.3x and
    is far past the ~24-character clip window, so the property is unchanged.
    """
    glyphs = 240 // width
    name = (glyph * glyphs) + ".json"
    root = bundle(tmp_path, real_workbook(), ORACLE_DRIFTED, oracle_name=name)
    proc = run_cli(str(root), "--max-bytes", str(rh.MIN_MAX_BYTES))
    assert proc.returncode == rh.EXIT_OK
    assert "HARD CAP" not in proc.stdout
    assert len(proc.stdout.encode("utf-8")) <= rh.MIN_MAX_BYTES
    # The unit the bound is expressed in, asserted directly rather than via the render.
    oracle = rh.OracleSource(json.loads(ORACLE_DRIFTED.read_text(encoding="utf-8")), Path(name))
    terse = rh._layout_drift_lines({}, oracle, terse=True)
    assert len(terse) == 1
    assert _blen(terse[0]) <= rh.TERSE_DRIFT_LINE_MAX_BYTES
    # BOTH halves are required, and asserting only the first is what let a mutation escape: a
    # character-counting clip produces an over-long candidate, the byte guard then discards the
    # whole citation, and the line is comfortably "bounded" - by having silently dropped the
    # provenance. A bound honoured by deleting the signal is the failure this file is about.
    assert terse[0].endswith("]"), "the citation must SURVIVE the clip, not be dropped to satisfy it"
    assert ".json]" in terse[0], "and it must still identify the report"
    assert glyph * glyphs not in terse[0]
    # ...and the clip must not have produced mojibake by splitting a code point.
    assert terse[0] == terse[0].encode("utf-8").decode("utf-8")
    assert "\ufffd" not in terse[0]
    assert width in (3, 4)  # pins that this case really is multibyte
