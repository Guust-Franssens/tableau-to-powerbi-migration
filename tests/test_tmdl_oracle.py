"""Tests for the TMDL oracle - the gate that replaced two hand-written TMDL grammars.

Issue #254 shipped a TMDL layout gate twice: first matching property NAMES, then enforcing the
documented INDENTATION contract. Blind review broke both, and in each case with false negatives
AND false positives:

  * `measure Probe =` / `1` / `isHidden` indented five or eight spaces parses clean and loses the
    property - missed, because the scanner capped one indentation level at a tab while AMO accepts
    wider units;
  * `measure Probe = 1` / `IsHidden` is VALID (TMDL keywords are case-insensitive; AMO sets
    IsHidden=True) - rejected;
  * two `tablePermission` entries in one role are VALID - the second was rejected, because the
    keyword was in one hand-kept list and missing from another.

The recurrence is the finding: re-implementing someone else's grammar is a completeness claim, and
a completeness claim cannot be finished by patching. So the mechanism changed. `scripts/
tmdl_oracle.py` hands the model to `TmdlSerializer.DeserializeDatabaseFromFolder` (AMO 19.84.1) -
the parser Power BI Desktop itself uses - and reports its verdict, then reads the parse back to
catch the one thing that parser cannot report: a property silently swallowed into an expression.

Every expectation below was measured against that parser. The cases marked `fatal_` are the round-2
reproductions verbatim; the cases marked `valid_` are the false positives it found. Both halves
matter: a gate that rejects a valid model gets switched off, which is strictly worse than a gate
with known blind spots.
"""

# pylint: disable=import-error,wrong-import-position,missing-function-docstring,redefined-outer-name

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
import check_datamodel
from check_datamodel import check_tmdl_model
from tmdl_oracle import OracleUnavailable, absorbed_properties, check_models, dotnet_executable, scrub

DATABASE = "database\n\tcompatibilityLevel: 1702\n"
MODEL = "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\nref table Shipments\n"
PARTITION = '\n\tpartition Shipments = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet S = #table({"Id"},{{1}}) in S\n'

needs_dotnet = pytest.mark.skipif(
    dotnet_executable() is None,
    reason="the TMDL oracle needs the .NET SDK; scripts/preflight.ps1 checks for it",
)


def _absorbing_document(unit: str, absorbed: str) -> str:
    """A table whose measure body sits AT the measure's property indent, so it swallows what follows.

    The indent unit is a parameter because that is exactly what the previous mechanism got wrong:
    it assumed a level is never wider than a tab, and AMO accepts five- and eight-space units.
    """
    return (
        "table Shipments\n"
        f"{unit}measure Probe =\n"
        f"{unit * 2}1\n"
        f"{unit * 2}{absorbed}\n"
        "\n"
        f"{unit}partition Shipments = m\n"
        f"{unit * 2}mode: import\n"
        f"{unit * 2}source =\n"
        f'{unit * 3}let S = #table({{"Id"}},{{{{1}}}}) in S\n'
    )


