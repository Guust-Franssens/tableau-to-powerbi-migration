"""Tests for scripts/check_datamodel.py.

Desktop reports a broken model as an unlocalised `Token ',' expected.`; the checker turns that into
`file:line:col`, so it must be both accurate AND quiet (a noisy checker gets switched off).
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
    iter_m_blocks_text,
    main,
)

DUMMY = Path("model.tmdl")
TMDL_DUMMY = Path("Table.tmdl")
BIFF8 = b"\xd0\xcf\x11\xe0"
NAME_NAV = 'Source{[Name="Orders"]}[Data]'
ITEM_NAV = 'Source{[Item="Orders", Kind="Sheet"]}[Data]'
TYPED = 'Table.TransformColumnTypes(Orders, {{"Sales", type number}})'


def _kinds(m_expression: str) -> set[str]:
    return {f.kind for f in _check_expression(DUMMY, m_expression)}


def _tmdl_codes(text: str) -> set[str]:
    return {f.code for f in check_tmdl_text(TMDL_DUMMY, text)}


def _model(root: Path, name: str, files: dict[str, str]) -> Path:
    """Write `<root>/<name>.SemanticModel/definition/<relpath>` files and return the model dir."""
    model = root / f"{name}.SemanticModel"
    for rel, text in files.items():
        path = model / "definition" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return model


# Each entry is valid M a real model carries, including every pinned adversarial false positive.
GOOD_M = [
    'let Source = #table({"A", "B"}, {{1, 2}}) in Source',
    'let S = Table.SelectRows(T, each [Country Name] <> "Greenland") in S',
    'let S = Table.TransformColumnTypes(T, {{"A", type text}, {"B", Int64.Type}}, "en-US") in S',
    'let S = Table.AddColumn(T, "Q", each "Q" & Text.From([Quarter Number]), type text) in S',
    '"a plain text parameter" meta [IsParameterQuery=true, Type="Text"]',
    "let x = if a then b else c, y = try x otherwise null in y",
    'let S = Csv.Document(File.Contents(DataFolder & "orders.csv"), [Delimiter=",", Encoding=65001]) in S',
    'let S = "he said ""hi""" in S',
    'let Types = {{"Amount", type number}}, Source = Table.TransformColumnTypes(T, Types) in Source',
    'let Pair = {"Amount", type number}, Source = Table.TransformColumnTypes(T, {Pair}) in Source',
    "let Source = Table.TransformColumnTypes(T, {}) in Source",
    'let Source = try "C:\\" otherwise "fallback" in Source',
    'let Source = "C:\\" ?? "fallback" in Source',
    'let S = "a { unclosed [ brace", // ) stray\n    T = 1 in T',
    "let mode = 1 in mode",
    "let annotation = 1 in annotation",
    "[let = 1]",
    "each [let value]",
    "type table [let = text]",
    "{try 1 catch () => 0}",
    "{0x0}",
]

BAD_M = [
    ('let S = #table({"A"}, {{1}, {2},}) in S', "TRAILING_COMMA"),
    ('let S = #table({"A" "B"}, {{1, 2}}) in S', "MISSING_SEPARATOR"),
    ('let S = Table.TransformColumnTypes(T, {{"A", type text}} in S', "UNBALANCED"),
    ('let S = #table({"A"}, {{1}}) in S)', "UNBALANCED"),
    ('let S = Record.Field(r, "a"} in S', "UNBALANCED"),
    ('let Source = #table({"A"}, {{1}})', "LET_WITHOUT_IN"),
    ('let S = "unterminated in S', "UNTERMINATED"),
    ("let S = 1 /* unterminated in S", "UNTERMINATED"),
    ('let Source = "a \\"quoted\\" value" in Source', "INVALID_STRING_ESCAPE"),
    ('let Source = "Say \\"!\\"" in Source', "INVALID_STRING_ESCAPE"),
    ('let Source = "foo\\"inside\\" stuff" in Source', "INVALID_STRING_ESCAPE"),
    ('let Source = "foo\\" & \\"bar" in Source', "INVALID_STRING_ESCAPE"),
    (
        'let Source = Table.TransformColumnTypes(T, {{{"Amount", type number}}}) in Source',
        "INVALID_TRANSFORM_COLUMN_TYPE_PAIR",
    ),
    (
        'let Source = Table.TransformColumnTypes(T, {{{"Amount", type number}, {"Count", Int64.Type}}}) in Source',
        "INVALID_TRANSFORM_COLUMN_TYPE_PAIR",
    ),
]


