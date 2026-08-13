r"""Tests for scripts/check_empty_model.py - the gate against a model that opens and loads no rows.

Every fixture here is written from a REAL emitted artifact, not invented. The shapes come from a
38-workbook estate run (2026-08-12, engine 2.126.0):

* the failing one - `Excel.Workbook(File.Contents("<absolute path from the authoring Mac>"))`,
  `mode: import`, in a model whose workbook came back `definition_of_done: warn` and `report_bound:
  true`. It builds, it binds, it validates, it deploys, and it contains nothing.
* the healthy landed one - `Csv.Document(File.Contents("<bundle>/data/.../Extract_Extract.csv"))`.
* the healthy PARAMETERISED one - `Csv.Document(File.Contents(#"SourceFolder" & "\public_Extract.csv"))`
  with `expression SourceFolder = "..."` in expressions.tmdl. 22 of that estate's 34 file-backed
  partitions were this shape, so a checker that only understood literals would have judged a third of
  its own subject matter and silently skipped the rest.
* the live one - `mode: directQuery` over `Snowflake.Databases(...)`. 58 partitions in that estate.
  A false alarm on these would have blocked a correct migration of a real customer estate, so the
  tests that prove they are NOT flagged carry as much weight as the one that proves detection.
* the needs-review stub - `#table(type table [], {})`. Already reported loudly by the engine as
  "N table(s) landed as a needs-review partition stub"; flagging it here would double-count.

The paths in these fixtures are synthetic. The repo is public.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_empty_model as cem  # noqa: E402  # pylint: disable=wrong-import-position

# The shape that ships empty: an absolute path belonging to the machine the Tableau workbook was
# authored on. Kept POSIX so it is foreign on the Windows CI/dev host and vice versa.
MAC_AUTHOR_PATH = "/Users/tableau-author/Datasets/Global Superstore.xlsx"
WINDOWS_AUTHOR_PATH = r"D:\Datasets\Global Superstore.xlsx"


def _model(
    root: Path,
    name: str = "Orders (Global Superstore).SemanticModel",
    tables: dict[str, str] | None = None,
    expressions: str = "",
) -> Path:
    """Write a minimal but structurally real `.SemanticModel` folder."""
    model = root / name
    definition = model / "definition"
    (definition / "tables").mkdir(parents=True, exist_ok=True)
    if expressions:
        (definition / "expressions.tmdl").write_text(expressions, encoding="utf-8")
    for table, tmdl in (tables or {}).items():
        (definition / "tables" / f"{table}.tmdl").write_text(tmdl, encoding="utf-8")
    return model


def _excel_partition(path: str, mode: str = "import") -> str:
    return (
        "table Orders\n\n"
        "\tpartition 'Orders$' = m\n"
        f"\t\tmode: {mode}\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t\tSource = Excel.Workbook(File.Contents("{path}"), null, true),\n'
        '\t\t\t\tNavigation = Source{[Item="Orders", Kind="Sheet"]}[Data]\n'
        "\t\t\tin\n"
        "\t\t\t\tNavigation\n"
    )


def _csv_partition(expression: str, mode: str = "import", table: str = "Extract") -> str:
    return (
        f"table {table}\n\n"
        f"\tpartition {table} = m\n"
        f"\t\tmode: {mode}\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t\tSource = Csv.Document(File.Contents({expression}), [Delimiter=",", Encoding=1252]),\n'
        "\t\t\t\tPromoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])\n"
        "\t\t\tin\n"
        "\t\t\t\tPromoted\n"
    )


SNOWFLAKE_DIRECTQUERY = (
    "table FACT_ORDERS\n\n"
    "\tpartition FACT_ORDERS = m\n"
    "\t\tmode: directQuery\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    '\t\t\t\tSource = Snowflake.Databases(#"Server", #"Warehouse"),\n'
    '\t\t\t\tDb = Source{[Name="MERIDIAN", Kind="Database"]}[Data]\n'
    "\t\t\tin\n"
    "\t\t\t\tDb\n"
)

SNOWFLAKE_IMPORT = SNOWFLAKE_DIRECTQUERY.replace("mode: directQuery", "mode: import")

CALCULATED_DATE = (
    "table Date\n\n"
    "\tpartition Date = calculated\n"
    "\t\tmode: import\n"
    "\t\tsource = CALENDAR(DATE(2020, 1, 1), DATE(2026, 12, 31))\n"
)

NEEDS_REVIEW_STUB = (
    "table 'sqlproxy (Groups)'\n\n"
    "\tpartition sqlproxy = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    "\t\t\t\t// TODO: complete the M partition for connector class 'csv' using Csv.Document\n"
    "\t\t\t\tSource = #table(type table [], {})\n"
    "\t\t\tin\n"
    "\t\t\t\tSource\n"
)


def _landed_csv(path: Path, rows: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"a{i},b{i}" for i in range(rows))
    path.write_text("col_a,col_b\n" + (body + "\n" if rows else ""), encoding="utf-8")
    return path


def _categories(model_dir: Path, root: Path | None = None) -> dict[str, int]:
    return cem.scan_model(model_dir, root or model_dir.parent)["categories"]


# ---------------------------------------------------------------------------
# The failure this module exists for
# ---------------------------------------------------------------------------


def test_an_unlanded_flat_file_import_is_reported_as_empty(tmp_path: Path) -> None:
    """The measured silent success: builds, binds, validates, deploys, contains zero rows.

    This is the whole point of the gate. If this test can pass while the check is disabled, the gate
    is decorative.
    """
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "global_superstores_db", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH)})

    report = cem.scan(bundle)

    assert report["status"] == "EMPTY_MODELS"
    assert report["models_empty"] == 1
    finding = report["models"][0]["findings"][0]
    assert finding["category"] == cem.CATEGORY_FOREIGN
    assert finding["path"] == MAC_AUTHOR_PATH


def test_a_missing_native_path_is_reported_as_empty(tmp_path: Path) -> None:
    """Same shape, same host: the file simply is not there. Nothing about it errors at build time."""
    bundle = tmp_path / "bundle"
    absent = tmp_path / "never-landed" / "Orders.csv"
    _model(bundle / "pbip" / "wb", tables={"Extract": _csv_partition(f'"{absent.as_posix()}"')})

    findings = cem.scan(bundle)["models"][0]["findings"]

    assert [f["category"] for f in findings] == [cem.CATEGORY_MISSING]


def test_a_header_only_csv_is_reported_as_empty(tmp_path: Path) -> None:
    """A landed file is not the same as landed DATA - a materializer can emit a header and no rows."""
    bundle = tmp_path / "bundle"
    landed = _landed_csv(bundle / "data" / "Extract.csv", rows=0)
    _model(bundle / "pbip" / "wb", tables={"Extract": _csv_partition(f'"{landed.as_posix()}"')})

    findings = cem.scan(bundle)["models"][0]["findings"]

    assert [f["category"] for f in findings] == [cem.CATEGORY_EMPTY]


def test_a_zero_byte_binary_source_is_reported_as_empty(tmp_path: Path) -> None:
    """xlsx/parquet cannot be counted offline, but a zero-byte one is unambiguous."""
    bundle = tmp_path / "bundle"
    landed = bundle / "data" / "Sales.xlsx"
    landed.parent.mkdir(parents=True)
    landed.write_bytes(b"")
    _model(bundle / "pbip" / "wb", tables={"Orders": _excel_partition(landed.as_posix())})

    assert [f["category"] for f in cem.scan(bundle)["models"][0]["findings"]] == [cem.CATEGORY_EMPTY]


def test_a_landed_csv_with_rows_is_not_flagged(tmp_path: Path) -> None:
    """The control for every test above."""
    bundle = tmp_path / "bundle"
    landed = _landed_csv(bundle / "data" / "Extract.csv")
    _model(bundle / "pbip" / "wb", tables={"Extract": _csv_partition(f'"{landed.as_posix()}"')})

    report = cem.scan(bundle)

    assert report["status"] == "OK"
    assert report["models"][0]["categories"] == {cem.CATEGORY_FILE_OK: 1}


# ---------------------------------------------------------------------------
# False-positive posture: a blocked customer estate is a real cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["directQuery", "dual"])
def test_a_live_partition_is_never_flagged(tmp_path: Path, mode: str) -> None:
    """A live source legitimately has no local rows. Flagging it turns a correct migration into an
    outage report. 58 of the measured estate's partitions were `directQuery`."""
    bundle = tmp_path / "bundle"
    tmdl = SNOWFLAKE_DIRECTQUERY.replace("mode: directQuery", f"mode: {mode}")
    _model(bundle / "pbip" / "Meridian_Revenue_by_Region", tables={"FACT_ORDERS": tmdl})

    report = cem.scan(bundle)

    assert report["status"] == "OK"
    assert report["models"][0]["categories"] == {"live": 1}


