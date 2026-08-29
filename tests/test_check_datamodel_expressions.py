"""Tests for the TMDL expression-layout gate in scripts/check_datamodel.py.

Every expectation here was cross-validated against `TmdlSerializer.DeserializeDatabaseFromFolder`
(AMO 19.84.1) - the same parser Power BI Desktop uses - on 34 synthetic TMDL variants. The gate and
the real parser agreed on all 34. That matters because the failure this guards against is total: the
model does not open at all, so there is no partial-success signal to notice.

The measured rule these encode, which is NARROWER than "DAX must be one line":

  * multi-line DAX is LEGAL                                   -> must not be flagged
  * blank lines INSIDE a multi-line expression are LEGAL      -> must not be flagged
  * starting the expression on the `=` line and then          -> MUST be flagged
    continuing onto the next line is fatal
  * an under-indented multi-line expression swallows the      -> MUST be flagged (silent corruption)
    object's own properties and still parses clean
"""

# pylint: disable=import-error,wrong-import-position,missing-function-docstring

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
from check_datamodel import check_tmdl_expressions, check_tmdl_model, main

TMDL = Path("Table.tmdl")
TAB = "\t"


def _codes(text: str) -> set[str]:
    return {f.code for f in check_tmdl_expressions(TMDL, text)}


# --- legal layouts: the gate must stay silent -------------------------------------------------


def test_single_line_dax_is_clean():
    assert _codes(f'table T\n{TAB}measure \'M\' = IF(1=1, "a", "b")\n{TAB}{TAB}formatString: 0.0%\n') == set()


def test_multi_line_dax_after_equals_is_legal():
    # measured: TmdlSerializer OPENS this and returns the expression with its newlines intact.
    text = (
        f"table T\n"
        f"{TAB}measure 'M' =\n"
        f"{TAB}{TAB}{TAB}IF(\n"
        f"{TAB}{TAB}{TAB}{TAB}DIVIDE(1, 2) >= 0.98,\n"
        f'{TAB}{TAB}{TAB}{TAB}"Healthy", "Bad"\n'
        f"{TAB}{TAB}{TAB})\n"
        f"{TAB}{TAB}formatString: 0.0%\n"
    )
    assert _codes(text) == set()


def test_blank_lines_inside_a_multi_line_expression_are_legal():
    # The headline finding: blank lines are NOT the hazard. Microsoft documents vertical whitespace
    # as part of the expression, and the parser confirms it.
    text = (
        f"table T\n"
        f"{TAB}measure 'M' =\n"
        f"{TAB}{TAB}{TAB}IF(\n"
        f"\n"
        f"{TAB}{TAB}{TAB}{TAB}DIVIDE(1, 2) >= 0.98,\n"
        f"\n"
        f'{TAB}{TAB}{TAB}{TAB}"Healthy", "Bad")\n'
        f"{TAB}{TAB}formatString: 0.0%\n"
    )
    assert _codes(text) == set()


def test_blank_line_between_measures_is_legal():
    text = (
        f"table T\n{TAB}measure 'A' = 1\n{TAB}{TAB}formatString: 0\n\n{TAB}measure 'B' = 2\n{TAB}{TAB}formatString: 0\n"
    )
    assert _codes(text) == set()


def test_backtick_enclosed_expression_is_read_verbatim():
    text = (
        f"table T\n"
        f"{TAB}measure 'M' = ```\n"
        f"{TAB}{TAB}{TAB}IF(\n"
        f"\n"
        f'{TAB}{TAB}{TAB}{TAB}1=1, "a", "b")\n'
        f"{TAB}{TAB}{TAB}```\n"
        f"{TAB}{TAB}formatString: 0.0%\n"
    )
    assert _codes(text) == set()


def test_partition_source_type_is_not_an_expression():
    # `partition X = m` names a source TYPE; only the nested `source =` carries M.
    text = (
        f"table T\n"
        f"{TAB}partition 'T-P' = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{TAB}{TAB}{TAB}let a = 1\n"
        f"{TAB}{TAB}{TAB}in\n"
        f"\n"
        f"{TAB}{TAB}{TAB}{TAB}a\n"
    )
    assert _codes(text) == set()


