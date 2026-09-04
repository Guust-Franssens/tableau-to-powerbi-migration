"""Tests for scripts/check_datamodel.py.

These exist because Power BI Desktop reports a broken model as an unlocalised
`M Engine error: 'Microsoft.Data.Mashup.Preview; Token ',' expected.'` - no file, no line, no
expression - which a real user hit repeatedly and could not act on. The checker's whole value is
turning that into `file:line:col`, so it has to be both accurate AND quiet: a checker that cries
wolf on good models gets ignored, which is worse than not having one.
"""

# pylint: disable=import-error,wrong-import-position,missing-function-docstring,use-implicit-booleaness-not-comparison

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
from check_datamodel import (
    ADVISORY_TMDL_CODES,
    _check_expression,
    _iter_m_blocks,
    check_datamodel,
    check_model,
    check_model_counted,
    check_tmdl_model,
    check_tmdl_text,
    find_compact_filters,
    find_incomplete_divide_calls,
    find_unguarded_divide_thresholds,
    main,
)

DUMMY = Path("model.tmdl")
TMDL_DUMMY = Path("Table.tmdl")


def _kinds(m_expression: str) -> set[str]:
    return {f.kind for f in _check_expression(DUMMY, m_expression)}


def _tmdl_codes(text: str) -> set[str]:
    return {f.code for f in check_tmdl_text(TMDL_DUMMY, text)}


GOOD_M = [
    'let Source = #table({"A", "B"}, {{1, 2}}) in Source',
    'let S = Table.SelectRows(T, each [Country Name] <> "Greenland") in S',  # field access with a space
    'let S = Table.TransformColumnTypes(T, {{"A", type text}, {"B", Int64.Type}}, "en-US") in S',
    'let S = Table.AddColumn(T, "Q", each "Q" & Text.From([Quarter Number]), type text) in S',
    '"a plain text parameter" meta [IsParameterQuery=true, Type="Text"]',
    "let x = if a then b else c, y = try x otherwise null in y",
    'let S = Csv.Document(File.Contents(DataFolder & "orders.csv"), [Delimiter=",", Encoding=65001]) in S',
]


@pytest.mark.parametrize("expression", GOOD_M)
def test_valid_m_produces_no_findings(expression: str) -> None:
    """Zero false positives is the hard requirement - a noisy checker gets switched off."""
    assert _check_expression(DUMMY, expression) == []


def test_trailing_comma_is_caught() -> None:
    """The classic JSON habit, and the most likely single cause of "Token ',' expected"."""
    assert "TRAILING_COMMA" in _kinds('let S = #table({"A"}, {{1}, {2},}) in S')


def test_missing_comma_between_list_values_is_caught() -> None:
    assert "MISSING_SEPARATOR" in _kinds('let S = #table({"A" "B"}, {{1, 2}}) in S')


@pytest.mark.parametrize(
    "expression",
    [
        'let S = Table.TransformColumnTypes(T, {{"A", type text}} in S',  # unclosed (
        'let S = #table({"A"}, {{1}}) in S)',  # stray )
        'let S = Record.Field(r, "a"} in S',  # mismatched pair
    ],
)
def test_unbalanced_delimiters_are_caught(expression: str) -> None:
    assert "UNBALANCED" in _kinds(expression)


def test_let_without_in_is_caught() -> None:
    assert "LET_WITHOUT_IN" in _kinds('let Source = #table({"A"}, {{1}})')


@pytest.mark.parametrize(
    "expression",
    ['let S = "unterminated in S', "let S = 1 /* unterminated in S"],
)
def test_unterminated_string_or_comment_is_caught(expression: str) -> None:
    assert "UNTERMINATED" in _kinds(expression)


def test_escaped_quotes_do_not_break_scanning() -> None:
    """`""` is M's escape for a quote; mis-scanning it would make every later check nonsense."""
    assert _check_expression(DUMMY, 'let S = "he said ""hi""" in S') == []


def test_json_style_quote_escape_is_caught() -> None:
    assert "INVALID_STRING_ESCAPE" in _kinds('let Source = "a \\"quoted\\" value" in Source')


def test_json_style_quote_escape_before_punctuation_is_caught() -> None:
    assert "INVALID_STRING_ESCAPE" in _kinds('let Source = "Say \\"!\\"" in Source')


def test_json_style_quote_escape_before_keyword_prefix_is_caught() -> None:
    assert "INVALID_STRING_ESCAPE" in _kinds('let Source = "foo\\"inside\\" stuff" in Source')


