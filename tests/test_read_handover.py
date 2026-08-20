"""Tests for scripts/read_handover.py - the reader that makes the engine's work queue readable.

Every test names the mutation it kills. This tool exists because a queue was silently unreachable,
so the tests that matter most are the ones that would catch it becoming unreachable again:

* `test_reads_requests_not_the_needs_review_decoy` - the whole defect. `needs_review[]` and
  `requests[]` describe the same calculations, and the reader must work from the one carrying the
  formula. A mutation swapping the key would leave a plausible-looking, useless report.
* `test_truncation_is_loud_and_names_every_omitted_item` - silent truncation IS the original bug.
  A mutation dropping the banner must fail here.
* `test_formula_is_never_abbreviated` - the formula is the entire payload; abbreviating it would
  reintroduce "compliant and blank" repairs.

No network, no Power BI Desktop, no engine: every fixture is written into `tmp_path`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import read_handover as rh  # noqa: E402  # pylint: disable=wrong-import-position

GUIDANCE = (
    "This calc is driven by a Tableau parameter, which is a Power BI MODEL OBJECT rather than a "
    "single expression. Identify the usage: a dimension swap maps to field parameters."
)

# A real dispatcher, glyphs included - this exact shape shipped as BLANK() on three workbooks.
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
            # Deliberately the DECOY: same names, 5 fields, no formula. Listed FIRST, as on disk.
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


# --------------------------------------------------------------------------------------------
# The defect this tool exists to fix
# --------------------------------------------------------------------------------------------


def test_reads_requests_not_the_needs_review_decoy(tmp_path, capsys):
    """Kills: sourcing the queue from `needs_review[]`, which has no formula to repair from."""
    wb = make_workbook([make_request("Selected Measure")])
    # Make the decoy distinguishable: it names a calc that is NOT in the real queue.
    wb["model_translation_handoff"]["needs_review"].append(
        {"category": "x", "fallback_reason": "y", "has_suggestion": False, "name": "DECOY ONLY", "role": "measure"}
    )
    path = write_slice(tmp_path, wb)

    code, out = run([str(path), "--category", "model_object_parameter"], capsys)

    assert code == 0
    assert "Selected Measure" in out
    assert "DECOY ONLY" not in out
    assert "CASE [Parameters].[Parameter 9]" in out


def test_default_view_warns_that_needs_review_is_not_the_queue(tmp_path, capsys):
    """Kills: dropping the decoy warning, which is what stops an agent 'reading the file' wrongly."""
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
    """Kills: echoing `category_guidance` per request - 60 copies is what buried the queue."""
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
# Truncation must never be silent - silent truncation IS the original bug
# --------------------------------------------------------------------------------------------


def test_truncation_is_loud_and_names_every_omitted_item(tmp_path, capsys):
    """Kills: any silent cap. Omitted names must be recoverable from the output alone."""
    path = write_slice(tmp_path, make_workbook([make_request(f"Measure {i}") for i in range(8)]))

    _, out = run([str(path), "--category", "model_object_parameter", "--max-bytes", "1200"], capsys)

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


def test_one_oversized_request_is_still_shown(tmp_path, capsys):
    """Kills: a budget that returns zero items, which would look like an empty queue."""
    path = write_slice(tmp_path, make_workbook([make_request("Huge", formula="X" * 5000)]))

    _, out = run([str(path), "--category", "model_object_parameter", "--max-bytes", "100"], capsys)

    assert "[1/1] Huge" in out


# --------------------------------------------------------------------------------------------
# Robustness against real-world payloads
# --------------------------------------------------------------------------------------------


def test_non_cp1252_glyph_in_a_formula_does_not_crash(tmp_path, capsys):
    """Kills: the UnicodeEncodeError measured on a real Tableau formula containing U+25B2."""
    path = write_slice(tmp_path, make_workbook([make_request("Group Sort")]))

    code, out = run([str(path), "--category", "model_object_parameter"], capsys)

    assert code == 0
    assert "# Members" in out


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


def test_viz_dedupes_remediation_text_within_a_category(tmp_path, capsys):
    """Kills: repeating identical fix text per item - the same bloat that buried `requests[]`."""
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


def test_default_view_surfaces_the_report_queue(tmp_path, capsys):
    """Kills: a model-only summary. `viz_fidelity`/`remediation_worklist` are even deeper in the file."""
    path = write_slice(tmp_path, _report_workbook())

    _, out = run([str(path)], capsys)

    assert "REPORT:" in out
    assert "EMPTIED" in out
    assert "--viz" in out


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


def test_name_lookup_returns_one_calc_with_its_guidance(tmp_path, capsys):
    path = write_slice(tmp_path, make_workbook([make_request("Selected Measure"), make_request("Other")]))

    _, out = run([str(path), "--name", "Selected Measure"], capsys)

    assert "Selected Measure" in out
    assert "[1/1]" in out
    assert GUIDANCE in out
    assert "] Other" not in out


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


def test_unknown_category_lists_what_is_actually_present(tmp_path, capsys):
    """Kills: an empty response that reads as 'nothing to do here'."""
    path = write_slice(tmp_path, make_workbook([make_request("A")]))

    _, out = run([str(path), "--category", "not_a_category"], capsys)

    assert "No requests in category" in out
    assert "model_object_parameter (1)" in out
