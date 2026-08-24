"""Tests for scripts/read_handover.py - the reader that projects the engine's work queue.

What the tool is actually for, stated narrowly so the tests are not asked to defend more than the
evidence supports:

* it surfaces ``pbip_ref_drops[].emptied`` - visuals whose every field binding was dropped, which
  render blank on a report that validates clean - instead of leaving them buried;
* it gives a stable, schema-aware projection of both queues (model-side ``requests[]`` and
  report-side ``remediation_worklist``/``viz_fidelity``);
* it de-duplicates ``category_guidance``, which the engine emits per REQUEST rather than per
  category (measured: 44,775 bytes, 12.6%, of a 347 KB file);
* it saves every consumer a parse-and-filter round trip.

It does NOT prevent any known class of shipped defect, and no test here should be read as claiming
that it does. An earlier version of this module described a "silent decoy" mechanism - a truncated
read quietly returning ``needs_review[]`` - and attributed shipped defects to it. That claim was
measured to be false and was retracted: the file-read tool refuses an oversized file loudly, there
is no silent-truncation band, and the asserted downstream cause was never evidenced.

Every test names the mutation it kills, because a test that cannot fail is worse than no test.

No network, no Power BI Desktop, no engine: every fixture is written into `tmp_path`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import read_handover as rh  # noqa: E402  # pylint: disable=wrong-import-position

GUIDANCE = (
    "This calc is driven by a Tableau parameter, which is a Power BI MODEL OBJECT rather than a "
    "single expression. Identify the usage: a dimension swap maps to field parameters."
)

# A real dispatcher, glyphs included. U+25B2 is three bytes in UTF-8 and unencodable in cp1252,
# which is what makes it load-bearing for both the byte-budget and the stdout-encoding tests.
DISPATCHER = """CASE [Parameters].[Parameter 9]
    WHEN 1 THEN [Calculation_844424952101298176]
    WHEN 2 THEN SUM([Number of Records 1])
    WHEN 3 THEN [# Members \u25b2]
END"""


def make_request(name: str, category: str = "model_object_parameter", formula: str = DISPATCHER) -> dict:
    return {
        "category": category,
        "category_guidance": GUIDANCE,
        "fallback_reason": "bare row-level field [..] not valid in a measure",
        "fields": [
            {"caption": "[Parameters].[Parameter 9]", "kind": "parameter"},
            {"caption": "Number of Records 1", "kind": "unresolved"},
        ],
        "formula": formula,
        "has_suggestion": False,
        "name": name,
        "role": "measure",
        "target_table": "_Measures",
    }


def make_workbook(requests: list[dict], name: str = "Admin_Insights_Starter") -> dict:
    """A handover slice shaped exactly like `run_estate.slice_handovers` writes it."""
    return {
        "name": name,
        "model_translation_handoff": {
            # `needs_review[]` is a strict field-subset of `requests[]` - same calcs, no formula -
            # and it is listed FIRST on disk, so it is the easy wrong key to read from.
            "needs_review": [
                {
                    "category": r["category"],
                    "fallback_reason": r["fallback_reason"],
                    "has_suggestion": False,
                    "name": r["name"],
                    "role": r["role"],
                }
                for r in requests
            ],
            "requests": requests,
            "summary": {
                "categories": {},
                "coverage_pct": 34.8,
                "stub": len(requests),
                "total": len(requests) + 32,
                "translated": 32,
            },
            "triage": {"cascadable": [], "irreducible": {}, "summary": {}},
        },
    }


def write_slice(tmp_path: Path, wb: dict, filename: str = "Admin_Insights_Starter.json") -> Path:
    path = tmp_path / filename
    path.write_text(json.dumps({"estate": {"tool": "t"}, "workbook": wb}, indent=2), encoding="utf-8")
    return path


def run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str]:
    code = rh.main(argv)
    return code, capsys.readouterr().out


def run_err(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    """Same, but keeps stderr - the cap-floor rejection says nothing on stdout."""
    code = rh.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------------------------
# Reading the right key
# --------------------------------------------------------------------------------------------


def test_reads_requests_not_needs_review(tmp_path, capsys):
    """Kills: sourcing the queue from `needs_review[]`, which has no formula to repair from."""
    wb = make_workbook([make_request("Selected Measure")])
    # Make the two keys distinguishable: this names a calc that is NOT in the real queue.
    wb["model_translation_handoff"]["needs_review"].append(
        {
            "category": "x",
            "fallback_reason": "y",
            "has_suggestion": False,
            "name": "NEEDS-REVIEW ONLY",
            "role": "measure",
        }
    )
    path = write_slice(tmp_path, wb)

    code, out = run([str(path), "--category", "model_object_parameter"], capsys)

    assert code == 0
    assert "Selected Measure" in out
    assert "NEEDS-REVIEW ONLY" not in out
    assert "CASE [Parameters].[Parameter 9]" in out


def test_default_view_warns_that_needs_review_is_not_the_queue(tmp_path, capsys):
    """Kills: dropping the note that `needs_review[]` has no formula and is not the work queue."""
    path = write_slice(tmp_path, make_workbook([make_request("Selected Measure")]))

    _, out = run([str(path)], capsys)

    assert "needs_review" in out
    assert "REPAIR" in out


def test_formula_is_never_abbreviated(tmp_path, capsys):
    """Kills: truncating/eliding the formula. Every source line must survive verbatim."""
    path = write_slice(tmp_path, make_workbook([make_request("Selected Measure")]))

    _, out = run([str(path), "--category", "model_object_parameter"], capsys)

    for line in DISPATCHER.splitlines():
        assert line.strip() in out
    assert "..." not in out.split("source formula:")[1][: len(DISPATCHER) + 200]


# --------------------------------------------------------------------------------------------
# Guidance de-duplication (measured lossless: one distinct string per category estate-wide)
# --------------------------------------------------------------------------------------------


def test_guidance_printed_once_not_once_per_request(tmp_path, capsys):
    """Kills: echoing `category_guidance` per request - the 44,775-byte, 12.6% repetition."""
    path = write_slice(tmp_path, make_workbook([make_request(f"M{i}") for i in range(12)]))

    _, out = run([str(path), "--category", "model_object_parameter", "--max-bytes", "500000"], capsys)

    assert out.count(GUIDANCE) == 1
    assert out.count("[12/12]") == 1  # all 12 still shown


def test_guidance_dedup_keeps_the_longest_variant(tmp_path):
    """Kills: dropping guidance content if the one-per-category invariant ever breaks upstream."""
    short = make_request("A")
    short["category_guidance"] = "short"
    long_one = make_request("B")
    long_one["category_guidance"] = GUIDANCE

    assert rh.guidance_by_category([short, long_one])["model_object_parameter"] == GUIDANCE


# --------------------------------------------------------------------------------------------
# --max-bytes is a strict cap
#
# `_capped()` clamps the assembled text as a last-resort net, so asserting `len(out) <= cap` ALONE
# would pass even with every renderer's budgeting removed. Every test below therefore also asserts
# that `HARD CAP` is absent: that string means the net fired, which is a budgeting defect.
# --------------------------------------------------------------------------------------------


def assert_within_cap(out: str, cap: int) -> None:
    assert len(out.encode("utf-8")) <= cap, f"output {len(out.encode('utf-8'))} B exceeded cap {cap}"
    assert "HARD CAP" not in out, "the last-resort clamp fired, so a renderer's own budgeting is broken"


def test_truncation_is_loud_and_names_every_omitted_item(tmp_path, capsys):
    """Kills: any silent cap. Omitted names must be recoverable from the output alone."""
    path = write_slice(tmp_path, make_workbook([make_request(f"Measure {i}") for i in range(8)]))

    _, out = run([str(path), "--category", "model_object_parameter", "--max-bytes", "1600"], capsys)

    assert "TRUNCATED" in out
    assert "You have NOT seen the whole queue" in out
    shown = sum(1 for i in range(8) if f"[{i + 1}/8] Measure {i}" in out)
    assert shown < 8, "fixture too small to trigger truncation"
    for i in range(shown, 8):
        assert f"Measure {i}" in out, "an omitted request was not named in the banner"


def test_untruncated_output_carries_no_truncation_banner(tmp_path, capsys):
    """Kills: an always-on banner, which would train callers to ignore it."""
    path = write_slice(tmp_path, make_workbook([make_request("Only One")]))

    _, out = run([str(path), "--category", "model_object_parameter"], capsys)

    assert "TRUNCATED" not in out


def test_one_oversized_request_is_named_rather_than_silently_dropped(tmp_path, capsys):
    """Kills: a budget that returns zero items AND says nothing, which reads as an empty queue.

    The original form asserted the request was *inlined* (`[1/1] Huge` in the body). That encoded a
    first-item bypass that let one request blow any cap, so the assertion moved to the contract:
    never silently empty, always named, always with a command that retrieves it.
    """
    path = write_slice(tmp_path, make_workbook([make_request("Huge", formula="X" * 5000)]))

    _, out = run([str(path), "--category", "model_object_parameter", "--max-bytes", "1500"], capsys)

    assert "TRUNCATED" in out, "a dropped request must never be silent"
    assert "Huge" in out, "the omitted request was not named anywhere"
    assert "[1/1] Huge" not in out, "an oversized request must not be inlined past the cap"
    assert_within_cap(out, 1500)


def test_the_named_escape_hatch_actually_returns_the_oversized_request(tmp_path, capsys):
    """Kills: a banner that points at `--name`, while `--name` is itself truncated.

    An EXACT `--name` is the one documented exception to the cap, and that is precisely what makes
    the strict cap elsewhere acceptable - there is always a way to get the full text.
    """
    path = write_slice(tmp_path, make_workbook([make_request("Huge", formula="X" * 5000)]))

    code, out = run([str(path), "--name", "Huge"], capsys)

    assert code == 0
    assert "X" * 5000 in out, "--name must return the whole formula, uncapped"


def test_the_truncation_banner_itself_fits_inside_max_bytes(tmp_path, capsys):
    """Kills all three measured overshoots: unbudgeted names, head, and footer + closing rule.

    The banner is content. Counting only some of its parts overshot a 20,000-byte cap three
    separate times (24,631 -> 20,401 -> 20,120), each invisible to reading. Long names make the
    banner's own list the dominant cost, which is when every one of those regressions appeared.
    """
    reqs = [make_request(f"Calculation_{i}_{'N' * 60}") for i in range(180)]
    path = write_slice(tmp_path, make_workbook(reqs))

    for cap in (2000, 8000, 20000):
        _, out = run([str(path), "--category", "model_object_parameter", "--max-bytes", str(cap)], capsys)

        assert "TRUNCATED" in out, f"fixture failed to truncate at cap {cap}"
        assert "more not named here" in out, f"fixture too small to overflow the name list at cap {cap}"
        assert_within_cap(out, cap)


def test_a_cap_too_small_to_hold_the_banner_is_rejected_not_silently_exceeded(tmp_path, capsys):
    """Kills: accepting an unhonourable cap.

    Measured on the repo's own fixture before the fix: `--max-bytes 100` printed 738 bytes of
    truncation banner and reported that the cap held. The banner costs ~540 bytes before it names
    anything, so the only honest answers are 'reject' or 'emit more than you asked for'.
    """
    path = write_slice(tmp_path, make_workbook([make_request("A")]))

    code, out, err = run_err([str(path), "--category", "model_object_parameter", "--max-bytes", "100"], capsys)

    assert code == 2
    assert out == "", "a rejected cap must print nothing at all on stdout"
    assert str(rh.MIN_MAX_BYTES) in err, "the error must name the minimum the caller has to raise to"


def test_oversized_category_guidance_is_withheld_rather_than_emitted_past_the_cap(tmp_path, capsys):
    """Kills: emitting `category_guidance` BEFORE budgeting.

    Measured before the fix: an 8,000-byte cap with 9,000 bytes of guidance emitted 9,523 bytes -
    and still printed a truncation banner claiming the cap had been honoured.
    """
    req = make_request("A")
    req["category_guidance"] = "G" * 9000
    path = write_slice(tmp_path, make_workbook([req]))

    _, out = run([str(path), "--category", "model_object_parameter", "--max-bytes", "8000"], capsys)

    assert_within_cap(out, 8000)
    assert "GUIDANCE WITHHELD" in out, "guidance that does not fit must say so, not vanish"
    assert "--max-bytes" in out, "the withheld guidance must come with the command that prints it"
    assert "G" * 9000 not in out


def test_emptied_visual_block_is_budgeted_and_keeps_its_full_count(tmp_path, capsys):
    """Kills: the unconditional emptied-block bypass.

    It was documented as deliberately outside `max_bytes`, justified by a 1,014-byte measurement on
    one real workbook - a property of that sample, not a bound. Measured before the fix: 100 emptied
    visuals emitted 21,937 bytes against a 20,000 cap and 1,000 emitted 219,038, neither with a
    banner. Priority (it is claimed from the budget first) is not exemption.
    """
    wb = _report_workbook()
    wb["pbip_ref_drops"] = [
        {"visual": f"v-page-{i:04d}-with-a-realistically-long-identifier", "emptied": True, "dropped": ["Values:x"]}
        for i in range(1000)
    ]
    path = write_slice(tmp_path, wb)

    _, out = run([str(path), "--viz", "--max-bytes", "20000"], capsys)

    assert_within_cap(out, 20000)
    assert "EMPTIED VISUALS (1000)" in out, "the count of blank visuals must survive even when the names do not"
    assert "more emptied visual(s) not named here" in out


@pytest.mark.parametrize("cap", [1500, 2500, 6000, 20000, 60000])
@pytest.mark.parametrize("mode", [[], ["--viz"], ["--fidelity"], ["--category", "model_object_parameter"]])
def test_every_view_honours_the_cap_at_every_size(tmp_path, capsys, mode, cap):
    """Kills: budgeting that holds for one view or one cap. Three paths were unbounded at once."""
    path = write_slice(tmp_path, _fat_workbook())

    _, out = run([str(path), *mode, "--max-bytes", str(cap)], capsys)

    assert_within_cap(out, cap)


@pytest.mark.parametrize("mode", [[], ["--viz"], ["--fidelity"], ["--category", "model_object_parameter"]])
def test_a_pathological_workbook_name_cannot_spend_the_whole_cap_on_a_heading(tmp_path, capsys, mode):
    """Kills: `_clip` reverting to identity.

    Headings are built from payload text, so a heading is only as short as the estate lets it be. A
    400-character workbook name spent an entire 1,500-byte cap on the title alone and left every
    section below budgeting against nothing - measured as a `HARD CAP` fire at caps 1500-1777.
    """
    wb = _fat_workbook()
    wb["name"] = "Workbook_" + "N" * 400
    path = write_slice(tmp_path, wb, "Pathological.json")

    _, out = run([str(path), *mode, "--max-bytes", "1500"], capsys)

    assert_within_cap(out, 1500)


def test_list_view_honours_the_cap(tmp_path, capsys):
    """Kills: `--list` rows emitted unbudgeted - a wide estate is the one that overflows."""
    (tmp_path / "handover").mkdir()
    for i in range(300):
        wb = make_workbook([make_request("A")], name=f"Workbook_{i:03d}_{'L' * 40}")
        write_slice(tmp_path / "handover", wb, f"W{i:03d}.json")

    _, out = run([str(tmp_path), "--list", "--max-bytes", "4000"], capsys)

    assert_within_cap(out, 4000)
    assert "300 workbook(s)" in out, "the total must survive even when most rows do not"
    assert "more workbook(s) not listed here" in out


def test_default_max_bytes_is_the_size_an_agent_read_tool_accepts(tmp_path, capsys):
    """Kills: raising DEFAULT_MAX_BYTES back to 40,000.

    Behavioural on purpose: a fixture sized between the two values truncates at the default and does
    not at an explicit 40,000, so restoring the old constant flips both assertions.
    """
    path = write_slice(tmp_path, make_workbook([make_request(f"Measure {i}") for i in range(60)]))

    _, out_default = run([str(path), "--category", "model_object_parameter"], capsys)
    _, out_wide = run([str(path), "--category", "model_object_parameter", "--max-bytes", "40000"], capsys)

    assert "TRUNCATED" in out_default, "the fixture must exceed the default cap for this test to mean anything"
    assert_within_cap(out_default, 20000)
    assert "TRUNCATED" not in out_wide, "the fixture must fit inside 40,000 for this test to mean anything"
    assert len(out_wide.encode("utf-8")) > 20000, "fixture too small to distinguish 20,000 from 40,000"


def test_budget_is_measured_in_bytes_not_characters(tmp_path, capsys):
    """Kills: `_blen` reverting to `len()`.

    A cap is a byte cap because that is what a read tool enforces. Counting characters under-counts
    every multibyte glyph, so a formula-heavy queue full of U+25B2 quietly exceeds it.
    """
    assert rh._blen("\u25b2") == 3, "U+25B2 is three bytes in UTF-8"  # pylint: disable=protected-access
    assert rh._blen("abc") == 3  # pylint: disable=protected-access

    glyphs = "\u25b2" * 400
    reqs = [make_request(f"M{i}", formula=f"[# Members {glyphs}]") for i in range(40)]
    path = write_slice(tmp_path, make_workbook(reqs))

    _, out = run([str(path), "--category", "model_object_parameter", "--max-bytes", "20000"], capsys)

    assert_within_cap(out, 20000)


# --------------------------------------------------------------------------------------------
# Robustness against real-world payloads
# --------------------------------------------------------------------------------------------


def test_non_cp1252_console_does_not_kill_the_run(tmp_path):
    """Kills: removing `_force_utf8_stdout()`.

    Must be a SUBPROCESS: in-process, pytest's capture replaces `sys.stdout` with a UTF-8 buffer, so
    an in-process assertion passes with the call removed - false coverage. Reproduced by mutation
    under `PYTHONIOENCODING=cp1252`: exit 0 with the call, exit 1 with `UnicodeEncodeError` without.
    """
    path = write_slice(tmp_path, make_workbook([make_request("Group Sort")]))
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    cmd = [sys.executable, str(SCRIPTS / "read_handover.py"), str(path), "--category", "model_object_parameter"]

    proc = subprocess.run(cmd, env=env, capture_output=True, check=False)

    assert proc.returncode == 0, f"cp1252 console killed the run: {proc.stderr.decode('utf-8', 'replace')[-400:]}"
    assert "\u25b2".encode("utf-8") in proc.stdout, "the glyph was lost rather than written as UTF-8"


def test_null_model_translation_handoff_is_not_a_crash(tmp_path, capsys):
    """Kills: assuming the key is a dict. `RESTAPISample` in _bundle-208 has it as null."""
    path = write_slice(tmp_path, {"name": "RESTAPISample", "model_translation_handoff": None})

    code, out = run([str(path)], capsys)

    assert code == 0
    assert "no residual calculations" in out


def test_missing_target_is_an_error_exit_not_a_traceback(tmp_path, capsys):
    code = rh.main([str(tmp_path / "nope.json")])
    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_ambiguous_workbook_refuses_to_guess(tmp_path, capsys):
    """Kills: silently picking the first workbook, which would report on the wrong one."""
    (tmp_path / "handover").mkdir()
    write_slice(tmp_path / "handover", make_workbook([make_request("A")], name="One"), "One.json")
    write_slice(tmp_path / "handover", make_workbook([make_request("B")], name="Two"), "Two.json")

    code = rh.main([str(tmp_path)])

    assert code == 2
    assert "--workbook" in capsys.readouterr().err


def test_bundle_directory_and_workbook_selection(tmp_path, capsys):
    (tmp_path / "handover").mkdir()
    write_slice(tmp_path / "handover", make_workbook([make_request("A")], name="One"), "One.json")
    write_slice(tmp_path / "handover", make_workbook([make_request("B")], name="Two"), "Two.json")

    code, out = run([str(tmp_path), "--workbook", "Two", "--category", "model_object_parameter"], capsys)

    assert code == 0
    assert "[1/1] B" in out


def test_estate_report_json_is_accepted(tmp_path, capsys):
    """Kills: only handling handover slices. `report.json` is the engine's own output shape."""
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"workbooks": [make_workbook([make_request("A")], name="Solo")]}), encoding="utf-8")

    code, out = run([str(report), "--category", "model_object_parameter"], capsys)

    assert code == 0
    assert "[1/1] A" in out