def test_json_style_quote_escape_outside_a_string_is_caught() -> None:
    assert "INVALID_STRING_ESCAPE" in _kinds('let Source = "foo\\" & \\"bar" in Source')


def test_transform_column_types_extra_pair_braces_are_caught() -> None:
    assert "INVALID_TRANSFORM_COLUMN_TYPE_PAIR" in _kinds(
        'let Source = Table.TransformColumnTypes(T, {{{"Amount", type number}}}) in Source'
    )


def test_transform_column_types_multiple_pairs_in_extra_braces_are_caught() -> None:
    assert "INVALID_TRANSFORM_COLUMN_TYPE_PAIR" in _kinds(
        'let Source = Table.TransformColumnTypes(T, {{{"Amount", type number}, {"Count", Int64.Type}}}) in Source'
    )


def test_transform_column_types_variable_pairs_are_allowed() -> None:
    assert (
        _check_expression(
            DUMMY, 'let Types = {{"Amount", type number}}, Source = Table.TransformColumnTypes(T, Types) in Source'
        )
        == []
    )


def test_transform_column_types_variable_pair_entry_is_allowed() -> None:
    assert (
        _check_expression(
            DUMMY, 'let Pair = {"Amount", type number}, Source = Table.TransformColumnTypes(T, {Pair}) in Source'
        )
        == []
    )


def test_transform_column_types_empty_pair_list_is_allowed() -> None:
    assert _check_expression(DUMMY, "let Source = Table.TransformColumnTypes(T, {}) in Source") == []


def test_windows_path_before_otherwise_is_allowed() -> None:
    assert _check_expression(DUMMY, 'let Source = try "C:\\" otherwise "fallback" in Source') == []


def test_windows_path_before_null_coalescing_is_allowed() -> None:
    assert _check_expression(DUMMY, 'let Source = "C:\\" ?? "fallback" in Source') == []


def test_delimiters_inside_strings_and_comments_are_ignored() -> None:
    assert _check_expression(DUMMY, 'let S = "a { unclosed [ brace", // ) stray\n    T = 1 in T') == []


def test_calculated_partitions_are_dax_and_must_be_skipped(tmp_path: Path) -> None:
    """A `= calculated` partition holds DAX, not M.

    Reading those as M produced 64 false positives across the committed examples - a DAX table
    constructor is full of constructs M would reject.
    """
    tables = tmp_path / "Bad.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "Params.tmdl").write_text(
        "table Params\n"
        "\tpartition Params = calculated\n"
        "\t\tmode: import\n"
        "\t\tsource = {(\"Sales\", NAMEOF('Sales'[Amount]), 0), (\"Profit\", NAMEOF('Sales'[P]), 1)}\n",
        encoding="utf-8",
    )
    assert check_model(tmp_path / "Bad.SemanticModel") == []


def test_m_partitions_are_still_read(tmp_path: Path) -> None:
    """The `= calculated` skip must not accidentally silence real M partitions."""
    tables = tmp_path / "M.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "Sales.tmdl").write_text(
        "table Sales\n\tpartition Sales = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet S = {1, 2,} in S\n",
        encoding="utf-8",
    )
    findings = check_model(tmp_path / "M.SemanticModel")
    assert [f.kind for f in findings] == ["TRAILING_COMMA"]


def test_reported_line_points_at_the_real_line(tmp_path: Path) -> None:
    """The entire point is localisation - an off-by-N line number would waste the user's time."""
    tables = tmp_path / "L.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "T.tmdl").write_text(
        "table T\n"
        "\tpartition T = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        '\t\t\t\t\tSource = #table({"A"}, {{1},}) \n'
        "\t\t\t\tin\n"
        "\t\t\t\t\tSource\n",
        encoding="utf-8",
    )
    findings = check_model(tmp_path / "L.SemanticModel")
    assert len(findings) == 1
    assert findings[0].line == 6  # the line the trailing comma is actually on


def test_expressions_tmdl_is_checked(tmp_path: Path) -> None:
    definition = tmp_path / "E.SemanticModel" / "definition"
    definition.mkdir(parents=True)
    (definition / "expressions.tmdl").write_text(
        'expression DataFolder = "C:\\data\\" meta [IsParameterQuery=true, Type="Text"]\n',
        encoding="utf-8",
    )
    assert check_model(tmp_path / "E.SemanticModel") == []


def test_committed_example_models_are_clean() -> None:
    """All 16 shipped models must pass - they open in Desktop, so any finding is a false positive."""
    models = sorted((REPO_ROOT / "examples").glob("*/fabric/*.SemanticModel"))
    assert models, "no example models found"
    offenders = {str(m.name): [f.kind for f in check_model(m)] for m in models if check_model(m)}
    assert not offenders, f"false positives on known-good models: {offenders}"


