"""Tests for scripts/check_stub_measures.py - the `= BLANK()` stub census from issue #257.

Every test names the mutation it kills, because this gate's failure mode is not "it goes red for
nothing" - it is "it prints a confident percentage that is wrong, and that percentage gets quoted in
a status report". The three that matter most:

* `test_authored_dax_that_merely_contains_blank_is_not_a_stub` - the defect that motivated the
  issue. An ad-hoc sweep reported ACMU `Selected Measure` as still stubbed when it was not, because
  it asked "does BLANK() appear" rather than "is the whole expression BLANK()".
* `test_multi_line_authored_expression_is_not_a_stub` - the same false positive one line lower down.
  Reading only the declaration line sees `measure X =` (empty), and reading only the NEXT line of a
  block sees a bare `BLANK()` inside a longer authored expression.
* `test_skipped_never_reports_a_ratio` - a census that prints `0/0 (0%)` for a model it never opened
  is worse than one that refuses to answer.

No network, no Power BI Desktop, no engine: every fixture is written into `tmp_path`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_stub_measures as csm  # noqa: E402  # pylint: disable=wrong-import-position

# One engine-shaped stub (formula preserved), one engine-shaped stub whose formula did NOT survive,
# and authored DAX that legitimately mentions BLANK(). Indentation is tabs, as the engine emits.
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


def _write_model(
    root: Path,
    tables: dict[str, str],
    *,
    model_name: str = "Book.SemanticModel",
    under_pbip: bool = False,
) -> Path:
    """Write a `.SemanticModel` folder and return the path the CLI should be pointed at."""
    base = root / "pbip" / "Book" if under_pbip else root
    model = base / model_name
    (model / "definition" / "tables").mkdir(parents=True)
    for table, text in tables.items():
        (model / "definition" / "tables" / f"{table}.tmdl").write_text(text, encoding="utf-8")
    return model


def _names(report: dict, *, actionable: bool | None = None) -> set[str]:
    """Every stub name in a merged report, optionally filtered by disposition."""
    return {
        finding["name"]
        for model in report["models"]
        for finding in model["findings"]
        if actionable is None or finding["actionable"] is actionable
    }


# --------------------------------------------------------------------------------------------
# The detection rule itself
# --------------------------------------------------------------------------------------------


def test_bare_blank_is_a_stub():
    """Kills: a rule that never matches anything (the gate that is always green)."""
    assert csm.is_stub_expression("BLANK()") is True


@pytest.mark.parametrize(
    "expr",
    [
        "IF(ISBLANK([Sales]), BLANK(), [Sales])",
        "DIVIDE([a], [b], BLANK())",
        "IF([x] > 0, [x], BLANK())",
        "VAR _v = BLANK() RETURN IF([x], _v, [x])",
        "COALESCE([a], BLANK())",
    ],
)
def test_authored_dax_that_merely_contains_blank_is_not_a_stub(expr):
    """THE test from issue #257. Kills: any substring/`in` test for `BLANK()`.

    A measure reported as stubbed when it is authored is the failure that makes this tool worse
    than nothing, because the number it prints is quoted downstream.
    """
    assert csm.is_stub_expression(expr) is False


@pytest.mark.parametrize("expr", ["BLANK ( )", "blank()", "Blank(  )", "BLANK\t()", "  BLANK()  "])
def test_spacing_and_case_variants_are_stubs(expr):
    """Kills: an exact `== "BLANK()"` string compare, or a case-sensitive match."""
    assert csm.is_stub_expression(expr) is True


def test_redundant_outer_parens_are_still_a_stub():
    """Kills: an anchored match that never normalises `(BLANK())`."""
    assert csm.is_stub_expression("(BLANK())") is True
    assert csm.is_stub_expression("((BLANK()))") is True


def test_parenthesised_authored_dax_is_not_a_stub():
    """Kills: a paren stripper that chews `(a) + (BLANK())` down to its last term."""
    assert csm.is_stub_expression("([a]) + (BLANK())") is False


@pytest.mark.parametrize(
    "expr",
    [
        "BLANK() // engine could not translate this LOD",
        "BLANK() -- engine could not translate this LOD",
        "/* untranslated */ BLANK()",
        "// leading note\nBLANK()",
        "BLANK()\n// trailing note",
    ],
)
def test_comments_around_a_stub_are_stripped(expr):
    """Kills: a rule that compares the raw text, so any annotation-by-comment hides the stub."""
    assert csm.is_stub_expression(expr) is True


def test_a_comment_inside_a_string_literal_is_not_a_comment():
    """Kills: a naive `split("//")` comment stripper.

    `"//"` is a legal DAX string; treating it as a comment truncates real authored DAX, which is
    the same class of error as the substring match - just in the opposite direction.
    """
    assert csm.is_stub_expression('IF([u] = "//host", [u], BLANK())') is False
    assert csm.is_stub_expression('"-- not a comment" & BLANK()') is False


def test_an_empty_expression_is_not_a_stub():
    """Kills: `not expr` folded into the stub test - an unparsed expression would count as a stub."""
    assert csm.is_stub_expression("") is False
    assert csm.is_stub_expression("   ") is False


def test_blank_with_an_argument_is_not_a_stub():
    """Kills: a `BLANK\\s*\\(` prefix match that ignores what is inside the call."""
    assert csm.is_stub_expression("BLANKS([Sales])") is False
    assert csm.is_stub_expression("BLANK() + 1") is False


# --------------------------------------------------------------------------------------------
# Reading the expression out of TMDL - the three serialisation forms
# --------------------------------------------------------------------------------------------


def test_multi_line_authored_expression_is_not_a_stub(tmp_path):
    """Kills: a line-at-a-time scan.

    Line 1 of this measure is `measure X =` (looks empty), and one of its body lines is a bare
    `BLANK()` (looks like a stub). Only reading the whole indented block gets it right.
    """
    tmdl = (
        "table Sales\n"
        "\n"
        "\tmeasure 'Guarded Rate' =\n"
        "\t\t\tIF(\n"
        "\t\t\t\tISBLANK([Denominator]),\n"
        "\t\t\t\tBLANK(),\n"
        "\t\t\t\tDIVIDE([Numerator], [Denominator])\n"
        "\t\t\t)\n"
        "\t\tformatString: 0.0%\n"
        "\n"
        "\tmeasure 'Next One' = BLANK()\n"
    )
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert report["measures"] == 2
    assert _names(report) == {"Next One"}


def test_a_multi_line_block_holding_only_blank_is_a_stub(tmp_path):
    """Kills: reading only the declaration line, which sees an empty expression and moves on."""
    tmdl = "table Sales\n\n\tmeasure 'Block Stub' =\n\t\t\tBLANK()\n\t\tformatString: 0\n"
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert _names(report) == {"Block Stub"}


def test_a_fenced_expression_block_is_read_whole(tmp_path):
    """Kills: an indentation-only block reader.

    TMDL's third form encloses the expression in three backticks (Microsoft Learn, TMDL overview:
    "To enforce a different indentation ... use the three backticks enclosing").
    """
    tmdl = (
        "table Sales\n"
        "\n"
        "\tmeasure 'Fenced Stub' = ```\n"
        "\t\t\tBLANK()\n"
        "\t\t\t```\n"
        "\t\tformatString: 0\n"
        "\n"
        "\tmeasure 'Fenced Authored' = ```\n"
        "IF(ISBLANK([a]), BLANK(), [a])\n"
        "\t\t\t```\n"
    )
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert report["measures"] == 2
    assert _names(report) == {"Fenced Stub"}


def test_properties_after_a_block_are_not_swallowed(tmp_path):
    """Kills: a block reader that runs to the next declaration, eating `formatString`/annotations.

    If the properties are swallowed into the expression, the TableauFormula annotation disappears
    with them and an actionable stub is misfiled as a dead end - the split silently inverts.
    """
    tmdl = (
        "table Sales\n"
        "\n"
        "\tmeasure 'Block Stub' =\n"
        "\t\t\tBLANK()\n"
        "\t\tformatString: 0\n"
        "\t\tannotation TableauFormula = SUM([Amount])\n"
        "\n"
        "\tmeasure 'Plain' = SUM(Sales[Amount])\n"
    )
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert report["measures"] == 2
    assert _names(report, actionable=True) == {"Block Stub"}


def test_a_doubled_apostrophe_name_survives(tmp_path):
    """Kills: a naive `'[^']*'` name regex.

    `examples/broadway-stage-to-screen` really does ship `column 'Sondheim''s Work'`; truncating the
    name there would mis-key the work queue an operator has to act on.
    """
    tmdl = "table Sales\n\n\tmeasure 'Sondheim''s Rate' = BLANK()\n"
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert _names(report) == {"Sondheim's Rate"}


def test_a_quoted_name_containing_an_equals_sign_survives(tmp_path):
    """Kills: splitting the declaration on the first `=`."""
    tmdl = "table Sales\n\n\tmeasure 'A = B' = BLANK()\n"
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert _names(report) == {"A = B"}


def test_a_plain_data_column_is_not_in_any_denominator(tmp_path):
    """Kills: counting every `column` line, which inflates the denominator and deflates the ratio."""
    tmdl = (
        "table Sales\n"
        "\n"
        "\tcolumn Amount\n"
        "\t\tdataType: double\n"
        "\t\tsummarizeBy: sum\n"
        "\n"
        "\tcolumn 'Stub Column' = BLANK()\n"
        "\t\tannotation TableauFormula = IIF([x], 1, 0)\n"
        "\n"
        "\tmeasure 'Total' = SUM(Sales[Amount])\n"
    )
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert report["measures"] == 1
    assert report["calculated_columns"] == 1
    assert report["measure_stubs"] == 0
    assert report["column_stubs"] == 1


def test_a_word_inside_dax_is_not_a_declaration(tmp_path):
    """Kills: matching `measure`/`column` anywhere rather than at a declaration's own indent."""
    tmdl = (
        "table Sales\n"
        "\n"
        "\tmeasure 'Has Var' =\n"
        "\t\t\tVAR _measure Phantom = 1\n"
        "\t\t\tRETURN [Sales]\n"
        "\n"
        "\tmeasure 'Real Stub' = BLANK()\n"
    )
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert report["measures"] == 2
    assert _names(report) == {"Real Stub"}


