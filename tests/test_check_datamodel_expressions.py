"""Tests for the TMDL expression-layout gate in scripts/tmdl_checks.py.

Every expectation here was cross-validated against `TmdlSerializer.DeserializeDatabaseFromFolder`
(AMO 19.84.1) - the same parser Power BI Desktop uses - on 42 synthetic TMDL variants. The gate and
the real parser agreed on all 42. That matters because the failure this guards against is total: the
model does not open at all, so there is no partial-success signal to notice.

The measured rule these encode, which is NARROWER than "DAX must be one line":

  * multi-line DAX is LEGAL                                   -> must not be flagged
  * blank lines INSIDE a multi-line expression are LEGAL      -> must not be flagged
  * starting the expression on the `=` line and then          -> MUST be flagged
    continuing onto the next line is fatal
  * an under-indented multi-line expression swallows the      -> MUST be flagged (silent corruption)
    object's own properties and still parses clean

The absorption check enforces the DOCUMENTED indentation contract rather than matching property
names. That distinction is load-bearing: the name-matching revision missed a bare `isHidden` and a
documented `isKey:`, and fired on property-shaped text inside a legal M block comment.
"""

# pylint: disable=import-error,wrong-import-position,missing-function-docstring

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
from check_datamodel import check_tmdl_expressions, check_tmdl_model, main
from tmdl_checks import indent_unit

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
    """Only the expression's OWN baseline indent can hold an absorbed property; anything deeper is
    expression content - here JSON inside an M string literal, which TmdlSerializer opens and
    round-trips verbatim.
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


def test_property_text_inside_an_m_block_comment_is_not_a_finding():
    """AMO opens this model. The name-matching revision flagged it; the indentation contract does
    not, because the expression is correctly indented and where a token sits is the actual rule.
    """
    text = (
        f"table T\n"
        f"{TAB}partition 'T-P' = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{TAB}{TAB}{TAB}/*\n"
        f"{TAB}{TAB}{TAB}formatString: 0.00\n"
        f"{TAB}{TAB}{TAB}*/\n"
        f"{TAB}{TAB}{TAB}let a = 1 in a\n"
    )
    assert _codes(text) == set()


def test_a_real_multi_step_m_let_block_is_legal_when_correctly_indented():
    text = (
        f"table T\n"
        f"{TAB}partition 'T-P' = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f"{TAB}{TAB}{TAB}let\n"
        f'{TAB}{TAB}{TAB}{TAB}Source = Csv.Document("x"),\n'
        f"{TAB}{TAB}{TAB}{TAB}Promoted = Table.PromoteHeaders(Source)\n"
        f"{TAB}{TAB}{TAB}in\n"
        f"{TAB}{TAB}{TAB}{TAB}Promoted\n"
    )
    assert _codes(text) == set()


def test_the_indent_unit_is_taken_from_the_document():
    assert indent_unit(["table T", "\tmeasure M = 1"]) == 4
    assert indent_unit(["table T", "  measure M = 1", "    formatString: 0"]) == 2
    assert indent_unit(["table T"]) == 4


def test_the_indent_unit_is_capped_at_one_tab():
    """A committed fixture writes `expression Source =` at column 0 with its `let` two tabs in, and
    AMO opens it. Uncapped, the smallest indent (8) became one level, which put the object's
    properties at column 8 too and made that legal file a finding.
    """
    fixture = (
        "expression Source =\n\t\tlet\n\t\t\tSource = #table(type table [A = number], {{1}})\n\t\tin\n\t\t\tSource\n"
    )
    assert indent_unit(fixture.splitlines()) == 4
    assert _codes(fixture) == set()


def test_a_two_space_indented_document_is_measured_on_its_own_unit():
    """The contract is "one level deeper", so the level has to come from the file, not a constant."""
    legal = 'table T\n  measure M =\n      IF(1=1, "a", "b")\n    formatString: 0.0%\n'
    absorbed = 'table T\n  measure M =\n    IF(1=1, "a", "b")\n    formatString: 0.0%\n'
    assert _codes(legal) == set()
    assert _codes(absorbed) == {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}


def test_committed_example_corpus_is_clean():
    """The 16 worked example models all open in Desktop, so the gate must not fire on any of them."""
    models = sorted((REPO_ROOT / "examples").glob("*/fabric/*.SemanticModel"))
    assert len(models) >= 10, "corpus disappeared - this test would otherwise silently pass on nothing"
    noisy = []
    for model in models:
        findings, _ = check_tmdl_model(model)
        noisy += [f for f in findings if f.code.startswith(("TMDL_EXPRESSION", "TMDL_MISPLACED", "TMDL_"))]
    assert noisy == [], f"false positives on known-good models: {[f.render(REPO_ROOT) for f in noisy]}"


def test_the_cli_reports_the_committed_corpus_clean_by_exit_code():
    """The gate is judged by its exit code, not by what it prints - so assert on the exit code."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_datamodel.py"), "--all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


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
        "Promoted",  # a bare M identifier: same shape as a boolean shortcut property
        'Source = Csv.Document("x")',  # an M let step: same shape as `annotation X = Y`
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