@pytest.mark.parametrize(
    ("expression", "why"),
    [
        ("let mode = 1 in mode", "an M step named `mode` is not a TMDL metadata key"),
        ("let annotation = 1 in annotation", "same for `annotation`"),
        ("[let = 1]", "`let` is a legal generalized field name"),
        ("each [let value]", "field access may be named after a keyword"),
        ("type table [let = text]", "and inside a type record"),
        ("{try 1 catch () => 0}", "`catch` is valid M error handling"),
        ("{0x0}", "hexadecimal literals are valid M"),
    ],
)
def test_reported_false_positives_stay_fixed(expression: str, why: str) -> None:
    """Every false positive an adversarial review found, pinned as a regression test.

    Precision matters more than recall here: this runs in CI and inside an agent's build loop, so
    crying wolf on valid M gets the tool switched off, which loses the entire benefit.
    """
    assert _check_expression(DUMMY, expression) == [], why


def test_non_utf8_file_does_not_crash(tmp_path: Path) -> None:
    """A stray non-UTF8 byte used to raise an uncaught UnicodeDecodeError traceback."""
    tables = tmp_path / "B.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "bad.tmdl").write_bytes(b"table \x96\n")
    assert check_model(tmp_path / "B.SemanticModel") == []


def test_nothing_checked_is_not_reported_as_clean(tmp_path: Path) -> None:
    """ "No findings" and "nothing was scanned" must be distinguishable.

    Otherwise a missing or unreadable model reports a reassuring "M syntax OK" while proving nothing,
    and an agent takes that as a green light.
    """
    empty = tmp_path / "Empty.SemanticModel"
    (empty / "definition").mkdir(parents=True)
    findings, scanned = check_model_counted(empty)
    assert findings == []
    assert scanned == 0


def test_column_accounts_for_the_tmdl_prefix(tmp_path: Path) -> None:
    """An inline `source = ...` starts partway along the line; a short column misdirects the fix."""
    tables = tmp_path / "C.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    line = "\t\tsource = let S = {1,} in S"
    (tables / "T.tmdl").write_text(f"table T\n\tpartition T = m\n\t\tmode: import\n{line}\n", encoding="utf-8")
    findings = check_model(tmp_path / "C.SemanticModel")
    assert len(findings) == 1
    # the offending comma is the one right before '}' - locate it in the real line
    assert findings[0].col == line.index(",}") + 1


def test_block_extraction_stops_at_tmdl_metadata() -> None:
    """`annotation`/`lineageTag` lines are TMDL, not M - swallowing them would corrupt the scan."""
    path = REPO_ROOT / "examples" / "health-tracker" / "fabric"
    expressions = next(path.glob("*.SemanticModel/definition/expressions.tmdl"))
    blocks = _iter_m_blocks(expressions)
    assert blocks
    assert all("lineageTag" not in body and "annotation" not in body for body, _, _ in blocks)


GOOD_TMDL = [
    "table S\n\tmeasure 'A' = SUM('S'[x])\n\t\tformatString: 0.00\n\n\tcolumn x\n\t\tdataType: int64\n",
    "table S\n\tcolumn x\n\t\tdataType: int64\n\t\tannotation a = 1\n\t\tannotation b = 2\n",
    "table S\n\tmeasure 'A' = 1\n\t\tformatString: 0.00\n\n\tmeasure 'B' = 2\n\t\tformatString: 0.00\n",
    "table S\n\t/// formatString: not a property, this is prose\n\tmeasure 'A' = 1\n\t\tformatString: 0.00\n",
]


@pytest.mark.parametrize("text", GOOD_TMDL)
def test_valid_tmdl_produces_no_findings(text: str) -> None:
    """Explicit negative coverage: valid TMDL must stay silent."""
    assert check_tmdl_text(TMDL_DUMMY, text) == []


def test_duplicate_tmdl_property_is_caught() -> None:
    """The known Desktop-blocking case: duplicate `formatString` on one measure."""
    text = "table S\n\tmeasure 'A' = SUM('S'[x])\n\t\tformatString: 0.00%\n\t\tformatString: $#,##0.00\n"
    assert "DUPLICATE_PROPERTY" in _tmdl_codes(text)


def test_measure_named_like_a_later_column_in_same_table_is_caught() -> None:
    """Ordering must not hide a true table-local collision."""
    text = "table S\n\tmeasure 'Profit' = SUM('S'[Profit])\n\n\tcolumn Profit\n\t\tdataType: double\n"
    assert "NAME_COLLISION" in _tmdl_codes(text)