def test_a_live_partition_over_a_FILE_connector_is_still_live(tmp_path: Path) -> None:
    """Mode is decided BEFORE source shape.

    A `directQuery` partition whose M happens to mention a file must not be judged on the file, or
    the ordering of the classifier silently becomes the gate's correctness condition.
    """
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH, mode="directQuery")})

    assert cem.scan(bundle)["status"] == "OK"


def test_an_import_over_a_remote_connector_is_not_judged(tmp_path: Path) -> None:
    """Import over Snowflake has rows or not depending on a server. Offline that is unknowable, and
    unknowable must never be reported as empty."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"FACT_ORDERS": SNOWFLAKE_IMPORT})

    report = cem.scan(bundle)

    assert report["status"] == "OK"
    assert report["models"][0]["categories"] == {"remote_import": 1}


def test_a_calculated_partition_is_not_judged(tmp_path: Path) -> None:
    """`= calculated` is DAX over other tables - it has no source file by design."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Date": CALCULATED_DATE})

    assert cem.scan(bundle)["models"][0]["categories"] == {"calculated": 1}


def test_a_needs_review_stub_is_counted_but_does_not_block(tmp_path: Path) -> None:
    """Deliberate non-escalation.

    The engine ALREADY says "N table(s) landed as a needs-review partition stub" in the definition of
    done. Blocking here too would make this gate fire on workbooks that are already reported, which
    is how a new check earns a reputation for noise and gets switched off.
    """
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "Admin_Insights_Starter", tables={"sqlproxy (Groups)": NEEDS_REVIEW_STUB})

    report = cem.scan(bundle)

    assert report["status"] == "OK"
    assert report["models"][0]["categories"] == {"stub": 1}