# --------------------------------------------------------------------------------------------
# The actionable / dead-end split - the point of the tool
# --------------------------------------------------------------------------------------------


def test_actionable_and_dead_end_are_split_by_the_preserved_formula(tmp_path):
    """Kills: reporting one total. The split is what turns a number into a work queue."""
    report = csm.scan(_write_model(tmp_path, {"_Measures": MEASURES_TMDL}))
    assert report["measure_stubs"] == 2
    assert _names(report, actionable=True) == {"Stub With Formula"}
    assert _names(report, actionable=False) == {"Stub Without Formula"}
    assert report["actionable"] == 1
    assert report["dead_end"] == 1


def test_the_preserved_formula_text_is_carried_into_the_finding(tmp_path):
    """Kills: recording only a boolean. Without the text the "work queue" needs a second tool."""
    report = csm.scan(_write_model(tmp_path, {"_Measures": MEASURES_TMDL}))
    finding = next(f for m in report["models"] for f in m["findings"] if f["name"] == "Stub With Formula")
    assert finding["tableau_formula"] == 'CASE [Parameters].[Sort] WHEN "A" THEN 1 END'
    assert finding["table"] == "_Measures"
    assert finding["kind"] == "measure"
    assert finding["line"] == 3


def test_an_empty_annotation_value_is_a_dead_end(tmp_path):
    """Kills: `"TableauFormula" in annotations` as the actionable test.

    The engine elides an empty annotation rather than writing `annotation X = ` (invalid TMDL), so
    a present-but-empty value means the formula did not survive.
    """
    tmdl = "table Sales\n\n\tmeasure 'Empty Formula' = BLANK()\n\t\tannotation TableauFormula =\n"
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    assert _names(report, actionable=False) == {"Empty Formula"}