@pytest.mark.parametrize(
    ("kind", "absorbed"),
    [
        ("measure", "isHidden"),  # bare-boolean shortcut syntax, no colon at all
        ("column", "isKey: true"),  # documented in the TMDL overview and the TOM Column reference
        ("measure", "someFutureProperty: 7"),  # a property no allowlist could have known about
        ("measure", "annotation Foo = Bar"),
    ],
)
def test_absorption_is_decided_by_indentation_not_by_the_property_name(kind, absorbed):
    """AMO opens every one of these and leaves the property UNSET - it is inside the expression.

    Deciding this by matching known property names is what made the first revision miss the first
    two and be structurally unable to catch the third.
    """
    text = f"table T\n{TAB}{kind} Probe =\n{TAB}{TAB}1\n{TAB}{TAB}{absorbed}\n"
    assert _codes(text) == {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}


def test_a_multiline_expression_with_no_properties_after_it_still_violates_the_contract():
    """Nothing is absorbed yet, but the next property added to this object silently would be."""
    assert _codes(f"table T\n{TAB}measure Probe =\n{TAB}{TAB}1\n") == {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}


def test_a_full_m_let_block_cannot_masquerade_as_object_body_syntax():
    """`Source = Csv.Document(...)` looks exactly like `annotation X = Y`, and a bare `in` looks
    exactly like a boolean shortcut property. AMO rejects this outright:
    "The keyword 'Source' is neither a property nor an object in the current context".
    """
    text = (
        f"table T\n"
        f"{TAB}partition 'T-P' = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source = let\n"
        f'{TAB}{TAB}{TAB}Source = Csv.Document("x"),\n'
        f"{TAB}{TAB}{TAB}Promoted = Table.PromoteHeaders(Source)\n"
        f"{TAB}{TAB}in\n"
        f"{TAB}{TAB}{TAB}Promoted\n"
    )
    assert _codes(text) == {"TMDL_EXPRESSION_CONTINUATION"}


def test_a_continuation_at_the_headers_own_indent_is_flagged():
    """The first revision ended its scan on any dedent, so this reported nothing. AMO rejects it."""
    assert _codes(f'table T\n{TAB}measure Probe = IF(1=1,\n{TAB}"a", "b")\n') == {"TMDL_EXPRESSION_CONTINUATION"}


@pytest.mark.parametrize("prop", ["formatStringDefinition", "detailRowsDefinition"])
def test_a_nested_multi_line_expression_property_is_not_read_as_a_continuation(prop):
    """AMO opens both of these. The body of `formatStringDefinition =` belongs to THAT expression,
    not to the measure's inline one, so walking it from the outer scan reported a legal model as
    broken. This also pins that the property is recognised as expression-bearing at all: drop it
    from the set and the outer scan flags its body again.
    """
    text = f'table T\n{TAB}measure Probe = 1\n{TAB}{TAB}{prop} =\n{TAB}{TAB}{TAB}"0.00"\n'
    assert _codes(text) == set()


def test_a_legal_sibling_after_an_inline_expression_is_still_scanned():
    """Stopping the inline scan must not swallow the next object - the second measure is broken."""
    text = (
        f"table T\n"
        f"{TAB}measure A = 1\n"
        f"{TAB}{TAB}formatString: 0\n"
        f"\n"
        f"{TAB}measure B = IF(1=1,\n"
        f'{TAB}{TAB}{TAB}"a", "b")\n'
    )
    assert _codes(text) == {"TMDL_EXPRESSION_CONTINUATION"}


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


def test_a_bom_prefixed_tmdl_file_is_reported_not_normalised_away(tmp_path):
    """AMO tolerates a BOM, so agreeing with AMO is not enough here. Power BI Desktop's project
    reader rejects it outright (`UTF8EncodingThrowOnBOM.CheckBom` -> "Only text with UTF8 encoding
    without BOM is supported") and the file does not open - see
    .github/skills/pbip-model-refresh/SKILL.md. Stripping it in memory made a deliverable Desktop
    cannot open exit 0 from the gate whose whole purpose is catching exactly that.
    """
    definition = tmp_path / "M.SemanticModel" / "definition"
    definition.mkdir(parents=True)
    (definition / "T.tmdl").write_bytes(b"\xef\xbb\xbftable T\n\tmeasure 'M' = 1\n")
    findings, _ = check_tmdl_model(tmp_path / "M.SemanticModel")
    assert {f.code for f in findings} == {"TMDL_BOM"}


def test_the_rest_of_a_bom_prefixed_file_is_still_checked(tmp_path):
    """The BOM is stripped in memory AFTER being reported, so it cannot mask a second defect."""
    definition = tmp_path / "M.SemanticModel" / "definition"
    definition.mkdir(parents=True)
    body = b'\xef\xbb\xbftable T\n\tmeasure \'M\' = IF(1=1,\n\t\t\t"a", "b")\n'
    (definition / "T.tmdl").write_bytes(body)
    findings, _ = check_tmdl_model(tmp_path / "M.SemanticModel")
    assert {f.code for f in findings} == {"TMDL_BOM", "TMDL_EXPRESSION_CONTINUATION"}


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


def test_main_exits_nonzero_on_a_bom(tmp_path):
    model = _write_model(tmp_path, f"table T\n{TAB}measure 'M' = 1\n{_GOOD_PARTITION}")
    target = model / "definition" / "tables" / "T.tmdl"
    target.write_bytes(b"\xef\xbb\xbf" + target.read_bytes())
    assert main([str(model)]) == 1


def test_main_exits_nonzero_on_an_absorbed_property(tmp_path):
    body = f"table T\n{TAB}measure 'M' =\n{TAB}{TAB}1\n{TAB}{TAB}isHidden\n{_GOOD_PARTITION}"
    assert main([str(_write_model(tmp_path, body))]) == 1