# --------------------------------------------------------------------------------------------
# Report side
# --------------------------------------------------------------------------------------------


def _report_workbook() -> dict:
    wb = make_workbook([make_request("A")])
    wb["remediation_worklist"] = {
        "kind": "tableau-fabric-remediation-worklist",
        "version": 1,
        "items": [
            {
                "category": "unsupported_visual",
                "severity": "blocking",
                "reason": "mark class 'Circle' not supported -> no visual emitted",
                "remediation": "Rebuild this visual by hand.",
                "worksheet": "Box & Whisker",
            },
            {
                "category": "unsupported_visual",
                "severity": "blocking",
                "reason": "mark class 'Shape' not supported -> no visual emitted",
                "remediation": "Rebuild this visual by hand.",
                "worksheet": "User Activity",
            },
            {
                "category": "filter",
                "severity": "medium",
                "reason": "context filter not translated",
                "remediation": "Re-apply as a report-level filter.",
                "worksheet": "Trend",
            },
        ],
        "summary": {"visuals_flagged": 2, "visuals_clean": 1, "by_severity": {"blocking": 2, "medium": 1}},
        "visuals": [],
    }
    wb["pbip_ref_drops"] = [{"visual": "v-page-abc", "emptied": True, "dropped": ["Values:column 'Duration'"]}]
    wb["viz_fidelity"] = [{"tier": "degraded", "status": "warned", "visual_type": "card", "worksheet": "x"}]
    return wb