def test_an_engine_translation_suggestion_is_surfaced(tmp_path):
    """Kills: dropping `TranslationSuggestion`, the cheapest stubs in the queue to clear."""
    tmdl = (
        "table Sales\n"
        "\n"
        "\tmeasure 'Suggested' = BLANK()\n"
        "\t\tannotation TableauFormula = WINDOW_SUM(SUM([Amount]))\n"
        "\t\tannotation TranslationSuggestion = CALCULATE(SUM(Sales[Amount]), ALLSELECTED())\n"
    )
    report = csm.scan(_write_model(tmp_path, {"Sales": tmdl}))
    finding = report["models"][0]["findings"][0]
    assert finding["suggestion"] == "CALCULATE(SUM(Sales[Amount]), ALLSELECTED())"
    assert report["suggested"] == 1


# --------------------------------------------------------------------------------------------
# Ratios, rendering and scope
# --------------------------------------------------------------------------------------------


def test_ratios_are_reported_per_table_and_model_wide(tmp_path):
    """Kills: a model-wide total only. A 75%-stubbed table hides inside a 20%-stubbed model."""
    hot = "table Hot\n" + "".join(f"\n\tmeasure 'S{i}' = BLANK()\n" for i in range(3))
    cold = "table Cold\n" + "".join(f"\n\tmeasure 'A{i}' = SUM(Cold[x])\n" for i in range(9))
    report = csm.scan(_write_model(tmp_path, {"Hot": hot, "Cold": cold}))
    assert report["measures"] == 12
    assert report["measure_stubs"] == 3
    tables = {t["table"]: t for t in report["models"][0]["tables"]}
    assert (tables["Hot"]["stubs"], tables["Hot"]["measures"]) == (3, 3)
    assert (tables["Cold"]["stubs"], tables["Cold"]["measures"]) == (0, 9)
    assert "3/12 (25%)" in csm.render(report)
    assert "3/3 (100%)" in csm.render(report)