def test_measure_named_like_an_earlier_column_in_same_table_is_caught() -> None:
    """The same-table verdict must not depend on whether the column or measure appears first."""
    text = "table S\n\tcolumn Profit\n\t\tdataType: double\n\n\tmeasure 'Profit' = SUM('S'[Profit])\n"
    assert "NAME_COLLISION" in _tmdl_codes(text)


def test_measure_and_column_same_name_in_different_tables_is_clean() -> None:
    """TMDL permits multiple tables per document; collision state must reset per table."""
    text = (
        "table Orders\n\tcolumn Sales\n\t\tdataType: double\n\n"
        "table _Measures\n\tmeasure Sales = SUM('Orders'[Sales])\n"
    )
    assert check_tmdl_text(TMDL_DUMMY, text) == []


def test_empty_measure_expression_is_caught() -> None:
    """A measure header with no expression and only properties is invalid, not merely blank."""
    text = "table S\n\tmeasure 'Broken' =\n\t\tformatString: 0.00\n"
    assert "EMPTY_EXPRESSION" in _tmdl_codes(text)


COMPACT_FILTER_CASES = [
    (True, "CALCULATE(SUM('S'[Profit]), 'S'[Region] = [Region Param])"),
    (True, "CALCULATETABLE(VALUES('S'[X]), 'S'[Year] = [Selected Year])"),
    (False, "CALCULATE(SUM('S'[Profit]), FILTER(ALL('S'[Region]), 'S'[Region] = [Region Param]))"),
    (False, "CALCULATE(SUM('S'[Profit]), 'S'[Region] = \"West\")"),
    (False, "CALCULATE(SUM('S'[Profit]), ALL('Date'))"),
    (False, "VAR _m = [Region Param] RETURN CALCULATE(SUM('S'[Profit]), 'S'[Region] = _m)"),
]


@pytest.mark.parametrize(("illegal", "expression"), COMPACT_FILTER_CASES)
def test_compact_filter_detection_is_precise(illegal: bool, expression: str) -> None:
    """Only direct compact filters with a measure on the right are flagged."""
    assert find_compact_filters(expression) is illegal


def test_compact_filter_in_measure_is_caught_by_gate() -> None:
    """Mutation coverage for the TMDL walker, not just the predicate helper."""
    text = "table S\n\tmeasure 'A' = CALCULATE(SUM('S'[Profit]), 'S'[Region] = [Region Param])\n"
    assert "COMPACT_FILTER" in _tmdl_codes(text)


UNGUARDED_BLANK_THRESHOLD_CASES = [
    # positive: unguarded, and 0 satisfies the comparison, so a blank denominator silently passes.
    (True, "DIVIDE([Errors], [Total]) < 0.05"),
    (True, "DIVIDE([Errors], [Total]) <= 0"),
    (True, "DIVIDE([Errors], [Total]) = 0"),
    (True, "DIVIDE('S'[Errors], 'S'[Total]) > -5"),
    # positive: a blank NUMERATOR passes straight through a 3-argument DIVIDE - the "alt" only
    # substitutes on a 0/blank denominator, so this is unsafe even with a 3rd argument present.
    (True, "DIVIDE([Errors], [Total], 0) < 0.05"),
    # positive: a blank "alt" itself is not a safe substitute - both real-Desktop findings from the
    # review (DIVIDE(BLANK(), 1, 0) and DIVIDE(1, 0, BLANK())) collapse to this same argument shape.
    (True, "DIVIDE([Errors], [Total], [Fallback]) < 0.05"),
    # positive: the unsafe comparison is nested inside IF(...) rather than the entire expression -
    # this used to escape the check entirely because DIVIDE(...) wasn't the whole predicate.
    (True, 'IF([Region] = "West", DIVIDE([Errors], [Total]) < 0.05, BLANK())'),
    # positive: nested inside CALCULATE(...FILTER(...)) - another real generated-DAX shape.
    (True, "CALCULATE(SUM('S'[x]), FILTER('S', DIVIDE([Errors], [Total]) < 0.05))"),
    # negative: correctly guarded - ISBLANK(...) wraps the exact same DIVIDE(...) call earlier.
    (False, "IF(ISBLANK(DIVIDE([Errors], [Total])), BLANK(), DIVIDE([Errors], [Total]) < 0.05)"),
    # negative: both DIVIDE arguments are literals, so it can never return BLANK().
    (False, "DIVIDE(1, 1) < 2"),
    # negative: numerator defaults via COALESCE to a non-blank literal, denominator is a nonzero
    # literal - DIVIDE can never return BLANK() here even though it's a bare 2-argument call.
    (False, "DIVIDE(COALESCE([Errors], 0), 1) < 0.05"),
    # negative: safe direction - 0 does NOT satisfy the comparison, so a blank reads FALSE.
    (False, "DIVIDE([Errors], [Total]) > 0.05"),
    (False, "DIVIDE([Sales], [Target]) >= 100"),
    # negative: an ordinary, non-threshold comparison entirely unrelated to DIVIDE.
    (False, "SUM('S'[Sales]) > 0"),
    (False, "'S'[Status] = \"Active\""),
    # negative: confirmed-good shapes from the review that must not start being flagged.
    (False, "NOT ISBLANK([Errors]) && DIVIDE([Errors], [Total]) > 0.05"),
    (False, "COALESCE(DIVIDE([Errors], [Total]), 0) < 0.05"),
    (False, "(DIVIDE([Errors], [Total]) + 0) > 0.05"),
]


