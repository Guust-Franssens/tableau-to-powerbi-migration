"""check_empty_model: the gate against a model that opens and loads no rows.

Fixture shapes come from a real 38-workbook estate (engine 2.126.0); paths are synthetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_empty_model as cem  # noqa: E402  # pylint: disable=wrong-import-position

MAC_AUTHOR_PATH = "/Users/<author>/Datasets/Global Superstore.xlsx"
WINDOWS_AUTHOR_PATH = r"D:\Datasets\Global Superstore.xlsx"
DEFAULT_MODEL = "Orders (Global Superstore).SemanticModel"


def _model(root: Path, name: str = DEFAULT_MODEL, tables: dict[str, str] | None = None, expressions: str = "") -> Path:
    """Write a minimal but structurally real `.SemanticModel` folder."""
    definition = root / name / "definition"
    (definition / "tables").mkdir(parents=True, exist_ok=True)
    if expressions:
        (definition / "expressions.tmdl").write_text(expressions, encoding="utf-8")
    for table, tmdl in (tables or {}).items():
        (definition / "tables" / f"{table}.tmdl").write_text(tmdl, encoding="utf-8")
    return root / name


def _partition(table: str, source: str, mode: str = "import", name: str | None = None) -> str:
    return (
        f"\tpartition {name or table} = m\n\t\tmode: {mode}\n\t\tsource =\n\t\t\tlet\n"
        f"\t\t\t\tSource = {source}\n\t\t\tin\n\t\t\t\tSource\n\n"
    )


def _excel(path: str, mode: str = "import") -> str:
    return "table Orders\n\n" + _partition(
        "Orders", f'Excel.Workbook(File.Contents("{path}"), null, true)', mode, "'Orders$'"
    )


def _csv(expression: str, mode: str = "import") -> str:
    return "table Extract\n\n" + _partition("Extract", f"Csv.Document(File.Contents({expression}))", mode)


SNOWFLAKE_DIRECTQUERY = "table FACT_ORDERS\n\n" + _partition(
    "FACT_ORDERS", 'Snowflake.Databases(#"Server", #"Warehouse")', "directQuery"
)
CALCULATED_DATE = "table Date\n\n\tpartition Date = calculated\n\t\tmode: import\n\t\tsource = CALENDAR(DATE(2020,1,1), DATE(2026,12,31))\n"
NEEDS_REVIEW_STUB = "table 'sqlproxy (Groups)'\n\n" + _partition("sqlproxy", "#table(type table [], {})")


def _landed_csv(path: Path, rows: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("col_a,col_b\n" + "".join(f"a{i},b{i}\n" for i in range(rows)), encoding="utf-8")
    return path


def _tables(case: str, bundle: Path) -> dict[str, str]:
    """One table per classifier branch; the real files a case needs are created under `bundle`."""
    if case == "landed":
        return {"Extract": _csv(f'"{_landed_csv(bundle / "data" / "E.csv").as_posix()}"')}
    if case == "header-only":
        return {"Extract": _csv(f'"{_landed_csv(bundle / "data" / "E.csv", rows=0).as_posix()}"')}
    if case == "zero-byte-xlsx":
        (bundle / "data").mkdir(parents=True)
        (bundle / "data" / "S.xlsx").write_bytes(b"")
        return {"Orders": _excel((bundle / "data" / "S.xlsx").as_posix())}
    if case == "missing-native":
        return {"Extract": _csv(f'"{(bundle / "never" / "O.csv").as_posix()}"')}
    return {
        "directQuery": {"F": SNOWFLAKE_DIRECTQUERY},
        "dual": {"F": SNOWFLAKE_DIRECTQUERY.replace("directQuery", "dual")},
        "live-over-file": {"Orders": _excel(MAC_AUTHOR_PATH, mode="directQuery")},  # mode decides before source
        "remote-import": {"F": SNOWFLAKE_DIRECTQUERY.replace("directQuery", "import")},
        "calculated": {"Date": CALCULATED_DATE},
        "stub": {"S": NEEDS_REVIEW_STUB},  # the engine already reports it; blocking would double-count
        "dynamic": {"Extract": _csv('Text.From(DateTime.LocalNow()) & ".csv"')},
        "undeclared-param": {"Extract": _csv('#"SourceFolder" & "/x.csv"')},
    }[case]


@pytest.mark.parametrize(
    ("case", "categories"),
    [
        ("landed", {cem.CATEGORY_FILE_OK: 1}),
        ("header-only", {cem.CATEGORY_EMPTY: 1}),
        ("zero-byte-xlsx", {cem.CATEGORY_EMPTY: 1}),
        ("missing-native", {cem.CATEGORY_MISSING: 1}),
        ("directQuery", {"live": 1}),
        ("dual", {"live": 1}),
        ("live-over-file", {"live": 1}),
        ("remote-import", {"remote_import": 1}),
        ("calculated", {"calculated": 1}),
        ("stub", {"stub": 1}),
        ("dynamic", {"dynamic_path": 1}),
        ("undeclared-param", {"dynamic_path": 1}),
    ],
)
def test_partition_classification(tmp_path: Path, case: str, categories: dict[str, int]) -> None:
    """Only provably empty sources block; live, remote, calculated, stub and unknowable never do."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables=_tables(case, bundle))
    report = cem.scan(bundle)
    assert report["models"][0]["categories"] == categories
    blocking = set(categories) & set(cem.BLOCKING_CATEGORIES)
    assert report["status"] == ("EMPTY_MODELS" if blocking else "OK")