# name -> (Shipments.tmdl body, the codes the gate must report)
CASES: dict[str, tuple[str, set[str]]] = {
    # --- silently absorbed properties: AMO parses these CLEANLY and the property is lost --------
    "absorb_tab": (_absorbing_document("\t", "isHidden"), {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}),
    "absorb_four_space": (_absorbing_document("    ", "isHidden"), {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}),
    "absorb_five_space": (_absorbing_document("     ", "isHidden"), {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}),
    "absorb_eight_space": (_absorbing_document("        ", "isHidden"), {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}),
    # not the FIRST line after the expression - the previous scanner only inspected that one
    "absorb_later_line": (_absorbing_document("\t", "formatString: 0.0%"), {"TMDL_EXPRESSION_ABSORBS_PROPERTY"}),
    # the swallowed property belongs to the expression's PARENT (Partition.Mode), not to the object
    # that carries the expression (MPartitionSource) - and partition M is where this corpus keeps
    # nearly all of its multi-line expressions
    "absorb_partition_mode": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tsource =\n"
        '\t\t\tlet\n\t\t\tS = #table({"Id"},{{1}})\n\t\t\tin S\n\t\t\tmode: import\n',
        {"TMDL_EXPRESSION_ABSORBS_PROPERTY"},
    ),
    # an M line comment carrying an UNBALANCED quote sits above the swallowed property: without
    # comment scrubbing that quote opens a string literal that blanks the rest of the expression
    # and the real defect disappears
    "absorb_after_comment_with_unbalanced_quote": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tsource =\n"
        '\t\t\tlet\n\t\t\t// rename the "Changed Type step\n\t\t\tS = #table({"Id"},{{1}})\n'
        "\t\t\tin S\n\t\t\tmode: import\n",
        {"TMDL_EXPRESSION_ABSORBS_PROPERTY"},
    ),
    # --- layouts the real parser REFUSES: the model does not open at all ------------------------
    "fatal_uppercase_kind": (
        "table Shipments\n\tMeasure Probe = IF(\n\t\t\t1=1, 1, 0)\n" + PARTITION,
        {"TMDL_PARSER_REJECTED"},
    ),
    "fatal_root_indent": (
        " table Shipments\n   measure Probe =\n       1\n"
        "\n   partition Shipments = m\n     mode: import\n     source =\n"
        '         let S = #table({"Id"},{{1}}) in S\n',
        {"TMDL_PARSER_REJECTED"},
    ),
    "fatal_member_order": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tsource =\n"
        '\t\t\t\tlet S = #table({"Id"},{{1}}) in S\n\t\tmode: import\n',
        {"TMDL_PARSER_REJECTED"},
    ),
    "fatal_fsd_then_ishidden": (
        'table Shipments\n\tmeasure Probe = 1\n\t\tformatStringDefinition =\n\t\t\t\t"0.0%"\n\t\tisHidden\n'
        + PARTITION,
        {"TMDL_PARSER_REJECTED"},
    ),
    "fatal_inline_then_continuation": (
        'table Shipments\n\tmeasure Probe = IF(1=1,\n\t\t\t"a", "b")\n' + PARTITION,
        {"TMDL_PARSER_REJECTED"},
    ),
    # --- valid TMDL: the gate MUST stay silent --------------------------------------------------
    "valid_baseline": ("table Shipments\n\tmeasure Probe = 1\n\t\tisHidden\n" + PARTITION, set()),
    # round-2 false positive: TMDL property names are case-insensitive, AMO sets IsHidden=True
    "valid_uppercase_property": ("table Shipments\n\tmeasure Probe = 1\n\t\tIsHidden\n" + PARTITION, set()),
    # a bare trailing `Source` is ordinary M, and `Partition.Source` is a real TOM property - so a
    # bare word only counts as absorbed when it names a BOOLEAN that is still unset
    "valid_m_ends_with_bare_source": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tmode: import\n\t\tsource =\n"
        '\t\t\t\tlet\n\t\t\t\t\tSource = #table({"Id"},{{1}})\n\t\t\t\tin\n\t\t\t\t\tSource\n',
        set(),
    ),
    "valid_property_text_in_block_comment": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tmode: import\n\t\tsource =\n"
        '\t\t\t\t/*\n\t\t\t\tmode: directQuery\n\t\t\t\t*/\n\t\t\t\tlet S = #table({"Id"},{{1}}) in S\n',
        set(),
    ),
    "valid_property_text_in_string_literal": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tmode: import\n\t\tsource =\n"
        '\t\t\t\tlet cfg = "{\n\t\t\t\tmode: directQuery\n\t\t\t\t}" in cfg\n',
        set(),
    ),
    "valid_multi_line_dax_correctly_indented": (
        "table Shipments\n\tmeasure Probe =\n\t\t\tIF(\n\n\t\t\t\t1 = 1,\n\t\t\t\t2, 3\n\t\t\t)\n"
        "\t\tformatString: 0.0%\n" + PARTITION,
        set(),
    ),
    "valid_nested_format_string_definition": (
        'table Shipments\n\tmeasure Probe = 1\n\t\tformatStringDefinition =\n\t\t\t\t"0.00"\n' + PARTITION,
        set(),
    ),
}