@pytest.mark.parametrize(("illegal", "expression"), UNGUARDED_BLANK_THRESHOLD_CASES)
def test_unguarded_divide_threshold_detection_is_precise(illegal: bool, expression: str) -> None:
    """Only an unguarded DIVIDE(...) compared unsafely against a literal threshold is flagged."""
    assert bool(find_unguarded_divide_thresholds(expression)) is illegal


def test_unguarded_divide_threshold_in_measure_is_caught_by_gate() -> None:
    """Mutation coverage for the TMDL walker, not just the predicate helper (issue #82)."""
    text = "table S\n\tmeasure 'Error Rate Flag' = DIVIDE([Errors], [Total]) < 0.05\n"
    assert "UNGUARDED_BLANK_THRESHOLD" in _tmdl_codes(text)


def test_unguarded_divide_threshold_nested_in_if_is_caught_by_gate() -> None:
    """A nested, not-the-entire-expression comparison must still be caught by the full TMDL walker."""
    text = "table S\n\tmeasure 'Flag' = IF([Region] = \"West\", DIVIDE([Errors], [Total]) < 0.05, BLANK())\n"
    assert "UNGUARDED_BLANK_THRESHOLD" in _tmdl_codes(text)


def test_guarded_divide_threshold_in_measure_is_not_flagged_by_gate() -> None:
    """A correctly guarded measure clears the gate - the negative control for the gate itself."""
    text = (
        "table S\n"
        "\tmeasure 'Error Rate Flag' = "
        "IF(ISBLANK(DIVIDE([Errors], [Total])), BLANK(), DIVIDE([Errors], [Total]) < 0.05)\n"
    )
    assert "UNGUARDED_BLANK_THRESHOLD" not in _tmdl_codes(text)


def test_provably_non_blank_divide_in_measure_is_not_flagged_by_gate() -> None:
    """A DIVIDE(...) that cannot return BLANK() is a false-positive risk this gate must not take."""
    text = "table S\n\tmeasure 'Literal Ratio' = DIVIDE(1, 1) < 2\n"
    assert "UNGUARDED_BLANK_THRESHOLD" not in _tmdl_codes(text)


def test_unguarded_divide_threshold_in_calculated_column_is_caught_by_gate() -> None:
    """The same unsafe shape on a calculated column, not just a measure."""
    text = "table S\n\tcolumn 'Below Target' = DIVIDE('S'[Errors], 'S'[Total]) <= 0.1\n"
    assert "UNGUARDED_BLANK_THRESHOLD" in _tmdl_codes(text)


def test_unguarded_blank_threshold_and_cannot_assess_are_advisory_codes() -> None:
    """Round-2 review contract: both codes must be in the advisory set the CLI keeps out of exit 1."""
    assert ADVISORY_TMDL_CODES == {"UNGUARDED_BLANK_THRESHOLD", "BLANK_THRESHOLD_CANNOT_ASSESS"}


def test_divide_pattern_inside_a_dax_comment_is_not_flagged() -> None:
    """An unguarded-looking DIVIDE(...) that only exists inside a `//` comment must not warn."""
    text = "table S\n\tmeasure 'A' = SUM('S'[x]) // example: DIVIDE([Errors], [Total]) < 0.05\n"
    assert "UNGUARDED_BLANK_THRESHOLD" not in _tmdl_codes(text)