def test_an_unresolvable_path_expression_is_not_judged(tmp_path: Path) -> None:
    """`unknowable` is not `empty`. A computed path is reported, never blocked on."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Extract": _csv_partition('Text.From(DateTime.LocalNow()) & ".csv"')})

    report = cem.scan(bundle)

    assert report["status"] == "OK"
    assert report["models"][0]["categories"] == {"dynamic_path": 1}


def test_a_mixed_model_blocks_on_the_broken_table_only(tmp_path: Path) -> None:
    """One unloadable table among healthy ones is still a silent-success failure - the report shows
    partial data, which is worse than none because it looks plausible."""
    bundle = tmp_path / "bundle"
    landed = _landed_csv(bundle / "data" / "Extract.csv")
    _model(
        bundle / "pbip" / "wb",
        tables={
            "Extract": _csv_partition(f'"{landed.as_posix()}"'),
            "Orders": _excel_partition(MAC_AUTHOR_PATH),
            "Date": CALCULATED_DATE,
            "FACT_ORDERS": SNOWFLAKE_DIRECTQUERY,
        },
    )

    model = cem.scan(bundle)["models"][0]

    assert model["status"] == "EMPTY"
    assert [f["table"] for f in model["findings"]] == ["Orders"]
    assert model["categories"] == {
        cem.CATEGORY_FILE_OK: 1,
        cem.CATEGORY_FOREIGN: 1,
        "calculated": 1,
        "live": 1,
    }


# ---------------------------------------------------------------------------
# Parameterised paths - two thirds of the measured file-backed surface
# ---------------------------------------------------------------------------


def test_a_parameterised_path_that_landed_is_resolved_and_passes(tmp_path: Path) -> None:
    """`#"SourceFolder" & "\\public_Extract.csv"` is what the engine actually emits for landed data."""
    bundle = tmp_path / "bundle"
    data = bundle / "pbip" / "Groups" / "Groups.Data"
    _landed_csv(data / "public_Extract.csv")
    _model(
        bundle / "pbip" / "Groups",
        name="Groups.SemanticModel",
        tables={"Extract": _csv_partition('#"SourceFolder" & "/public_Extract.csv"')},
        expressions=f'expression SourceFolder = "{data.as_posix()}" meta [IsParameterQuery=true, Type="Text"]\n',
    )

    report = cem.scan(bundle)

    assert report["status"] == "OK"
    assert report["models"][0]["categories"] == {cem.CATEGORY_FILE_OK: 1}


def test_a_parameterised_path_whose_folder_never_landed_is_reported_as_empty(tmp_path: Path) -> None:
    """The mutation that matters: if parameter resolution is dropped, this becomes `dynamic_path` and
    the finding disappears - silently, on the majority shape."""
    bundle = tmp_path / "bundle"
    data = bundle / "pbip" / "Groups" / "Groups.Data"  # never created
    _model(
        bundle / "pbip" / "Groups",
        name="Groups.SemanticModel",
        tables={"Extract": _csv_partition('#"SourceFolder" & "/public_Extract.csv"')},
        expressions=f'expression SourceFolder = "{data.as_posix()}" meta [IsParameterQuery=true, Type="Text"]\n',
    )

    findings = cem.scan(bundle)["models"][0]["findings"]

    assert [f["category"] for f in findings] == [cem.CATEGORY_MISSING]
    assert "public_Extract.csv" in findings[0]["path"]


def test_an_undeclared_parameter_is_unresolvable_not_empty(tmp_path: Path) -> None:
    """No `expression SourceFolder` anywhere - so the path is unknown, not absent."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Extract": _csv_partition('#"SourceFolder" & "/x.csv"')})

    assert cem.scan(bundle)["models"][0]["categories"] == {"dynamic_path": 1}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('"C:/data/x.csv"', "C:/data/x.csv"),
        ('#"Folder" & "/x.csv"', "/base/x.csv"),
        ('Folder & "/x.csv"', "/base/x.csv"),
        ('#"Folder" & "/sub" & "/x.csv"', "/base/sub/x.csv"),
        ('#"Missing" & "/x.csv"', None),
        ('Text.From(1) & "/x.csv"', None),
    ],
)
def test_m_path_evaluation(expression: str, expected: str | None) -> None:
    """The concatenation evaluator, in isolation - including the two shapes it must REFUSE."""
    assert cem.eval_m_path(expression, {"Folder": "/base"}) == expected