def test_m_expression_classifier_is_precise() -> None:
    """Zero false positives on valid M, and every distinct syntax-error branch names its kind."""
    noisy = {e: _kinds(e) for e in GOOD_M if _check_expression(DUMMY, e)}
    assert not noisy, f"false positives: {noisy}"
    missed = [(e, k) for e, k in BAD_M if k not in _kinds(e)]
    assert not missed


def test_model_walk_reads_m_skips_dax_and_localises(tmp_path: Path) -> None:
    """`= calculated` is DAX and skipped; real M is read; line and column point at the real defect."""
    assert (
        check_model(
            _model(
                tmp_path,
                "Calc",
                {
                    "tables/Params.tmdl": "table Params\n\tpartition Params = calculated\n\t\tmode: import\n"
                    "\t\tsource = {(\"Sales\", NAMEOF('Sales'[Amount]), 0)}\n",
                    "expressions.tmdl": 'expression DataFolder = "C:\\data\\" meta [IsParameterQuery=true, Type="Text"]\n',
                },
            )
        )
        == []
    )
    deferred = "table T\n\tpartition T = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n"
    deferred += '\t\t\t\t\tSource = #table({"A"}, {{1},}) \n\t\t\t\tin\n\t\t\t\t\tSource\n'
    findings = check_model(_model(tmp_path, "L", {"tables/T.tmdl": deferred}))
    assert [(f.kind, f.line) for f in findings] == [("TRAILING_COMMA", 6)]
    line = "\t\tsource = let S = {1,} in S"
    findings = check_model(
        _model(tmp_path, "C", {"tables/T.tmdl": f"table T\n\tpartition T = m\n\t\tmode: import\n{line}\n"})
    )
    assert [(f.kind, f.col) for f in findings] == [("TRAILING_COMMA", line.index(",}") + 1)]


def test_non_utf8_and_empty_models_do_not_pass_as_checked(tmp_path: Path) -> None:
    """A stray byte must not crash; "no findings" and "nothing scanned" must be distinguishable."""
    bad = tmp_path / "B.SemanticModel" / "definition" / "tables"
    bad.mkdir(parents=True)
    (bad / "bad.tmdl").write_bytes(b"table \x96\n")
    assert check_model(tmp_path / "B.SemanticModel") == []
    empty = tmp_path / "Empty.SemanticModel"
    (empty / "definition").mkdir(parents=True)
    assert check_model_counted(empty) == ([], 0)
    lone = tmp_path / "lone"
    lone.mkdir()
    assert main([str(lone)]) == 2


def test_non_m_source_body_cannot_introduce_m_headers_but_later_siblings_are_checked(tmp_path: Path) -> None:
    text = (
        "table Calc\n"
        "\tpartition Calc = calculated\n"
        "\t\tsource =\n"
        "\t\t\t/*\n"
        "\t\t\texpression Fake = File.Contents(Missing)\n"
        "\t\t\tpartition Fake = m\n"
        "\t\t\tsource = Folder.Files(Missing)\n"
        "\t\t\t*/\n"
        "\t\t\tVAR expression = { 1 }\n"
        "\t\t\tRETURN\n"
        "\t\t\t\texpression\n"
        "\tpartition Later = m\n"
        "\t\tsource = {1, 2,}\n"
        'expression Caption = "kept"\n'
    )
    model = _model(tmp_path, "Calc", {"tables/Calc.tmdl": text})
    expected = [("{1, 2,}", 13, len("\t\tsource = ")), ('"kept"', 14, len("expression Caption = "))]
    assert iter_m_blocks_text(text) == expected
    assert _iter_m_blocks(model / "definition" / "tables" / "Calc.tmdl") == expected
    assert [(finding.kind, finding.line) for finding in check_model(model)] == [("TRAILING_COMMA", 13)]