def test_divide_pattern_inside_a_string_literal_is_not_flagged() -> None:
    """An unguarded-looking DIVIDE(...) that only exists inside a DAX string literal must not warn."""
    text = "table S\n\tmeasure 'A' = \"example: DIVIDE([Errors], [Total]) < 0.05\"\n"
    assert "UNGUARDED_BLANK_THRESHOLD" not in _tmdl_codes(text)


def test_incomplete_divide_call_is_flagged_cannot_assess_not_silently_clean() -> None:
    """A DIVIDE( with no matching close paren on the line must be named CANNOT_ASSESS, not skipped."""
    text = "table S\n\tmeasure 'A' = DIVIDE([Errors], [Total] < 0.05\n"
    assert find_incomplete_divide_calls("DIVIDE([Errors], [Total] < 0.05")
    assert "BLANK_THRESHOLD_CANNOT_ASSESS" in _tmdl_codes(text)
    assert "UNGUARDED_BLANK_THRESHOLD" not in _tmdl_codes(text)


def test_multiline_measure_expression_is_flagged_cannot_assess() -> None:
    """A measure whose expression is deferred to following lines cannot be scanned - say so."""
    text = "table S\n\tmeasure 'A' =\n\t\tDIVIDE([Errors], [Total]) < 0.05\n\t\tformatString: 0.00\n"
    assert "BLANK_THRESHOLD_CANNOT_ASSESS" in _tmdl_codes(text)


def test_multiline_calculated_column_expression_is_flagged_cannot_assess() -> None:
    """The same deferred-expression shape on a calculated column, not just a measure."""
    text = "table S\n\tcolumn 'A' =\n\t\tDIVIDE([Errors], [Total]) < 0.05\n\tcolumn 'B'\n"
    assert "BLANK_THRESHOLD_CANNOT_ASSESS" in _tmdl_codes(text)


def test_genuinely_empty_measure_header_does_not_get_a_cannot_assess_finding() -> None:
    """A trailing bare `=` immediately followed by another object is EMPTY_EXPRESSION, not deferred."""
    text = "table S\n\tmeasure 'A' =\n\tmeasure 'B' = SUM('S'[x])\n"
    codes = _tmdl_codes(text)
    assert "BLANK_THRESHOLD_CANNOT_ASSESS" not in codes
    assert "EMPTY_EXPRESSION" in codes


def test_full_datamodel_gate_has_no_false_positive_on_valid_model(tmp_path: Path) -> None:
    """The integrated gate must pass a valid model while scanning both M and TMDL."""
    tables = tmp_path / "Good.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "S.tmdl").write_text(
        "table S\n"
        "\tmeasure 'A' = SUM('S'[x])\n"
        "\t\tformatString: 0.00\n\n"
        "\tcolumn x\n"
        "\t\tdataType: int64\n\n"
        "\tpartition S = m\n"
        "\t\tmode: import\n"
        '\t\tsource = let Source = #table({"x"}, {{1}}) in Source\n',
        encoding="utf-8",
    )
    m_findings, m_scanned, tmdl_findings, tmdl_scanned = check_datamodel(tmp_path / "Good.SemanticModel")
    assert (m_findings, tmdl_findings) == ([], [])
    assert m_scanned == 1
    assert tmdl_scanned == 1


def _xls_backed_model(
    root: Path, *, magic: bytes, navigation: str, typed: str | None, filename: str = "Orders.xls"
) -> Path:
    """A one-partition model reading `root/<filename>`, whose first four bytes are ``magic``."""
    source = root / filename
    source.write_bytes(magic + b"payload")
    tables = root / "Model.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    steps = [
        f'Source = Excel.Workbook(File.Contents("{source.as_posix()}"), null, true)',
        f"Orders = {navigation}",
    ]
    if typed is not None:
        steps.append(f"Typed = {typed}")
    body = (",\n" + "\t" * 4).join(steps)
    (tables / "Orders.tmdl").write_text(
        "table Orders\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource = let\n"
        f"\t\t\t\t{body}\n"
        "\t\t\tin\n"
        f"\t\t\t\t{'Typed' if typed is not None else 'Orders'}\n",
        encoding="utf-8",
    )
    return root / "Model.SemanticModel"