def test_model_parameters_ignores_a_non_literal_parameter(tmp_path: Path) -> None:
    """A computed parameter must not be half-resolved into a plausible-looking wrong path."""
    model = _model(
        tmp_path,
        expressions=(
            'expression Good = "C:/data" meta [IsParameterQuery=true]\n'
            "expression Computed = Text.From(DateTime.LocalNow())\n"
        ),
    )
    assert cem.model_parameters(model) == {"Good": "C:/data"}


# ---------------------------------------------------------------------------
# Attribution: from the artifact, never from a field copied forward
# ---------------------------------------------------------------------------


def test_the_finding_names_the_workbook_the_model_the_table_and_the_path(tmp_path: Path) -> None:
    """The engine's own warning for this failure was measured attached to the WRONG workbook on the
    same estate, so every identifier in a finding is read off the artifact being inspected."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "global_superstores_db", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH)})

    rendered = cem.render(cem.scan(bundle))

    assert "global_superstores_db" in rendered
    assert "Orders (Global Superstore).SemanticModel" in rendered
    assert "table 'Orders'" in rendered
    assert MAC_AUTHOR_PATH in rendered


def test_no_engine_report_is_read_at_all(tmp_path: Path) -> None:
    """A `report.json` blaming a different datasource must not change the verdict - because it is
    never opened. The bundle here contains a deliberately misleading one."""
    bundle = tmp_path / "bundle"
    (bundle).mkdir()
    (bundle / "report.json").write_text(
        json.dumps({"workbooks": [{"name": "RESTAPISample", "pbip_warnings": ["blames 'World Indicators'"]}]}),
        encoding="utf-8",
    )
    _model(bundle / "pbip" / "global_superstores_db", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH)})

    rendered = cem.render(cem.scan(bundle))

    assert "World Indicators" not in rendered
    assert "RESTAPISample" not in rendered


def test_a_standalone_datasource_model_is_labelled_as_one(tmp_path: Path) -> None:
    """`semantic_models/<X>.SemanticModel` belongs to no workbook; claiming one would be a guess."""
    bundle = tmp_path / "bundle"
    _model(bundle / "semantic_models", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH)})

    assert cem.scan(bundle)["models"][0]["owner"] == "(standalone datasource model)"


# ---------------------------------------------------------------------------
# Host awareness - the remap hazard that makes `exists()` alone unsafe
# ---------------------------------------------------------------------------


def test_a_posix_path_is_foreign_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On Windows `Path('/Users/x/y').exists()` is probed against the CURRENT DRIVE, so a same-named
    local folder can answer "present" for a file the model can never read. The flavour check runs
    first precisely so that remap cannot produce a false pass."""
    monkeypatch.setattr(cem, "HOST_OS", "nt")
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH)})

    assert cem.scan(bundle)["models"][0]["findings"][0]["category"] == cem.CATEGORY_FOREIGN