# round-2 false positive #3: `tablePermission` was in one hand-kept list and missing from another,
# so the SECOND permission in a role was rejected. It needs its own model shape (a roles folder).
ROLE_MODEL = (
    "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
    "\nref table Shipments\nref table Other\n\nref role R\n"
)
ROLE_DOCUMENT = (
    "role R\n\tmodelPermission: read\n\n\ttablePermission Shipments = TRUE()\n\ttablePermission Other = TRUE()\n"
)


def _write_model(root: Path, body: str, *, model: str = MODEL, role: str | None = None) -> Path:
    """Materialise a minimal one-table semantic model whose Shipments.tmdl body is `body`."""
    definition = root / "P.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True)
    (definition / "database.tmdl").write_text(DATABASE, encoding="utf-8")
    (definition / "model.tmdl").write_text(model, encoding="utf-8")
    (definition / "tables" / "Shipments.tmdl").write_text(body, encoding="utf-8")
    if role is not None:
        (definition / "roles").mkdir()
        (definition / "roles" / "R.tmdl").write_text(role, encoding="utf-8")
        (definition / "tables" / "Other.tmdl").write_text(
            "table Other\n\tmeasure Other1 = 1\n"
            "\n\tpartition Other = m\n\t\tmode: import\n\t\tsource =\n"
            '\t\t\t\tlet S = #table({"Id"},{{1}}) in S\n',
            encoding="utf-8",
        )
    return root / "P.SemanticModel"


@pytest.fixture(scope="module")
def verdicts(tmp_path_factory) -> dict[str, set[str]]:
    """Build every case once and hand them all to the oracle in a single process."""
    base = tmp_path_factory.mktemp("oracle-cases")
    roots = {name: _write_model(base / name, body) for name, (body, _) in CASES.items()}
    roots["valid_two_table_permissions"] = _write_model(
        base / "valid_two_table_permissions",
        "table Shipments\n\tmeasure Probe = 1\n" + PARTITION,
        model=ROLE_MODEL,
        role=ROLE_DOCUMENT,
    )
    findings, inspected = check_models(list(roots.values()))
    assert inspected == len(roots), "a case was silently skipped - the rest of this file would pass on nothing"
    return {name: {f.code for f in findings if str(root) in str(f.file)} for name, root in roots.items()}


@needs_dotnet
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_oracle_agrees_with_the_real_parser(case, verdicts):
    assert verdicts[case] == CASES[case][1]


@needs_dotnet
def test_two_table_permissions_in_one_role_are_valid(verdicts):
    """Round-2 false positive: AMO parses this and reports permissions=2."""
    assert verdicts["valid_two_table_permissions"] == set()


@needs_dotnet
def test_the_absorption_finding_names_the_document_and_line_of_the_swallowed_property(tmp_path):
    """A finding that cannot be navigated to is barely a finding - Desktop already names no line."""
    root = _write_model(tmp_path, _absorbing_document("\t", "isHidden"))
    findings, _ = check_models([root])
    assert [f.code for f in findings] == ["TMDL_EXPRESSION_ABSORBS_PROPERTY"]
    assert findings[0].file.name == "Shipments.tmdl"
    assert findings[0].line == 4


@needs_dotnet
def test_a_rejected_model_reports_the_parsers_own_document_and_line(tmp_path):
    """AMO knows exactly where it gave up; passing that through is most of the value."""
    root = _write_model(tmp_path, CASES["fatal_member_order"][0])
    findings, _ = check_models([root])
    assert [f.code for f in findings] == ["TMDL_PARSER_REJECTED"]
    assert findings[0].file.name == "Shipments.tmdl"
    assert findings[0].line == 7