def test_biff8_xls_requires_name_navigation_and_explicit_culture(tmp_path: Path) -> None:
    """A structural pass cannot hide the legacy-reader refresh and locale defects."""
    source = tmp_path / "Orders.xls"
    source.write_bytes(b"\xd0\xcf\x11\xe0" + b"BIFF8")
    definition = tmp_path / "Bad.SemanticModel" / "definition"
    definition.mkdir(parents=True)
    (definition / "expressions.tmdl").write_text(
        f'expression SourceFolder = "{tmp_path.as_posix()}" meta [IsParameterQuery=true, Type="Text"]\n',
        encoding="utf-8",
    )
    tables = definition / "tables"
    tables.mkdir()
    (tables / "Orders.tmdl").write_text(
        "table Orders\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource = let\n"
        '\t\t\t\tSource = Excel.Workbook(File.Contents(SourceFolder & "/Orders.xls"), null, true),\n'
        '\t\t\t\t// Source{[Name="Orders"]}[Data] is not executable navigation,\n'
        '\t\t\t\tNote = "use Source{[Name=""Orders""]}[Data]" is also not navigation,\n'
        '\t\t\t\tOrders = Source{[Item="Orders", Kind="Sheet"]}[Data],\n'
        '\t\t\t\tTyped = Table.TransformColumnTypes(Orders, {{"Sales", type number}}, '
        '[MissingField=if "Culture=en-BE" <> "" then MissingField.Ignore else MissingField.Error])\n'
        "\t\t\tin\n"
        "\t\t\t\tTyped\n",
        encoding="utf-8",
    )
    kinds = {finding.kind for finding in check_model(tmp_path / "Bad.SemanticModel")}
    assert {"BIFF8_XLS_NAVIGATION_KEY", "BIFF8_XLS_CULTURE"} <= kinds


def test_biff8_xls_with_name_navigation_and_culture_is_clean(tmp_path: Path) -> None:
    """The narrow gate accepts the proven legacy-reader form."""
    source = tmp_path / "Orders.xls"
    source.write_bytes(b"\xd0\xcf\x11\xe0" + b"BIFF8")
    tables = tmp_path / "Good.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "Orders.tmdl").write_text(
        "table Orders\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource = let\n"
        f'\t\t\t\tSource = Excel.Workbook(File.Contents("{source.as_posix()}"), null, true),\n'
        '\t\t\t\tOrders = Source{[Name="Orders"]}[Data],\n'
        '\t\t\t\tTyped = Table.TransformColumnTypes(Orders, {{"Sales", type number}}, "en-BE")\n'
        "\t\t\tin\n"
        "\t\t\t\tTyped\n",
        encoding="utf-8",
    )
    assert check_model(tmp_path / "Good.SemanticModel") == []


def test_biff8_gate_ignores_a_non_biff8_file_named_xls(tmp_path: Path) -> None:
    """Detecting the legacy reader by EXTENSION is the exact failure the magic-byte read prevents.

    Identical defective M to the firing case above; only the source file's first bytes differ (a
    modern workbook named `.xls`). That file is read by the provider whose navigation table really
    does have `Item`/`Kind` columns, so a finding here would be a false positive on correct M.
    """
    model = _xls_backed_model(
        tmp_path,
        magic=b"PK\x03\x04",
        navigation='Source{[Item="Orders", Kind="Sheet"]}[Data]',
        typed='Table.TransformColumnTypes(Orders, {{"Sales", type number}})',
    )
    assert check_model(model) == []


def test_biff8_xls_without_a_type_step_is_not_a_culture_finding(tmp_path: Path) -> None:
    """The engine emits `Typed` only when it has type pairs, so no conversion is a real output shape.

    "Type conversion must pass an explicit culture" cannot be acted on when the partition contains no
    conversion at all, and there is no locale-dependent parse to get wrong.
    """
    model = _xls_backed_model(
        tmp_path, magic=b"\xd0\xcf\x11\xe0", navigation='Source{[Name="Orders"]}[Data]', typed=None
    )
    assert check_model(model) == []


def test_biff8_xls_with_a_literal_null_culture_is_a_finding(tmp_path: Path) -> None:
    """An explicit `null` culture IS the defect - it selects the ambient locale of the build host."""
    model = _xls_backed_model(
        tmp_path,
        magic=b"\xd0\xcf\x11\xe0",
        navigation='Source{[Name="Orders"]}[Data]',
        typed='Table.TransformColumnTypes(Orders, {{"Sales", type number}}, null)',
    )
    assert {finding.kind for finding in check_model(model)} == {"BIFF8_XLS_CULTURE"}


def test_biff8_gate_is_scoped_to_the_xls_suffix_on_purpose(tmp_path: Path) -> None:
    """A deliberate MISS, pinned so it stays deliberate rather than accidental.

    BIFF8 bytes carrying a modern suffix are not flagged: the module's stated scope is that a false
    positive is worse than a miss, and the `.xls` pre-filter is what keeps any other OLE2/CFB file
    (a `.doc`, a `.msg`) out of a finding that would give it Excel-navigation advice. Widening this
    to magic-only is a defensible future change - it just must be a measured one, not a silent one.
    """
    model = _xls_backed_model(
        tmp_path,
        magic=b"\xd0\xcf\x11\xe0",
        navigation='Source{[Item="Orders", Kind="Sheet"]}[Data]',
        typed='Table.TransformColumnTypes(Orders, {{"Sales", type number}})',
        filename="Orders.xlsx",
    )
    assert check_model(model) == []