def test_property_lookalike_inside_a_correctly_indented_expression_is_clean():
    text = (
        f"table T\n"
        f"{TAB}measure 'M' =\n"
        f"{TAB}{TAB}{TAB}VAR formatString: = 1\n"
        f"{TAB}{TAB}{TAB}RETURN 1\n"
        f"{TAB}{TAB}formatString: 0.0%\n"
    )
    assert _codes(text) == set()


def test_property_shaped_lines_deeper_inside_an_expression_are_not_absorbed_properties():
    """A real property that got absorbed sits at the expression's OWN baseline indent; anything
    deeper is expression content - here JSON inside an M string literal, which TmdlSerializer opens
    and round-trips verbatim. Without the baseline guard this shape is a false positive.
    """
    text = (
        f"table T\n"
        f"{TAB}partition 'T-P' = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f'{TAB}{TAB}{TAB}let cfg = "{{\n'
        f"{TAB}{TAB}{TAB}{TAB}dataType: string,\n"
        f"{TAB}{TAB}{TAB}{TAB}formatString: 0.00\n"
        f'{TAB}{TAB}{TAB}}}" in cfg\n'
    )
    assert _codes(text) == set()


def test_committed_example_corpus_is_clean():
    """The 16 worked example models all open in Desktop, so the gate must not fire on any of them."""
    models = sorted((REPO_ROOT / "examples").glob("*/fabric/*.SemanticModel"))
    assert len(models) >= 10, "corpus disappeared - this test would otherwise silently pass on nothing"
    noisy = []
    for model in models:
        findings, _ = check_tmdl_model(model)
        noisy += [f for f in findings if f.code.startswith(("TMDL_EXPRESSION", "TMDL_MISPLACED", "TMDL_UNREADABLE"))]
    assert noisy == [], f"false positives on known-good models: {[f.render(REPO_ROOT) for f in noisy]}"


# --- fatal layouts: the gate must fire --------------------------------------------------------


def test_inline_expression_with_continuation_is_flagged():
    # The reported crash: `TMDL Format Error: Unexpected line type: Other!`
    text = (
        f"table T\n"
        f"{TAB}measure 'M' = IF(\n"
        f"{TAB}{TAB}{TAB}DIVIDE(1, 2) >= 0.98,\n"
        f'{TAB}{TAB}{TAB}"Healthy", "Bad")\n'
        f"{TAB}{TAB}formatString: 0.0%\n"
    )
    assert _codes(text) == {"TMDL_EXPRESSION_CONTINUATION"}


def test_inline_expression_with_blank_lines_between_fragments_is_flagged():
    # The issue's verbatim shape. It is fatal because of the INLINE start, not the blank lines.
    text = (
        f"table T\n"
        f"{TAB}measure 'M' = IF(\n"
        f"\n"
        f"{TAB}{TAB}{TAB}DIVIDE(1, 2) >= 0.98,\n"
        f"\n"
        f'{TAB}{TAB}{TAB}"Healthy", "Bad")\n'
    )
    assert _codes(text) == {"TMDL_EXPRESSION_CONTINUATION"}


@pytest.mark.parametrize(
    "continuation",
    [
        'DIVIDE(1, 2), "x")',  # bare call
        "VAR x = 1",  # UnsupportedObjectType - VAR is not a supported property
        "RETURN x",
        ")",
        '"Healthy",',
    ],
)
def test_every_measured_fatal_continuation_shape_is_flagged(continuation):
    text = f'table T\n{TAB}measure \'M\' = IF(1=1, "a", "b")\n{TAB}{TAB}{continuation}\n'
    assert _codes(text) == {"TMDL_EXPRESSION_CONTINUATION"}


def test_inline_m_source_with_continuation_is_flagged():
    text = (
        f"table T\n"
        f"{TAB}partition 'T-P' = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source = let a = 1\n"
        f"{TAB}{TAB}{TAB}in a\n"
    )
    assert _codes(text) == {"TMDL_EXPRESSION_CONTINUATION"}


def test_under_indented_expression_swallowing_a_property_is_flagged():
    # Worse than a crash: TmdlSerializer OPENS this and the DAX silently ends with
    # "...\nformatString: 0.0%". Nothing else in the toolchain reports it.
    text = f'table T\n{TAB}measure \'M\' =\n{TAB}{TAB}IF(1=1,\n{TAB}{TAB}"a", "b")\n{TAB}{TAB}formatString: 0.0%\n'
    assert _codes(text) == {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}


def test_expression_dedenting_out_mid_way_is_flagged():
    text = f"table T\n{TAB}measure 'M' =\n{TAB}{TAB}{TAB}IF(\n{TAB}DIVIDE(1, 2) >= 0.98,\n{TAB}{TAB}{TAB}\"Healthy\")\n"
    assert _codes(text) == {"TMDL_EXPRESSION_UNINDENTED"}


def test_blank_line_inside_a_string_literal_that_dedents_is_flagged():
    text = f'table T\n{TAB}measure \'M\' =\n{TAB}{TAB}{TAB}IF(1=1, "line one\n\nline two", "b")\n'
    assert _codes(text) == {"TMDL_EXPRESSION_UNINDENTED"}


def test_description_inside_an_object_body_is_flagged():
    text = f"table T\n{TAB}measure 'M' = 1\n{TAB}{TAB}/// misplaced\n{TAB}{TAB}formatString: 0\n"
    assert _codes(text) == {"TMDL_MISPLACED_DESCRIPTION"}


# --- unassessable must never look clean -------------------------------------------------------


def test_unterminated_backtick_block_is_reported_not_skipped():
    text = f'table T\n{TAB}measure \'M\' = ```\n{TAB}{TAB}{TAB}IF(1=1, "a", "b")\n'
    assert _codes(text) == {"TMDL_UNTERMINATED_EXPRESSION"}


def test_undecodable_tmdl_file_is_reported_not_treated_as_clean(tmp_path):
    definition = tmp_path / "M.SemanticModel" / "definition"
    definition.mkdir(parents=True)
    (definition / "broken.tmdl").write_bytes(b"table T\n\tmeasure 'M' = \xff\xfe not utf8\n")
    findings, scanned = check_tmdl_model(tmp_path / "M.SemanticModel")
    assert scanned == 1
    assert {f.code for f in findings} == {"TMDL_UNREADABLE"}


# --- exit codes: the gate is judged by these, never by printed text ---------------------------


def _write_model(root: Path, body: str) -> Path:
    definition = root / "M.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True)
    (definition / "database.tmdl").write_text("database M\n\tcompatibilityLevel: 1550\n", encoding="utf-8")
    (definition / "model.tmdl").write_text("model Model\n\tculture: en-US\n", encoding="utf-8")
    (definition / "tables" / "T.tmdl").write_text(body, encoding="utf-8")
    return root / "M.SemanticModel"


_GOOD_PARTITION = (
    f"{TAB}partition 'T-P' = m\n{TAB}{TAB}mode: import\n{TAB}{TAB}source =\n{TAB}{TAB}{TAB}let a = 1 in a\n"
)


def test_main_exits_nonzero_on_an_inline_continuation(tmp_path):
    model = _write_model(
        tmp_path, f'table T\n{TAB}measure \'M\' = IF(\n{TAB}{TAB}{TAB}1=1, "a", "b")\n{_GOOD_PARTITION}'
    )
    assert main([str(model)]) == 1


def test_main_exits_zero_on_multi_line_dax_with_blank_lines(tmp_path):
    body = (
        f"table T\n"
        f"{TAB}measure 'M' =\n"
        f"{TAB}{TAB}{TAB}IF(\n"
        f"\n"
        f'{TAB}{TAB}{TAB}{TAB}1=1, "a", "b")\n'
        f"{TAB}{TAB}formatString: 0.0%\n"
        f"{_GOOD_PARTITION}"
    )
    assert main([str(_write_model(tmp_path, body))]) == 0