# --- the false-positive-critical half, testable without the .NET SDK --------------------------


def test_scrub_blanks_strings_and_comments_without_moving_lines():
    text = 'let a = "x\nformatString: 1\ny" // formatString: 2\nin /* formatString: 3 */ a'
    scrubbed = scrub(text)
    assert scrubbed.count("\n") == text.count("\n")
    assert len(scrubbed) == len(text)
    assert "formatString" not in scrubbed


def test_a_colon_property_line_is_absorbed():
    found = absorbed_properties("1\nformatString: 0.0%", {"formatstring", "ishidden"}, set())
    assert [(f.index, f.name) for f in found] == [(1, "formatString")]


def test_a_bare_word_is_absorbed_only_when_it_names_an_unset_boolean():
    assert absorbed_properties("1\nisHidden", {"ishidden"}, {"ishidden"})
    # `Partition.Source` is a real property but not a boolean, so a trailing M step is not a finding
    assert absorbed_properties("let a = 1\nin\nSource", {"source"}, set()) == []


def test_the_first_line_is_never_treated_as_an_absorbed_property():
    """It IS the expression - the object's own default property, written after `=`."""
    assert absorbed_properties("formatString: 0.0%", {"formatstring"}, set()) == []


def test_property_shaped_text_inside_a_comment_or_string_is_not_absorbed():
    assert absorbed_properties("1\n// isHidden", {"ishidden"}, {"ishidden"}) == []
    assert absorbed_properties("1\n-- isHidden", {"ishidden"}, {"ishidden"}) == []
    assert absorbed_properties('let a = "\nisHidden\n" in a', {"ishidden"}, {"ishidden"}) == []


def test_an_unknown_name_is_not_absorbed():
    """The vocabulary comes from the TOM type by reflection, so 'not a property' means exactly that."""
    assert absorbed_properties("1\nnotAProperty: 7", {"formatstring"}, set()) == []


# --- "could not run" must never be reported as "clean" -----------------------------------------


def test_an_unavailable_oracle_fails_the_run_under_require(monkeypatch):
    def explode(_models):
        raise OracleUnavailable("no dotnet")

    monkeypatch.setattr(check_datamodel, "check_models", explode)
    assert check_datamodel._run_oracle([Path("x")], "require") == (1, False)


def test_an_unavailable_oracle_only_warns_by_default(monkeypatch):
    def explode(_models):
        raise OracleUnavailable("no dotnet")

    monkeypatch.setattr(check_datamodel, "check_models", explode)
    assert check_datamodel._run_oracle([Path("x")], "auto") == (0, False)


def test_no_oracle_reports_that_nothing_was_checked():
    assert check_datamodel._run_oracle([Path("x")], "off") == (0, False)


# --- the committed corpus ----------------------------------------------------------------------


@needs_dotnet
def test_the_cli_reports_the_committed_corpus_clean_by_exit_code():
    """Judged by exit code, not by printed text: the previous round's harness scored a false
    positive by matching the string "ERROR" against the gate's own log header.
    """
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_datamodel.py"), "--all", "--require-oracle"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- unassessable must never look clean --------------------------------------------------------


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
    .github/skills/pbip-model-refresh/SKILL.md. This is the one TMDL check that is deliberately
    STRICTER than the oracle, and the comment is why.
    """
    definition = tmp_path / "M.SemanticModel" / "definition"
    definition.mkdir(parents=True)
    (definition / "t.tmdl").write_bytes("\ufefftable T\n\tmeasure 'M' = 1\n".encode("utf-8"))
    findings, _ = check_tmdl_model(tmp_path / "M.SemanticModel")
    assert "TMDL_BOM" in {f.code for f in findings}