def test_a_windows_path_is_foreign_on_posix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The mirror image, so the check behaves the same on a Linux CI runner as on a Windows laptop."""
    monkeypatch.setattr(cem, "HOST_OS", "posix")
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Orders": _excel_partition(WINDOWS_AUTHOR_PATH)})

    assert cem.scan(bundle)["models"][0]["findings"][0]["category"] == cem.CATEGORY_FOREIGN


def test_a_native_path_is_never_called_foreign(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The false-positive guard on the host check itself: every landed bundle path is native."""
    monkeypatch.setattr(cem, "HOST_OS", "posix")
    bundle = tmp_path / "bundle"
    landed = _landed_csv(bundle / "data" / "Extract.csv")
    _model(bundle / "pbip" / "wb", tables={"Extract": _csv_partition(f'"/{landed.as_posix().lstrip("/")}"')})

    categories = cem.scan(bundle)["models"][0]["categories"]

    assert cem.CATEGORY_FOREIGN not in categories


# ---------------------------------------------------------------------------
# TMDL parsing - the partition block walker
# ---------------------------------------------------------------------------


def test_partition_bodies_do_not_bleed_into_the_next_partition(tmp_path: Path) -> None:
    """Two partitions in one table file: a body that over-reads would inherit the neighbour's source
    and judge the wrong thing. `Sales` is broken, `Archive` is fine."""
    bundle = tmp_path / "bundle"
    landed = _landed_csv(bundle / "data" / "Archive.csv")
    tmdl = (
        "table Orders\n\n"
        "\tpartition Sales = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t\tSource = Csv.Document(File.Contents("{(tmp_path / "gone.csv").as_posix()}"))\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n\n"
        "\tpartition Archive = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t\tSource = Csv.Document(File.Contents("{landed.as_posix()}"))\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n\n"
        "\tannotation PBI_Id = Orders\n"
    )
    _model(bundle / "pbip" / "wb", tables={"Orders": tmdl})

    model = cem.scan(bundle)["models"][0]

    assert model["categories"] == {cem.CATEGORY_MISSING: 1, cem.CATEGORY_FILE_OK: 1}
    assert [f["partition"] for f in model["findings"]] == ["Sales"]


def test_a_quoted_partition_name_is_unquoted(tmp_path: Path) -> None:
    """`partition 'Orders$' = m` - the engine quotes names containing `$`, and the finding must read
    back as the name a human sees in Desktop."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH)})

    assert cem.scan(bundle)["models"][0]["findings"][0]["partition"] == "Orders$"


def test_a_model_with_no_tables_folder_is_not_a_finding(tmp_path: Path) -> None:
    """An empty scan target must produce OK, not a crash and not a false alarm."""
    bundle = tmp_path / "bundle"
    (bundle / "pbip" / "wb" / "Empty.SemanticModel" / "definition").mkdir(parents=True)

    report = cem.scan(bundle)

    assert report["models_scanned"] == 1
    assert report["status"] == "OK"


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_cli_exits_five_on_an_empty_model_and_writes_the_json(tmp_path: Path, capsys) -> None:
    """The exit code IS the gate; a message alone is what the engine already has."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH)})
    out = tmp_path / "out" / "empty.json"

    code = cem.main([str(bundle), "--json", str(out)])

    assert code == cem.EXIT_EMPTY_MODEL
    assert json.loads(out.read_text(encoding="utf-8"))["models_empty"] == 1
    assert "EMPTY_MODEL" in capsys.readouterr().out


def test_cli_exits_zero_on_a_healthy_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    landed = _landed_csv(bundle / "data" / "Extract.csv")
    _model(bundle / "pbip" / "wb", tables={"Extract": _csv_partition(f'"{landed.as_posix()}"')})

    assert cem.main([str(bundle)]) == cem.EXIT_OK


def test_cli_warn_only_reports_but_does_not_block(tmp_path: Path, capsys) -> None:
    """An explicitly accepted snapshot still has to SAY it is empty."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Orders": _excel_partition(MAC_AUTHOR_PATH)})

    assert cem.main([str(bundle), "--warn-only"]) == cem.EXIT_OK
    assert "EMPTY_MODEL" in capsys.readouterr().out


def test_cli_rejects_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert cem.main([str(missing)]) == cem.EXIT_USAGE


def test_the_posture_is_printed_even_when_everything_passes(tmp_path: Path) -> None:
    """A gate whose skip categories are invisible cannot be audited. If `live` ever collapses to zero
    on an estate full of warehouses, the printed line is what makes that noticeable."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"FACT_ORDERS": SNOWFLAKE_DIRECTQUERY})

    rendered = cem.render(cem.scan(bundle))

    assert "not judged" in rendered
    assert "live=1" in rendered