def _fat_workbook() -> dict:
    """Every queue populated well past any cap, with multibyte glyphs in the bulk."""
    wb = make_workbook([make_request(f"Calculation_{i}_\u25b2_{'N' * 40}") for i in range(120)])
    wb["model_translation_handoff"]["triage"]["cascadable"] = [f"Cascade_{i}_\u25b2" for i in range(60)]
    wb["remediation_worklist"] = {
        "items": [
            {
                "category": f"category_{i % 7}",
                "severity": ("blocking", "high", "medium", "low")[i % 4],
                "reason": f"reason \u25b2 {i} " + "R" * 60,
                "remediation": f"Rebuild by hand ({i % 7}). " + "F" * 40,
                "worksheet": f"Sheet \u25b2 {i} " + "W" * 30,
            }
            for i in range(150)
        ],
        "summary": {},
        "visuals": [],
    }
    wb["pbip_ref_drops"] = [
        {"visual": f"v-page-{i:04d}-\u25b2-{'V' * 30}", "emptied": True, "dropped": [f"Values:col {i}"]}
        for i in range(120)
    ]
    wb["viz_fidelity"] = [
        {
            "tier": ("full", "degraded", "rebuilt")[i % 3],
            "status": "warned" if i % 3 else "ok",
            "visual_type": "card",
            "worksheet": f"Fidelity \u25b2 {i}",
            "reason": f"deferral \u25b2 {i % 9} " + "D" * 40 if i % 3 else "",
        }
        for i in range(99)
    ]
    return wb


