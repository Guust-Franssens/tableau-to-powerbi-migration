"""
purpose: Regression tests for scripts/check_tmdl.py - the gate that closes the hole a battle-test run
         exposed: a model that Power BI Desktop could not open at all passed BOTH existing structural
         gates (check_m_syntax reads only M; powerbi-report-author validate checks only the Report
         item). Only a full TMDL round-trip caught it.
usage:   pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from check_tmdl import check_model, check_tmdl_text, find_compact_filters  # noqa: E402

DUMMY = Path("Table.tmdl")


def _codes(text: str) -> set[str]:
    return {f.code for f in check_tmdl_text(DUMMY, text)}


GOOD_TMDL = [
    "table S\n\tmeasure 'A' = SUM('S'[x])\n\t\tformatString: 0.00\n\n\tcolumn x\n\t\tdataType: int64\n",
    # annotations legitimately repeat and must not read as duplicate properties
    "table S\n\tcolumn x\n\t\tdataType: int64\n\t\tannotation a = 1\n\t\tannotation b = 2\n",
    # the same property name in DIFFERENT objects is fine
    "table S\n\tmeasure 'A' = 1\n\t\tformatString: 0.00\n\n\tmeasure 'B' = 2\n\t\tformatString: 0.00\n",
    # a description line must not be mistaken for a property
    "table S\n\t/// formatString: not a property, this is prose\n\tmeasure 'A' = 1\n\t\tformatString: 0.00\n",
]


@pytest.mark.parametrize("text", GOOD_TMDL)
def test_valid_tmdl_produces_no_findings(text: str):
    """Zero false positives is the hard requirement - a noisy checker gets switched off."""
    assert check_tmdl_text(DUMMY, text) == []


def test_no_false_positives_across_the_committed_corpus():
    """The 16 real migrated models (167 TMDL documents) are the false-positive regression suite.

    This test earned its keep immediately: a first, naive version of the compact-filter rule fired
    18 times here, and every one was legal DAX (a predicate inside FILTER(), which is the recommended
    FIX for the very bug being detected).
    """
    offenders = {}
    for model in sorted((REPO / "examples").glob("*/fabric/*.SemanticModel")):
        findings, _ = check_model(model)
        if findings:
            offenders[model.name] = [f"{f.code} {f.file.name}:{f.line}" for f in findings]
    assert not offenders


def test_duplicate_property_is_caught():
    """The exact defect that blocked a model load while both other gates reported green."""
    text = "table S\n\tmeasure 'A' = SUM('S'[x])\n\t\tformatString: 0.00%\n\t\tformatString: $#,##0.00\n"
    assert "DUPLICATE_PROPERTY" in _codes(text)


def test_measure_named_like_a_column_is_caught():
    """Tabular requires names to be unique within a table; this deserializes fine and fails on
    model commit, which is why neither existing gate sees it."""
    text = "table S\n\tmeasure 'Profit' = SUM('S'[Profit])\n\n\tcolumn Profit\n\t\tdataType: double\n"
    assert "NAME_COLLISION" in _codes(text)


COMPACT_FILTER_CASES = [
    # (illegal?, expression) - the distinction is whether the predicate is a DIRECT CALCULATE argument
    (True, "CALCULATE(SUM('S'[Profit]), 'S'[Region] = [Region Param])"),
    (True, "CALCULATETABLE(VALUES('S'[X]), 'S'[Year] = [Selected Year])"),
    (False, "CALCULATE(SUM('S'[Profit]), FILTER(ALL('S'[Region]), 'S'[Region] = [Region Param]))"),
    (False, "CALCULATE(SUM('S'[Profit]), 'S'[Region] = \"West\")"),
    (False, "CALCULATE(SUM('S'[Profit]), ALL('Date'))"),
    (False, "VAR _m = [Region Param] RETURN CALCULATE(SUM('S'[Profit]), 'S'[Region] = _m)"),
]


@pytest.mark.parametrize(("illegal", "expression"), COMPACT_FILTER_CASES)
def test_compact_filter_detection_is_precise(illegal: bool, expression: str):
    """`'Table'[Col] = [Measure]` is illegal as a bare CALCULATE argument and legal inside FILTER.

    One real migration shipped the illegal form 58 times in a single model, so the check is worth
    having - but only if it distinguishes the two, otherwise it flags the recommended fix as the bug.
    The last case is the VAR-hoisting fix itself and must stay clean.
    """
    assert find_compact_filters(expression) is illegal


def test_gate_reports_not_a_pass_when_it_finds_no_model(tmp_path: Path, capsys):
    """A gate that cannot find its subject has not passed - it has not run. Same rule as
    check_m_syntax, where exiting 0 on a mistyped path was a false green in a mandatory gate."""
    import check_tmdl  # noqa: PLC0415

    assert check_tmdl.main([str(tmp_path)]) == 2