@pytest.mark.parametrize(
    ("host", "path", "category"),
    [
        ("nt", MAC_AUTHOR_PATH, cem.CATEGORY_FOREIGN),  # flavour check first: a POSIX root probes the current drive
        ("posix", MAC_AUTHOR_PATH, cem.CATEGORY_MISSING),
        ("posix", WINDOWS_AUTHOR_PATH, cem.CATEGORY_FOREIGN),
        ("nt", r"D:\Datasets\Orders.csv", cem.CATEGORY_MISSING),  # native is never called foreign
        ("posix", "/data/Orders.csv", cem.CATEGORY_MISSING),
    ],
)
def test_host_awareness_changes_the_category_not_the_verdict(monkeypatch, tmp_path, host, path, category) -> None:
    """The unlanded authoring-machine path blocks on either host, under the right remedy."""
    monkeypatch.setattr(cem, "HOST_OS", host)
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "global_superstores_db", tables={"Orders": _excel(path)})
    report = cem.scan(bundle)
    assert report["status"] == "EMPTY_MODELS" and report["models_empty"] == 1
    finding = report["models"][0]["findings"][0]
    assert (finding["category"], finding["path"], finding["partition"]) == (category, path, "Orders$")


def test_a_mixed_model_blocks_on_the_broken_table_and_bodies_do_not_bleed(tmp_path: Path) -> None:
    """One unloadable table among healthy ones still blocks; two partitions in one file stay apart."""
    bundle = tmp_path / "bundle"
    landed = _landed_csv(bundle / "data" / "Archive.csv")
    two = (
        "table Two\n\n"
        + _partition("Sales", f'Csv.Document(File.Contents("{(tmp_path / "gone.csv").as_posix()}"))')
        + _partition("Archive", f'Csv.Document(File.Contents("{landed.as_posix()}"))')
        + "\tannotation PBI_Id = Two\n"
    )
    tables = {"Two": two, "Orders": _excel(MAC_AUTHOR_PATH), "Date": CALCULATED_DATE, "F": SNOWFLAKE_DIRECTQUERY}
    model = cem.scan(_model(bundle / "pbip" / "wb", tables=tables).parent.parent.parent)["models"][0]
    assert model["status"] == "EMPTY"
    assert sorted((f["table"], f["partition"]) for f in model["findings"]) == [("Orders", "Orders$"), ("Two", "Sales")]
    assert all(f["category"] in cem.BLOCKING_CATEGORIES for f in model["findings"])
    assert (model["categories"][cem.CATEGORY_FILE_OK], model["categories"]["calculated"]) == (1, 1)
    assert model["categories"]["live"] == 1