def test_viz_dedupes_remediation_text_within_a_category(tmp_path, capsys):
    """Kills: repeating identical fix text per item - the same bloat as the guidance repetition."""
    path = write_slice(tmp_path, _report_workbook())

    _, out = run([str(path), "--viz"], capsys)

    assert out.count("Rebuild this visual by hand.") == 1
    assert "Box & Whisker" in out and "User Activity" in out


def test_emptied_visuals_survive_a_severity_filter(tmp_path, capsys):
    """Kills: hiding emptied visuals behind --severity. A blank visual is the worst outcome there is."""
    path = write_slice(tmp_path, _report_workbook())

    _, out = run([str(path), "--viz", "--severity", "medium"], capsys)

    assert "EMPTIED" in out
    assert "v-page-abc" in out


def test_severity_filter_actually_filters(tmp_path, capsys):
    path = write_slice(tmp_path, _report_workbook())

    _, out = run([str(path), "--viz", "--severity", "blocking"], capsys)

    assert "Box & Whisker" in out
    assert "Trend" not in out


def test_viz_truncation_is_loud_and_the_queue_stays_inside_the_cap(tmp_path, capsys):
    """Kills two mutations at once: disabling `--viz` budgeting, and removing its banner.

    Without budgeting the last-resort clamp fires (`HARD CAP`); without the banner a caller is left
    believing a 150-item queue was 20 items long.
    """
    path = write_slice(tmp_path, _fat_workbook())

    _, out = run([str(path), "--viz", "--max-bytes", "8000"], capsys)

    assert_within_cap(out, 8000)
    assert "OUTPUT TRUNCATED" in out, "a cut worklist must say so"
    assert "NOT shown" in out
    shown = sum(1 for i in range(150) if f"Sheet \u25b2 {i} " in out)
    assert shown < 150, "fixture too small to trigger truncation"
    assert f"of {150 + 120} item(s) shown" in out or "item(s) shown" in out