@pytest.mark.parametrize(
    ("newline", "bom", "name"),
    [
        ("\n", "", "'Owner''s = Root'"),
        ("\r\n", "\ufeff", "'Owner''s \"Archive\" Root'"),
        ("\n", "\ufeff", '#"Legacy = ""Archive"" Root"'),
    ],
)
def test_held_text_extractor_keeps_quoted_headers_and_matches_the_path_wrapper(
    tmp_path: Path, newline: str, bom: str, name: str
) -> None:
    """Quoted headers survive; `lineageTag`/`annotation` lines and calculated neighbours are not M."""
    header = f'expression {name} = "Q:\\archive\\" meta [IsParameterQuery=true, Type="Text"]'
    text = bom + newline.join(
        [
            header,
            "\tlineageTag: fixed-fixture",
            '\tannotation Note = "File.Contents(NotAnMReference)"',
            "table Rows",
            "\tpartition 'Owner''s = Partition' = m",
            "\t\tmode: import",
            "\t\tsource =",
            "\t\t\tlet rows = {1} in rows",
            "\t\tannotation Note = {1, 2,}",
            "\tpartition 'Calculated = Neighbor' = calculated",
            "\t\tsource = {(\"DAX\", NAMEOF('Rows'[Value]), 0)}",
            "",
        ]
    )
    expected = [
        ('"Q:\\archive\\" meta [IsParameterQuery=true, Type="Text"]', 1, header.index('"Q:')),
        ("\n\t\t\tlet rows = {1} in rows", 7, len("\t\tsource =")),
    ]
    path = tmp_path / "quoted.tmdl"
    path.write_bytes(text.encode("utf-8"))
    assert iter_m_blocks_text(text) == expected
    assert _iter_m_blocks(path) == expected


def test_held_text_extraction_has_no_io_and_retains_existing_path_decode_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "replacement.tmdl"
    path.write_bytes(b'expression Caption = "\xff"\r\n')
    assert _iter_m_blocks(path) == [('"\ufffd"', 1, len("expression Caption = "))]

    def no_read(*_args, **_kwargs):
        pytest.fail("held-text extraction performed filesystem I/O")

    monkeypatch.setattr(Path, "open", no_read)
    assert iter_m_blocks_text('expression Caption = "kept"\n') == [('"kept"', 1, len("expression Caption = "))]


def test_committed_example_models_are_clean_for_m_and_tmdl() -> None:
    """All 16 shipped models open in Desktop, so any finding is a false positive."""
    models = sorted((REPO_ROOT / "examples").glob("*/fabric/*.SemanticModel"))
    assert len(models) == 16
    offenders = {}
    for model in models:
        tmdl, _ = check_tmdl_model(model)
        kinds = [f.kind for f in check_model(model)] + [f"{f.code} {f.file.name}:{f.line}" for f in tmdl]
        if kinds:
            offenders[model.name] = kinds
    assert not offenders, f"false positives on known-good models: {offenders}"


GOOD_TMDL = [
    "table S\n\tmeasure 'A' = SUM('S'[x])\n\t\tformatString: 0.00\n\n\tcolumn x\n\t\tdataType: int64\n",
    "table S\n\tcolumn x\n\t\tdataType: int64\n\t\tannotation a = 1\n\t\tannotation b = 2\n",
    "table S\n\tmeasure 'A' = 1\n\t\tformatString: 0.00\n\n\tmeasure 'B' = 2\n\t\tformatString: 0.00\n",
    "table S\n\t/// formatString: not a property, this is prose\n\tmeasure 'A' = 1\n\t\tformatString: 0.00\n",
    "table Orders\n\tcolumn Sales\n\t\tdataType: double\n\ntable _Measures\n\tmeasure Sales = SUM('Orders'[Sales])\n",
]

