"""Tests for scripts/check_m_syntax.py.

These exist because Power BI Desktop reports a broken model as an unlocalised
`M Engine error: 'Microsoft.Data.Mashup.Preview; Token ',' expected.'` - no file, no line, no
expression - which a real user hit repeatedly and could not act on. The checker's whole value is
turning that into `file:line:col`, so it has to be both accurate AND quiet: a checker that cries
wolf on good models gets ignored, which is worse than not having one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
from check_m_syntax import _check_expression, _iter_m_blocks, check_model

DUMMY = Path("model.tmdl")


def _kinds(m_expression: str) -> set[str]:
    return {f.kind for f in _check_expression(DUMMY, m_expression)}


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


def test_block_extraction_stops_at_tmdl_metadata() -> None:
    """`annotation`/`lineageTag` lines are TMDL, not M - swallowing them would corrupt the scan."""
    path = REPO_ROOT / "examples" / "health-tracker" / "fabric"
    expressions = next(path.glob("*.SemanticModel/definition/expressions.tmdl"))
    blocks = _iter_m_blocks(expressions)
    assert blocks
    assert all("lineageTag" not in body and "annotation" not in body for body, _ in blocks)