def test_biff8_gate_reaches_the_cli_exit_code(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The wiring, not just the helper: `check_datamodel.py <path>` must exit 1 and name the kind."""
    _xls_backed_model(
        tmp_path,
        magic=b"\xd0\xcf\x11\xe0",
        navigation='Source{[Item="Orders", Kind="Sheet"]}[Data]',
        typed='Table.TransformColumnTypes(Orders, {{"Sales", type number}})',
    )
    with caplog.at_level(logging.ERROR):
        assert main([str(tmp_path)]) == 1
    assert "BIFF8_XLS_NAVIGATION_KEY" in caplog.text
    assert "BIFF8_XLS_CULTURE" in caplog.text


def _model_with_measure(tmp_path: Path, measure_expr_lines: str, *, name: str = "Advisory.SemanticModel") -> Path:
    """Build the smallest `.SemanticModel` the CLI will scan: one measure, one M partition."""
    tables = tmp_path / name / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "S.tmdl").write_text(
        f"table S\n{measure_expr_lines}"
        "\tcolumn x\n\t\tdataType: int64\n\n"
        "\tpartition S = m\n\t\tmode: import\n"
        '\t\tsource = let Source = #table({"x"}, {{1}}) in Source\n',
        encoding="utf-8",
    )
    return tmp_path / name


def test_unguarded_blank_threshold_alone_does_not_fail_the_cli_exit_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Round-2 contract point 1/2: this finding is advisory - it must NOT contribute to exit 1."""
    _model_with_measure(tmp_path, "\tmeasure 'Flag' = DIVIDE([Errors], [Total]) < 0.05\n\t\tformatString: 0.00\n\n")
    with caplog.at_level(logging.WARNING):
        assert main([str(tmp_path)]) == 0
    assert "UNGUARDED_BLANK_THRESHOLD" in caplog.text
    assert "BLANK()-THRESHOLD ADVISORY" in caplog.text
    assert "manual verification" in caplog.text.lower() or "manual Tableau/Power BI verification" in caplog.text


def test_blank_threshold_cannot_assess_alone_does_not_fail_the_cli_exit_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A multi-line expression is unassessed, not clean, but it must still not force exit 1."""
    _model_with_measure(
        tmp_path,
        "\tmeasure 'Flag' =\n\t\tDIVIDE([Errors], [Total]) < 0.05\n\t\tformatString: 0.00\n\n",
    )
    with caplog.at_level(logging.WARNING):
        assert main([str(tmp_path)]) == 0
    assert "BLANK_THRESHOLD_CANNOT_ASSESS" in caplog.text


def test_a_real_structural_error_still_fails_the_cli_exit_code_alongside_an_advisory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A genuine TMDL structural error must still exit 1 even when an advisory finding is present."""
    tables = tmp_path / "Blocking.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "S.tmdl").write_text(
        "table S\n"
        "\tmeasure 'Flag' = DIVIDE([Errors], [Total]) < 0.05\n"
        "\t\tformatString: 0.00\n"
        "\t\tformatString: 0.00\n\n"
        "\tcolumn x\n\t\tdataType: int64\n\n"
        "\tpartition S = m\n\t\tmode: import\n"
        '\t\tsource = let Source = #table({"x"}, {{1}}) in Source\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        assert main([str(tmp_path)]) == 1
    assert "DUPLICATE_PROPERTY" in caplog.text
    assert "UNGUARDED_BLANK_THRESHOLD" in caplog.text


def test_tmdl_has_no_false_positives_across_the_committed_corpus() -> None:
    """The real examples are the false-positive regression suite for the TMDL checks."""
    offenders = {}
    models = sorted((REPO_ROOT / "examples").glob("*/fabric/*.SemanticModel"))
    assert len(models) == 16
    for model in models:
        findings, _ = check_tmdl_model(model)
        if findings:
            offenders[model.name] = [f"{finding.code} {finding.file.name}:{finding.line}" for finding in findings]
    assert not offenders


def test_gate_reports_not_a_pass_when_it_finds_no_model(tmp_path: Path) -> None:
    """A mistyped gate path has not passed; it did not check a model."""
    assert main([str(tmp_path)]) == 2