# (text, code expected present, code expected absent)
TMDL_CASES = [
    (
        "table S\n\tmeasure 'A' = SUM('S'[x])\n\t\tformatString: 0.00%\n\t\tformatString: $#,##0.00\n",
        "DUPLICATE_PROPERTY",
        None,
    ),
    (
        "table S\n\tmeasure 'Profit' = SUM('S'[Profit])\n\n\tcolumn Profit\n\t\tdataType: double\n",
        "NAME_COLLISION",
        None,
    ),
    (
        "table S\n\tcolumn Profit\n\t\tdataType: double\n\n\tmeasure 'Profit' = SUM('S'[Profit])\n",
        "NAME_COLLISION",
        None,
    ),
    ("table S\n\tmeasure 'Broken' =\n\t\tformatString: 0.00\n", "EMPTY_EXPRESSION", None),
    ("table S\n\tmeasure 'A' = CALCULATE(SUM('S'[Profit]), 'S'[Region] = [Region Param])\n", "COMPACT_FILTER", None),
    ("table S\n\tmeasure 'F' = DIVIDE([Errors], [Total]) < 0.05\n", "UNGUARDED_BLANK_THRESHOLD", None),
    (
        "table S\n\tmeasure 'F' = IF([R] = \"W\", DIVIDE([Errors], [Total]) < 0.05, BLANK())\n",
        "UNGUARDED_BLANK_THRESHOLD",
        None,
    ),
    ("table S\n\tmeasure 'F' = DIVIDE([Errors], [Total]) /* note */ < 0.05\n", "UNGUARDED_BLANK_THRESHOLD", None),
    ("table S\n\tmeasure 'F' = DIVIDE([Errors], /* ) */ [Total]) < 0.05\n", "UNGUARDED_BLANK_THRESHOLD", None),
    ("table S\n\tcolumn 'Below' = DIVIDE('S'[Errors], 'S'[Total]) <= 0.1\n", "UNGUARDED_BLANK_THRESHOLD", None),
    (
        "table S\n\tmeasure 'F' = IF(ISBLANK(DIVIDE([Errors], [Total])), BLANK(), DIVIDE([Errors], [Total]) < 0.05)\n",
        None,
        "UNGUARDED_BLANK_THRESHOLD",
    ),
    ("table S\n\tmeasure 'Literal' = DIVIDE(1, 1) < 2\n", None, "UNGUARDED_BLANK_THRESHOLD"),
    (
        "table S\n\tmeasure 'A' = SUM('S'[x]) // example: DIVIDE([Errors], [Total]) < 0.05\n",
        None,
        "UNGUARDED_BLANK_THRESHOLD",
    ),
    ("table S\n\tmeasure 'A' = \"example: DIVIDE([Errors], [Total]) < 0.05\"\n", None, "UNGUARDED_BLANK_THRESHOLD"),
    (
        "table S\n\tmeasure 'A' = DIVIDE([Errors], [Total] < 0.05\n",
        "BLANK_THRESHOLD_CANNOT_ASSESS",
        "UNGUARDED_BLANK_THRESHOLD",
    ),
    (
        "table S\n\tmeasure 'A' =\n\t\tDIVIDE([E], [T]) < 0.05\n\t\tformatString: 0.00\n",
        "BLANK_THRESHOLD_CANNOT_ASSESS",
        None,
    ),
    ("table S\n\tcolumn 'A' =\n\t\tDIVIDE([E], [T]) < 0.05\n\tcolumn 'B'\n", "BLANK_THRESHOLD_CANNOT_ASSESS", None),
    ("table S\n\tmeasure 'A' =\n\tmeasure 'B' = SUM('S'[x])\n", "EMPTY_EXPRESSION", "BLANK_THRESHOLD_CANNOT_ASSESS"),
]


