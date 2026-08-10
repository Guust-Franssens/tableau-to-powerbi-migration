"""Tests for scripts/check_datamodel.py.

These exist because Power BI Desktop reports a broken model as an unlocalised
`M Engine error: 'Microsoft.Data.Mashup.Preview; Token ',' expected.'` - no file, no line, no
expression - which a real user hit repeatedly and could not act on. The checker's whole value is
turning that into `file:line:col`, so it has to be both accurate AND quiet: a checker that cries
wolf on good models gets ignored, which is worse than not having one.
"""

# pylint: disable=import-error,wrong-import-position,missing-function-docstring,use-implicit-booleaness-not-comparison

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
from check_datamodel import (
    _check_expression,
    _iter_m_blocks,
    check_datamodel,
    check_model,
    check_model_counted,
    check_tmdl_model,
    check_tmdl_text,
    find_compact_filters,
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


def test_measure_named_like_a_column_is_caught() -> None:
    """Tabular requires table-local measure and column names to be unique."""
    text = "table S\n\tmeasure 'Profit' = SUM('S'[Profit])\n\n\tcolumn Profit\n\t\tdataType: double\n"
    assert "NAME_COLLISION" in _tmdl_codes(text)


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
