"""Tests for scripts/check_stub_measures.py, the `= BLANK()` stub census (#257)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_stub_measures as csm  # noqa: E402  # pylint: disable=wrong-import-position

# Engine-shaped stubs with and without a preserved formula, plus authored DAX that mentions BLANK().
MEASURES_TMDL = """table _Measures

\tmeasure 'Stub With Formula' = BLANK()
\t\tlineageTag: 1c384e71-b155-5ffb-ba1c-8dc53914ce1d
\t\tannotation TableauFormula = CASE [Parameters].[Sort] WHEN "A" THEN 1 END
\t\tannotation SummarizationSetBy = Automatic

\tmeasure 'Stub Without Formula' = BLANK()
\t\tlineageTag: 2c384e71-b155-5ffb-ba1c-8dc53914ce1d
\t\tannotation SummarizationSetBy = Automatic

\tmeasure 'Selected Measure' = IF(ISBLANK([Sales]), BLANK(), [Sales])
\t\tlineageTag: 3c384e71-b155-5ffb-ba1c-8dc53914ce1d
\t\tannotation TableauFormula = ZN([Sales])
\t\tannotation TranslatedBy = deterministic

\tmeasure 'Safe Divide' = DIVIDE([a], [b], BLANK())
\t\tannotation TranslatedBy = deterministic
"""
CLEAN_TMDL = "table Sales\n\n\tmeasure 'Total' = SUM(Sales[x])\n"


def _write_model(root: Path, tables: dict[str, str], *, model_name: str = "Book.SemanticModel") -> Path:
    model = root / model_name
    (model / "definition" / "tables").mkdir(parents=True)
    for table, text in tables.items():
        (model / "definition" / "tables" / f"{table}.tmdl").write_text(text, encoding="utf-8")
    return model


def _names(report: dict, *, actionable: bool | None = None) -> set[str]:
    return {
        finding["name"]
        for model in report["models"]
        for finding in model["findings"]
        if actionable is None or finding["actionable"] is actionable
    }


@pytest.mark.parametrize(
    ("expr", "stub"),
    [
        ("BLANK()", True),
        ("  blank ( )  ", True),
        ("((BLANK()))", True),
        ("BLANK() // engine could not translate", True),
        ("BLANK() -- engine could not translate", True),
        ("/* untranslated */ BLANK()", True),
        ("IF(ISBLANK([Sales]), BLANK(), [Sales])", False),  # #257: containing BLANK() is not a stub
        ("([a]) + (BLANK())", False),
        ('IF([u] = "//host", [u], BLANK())', False),
        ("", False),
        ("BLANKS([Sales])", False),
        ("BLANK() + 1", False),
    ],
)
def test_only_a_whole_expression_blank_call_is_a_stub(expr: str, stub: bool) -> None:
    assert csm.is_stub_expression(expr) is stub


def test_public_helpers_respect_string_literals_and_paren_balance() -> None:
    """Unreachable at verdict level, but documented contracts of public helpers."""
    assert csm.strip_comments('"//x" & [a]') == '"//x" & [a]'
    assert csm.strip_comments('"a""--b" & [c]') == '"a""--b" & [c]'
    assert csm.strip_outer_parens("(a) + (b)") == "(a) + (b)"
    assert csm.strip_outer_parens("((BLANK()))") == "BLANK()"


def test_tmdl_reader_handles_every_serialisation_form(tmp_path: Path) -> None:
    """Multi-line, fenced and quoted declarations; properties after a block are not swallowed."""
    tmdl = (
        "table Sales\n\n"
        "\tcolumn Amount\n\t\tdataType: double\n\n"
        "\tmeasure 'Guarded Rate' =\n\t\t\tIF(\n\t\t\t\tISBLANK([D]),\n\t\t\t\tBLANK(),\n\t\t\t\t[N]\n\t\t\t)\n"
        "\t\tformatString: 0.0%\n\n"
        "\tmeasure 'Block Stub' =\n\t\t\tBLANK()\n\t\tformatString: 0\n"
        "\t\tannotation TableauFormula = SUM([Amount])\n\n"
        "\tmeasure 'Fenced Stub' = ```\n\t\t\tBLANK()\n\t\t\t```\n\n"
        "\tmeasure 'Fenced Authored' = ```\nIF(ISBLANK([a]), BLANK(), [a])\n\t\t\t```\n\n"
        "\tmeasure 'Has Var' =\n\t\t\tVAR _measure Phantom = 1\n\t\t\tRETURN [Sales]\n\n"
        "\tmeasure 'Sondheim''s Rate' = BLANK()\n\n"
        "\tmeasure 'A = B' = BLANK()\n\n"
        "\tcolumn 'Stub Column' = BLANK()\n\t\tannotation TableauFormula = IIF([x], 1, 0)\n"
    )
    model = _write_model(tmp_path, {"Sales": tmdl})
    (model / "definition" / "tables" / "Broken.tmdl").write_bytes(b"table Broken\n\n\tmeasure 'X' = \xff\xfe()\n")

    report = csm.scan(model)

    assert report["measures"] == 8
    assert report["calculated_columns"] == 1
    assert report["column_stubs"] == 1
    assert _names(report) == {"Block Stub", "Fenced Stub", "Sondheim's Rate", "A = B", "Stub Column"}
    assert _names(report, actionable=True) == {"Block Stub", "Stub Column"}


def test_findings_split_actionable_from_dead_end_and_carry_the_formula(tmp_path: Path) -> None:
    tmdl = MEASURES_TMDL + (
        "\n\tmeasure 'Empty Formula' = BLANK()\n\t\tannotation TableauFormula =\n"
        "\n\tmeasure 'Suggested' = BLANK()\n\t\tannotation TableauFormula = WINDOW_SUM(SUM([Amount]))\n"
        "\t\tannotation TranslationSuggestion = CALCULATE(SUM(Sales[Amount]), ALLSELECTED())\n"
    )
    report = csm.scan(_write_model(tmp_path, {"_Measures": tmdl}))
    findings = {f["name"]: f for m in report["models"] for f in m["findings"]}

    assert _names(report, actionable=True) == {"Stub With Formula", "Suggested"}
    assert _names(report, actionable=False) == {"Stub Without Formula", "Empty Formula"}
    assert (report["actionable"], report["dead_end"], report["suggested"]) == (2, 2, 1)
    assert findings["Stub With Formula"]["tableau_formula"] == 'CASE [Parameters].[Sort] WHEN "A" THEN 1 END'
    assert (findings["Stub With Formula"]["table"], findings["Stub With Formula"]["kind"]) == ("_Measures", "measure")
    assert findings["Stub With Formula"]["line"] == 3
    assert findings["Suggested"]["suggestion"] == "CALCULATE(SUM(Sales[Amount]), ALLSELECTED())"


def test_render_reports_per_table_ratios_reconciled_split_and_dead_ends(tmp_path: Path) -> None:
    hot = "table Hot\n" + "".join(f"\n\tmeasure 'S{i}' = BLANK()\n" for i in range(3))
    cold = "table Cold\n" + "".join(f"\n\tmeasure 'A{i}' = SUM(Cold[x])\n" for i in range(9))
    cols = "table Cols\n\n\tcolumn 'C Stub' = BLANK()\n\t\tannotation TableauFormula = IIF([x], 1, 0)\n\n"
    cols += "\tcolumn 'C Fine' = [x] * 2\n"
    report = csm.scan(_write_model(tmp_path, {"Hot": hot, "Cold": cold, "Cols": cols}))
    tables = {t["table"]: t for t in report["models"][0]["tables"]}
    text = csm.render(report)

    assert (tables["Hot"]["stubs"], tables["Hot"]["measures"]) == (3, 3)
    assert (tables["Cold"]["stubs"], tables["Cold"]["measures"]) == (0, 9)
    assert "3/12 (25%)" in text and "3/3 (100%)" in text
    assert "measures 3/12 (25%), calc columns 1/2 (50%)" in text
    assert "actionable 1, dead end 3" in text
    assert "DEAD END" in text and "S0" in text
    assert "  Book.SemanticModel  " in text
    assert csm.ratio(64, 89) == "64/89 (72%)"
    assert csm.ratio(0, 0) == "0/0 (n/a)"
    actionable_only = csm.render(csm.scan(_write_model(tmp_path / "actionable", {"Cols": cols})))
    assert "dead end 0" in actionable_only and "DEAD END (no" not in actionable_only


def test_bundles_scan_pbip_only_and_merge_disambiguated_models(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _write_model(bundle / "pbip" / "Book", {"_Measures": MEASURES_TMDL})
    _write_model(bundle / "semantic_models", {"_Measures": MEASURES_TMDL})
    assert (csm.scan(bundle)["models_scanned"], csm.scan(bundle)["measure_stubs"]) == (1, 2)

    estate = tmp_path / "estate"
    for owner in ("wb_a", "wb_b"):
        _write_model(estate / owner, {"_Measures": MEASURES_TMDL}, model_name="Shared.SemanticModel")
    _write_model(estate / "wb_c", {"Sales": CLEAN_TMDL}, model_name="C.SemanticModel")
    report = csm.scan(estate)
    text = csm.render(report)

    assert report["models_scanned"] == 3
    assert report["measures"] == 9
    assert "wb_a/Shared.SemanticModel" in text and "wb_b/Shared.SemanticModel" in text


@pytest.mark.parametrize(
    ("tables", "args", "code", "out"),
    [
        ({"Sales": CLEAN_TMDL}, [], csm.EXIT_OK, "STUB MEASURE CHECK: OK"),
        ({"_Measures": MEASURES_TMDL}, [], csm.EXIT_OK, "STUB MEASURE CHECK: STUBS"),  # census, not a gate
        ({"_Measures": MEASURES_TMDL}, ["--verbose"], csm.EXIT_OK, "Stub With Formula"),
        ({"_Measures": MEASURES_TMDL}, ["--strict", "--quiet"], csm.EXIT_STRICT, ""),
        ({"Sales": CLEAN_TMDL}, ["--strict", "--quiet"], csm.EXIT_OK, ""),
        ({"Sales": "table Sales\n\n\tcolumn Amount\n\t\tdataType: double\n"}, [], csm.EXIT_SKIPPED, "SKIPPED"),
        (None, [], csm.EXIT_SKIPPED, "SKIPPED"),
    ],
)
def test_cli_exit_codes_and_output(tmp_path: Path, capsys, tables, args, code, out) -> None:
    if tables is None:
        target = tmp_path / "nothing"
        target.mkdir()
    else:
        target = _write_model(tmp_path, tables)
    assert csm.main([str(target), *args]) == code
    printed = capsys.readouterr().out
    assert out in printed
    if "--quiet" in args:
        assert printed == ""
    if code == csm.EXIT_SKIPPED:
        assert "(0%)" not in printed and "OK" not in printed


def test_cli_json_carries_the_census_and_missing_path_is_usage(tmp_path: Path) -> None:
    model = _write_model(tmp_path, {"_Measures": MEASURES_TMDL})
    out = tmp_path / "census.json"
    assert csm.main([str(model), "--json", str(out), "--quiet"]) == csm.EXIT_OK
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "STUBS"
    assert payload["stub_ratio"] == pytest.approx(0.5)
    assert {f["name"] for m in payload["models"] for f in m["findings"]} == {
        "Stub With Formula",
        "Stub Without Formula",
    }

    with pytest.raises(SystemExit) as exc:
        csm.main([str(tmp_path / "does-not-exist")])
    assert exc.value.code == csm.EXIT_USAGE