def test_tmdl_text_checks_are_precise() -> None:
    """Valid TMDL is silent; each walker branch emits (or deliberately withholds) its code."""
    assert {i: check_tmdl_text(TMDL_DUMMY, t) for i, t in enumerate(GOOD_TMDL) if check_tmdl_text(TMDL_DUMMY, t)} == {}
    wrong = []
    for text, present, absent in TMDL_CASES:
        codes = _tmdl_codes(text)
        if (present and present not in codes) or (absent and absent in codes):
            wrong.append((text, codes))
    assert not wrong
    assert find_incomplete_divide_calls("DIVIDE([Errors], [Total] < 0.05")
    assert ADVISORY_TMDL_CODES == {"UNGUARDED_BLANK_THRESHOLD", "BLANK_THRESHOLD_CANNOT_ASSESS"}


@pytest.mark.parametrize(
    ("orders_measure", "other_measure", "duplicate"),
    [
        ("Sales", "Sales", True),  # #413: invisible to a single-file scan, refused by Desktop
        ("sales", "Sales", True),  # case-insensitive, as TOM
        ("Sales", "Profit", False),
        ("'O''Brien Sales'", "'O''Connor Sales'", False),  # doubled apostrophe is not a terminator
        ("'O''Brien Sales'", "'O''Brien Sales'", True),
    ],
)
def test_measure_names_are_unique_across_the_whole_model(
    tmp_path: Path, orders_measure, other_measure, duplicate
) -> None:
    model = _model(
        tmp_path,
        "M",
        {
            "Orders.tmdl": f"table Orders\n\tmeasure {orders_measure} = SUM('Orders'[Amount])\n\n\tcolumn Amount\n\t\tdataType: double\n",
            "_Measures.tmdl": f"table _Measures\n\tmeasure {other_measure} = SUM('Orders'[Amount])\n",
        },
    )
    findings, _ = check_tmdl_model(model)
    assert ("MEASURE_NAME_DUPLICATE" in {f.code for f in findings}) is duplicate


COMPACT_FILTER_CASES = [
    (True, "CALCULATE(SUM('S'[Profit]), 'S'[Region] = [Region Param])"),
    (True, "CALCULATETABLE(VALUES('S'[X]), 'S'[Year] = [Selected Year])"),
    (False, "CALCULATE(SUM('S'[Profit]), FILTER(ALL('S'[Region]), 'S'[Region] = [Region Param]))"),
    (False, "CALCULATE(SUM('S'[Profit]), 'S'[Region] = \"West\")"),
    (False, "CALCULATE(SUM('S'[Profit]), ALL('Date'))"),
    (False, "VAR _m = [Region Param] RETURN CALCULATE(SUM('S'[Profit]), 'S'[Region] = _m)"),
]