def test_worklist_does_not_resume_after_omitting_an_item(tmp_path):
    """Kills: `if _blen(line) + 1 > budget` without the `omitted or` guard.

    A queue that skips item 40 and then prints item 41 reads as if 40 does not exist, which is the
    exact failure the banner's "N of M shown" wording is meant to rule out. Measured before the fix:
    an oversized first item was omitted and a small second item printed anyway.
    """
    grouped = {
        "cat": [
            {"severity": "blocking", "worksheet": "BIG", "reason": "R" * 4000},
            {"severity": "low", "worksheet": "small", "reason": "s"},
        ]
    }

    lines, omitted, shown = rh._budgeted_worklist(grouped, 500)  # pylint: disable=protected-access

    body = "\n".join(lines)
    assert shown == 0
    assert "small" not in body, "the view resumed after an omission"
    assert len(omitted) == 2, "both items must be named in the banner"


def test_default_view_surfaces_the_report_queue(tmp_path, capsys):
    """Kills: a model-only summary. `viz_fidelity`/`remediation_worklist` are deeper in the file."""
    path = write_slice(tmp_path, _report_workbook())

    _, out = run([str(path)], capsys)

    assert "REPORT:" in out
    assert "EMPTIED" in out
    assert "--viz" in out


def test_real_measure_filter_fixture_is_report_side_blocking_numeric_fidelity(capsys):
    """Kills: ignoring the real `measure_filters_needs_review` field from Admin_Insights_Starter."""
    path = FIXTURES / "handover-measure-filters.json"

    _, default = run([str(path)], capsys)
    _, viz = run([str(path), "--viz"], capsys)
    _, blocking = run([str(path), "--viz", "--severity", "blocking"], capsys)

    assert "REPORT: 2 remediation item(s)" in default
    assert "severity: blocking 2" in default
    assert "2 dropped aggregate/calculated measure filter(s)" in default
    assert "INVISIBLE numeric-fidelity risk" in default
    assert "## measure_filter_needs_review  (2 item(s))" in viz
    assert "Above Thresholds - BAN" in viz
    assert "Days since last login" in viz
    assert "visual renders, values are wrong" in viz
    assert "re-apply it as a visual-level filter" in viz
    assert "Above Thresholds - BAN" in blocking


def test_viz_category_drills_into_measure_filters_without_emptied_visual_noise(capsys):
    """Kills: `--viz --category measure_filter_needs_review` falling through to model categories."""
    path = FIXTURES / "handover-measure-filters.json"

    _, out = run([str(path), "--viz", "--category", rh.MEASURE_FILTER_CATEGORY], capsys)

    assert "## measure_filter_needs_review  (2 item(s))" in out
    assert "Above Thresholds - BAN" in out
    assert "No requests in category" not in out
    assert "EMPTIED VISUALS" not in out


def test_viz_category_truncation_recovery_does_not_suggest_severity(tmp_path, capsys):
    """Kills: scoped report-category truncation pointing at `--severity`, which no longer narrows it."""
    wb = make_workbook([])
    wb["measure_filters_needs_review"] = {
        "count": 40,
        "note": "re-apply it as a visual-level filter in Power BI",
        "worksheets": [{"worksheet": f"Sheet {i}", "reason": "R" * 200} for i in range(40)],
    }
    path = write_slice(tmp_path, wb)

    _, out = run([str(path), "--viz", "--category", rh.MEASURE_FILTER_CATEGORY, "--max-bytes", "3000"], capsys)

    assert "OUTPUT TRUNCATED" in out
    assert "--json <file>" in out
    assert "--severity" not in out


def test_measure_filter_json_preserves_present_vs_missing(tmp_path, capsys):
    """Kills: reducing missing `measure_filters_needs_review` to the same shape as zero dropped filters."""
    present_json = tmp_path / "present.json"
    missing_json = tmp_path / "missing.json"
    present = FIXTURES / "handover-measure-filters.json"
    missing = write_slice(tmp_path, make_workbook([]), "missing-key.json")

    assert run([str(present), "--json", str(present_json)], capsys)[0] == 0
    assert run([str(missing), "--json", str(missing_json)], capsys)[0] == 0

    present_payload = json.loads(present_json.read_text(encoding="utf-8"))
    missing_payload = json.loads(missing_json.read_text(encoding="utf-8"))
    assert present_payload["measure_filters_needs_review"]["count"] == 2
    assert len(present_payload["measure_filter_items"]) == 2
    assert missing_payload["measure_filters_needs_review"] == rh.MEASURE_FILTER_MISSING
    assert missing_payload["measure_filter_items"] == []


def test_measure_filter_missing_is_not_a_false_zero(tmp_path, capsys):
    """Kills: printing a clean zero when the engine never emitted the audit field at all."""
    path = write_slice(tmp_path, make_workbook([]))

    _, out = run([str(path)], capsys)

    assert "measure filters: NOT RECORDED" in out
    assert "0 dropped" not in out


def test_measure_filter_present_zero_prints_no_empty_section(tmp_path, capsys):
    """Kills: adding a noisy empty section for a workbook that explicitly recorded no dropped filters."""
    wb = make_workbook([])
    wb["measure_filters_needs_review"] = {"count": 0, "note": "none", "worksheets": []}
    path = write_slice(tmp_path, wb)

    _, default = run([str(path)], capsys)
    _, viz = run([str(path), "--viz"], capsys)

    assert "measure filters:" not in default
    assert "measure_filter_needs_review" not in viz
    assert "dropped aggregate/calculated measure filter" not in default
    assert "No remediation worklist items" in viz


def test_real_pbip_warning_fixture_is_grouped_into_report_queue(capsys):
    """Kills: leaving real `pbip_warnings[]` as prose-only instead of report-side work items."""
    path = FIXTURES / "handover-pbip-warnings.json"

    _, default = run([str(path)], capsys)
    _, viz = run([str(path), "--viz"], capsys)
    _, blocking = run([str(path), "--viz", "--severity", "blocking"], capsys)

    assert "!! 4 PBIP warning(s): dangling_refs 1 | no_relationship 2 | tableau_blend 1" in default
    assert "REPORT: 4 remediation item(s)" in default
    assert "severity: blocking 3 | high 1" in default
    assert "## pbip_warning_no_relationship  (2 item(s))" in viz
    assert "## pbip_warning_tableau_blend  (1 item(s))" in viz
    assert "## pbip_warning_dangling_refs  (1 item(s))" in viz
    assert "Activity Threshold" in viz
    assert "Tableau BLENDS 'Groups' with 'TS Users'" in viz
    assert "visual field reference(s) name a model object" in viz
    assert "pbip_warning_dangling_refs" not in blocking