def test_the_customer_ratio_shape_is_reproduced():
    """Kills: a different rounding or format. `64/89 (72%)` is the shape already in field use."""
    assert csm.ratio(64, 89) == "64/89 (72%)"
    assert csm.ratio(0, 10) == "0/10 (0%)"
    assert csm.ratio(0, 0) == "0/0 (n/a)"


def test_a_clean_model_is_ok_and_exits_zero(tmp_path, capsys):
    """Kills: a status that is STUBS whenever the scan runs."""
    model = _write_model(tmp_path, {"Sales": "table Sales\n\n\tmeasure 'Total' = SUM(Sales[x])\n"})
    assert csm.main([str(model)]) == csm.EXIT_OK
    out = capsys.readouterr().out
    assert "STUB MEASURE CHECK: OK" in out


def test_finding_stubs_still_exits_zero(tmp_path, capsys):
    """Kills: exiting non-zero on every in-progress model.

    Stubs are the EXPECTED state mid-migration. A census that fails the build for existing at all
    gets muted, and a muted tool reports nothing.
    """
    model = _write_model(tmp_path, {"_Measures": MEASURES_TMDL})
    assert csm.main([str(model)]) == csm.EXIT_OK
    assert "STUB MEASURE CHECK: STUBS" in capsys.readouterr().out


def test_strict_turns_the_census_into_a_gate(tmp_path):
    """Kills: a `--strict` flag that is parsed and then ignored."""
    model = _write_model(tmp_path, {"_Measures": MEASURES_TMDL})
    assert csm.main([str(model), "--strict", "--quiet"]) == csm.EXIT_STRICT


def test_strict_on_a_clean_model_still_exits_zero(tmp_path):
    """Kills: a `--strict` that trips on the scan rather than on the findings."""
    model = _write_model(tmp_path, {"Sales": "table Sales\n\n\tmeasure 'Total' = SUM(Sales[x])\n"})
    assert csm.main([str(model), "--strict", "--quiet"]) == csm.EXIT_OK


def test_skipped_never_reports_a_ratio(tmp_path, capsys):
    """Kills: printing `0/0 (0%)` (or `OK`) for a folder that held no model at all.

    Same review finding as `check_field_bindings.check_pair`: an affirmative verdict must mean
    something was actually measured.
    """
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert csm.main([str(empty)]) == csm.EXIT_SKIPPED
    out = capsys.readouterr().out
    assert "SKIPPED" in out
    assert "(0%)" not in out
    assert "OK" not in out


def test_a_model_with_no_measures_is_reported_as_such(tmp_path, capsys):
    """Kills: dividing by zero, or printing a healthy-looking `0/0 (0%)` for a measureless model."""
    model = _write_model(tmp_path, {"Sales": "table Sales\n\n\tcolumn Amount\n\t\tdataType: double\n"})
    assert csm.main([str(model)]) == csm.EXIT_SKIPPED
    assert "(0%)" not in capsys.readouterr().out


def test_a_missing_path_is_a_usage_error_not_a_verdict(tmp_path):
    """Kills: rglob over a nonexistent folder quietly reporting "no stubs"."""
    with pytest.raises(SystemExit) as exc:
        csm.main([str(tmp_path / "does-not-exist")])
    assert exc.value.code == csm.EXIT_USAGE


def test_a_bundle_is_scanned_through_pbip_only(tmp_path):
    """Kills: counting the engine's reference-only `semantic_models/` baseline as well.

    `<bundle>/reports/` and `<bundle>/semantic_models/` are never edited; only `pbip/` ships. Double
    counting would halve the reported completion of every bundle.
    """
    _write_model(tmp_path, {"_Measures": MEASURES_TMDL}, under_pbip=True)
    truth = tmp_path / "semantic_models" / "Book.SemanticModel" / "definition" / "tables"
    truth.mkdir(parents=True)
    (truth / "_Measures.tmdl").write_text(MEASURES_TMDL, encoding="utf-8")
    report = csm.scan(tmp_path)
    assert report["models_scanned"] == 1
    assert report["measure_stubs"] == 2