# Positive: unguarded, and 0 satisfies the comparison. A 3-arg DIVIDE does not save a blank numerator,
# and a blank alt is no substitute. Negative: guarded, provably non-blank, or the safe direction.
UNGUARDED_BLANK_THRESHOLD_CASES = [
    (True, "DIVIDE([Errors], [Total]) < 0.05"),
    (True, "DIVIDE([Errors], [Total]) <= 0"),
    (True, "DIVIDE([Errors], [Total]) = 0"),
    (True, "DIVIDE('S'[Errors], 'S'[Total]) > -5"),
    (True, "DIVIDE([Errors], [Total], 0) < 0.05"),
    (True, "DIVIDE([Errors], [Total], [Fallback]) < 0.05"),
    (True, 'IF([Region] = "West", DIVIDE([Errors], [Total]) < 0.05, BLANK())'),
    (True, "CALCULATE(SUM('S'[x]), FILTER('S', DIVIDE([Errors], [Total]) < 0.05))"),
    (True, "DIVIDE([Errors], [Total]) /* note */ < 0.05"),
    (True, "DIVIDE([Errors], /* ) */ [Total]) < 0.05"),
    (False, "IF(ISBLANK(DIVIDE([Errors], [Total])), BLANK(), DIVIDE([Errors], [Total]) < 0.05)"),
    (False, "DIVIDE(1, 1) < 2"),
    (False, "DIVIDE(COALESCE([Errors], 0), 1) < 0.05"),
    (False, "DIVIDE([Errors], [Total]) > 0.05"),
    (False, "DIVIDE([Sales], [Target]) >= 100"),
    (False, "SUM('S'[Sales]) > 0"),
    (False, "'S'[Status] = \"Active\""),
    (False, "NOT ISBLANK([Errors]) && DIVIDE([Errors], [Total]) > 0.05"),
    (False, "COALESCE(DIVIDE([Errors], [Total]), 0) < 0.05"),
    (False, "(DIVIDE([Errors], [Total]) + 0) > 0.05"),
]


def test_dax_predicates_are_precise() -> None:
    wrong = [e for illegal, e in COMPACT_FILTER_CASES if find_compact_filters(e) is not illegal]
    wrong += [
        e for illegal, e in UNGUARDED_BLANK_THRESHOLD_CASES if bool(find_unguarded_divide_thresholds(e)) is not illegal
    ]
    assert not wrong


def test_full_datamodel_gate_has_no_false_positive_on_valid_model(tmp_path: Path) -> None:
    model = _model(
        tmp_path,
        "Good",
        {
            "tables/S.tmdl": "table S\n\tmeasure 'A' = SUM('S'[x])\n\t\tformatString: 0.00\n\n\tcolumn x\n\t\tdataType: int64\n\n"
            '\tpartition S = m\n\t\tmode: import\n\t\tsource = let Source = #table({"x"}, {{1}}) in Source\n'
        },
    )
    assert check_datamodel(model) == ([], 1, [], 1)


def _xls_backed_model(
    root: Path, *, magic: bytes, navigation: str, typed: str | None, filename: str = "Orders.xls"
) -> Path:
    """A one-partition model reading `root/<filename>`, whose first four bytes are ``magic``."""
    source = root / filename
    source.write_bytes(magic + b"payload")
    steps = [f'Source = Excel.Workbook(File.Contents("{source.as_posix()}"), null, true)', f"Orders = {navigation}"]
    if typed is not None:
        steps.append(f"Typed = {typed}")
    body = (",\n" + "\t" * 4).join(steps)
    tail = "Typed" if typed is not None else "Orders"
    text = f"table Orders\n\tpartition Orders = m\n\t\tmode: import\n\t\tsource = let\n\t\t\t\t{body}\n\t\t\tin\n\t\t\t\t{tail}\n"
    return _model(root, "Model", {"tables/Orders.tmdl": text})


@pytest.mark.parametrize(
    ("magic", "navigation", "typed", "filename", "expected"),
    [
        (BIFF8, NAME_NAV, TYPED.replace("}})", '}}, "en-BE")'), "Orders.xls", set()),  # the proven legacy form
        (b"PK\x03\x04", ITEM_NAV, TYPED, "Orders.xls", set()),  # magic bytes, not the extension, decide
        (BIFF8, NAME_NAV, None, "Orders.xls", set()),  # no conversion: no culture to get wrong
        (BIFF8, NAME_NAV, TYPED.replace("}})", "}}, null)"), "Orders.xls", {"BIFF8_XLS_CULTURE"}),
        (BIFF8, ITEM_NAV, TYPED, "Orders.xlsx", set()),  # deliberate miss: scoped to the .xls suffix
    ],
    ids=["clean", "modern-named-xls", "no-type-step", "null-culture", "xlsx-suffix-scoped"],
)
def test_biff8_xls_gate_branches(tmp_path: Path, magic, navigation, typed, filename, expected) -> None:
    model = _xls_backed_model(tmp_path, magic=magic, navigation=navigation, typed=typed, filename=filename)
    assert {finding.kind for finding in check_model(model)} == expected