def test_viz_category_drills_into_one_pbip_warning_family(capsys):
    """Kills: treating every PBIP warning as one flat bucket with no family-specific drill-down."""
    path = FIXTURES / "handover-pbip-warnings.json"

    _, out = run([str(path), "--viz", "--category", "pbip_warning_no_relationship"], capsys)

    assert "## pbip_warning_no_relationship  (2 item(s))" in out
    assert "Activity Threshold" in out
    assert "Timezone" in out
    assert "Tableau BLENDS" not in out


def test_pbip_warning_json_preserves_present_vs_missing(tmp_path, capsys):
    """Kills: reducing missing `pbip_warnings` to the same shape as present-and-empty warnings."""
    present_json = tmp_path / "present-pbip.json"
    missing_json = tmp_path / "missing-pbip.json"
    present = FIXTURES / "handover-pbip-warnings.json"
    missing = write_slice(tmp_path, make_workbook([]), "missing-pbip-key.json")

    assert run([str(present), "--json", str(present_json)], capsys)[0] == 0
    assert run([str(missing), "--json", str(missing_json)], capsys)[0] == 0

    present_payload = json.loads(present_json.read_text(encoding="utf-8"))
    missing_payload = json.loads(missing_json.read_text(encoding="utf-8"))
    assert len(present_payload["pbip_warnings"]) == 4
    assert len(present_payload["pbip_warning_items"]) == 4
    assert missing_payload["pbip_warnings"] == rh.PBIP_WARNING_MISSING
    assert missing_payload["pbip_warning_items"] == []


def test_pbip_warning_missing_and_empty_are_not_conflated(tmp_path, capsys):
    """Kills: a false green where an unrecorded audit key prints like an explicitly empty list."""
    missing = write_slice(tmp_path, make_workbook([]), "missing-pbip-key.json")
    empty_wb = make_workbook([])
    empty_wb["pbip_warnings"] = []
    empty = write_slice(tmp_path, empty_wb, "empty-pbip-key.json")

    _, missing_out = run([str(missing)], capsys)
    _, empty_out = run([str(empty)], capsys)

    assert "pbip warnings: NOT RECORDED" in missing_out
    assert "pbip warnings: none recorded (key present and empty)" in empty_out
    assert "zero-warning" in missing_out


def test_pbip_warning_list_ranks_warning_heavy_workbooks(tmp_path, capsys):
    """Kills: bundle list sorting only by calc/report worklist size, burying PBIP warnings."""
    handover = tmp_path / "handover"
    handover.mkdir()
    quiet = make_workbook([], name="Quiet")
    quiet["pbip_warnings"] = []
    loud = make_workbook([], name="Loud")
    loud["pbip_warnings"] = ["manual attention required: table 'T' landed with NO relationship to any other table"]
    write_slice(handover, quiet, "quiet.json")
    write_slice(handover, loud, "loud.json")

    _, out = run([str(tmp_path), "--list"], capsys)

    lines = [line for line in out.splitlines() if "calc request" in line]
    assert "Loud" in lines[0]
    assert "!! 1 PBIP-WARNINGS" in lines[0]


def test_pbip_warning_command_failure_is_not_scored_as_expected_output():
    """Mutation harness guard: command failure is a failure, not a caught-output mutation."""
    cmd = [sys.executable, str(SCRIPTS / "read_handover.py"), "does-not-exist.json", "--viz"]

    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 2
    assert "PBIP warning" not in result.stdout
    assert "does not exist" in result.stderr


# --------------------------------------------------------------------------------------------
# Model-side partition scaffold deferrals (issue #326)
#
# `partitions_needs_review[]` shape confirmed against a real slice:
# `_bundle-208/handover/Admin_Insights_Starter.json` -> `workbook.partitions_needs_review` ==
# `[{"kind": "m_partition", "reason": "flat-file source; set the file path ...", "table": "sqlproxy"}, ...]`.
# The specific "custom SQL native query for this connector isn't verified" reason used below comes
# from the engine source (`connection_to_m.py`'s `_scaffold_review` call sites) rather than a slice
# on this machine, since no local slice happened to carry a live-connector deferral -- it is the
# same field and shape, just a different member of the reason FAMILY.
# --------------------------------------------------------------------------------------------


def test_partition_review_ranks_first_and_groups_tables_under_one_reason(tmp_path, capsys):
    """Kills: printing the reason once per table (the engine's own repetition), and burying it
    below the calc-stub queue instead of ranking it ahead of a mere stub (which still evaluates)."""
    wb = make_workbook([make_request("A")])
    wb["partitions_needs_review"] = [
        {
            "kind": "m_partition",
            "reason": "custom SQL native query for this connector isn't verified; complete it manually",
            "table": "sqlproxy (Sales)",
        },
        {
            "kind": "m_partition",
            "reason": "custom SQL native query for this connector isn't verified; complete it manually",
            "table": "sqlproxy (Costs)",
        },
        {
            "kind": "m_partition",
            "reason": "generic ODBC source carried neither a DSN nor a driver name, so no connection "
            "string could be reconstructed; set the ODBC connection manually",
            "table": "OtherTable",
        },
    ]
    path = write_slice(tmp_path, wb)

    _, out = run([str(path)], capsys)

    assert out.count("isn't verified; complete it manually") == 1, "reason must print once, not per table"
    assert "sqlproxy (Sales)" in out and "sqlproxy (Costs)" in out
    assert "OtherTable" in out
    assert "3 table partition(s) across 2 distinct reason(s)" in out
    partition_idx = out.index("NEED MANUAL COMPLETION")
    model_idx = out.index("MODEL:")
    assert partition_idx < model_idx, "an unresolved M partition (zero rows) must outrank a mere stub calc"


def test_partition_review_missing_is_not_a_false_zero(tmp_path, capsys):
    """Kills: printing a clean '0 table partitions' when the engine never emitted the key at all.

    This repo has shipped exactly this false-green three times (#276, #299, #309); the rule per
    `check_stub_measures.py:68-69` is that 'no stubs' and 'no model' must never print the same way.
    """
    path = write_slice(tmp_path, make_workbook([]))

    _, out = run([str(path)], capsys)

    assert "partition scaffolds: NOT RECORDED" in out
    assert "0 table" not in out
    assert "NEED MANUAL COMPLETION" not in out