def test_several_models_are_merged_and_ranked(tmp_path):
    """Kills: reporting only the first model, or losing the per-model breakdown in the merge."""
    _write_model(tmp_path / "a", {"_Measures": MEASURES_TMDL}, model_name="A.SemanticModel")
    _write_model(tmp_path / "b", {"Sales": "table Sales\n\n\tmeasure 'Total' = SUM(Sales[x])\n"}, model_name="B.SemanticModel")
    report = csm.scan(tmp_path)
    assert report["models_scanned"] == 2
    assert [m["model"] for m in report["models"]] == ["A.SemanticModel", "B.SemanticModel"]
    assert report["measures"] == 5


def test_json_carries_the_whole_census(tmp_path):
    """Kills: a `--json` that writes only the headline, so no queue can be built from it."""
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


def test_the_rendered_verdict_names_the_dead_ends(tmp_path):
    """Kills: rendering totals only. The dead-end list IS the escalation list."""
    text = csm.render(csm.scan(_write_model(tmp_path, {"_Measures": MEASURES_TMDL})))
    assert "Stub Without Formula" in text
    assert "DEAD END" in text


def test_a_models_ratios_reconcile_with_its_split(tmp_path):
    """Kills: printing the measure ratio beside a split that also counts calculated columns.

    Measured on the real 38-workbook bundle: `Admin_Insights_Starter 27/51 -> actionable 60` reads
    as broken arithmetic. The missing 33 were stubbed calculated columns, invisible on that line.
    """
    tmdl = (
        "table Sales\n"
        "\n"
        "\tmeasure 'M Stub' = BLANK()\n"
        "\t\tannotation TableauFormula = SUM([x])\n"
        "\n"
        "\tcolumn 'C Stub' = BLANK()\n"
        "\t\tannotation TableauFormula = IIF([x], 1, 0)\n"
        "\n"
        "\tcolumn 'C Fine' = [x] * 2\n"
    )
    text = csm.render(csm.scan(_write_model(tmp_path, {"Sales": tmdl})))
    assert "measures 1/1 (100%), calc columns 1/2 (50%)" in text
    assert "actionable 2, dead end 0" in text


def test_two_models_sharing_a_name_are_disambiguated(tmp_path):
    """Kills: rendering two different models under one identical label.

    Real shape from the 38-workbook bundle: two `Meridian Sales (Live Snowflake).SemanticModel`
    under different workbook folders, with different ratios.
    """
    for owner in ("wb_a", "wb_b"):
        _write_model(tmp_path / owner, {"_Measures": MEASURES_TMDL}, model_name="Shared.SemanticModel")
    text = csm.render(csm.scan(tmp_path))
    assert "wb_a/Shared.SemanticModel" in text
    assert "wb_b/Shared.SemanticModel" in text


def test_a_uniquely_named_model_keeps_its_plain_name(tmp_path):
    """Kills: prefixing every label with a folder, which is noise on the common single-model case."""
    text = csm.render(csm.scan(_write_model(tmp_path, {"_Measures": MEASURES_TMDL})))
    assert "  Book.SemanticModel  " in text


def test_verbose_lists_the_actionable_queue(tmp_path, capsys):
    """Kills: a `--verbose` that changes nothing, leaving the work queue JSON-only."""
    model = _write_model(tmp_path, {"_Measures": MEASURES_TMDL})
    assert csm.main([str(model), "--verbose"]) == csm.EXIT_OK
    assert "Stub With Formula" in capsys.readouterr().out


def test_quiet_prints_nothing(tmp_path, capsys):
    """Kills: a `--quiet` that still prints, which corrupts a caller parsing stdout."""
    model = _write_model(tmp_path, {"_Measures": MEASURES_TMDL})
    csm.main([str(model), "--quiet"])
    assert capsys.readouterr().out == ""


def test_an_unreadable_tmdl_file_does_not_abort_the_scan(tmp_path):
    """Kills: letting a decode error escape. One bad file must not take a whole estate sweep down."""
    model = _write_model(tmp_path, {"_Measures": MEASURES_TMDL})
    (model / "definition" / "tables" / "Broken.tmdl").write_bytes(b"table Broken\n\n\tmeasure 'X' = \xff\xfe()\n")
    report = csm.scan(model)
    assert report["measure_stubs"] == 2