@pytest.mark.parametrize("landed", [True, False])
def test_a_parameterised_path_is_resolved_before_judging(tmp_path: Path, landed: bool) -> None:
    """`#"SourceFolder" & "/x.csv"` is the majority shape; dropping resolution hides the finding."""
    bundle = tmp_path / "bundle"
    data = bundle / "pbip" / "Groups" / "Groups.Data"
    if landed:
        _landed_csv(data / "public_Extract.csv")
    _model(
        bundle / "pbip" / "Groups",
        name="Groups.SemanticModel",
        tables={"Extract": _csv('#"SourceFolder" & "/public_Extract.csv"')},
        expressions=f'expression SourceFolder = "{data.as_posix()}" meta [IsParameterQuery=true, Type="Text"]\n',
    )
    model = cem.scan(bundle)["models"][0]
    if landed:
        assert model["categories"] == {cem.CATEGORY_FILE_OK: 1}
    else:
        assert [f["category"] for f in model["findings"]] == [cem.CATEGORY_MISSING]
        assert "public_Extract.csv" in model["findings"][0]["path"]


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
    """The concatenation evaluator, including the two shapes it must REFUSE."""
    assert cem.eval_m_path(expression, {"Folder": "/base"}) == expected


def test_model_parameters_ignores_a_non_literal_parameter(tmp_path: Path) -> None:
    """A computed parameter must not be half-resolved into a plausible-looking wrong path."""
    expressions = 'expression Good = "C:/data" meta [IsParameterQuery=true]\nexpression Computed = Text.From(1)\n'
    assert cem.model_parameters(_model(tmp_path, expressions=expressions)) == {"Good": "C:/data"}


def test_attribution_comes_from_the_artifact_never_the_engine_report(tmp_path: Path) -> None:
    """Every identifier is read off the model; a misleading `report.json` is never opened."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    misleading = {"workbooks": [{"name": "RESTAPISample", "pbip_warnings": ["blames 'World Indicators'"]}]}
    (bundle / "report.json").write_text(json.dumps(misleading), encoding="utf-8")
    _model(bundle / "pbip" / "global_superstores_db", tables={"Orders": _excel(MAC_AUTHOR_PATH)})
    _model(bundle / "semantic_models", name="DS.SemanticModel", tables={"F": SNOWFLAKE_DIRECTQUERY})
    report = cem.scan(bundle)
    rendered = cem.render(report)
    for needle in ("global_superstores_db", DEFAULT_MODEL, "table 'Orders'", MAC_AUTHOR_PATH):
        assert needle in rendered
    assert "World Indicators" not in rendered and "RESTAPISample" not in rendered
    owners = {m["model"]: m["owner"] for m in report["models"]}
    assert owners["DS.SemanticModel"] == "(standalone datasource model)"


def test_cli_contract(tmp_path: Path, capsys) -> None:
    """Exit 5 blocks and writes JSON, warn-only still says EMPTY, posture prints on pass, usage/skip."""
    bundle = tmp_path / "bundle"
    _model(bundle / "pbip" / "wb", tables={"Orders": _excel(MAC_AUTHOR_PATH)})
    out = tmp_path / "out" / "empty.json"
    assert cem.main([str(bundle), "--json", str(out)]) == cem.EXIT_EMPTY_MODEL
    assert json.loads(out.read_text(encoding="utf-8"))["models_empty"] == 1
    assert "EMPTY_MODEL" in capsys.readouterr().out
    assert cem.main([str(bundle), "--warn-only"]) == cem.EXIT_OK
    assert "EMPTY_MODEL" in capsys.readouterr().out

    live = tmp_path / "live"
    _model(live / "pbip" / "wb", tables={"F": SNOWFLAKE_DIRECTQUERY})
    assert cem.main([str(live)]) == cem.EXIT_OK
    rendered = capsys.readouterr().out
    assert "not judged" in rendered and "live=1" in rendered

    assert cem.main([str(tmp_path / "nope")]) == cem.EXIT_USAGE
    empty = tmp_path / "emptymodel"
    (empty / "pbip" / "wb" / "Empty.SemanticModel" / "definition").mkdir(parents=True)
    report = cem.scan(empty)
    assert (report["models_scanned"], report["models_skipped"], report["status"]) == (1, 1, cem.STATUS_SKIPPED)
    assert cem.main([str(empty)]) == cem.EXIT_SKIPPED