def test_partition_review_present_empty_prints_no_section(tmp_path, capsys):
    """Kills: treating an engine-recorded empty list the same as a missing key (or vice versa)."""
    wb = make_workbook([])
    wb["partitions_needs_review"] = []
    path = write_slice(tmp_path, wb)

    _, out = run([str(path)], capsys)

    assert "partition scaffolds: NOT RECORDED" not in out
    assert "NEED MANUAL COMPLETION" not in out
    assert "partition scaffolds:" not in out


def test_partition_review_invalid_shape_is_reported_not_silently_dropped(tmp_path, capsys):
    """Kills: a non-list `partitions_needs_review` (engine schema drift) rendering as clean-empty."""
    wb = make_workbook([])
    wb["partitions_needs_review"] = {"unexpected": "shape"}
    path = write_slice(tmp_path, wb)

    _, out = run([str(path)], capsys)

    assert "partition scaffolds: INVALID SHAPE" in out


def test_partition_review_json_preserves_present_vs_missing(tmp_path, capsys):
    """Kills: reducing a missing `partitions_needs_review` key to the same JSON shape as zero rows."""
    present_wb = make_workbook([])
    present_wb["partitions_needs_review"] = [
        {"kind": "m_partition", "reason": "R1", "table": "T1"},
        {"kind": "m_partition", "reason": "R1", "table": "T2"},
    ]
    present = write_slice(tmp_path, present_wb, "present.json")
    missing = write_slice(tmp_path, make_workbook([]), "missing.json")
    present_json = tmp_path / "present-out.json"
    missing_json = tmp_path / "missing-out.json"

    assert run([str(present), "--json", str(present_json)], capsys)[0] == 0
    assert run([str(missing), "--json", str(missing_json)], capsys)[0] == 0

    present_payload = json.loads(present_json.read_text(encoding="utf-8"))
    missing_payload = json.loads(missing_json.read_text(encoding="utf-8"))
    assert len(present_payload["partitions_needs_review"]) == 2
    assert present_payload["partitions_needs_review_groups"] == {"R1": ["T1", "T2"]}
    assert missing_payload["partitions_needs_review"] == rh.PARTITION_REVIEW_MISSING
    assert missing_payload["partitions_needs_review_groups"] == {}


def test_partition_review_outranks_pbip_warnings_in_list(tmp_path, capsys):
    """Kills: leaving partition scaffolds out of `--list` urgency, or ranking them below PBIP
    warnings -- an unresolved partition means zero rows, which this repo's field incident (#326)
    showed gets silently "fixed" by materializing the wrong data source unless it outranks noise."""
    handover = tmp_path / "handover"
    handover.mkdir()
    warned_only = make_workbook([], name="WarnedOnly")
    warned_only["pbip_warnings"] = ["manual attention required: table 'T' landed with NO relationship to any other"]
    scaffolded = make_workbook([], name="Scaffolded")
    scaffolded["partitions_needs_review"] = [{"kind": "m_partition", "reason": "R", "table": "T"}]
    write_slice(handover, warned_only, "warned.json")
    write_slice(handover, scaffolded, "scaffolded.json")

    _, out = run([str(tmp_path), "--list"], capsys)

    lines = [line for line in out.splitlines() if "calc request" in line]
    assert "Scaffolded" in lines[0], "an unresolved M partition must outrank a PBIP-warning-only workbook"
    assert "PARTITION-SCAFFOLDS" in lines[0]
    assert "Partition scaffolds: 1 table(s)" in out


# --------------------------------------------------------------------------------------------
# --fidelity
# --------------------------------------------------------------------------------------------


def test_fidelity_is_its_own_view_and_is_actually_dispatched(tmp_path, capsys):
    """Kills: dropping the `--fidelity` branch, which silently falls through to the default view."""
    path = write_slice(tmp_path, _report_workbook())

    code, out = run([str(path), "--fidelity"], capsys)

    assert code == 0
    assert "- VISUAL FIDELITY (1 visual(s)) ===" in out, "the --fidelity view header is missing"
    assert "HANDOVER QUEUE" not in out, "--fidelity fell through to the default view"


def test_fidelity_prints_rows_with_no_reason_instead_of_only_counting_them(tmp_path, capsys):
    """Kills: dropping the clean-row group.

    `--fidelity` claims to print `viz_fidelity[]` in full. Measured on a real file: 99 rows, of
    which 30 carry no reason - and those 30 appeared only inside an aggregate tier count.
    """
    wb = _report_workbook()
    wb["viz_fidelity"] = [
        {"tier": "degraded", "status": "warned", "visual_type": "card", "worksheet": "HasReason", "reason": "deferred"},
        {"tier": "full", "status": "ok", "visual_type": "card", "worksheet": "NoReasonRecorded"},
    ]
    path = write_slice(tmp_path, wb)

    _, out = run([str(path), "--fidelity"], capsys)

    assert "HasReason" in out
    assert "NoReasonRecorded" in out, "a row without a reason must still be listed individually"
    assert rh.CLEAN_FIDELITY_GROUP in out


# --------------------------------------------------------------------------------------------
# The truncation banner's recovery command must match the view it is printed under
# --------------------------------------------------------------------------------------------


def test_recovery_hint_is_per_view_not_a_shared_severity_guess(tmp_path, capsys):
    """Kills: the hard-coded `--severity` recovery line.

    `--severity` only affects `--viz`: `--fidelity --severity blocking` is byte-identical to
    `--fidelity`, and `--category` printed a correct `--name` hint and a useless `--severity` one
    side by side.
    """
    path = write_slice(tmp_path, _fat_workbook())
    cap = ["--max-bytes", "4000"]

    _, cat = run([str(path), "--category", "model_object_parameter", *cap], capsys)
    _, viz = run([str(path), "--viz", *cap], capsys)
    _, fid = run([str(path), "--fidelity", *cap], capsys)

    for name, out in (("category", cat), ("viz", viz), ("fidelity", fid)):
        assert "OUTPUT TRUNCATED" in out, f"{name} fixture failed to truncate"

    assert "--name '<name>'" in cat and "--severity" not in cat
    assert "--severity <blocking|high|medium|low>" in viz
    assert "--json <file>" in fid and "--severity" not in fid