def test_biff8_xls_requires_name_navigation_and_explicit_culture(tmp_path: Path) -> None:
    """Commented or quoted navigation is not navigation; a culture-looking string is not a culture."""
    (tmp_path / "Orders.xls").write_bytes(BIFF8 + b"BIFF8")
    model = _model(
        tmp_path,
        "Bad",
        {
            "expressions.tmdl": f'expression SourceFolder = "{tmp_path.as_posix()}" meta [IsParameterQuery=true, Type="Text"]\n',
            "tables/Orders.tmdl": "table Orders\n\tpartition Orders = m\n\t\tmode: import\n\t\tsource = let\n"
            '\t\t\t\tSource = Excel.Workbook(File.Contents(SourceFolder & "/Orders.xls"), null, true),\n'
            '\t\t\t\t// Source{[Name="Orders"]}[Data] is not executable navigation,\n'
            '\t\t\t\tNote = "use Source{[Name=""Orders""]}[Data]" is also not navigation,\n'
            '\t\t\t\tOrders = Source{[Item="Orders", Kind="Sheet"]}[Data],\n'
            '\t\t\t\tTyped = Table.TransformColumnTypes(Orders, {{"Sales", type number}}, '
            '[MissingField=if "Culture=en-BE" <> "" then MissingField.Ignore else MissingField.Error])\n'
            "\t\t\tin\n\t\t\t\tTyped\n",
        },
    )
    assert {"BIFF8_XLS_NAVIGATION_KEY", "BIFF8_XLS_CULTURE"} <= {f.kind for f in check_model(model)}


def _cli_model(root: Path, measure: str) -> None:
    _model(
        root,
        "Advisory",
        {
            "tables/S.tmdl": f"table S\n{measure}\tcolumn x\n\t\tdataType: int64\n\n\tpartition S = m\n\t\tmode: import\n"
            '\t\tsource = let Source = #table({"x"}, {{1}}) in Source\n'
        },
    )


def test_cli_exit_codes_keep_advisories_out_of_exit_one(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Advisory codes are reported but exit 0; a real error still exits 1; BIFF8 reaches the exit code."""
    cases = [
        (
            "\tmeasure 'Flag' = DIVIDE([Errors], [Total]) < 0.05\n\t\tformatString: 0.00\n\n",
            0,
            ["UNGUARDED_BLANK_THRESHOLD", "BLANK()-THRESHOLD ADVISORY", "manual"],
        ),
        (
            "\tmeasure 'Flag' =\n\t\tDIVIDE([Errors], [Total]) < 0.05\n\t\tformatString: 0.00\n\n",
            0,
            ["BLANK_THRESHOLD_CANNOT_ASSESS"],
        ),
        (
            "\tmeasure 'Flag' = DIVIDE([Errors], [Total]) < 0.05\n\t\tformatString: 0.00\n\t\tformatString: 0.00\n\n",
            1,
            ["DUPLICATE_PROPERTY", "UNGUARDED_BLANK_THRESHOLD"],
        ),
    ]
    for index, (measure, code, needles) in enumerate(cases):
        root = tmp_path / str(index)
        _cli_model(root, measure)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert main([str(root)]) == code
        assert all(n in caplog.text for n in needles), (index, caplog.text)
    xls = tmp_path / "xls"
    xls.mkdir()
    _xls_backed_model(xls, magic=BIFF8, navigation=ITEM_NAV, typed=TYPED)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert main([str(xls)]) == 1
    assert "BIFF8_XLS_NAVIGATION_KEY" in caplog.text and "BIFF8_XLS_CULTURE" in caplog.text