def test_severity_is_a_no_op_outside_viz(tmp_path, capsys):
    """Pins the reason the shared `--severity` hint was wrong, so the finding cannot silently rot."""
    path = write_slice(tmp_path, _report_workbook())

    _, plain = run([str(path), "--fidelity"], capsys)
    _, filtered = run([str(path), "--fidelity", "--severity", "blocking"], capsys)

    assert plain == filtered


# --------------------------------------------------------------------------------------------
# --name
# --------------------------------------------------------------------------------------------


def test_name_lookup_returns_one_calc_with_its_guidance(tmp_path, capsys):
    path = write_slice(tmp_path, make_workbook([make_request("Selected Measure"), make_request("Other")]))

    _, out = run([str(path), "--name", "Selected Measure"], capsys)

    assert "Selected Measure" in out
    assert "[1/1]" in out
    assert GUIDANCE in out
    assert "] Other" not in out


def test_ambiguous_name_lists_candidates_instead_of_dumping_every_body(tmp_path, capsys):
    """Kills: substring matching that recreates bulk output through the one uncapped view.

    Measured on a real file: `--name a` matched 47 calculations and returned 66,075 bytes, which
    contradicts the 'one calculation in full' contract the truncation banner points at.
    """
    reqs = [make_request(f"Measure Alpha {i}") for i in range(47)]
    path = write_slice(tmp_path, make_workbook(reqs))

    code, out = run([str(path), "--name", "a", "--max-bytes", "20000"], capsys)

    assert code == 0
    assert "AMBIGUOUS" in out
    assert "47 calculation(s) match" in out
    assert "Measure Alpha 0" in out, "the candidate names are the whole point of the ambiguous view"
    assert "source formula:" not in out, "an ambiguous --name must print no bodies at all"
    assert_within_cap(out, 20000)


def test_an_exact_name_still_wins_over_a_substring_collision(tmp_path, capsys):
    """Kills: routing an exact match into the ambiguous branch when it is also a substring."""
    reqs = [make_request("Sales"), make_request("Sales YoY"), make_request("Sales YTD")]
    path = write_slice(tmp_path, make_workbook(reqs))

    _, out = run([str(path), "--name", "Sales"], capsys)

    assert "AMBIGUOUS" not in out
    assert "source formula:" in out
    assert "] Sales YoY" not in out


def test_a_single_substring_match_is_still_printed_in_full(tmp_path, capsys):
    """Kills: forcing every inexact match into the candidate list, which would break the hatch."""
    path = write_slice(tmp_path, make_workbook([make_request("Huge", formula="X" * 5000), make_request("Other")]))

    _, out = run([str(path), "--name", "Hug"], capsys)

    assert "AMBIGUOUS" not in out
    assert "X" * 5000 in out


# --------------------------------------------------------------------------------------------
# Misc surfaces
# --------------------------------------------------------------------------------------------


def test_cascadable_stubs_are_called_out_with_ordering_advice(tmp_path, capsys):
    """Kills: dropping the cascade warning - fixing an outer stub first leaves it BLANK() anyway."""
    wb = make_workbook([make_request("Outer")])
    wb["model_translation_handoff"]["triage"]["cascadable"] = ["% Diff Sales per Customers"]
    path = write_slice(tmp_path, wb)

    _, out = run([str(path)], capsys)

    assert "CASCADABLE" in out
    assert "% Diff Sales per Customers" in out
    assert "dependency order" in out


def test_json_output_hoists_guidance_out_of_the_requests(tmp_path, capsys):
    """Kills: a JSON dump that re-duplicates guidance per request."""
    path = write_slice(tmp_path, make_workbook([make_request(f"M{i}") for i in range(5)]))
    out_json = tmp_path / "q.json"

    run([str(path), "--json", str(out_json)], capsys)
    payload = json.loads(out_json.read_text(encoding="utf-8"))

    assert payload["guidance"]["model_object_parameter"] == GUIDANCE
    assert len(payload["requests"]) == 5
    assert all("category_guidance" not in r for r in payload["requests"])
    assert all(r["formula"] for r in payload["requests"])


def test_list_shows_every_workbook_with_its_queue_size(tmp_path, capsys):
    (tmp_path / "handover").mkdir()
    write_slice(tmp_path / "handover", make_workbook([make_request("A")], name="One"), "One.json")
    write_slice(tmp_path / "handover", make_workbook([make_request(f"B{i}") for i in range(3)], name="Two"), "Two.json")

    code, out = run([str(tmp_path), "--list"], capsys)

    assert code == 0
    assert "One" in out and "Two" in out
    assert "2 workbook(s)" in out


def test_list_ranks_by_urgency_not_alphabetically(tmp_path, capsys):
    """Kills: restoring the alphabetical `--list` order.

    Measured on `_bundle-208`: `Meridian_Hostile_Identifiers` has an emptied visual but zero calc
    requests and zero worklist items, so a name-sorted `N calc / N report` line rendered it as
    `0 / 0` - the least urgent-looking row in the estate, and the most urgent one in fact.
    """
    (tmp_path / "handover").mkdir()
    busy = make_workbook([make_request(f"M{i}") for i in range(9)], name="Aaa_Busy_But_Fine")
    blank = make_workbook([], name="Zzz_Blank_Visual")
    blank["pbip_ref_drops"] = [{"visual": "v-1", "emptied": True, "dropped": ["Values:x"]}]
    write_slice(tmp_path / "handover", busy, "Aaa.json")
    write_slice(tmp_path / "handover", blank, "Zzz.json")

    _, out = run([str(tmp_path), "--list"], capsys)

    lines = out.splitlines()
    urgent = next(i for i, line in enumerate(lines) if "Zzz_Blank_Visual" in line)
    routine = next(i for i, line in enumerate(lines) if "Aaa_Busy_But_Fine" in line)
    assert urgent < routine, "an emptied visual must outrank an alphabetically-earlier busy workbook"
    assert "EMPTIED" in lines[urgent], "the emptied count must be visible on the row itself"


def test_unknown_category_lists_what_is_actually_present(tmp_path, capsys):
    """Kills: an empty response that reads as 'nothing to do here'."""
    path = write_slice(tmp_path, make_workbook([make_request("A")]))

    _, out = run([str(path), "--category", "not_a_category"], capsys)

    assert "No requests in category" in out
    assert "model_object_parameter (1)" in out
