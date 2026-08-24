"""Tests for scripts/check_connection_fidelity.py - the silent live->flat-file downgrade gate (#328).

Every positive assertion has its negative beside it: the gate must FIRE on a live source shipped as a
flat file, and must stay SILENT on a source that is legitimately a file. The final block mutation-tests
the gate itself - it breaks the production logic four ways (one per truth-table row) and asserts a test
would fail each time, after first asserting the mutation actually landed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_connection_fidelity as ccf  # noqa: E402  # pylint: disable=wrong-import-position

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "connection-fidelity"


# --- partition M builders (verified against check_empty_model.classify_partition) -------------------


def _snowflake_m() -> str:
    return (
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Snowflake.Databases("ses.snowflakecomputing.com", "WH"),\n'
        '\t\t\t    Data = Source{[Name = "SCORECARD"]}[Data]\n'
        "\t\t\tin\n"
        "\t\t\t    Data\n"
    )


def _sqlserver_m() -> str:
    return (
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Source = Sql.Database("srv.contoso.com", "SALES")\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n"
    )


def _csv_m(filename: str) -> str:
    return (
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t    Source = Csv.Document(File.Contents("{filename}"), [Delimiter = ","]),\n'
        "\t\t\t    Promoted = Table.PromoteHeaders(Source)\n"
        "\t\t\tin\n"
        "\t\t\t    Promoted\n"
    )


_M_BUILDERS = {
    "snowflake": lambda name: _snowflake_m(),
    "sqlserver": lambda name: _sqlserver_m(),
    "csv_missing": lambda name: _csv_m("does_not_exist.csv"),
    "csv_present": lambda name: _csv_m(f"{name}.csv"),
}


def _write_model(unit: Path, tables: dict[str, str]) -> None:
    """Emit one .SemanticModel with a table per (name -> kind) entry."""
    model = unit / "Unit.SemanticModel" / "definition" / "tables"
    model.mkdir(parents=True, exist_ok=True)
    for name, kind in tables.items():
        body = _M_BUILDERS[kind](name)
        (model / f"{name}.tmdl").write_text(f"table {name}\n\tpartition {name} = m\n{body}", encoding="utf-8")
        if kind == "csv_present":
            (model.parent.parent / f"{name}.csv").write_text("a,b\n1,2\n", encoding="utf-8")


def _connection(connection_class: str, mode: str, target: str | None) -> dict:
    conn = {"class": connection_class, "mode": mode}
    if target is not None:
        conn["powerbi_target"] = target
    return conn


def _build_unit(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    unit: Path,
    sources: list[dict],
    tables: dict[str, str],
    limitations: list[dict] | None = None,
    declarations: list[dict] | None = None,
    *,
    write_spec: bool = True,
) -> Path:
    """Write a migration unit: migration-spec.json + one model + optional decisions."""
    unit.mkdir(parents=True, exist_ok=True)
    if write_spec:
        spec = {
            "migration_spec_version": "1.0",
            "source": {"workbook": "Unit.twbx"},
            "data_sources": sources,
            "limitations_encountered": limitations or [],
        }
        (unit / "migration-spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    _write_model(unit, tables)
    for index, declaration in enumerate(declarations or []):
        decl_dir = unit / "_build" / "generated-edit-declarations"
        decl_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, **declaration}
        (decl_dir / f"{index}.json").write_text(json.dumps(payload), encoding="utf-8")
    return unit


def _live_source(
    source_id: str,
    connection_class: str,
    mode: str = "extract",
    target: str | None = "live_source",
    tables: list[str] | None = None,
) -> dict:
    return {
        "id": source_id,
        "caption": f"{connection_class} source",
        "connection": _connection(connection_class, mode, target),
        "tables": [{"id": f"tbl.{t}", "name": t, "source_relation": "custom-sql"} for t in (tables or [])],
    }


def _run(unit: Path) -> dict:
    return ccf.scan(unit)


# --- truth table: the four rows, each with its negative --------------------------------------------


def test_live_source_connected_passes(tmp_path: Path) -> None:
    """Row 1: live source (snowflake) -> remote connector present -> PASS."""
    unit = _build_unit(
        tmp_path / "u", [_live_source("ds.sf", "snowflake", tables=["Scorecard"])], {"Scorecard": "snowflake"}
    )
    report = _run(unit)
    assert report["status"] == ccf.STATUS_OK
    assert report["units"][0]["connected"] == 1
    assert report["units"][0]["downgraded"] == 0


def test_extract_mode_does_not_hide_a_preserved_connection(tmp_path: Path) -> None:
    """The discriminator is powerbi_target, NOT mode: a mode=extract live class still passes when connected."""
    unit = _build_unit(
        tmp_path / "u",
        [_live_source("ds.sf", "snowflake", mode="extract", target=None, tables=["Scorecard"])],
        {"Scorecard": "snowflake"},
    )
    # No stamped powerbi_target -> computed from class; mode=extract must not turn it into flat_file.
    assert _run(unit)["status"] == ccf.STATUS_OK


def test_legitimate_flat_file_is_not_flagged(tmp_path: Path) -> None:
    """Row 2 (negative-of-defect): a source that IS a file (csv) is never a downgrade."""
    source = {
        "id": "ds.csv",
        "caption": "csv source",
        "connection": _connection("csv", "extract", "flat_file"),
        "tables": [{"id": "tbl.Sheet1", "name": "Sheet1", "source_relation": "table"}],
    }
    unit = _build_unit(tmp_path / "u", [source], {"Sheet1": "csv_present"})
    report = _run(unit)
    # No live source to judge -> nothing measured, and crucially NOT a finding.
    assert report["status"] == ccf.STATUS_SKIPPED
    assert report["downgraded_sources"] == 0


def test_live_source_shipped_as_file_is_a_finding(tmp_path: Path) -> None:
    """Row 3: the defect - live source (snowflake) materialised to CSV -> DOWNGRADED."""
    unit = _build_unit(
        tmp_path / "u", [_live_source("ds.sf", "snowflake", tables=["Scorecard"])], {"Scorecard": "csv_missing"}
    )
    report = _run(unit)
    assert report["status"] == ccf.STATUS_DOWNGRADED
    assert report["downgraded_sources"] == 1


def test_unmapped_live_class_is_not_checked_never_pass(tmp_path: Path) -> None:
    """Row 4: a live class with no known connector mapping -> NOT_CHECKED, never a silent PASS/FINDING."""
    source = {
        "id": "ds.weird",
        "caption": "mystery source",
        "connection": {"class": "some-exotic-driver", "mode": "extract"},
        "tables": [{"id": "tbl.X", "name": "X", "source_relation": "table"}],
    }
    unit = _build_unit(tmp_path / "u", [source], {"X": "csv_missing"})
    report = _run(unit)
    assert report["status"] == ccf.STATUS_SKIPPED
    assert report["units"][0]["sources"][0]["verdict"] == ccf.SOURCE_NOT_CHECKED


def test_absent_spec_is_not_checked(tmp_path: Path) -> None:
    """Row 4: no spec at all -> SKIPPED, never PASS."""
    unit = _build_unit(tmp_path / "u", [], {"X": "csv_missing"}, write_spec=False)
    assert _run(unit)["status"] == ccf.STATUS_SKIPPED


# --- the discrimination that makes this hard -------------------------------------------------------


def test_mixed_unit_flags_only_the_downgraded_live_source(tmp_path: Path) -> None:
    """SES shape: a downgraded snowflake source AND a legitimate csv source in one unit.

    The gate must fire on the snowflake source and stay silent on the csv - flagging CSV per se would
    make it worse than no gate.
    """
    sources = [
        _live_source("ds.sf", "snowflake", tables=["Scorecard"]),
        {
            "id": "ds.csv",
            "caption": "csv source",
            "connection": _connection("csv", "extract", "flat_file"),
            "tables": [{"id": "tbl.Ref", "name": "Ref", "source_relation": "table"}],
        },
    ]
    unit = _build_unit(tmp_path / "u", sources, {"Scorecard": "csv_missing", "Ref": "csv_present"})
    report = _run(unit)
    assert report["status"] == ccf.STATUS_DOWNGRADED
    assert report["downgraded_sources"] == 1
    downgraded = [s for s in report["units"][0]["sources"] if s["verdict"] == ccf.SOURCE_DOWNGRADED]
    assert [s["source_id"] for s in downgraded] == ["ds.sf"]
    # The csv source never even entered the live-source judgement.
    assert all(s["source_id"] != "ds.csv" for s in report["units"][0]["sources"])


def test_two_live_sources_one_preserved_one_downgraded(tmp_path: Path) -> None:
    """Per-connector attribution: sql preserved + snowflake downgraded -> flags only snowflake."""
    sources = [
        _live_source("ds.sql", "sqlserver", tables=["Sales"]),
        _live_source("ds.sf", "snowflake", tables=["Scorecard"]),
    ]
    unit = _build_unit(tmp_path / "u", sources, {"Sales": "sqlserver", "Scorecard": "csv_missing"})
    report = _run(unit)
    assert report["status"] == ccf.STATUS_DOWNGRADED
    downgraded = [s for s in report["units"][0]["sources"] if s["verdict"] == ccf.SOURCE_DOWNGRADED]
    assert [s["source_id"] for s in downgraded] == ["ds.sf"]


# --- the sanctioned escape hatch -------------------------------------------------------------------


def test_build_stage_limitation_excuses_the_downgrade(tmp_path: Path) -> None:
    """A decided downgrade recorded at stage semantic_build is DECLARED, not a finding."""
    limitations = [
        {
            "item": "ds.sf",
            "issue": "extract-baked custom-SQL - deliberately modelled as one flat table, no live rebuild",
            "severity": "medium",
            "stage": "semantic_build",
        }
    ]
    unit = _build_unit(
        tmp_path / "u",
        [_live_source("ds.sf", "snowflake", tables=["Scorecard"])],
        {"Scorecard": "csv_missing"},
        limitations=limitations,
    )
    report = _run(unit)
    assert report["status"] == ccf.STATUS_OK
    assert report["units"][0]["declared"] == 1
    assert report["units"][0]["downgraded"] == 0


def test_parse_stage_limitation_does_not_excuse(tmp_path: Path) -> None:
    """A parse-stage 'extract-based' note is the parser observing, not a decision - it excuses nothing."""
    limitations = [
        {
            "item": "ds.sf",
            "issue": "extract-based (.hyper) data source - row data requires a separate extraction step",
            "severity": "info",
            "stage": "parse",
        }
    ]
    unit = _build_unit(
        tmp_path / "u",
        [_live_source("ds.sf", "snowflake", tables=["Scorecard"])],
        {"Scorecard": "csv_missing"},
        limitations=limitations,
    )
    assert _run(unit)["status"] == ccf.STATUS_DOWNGRADED


def test_generated_edit_declaration_excuses_the_downgrade(tmp_path: Path) -> None:
    """A generated-edit declaration touching the file-backed table is DECLARED, not a finding."""
    declarations = [
        {
            "run_id": "r1",
            "kind": "changed",
            "target": "Unit.SemanticModel/definition/tables/Scorecard.tmdl",
            "reason": "declared generated-artifact repair",
        }
    ]
    unit = _build_unit(
        tmp_path / "u",
        [_live_source("ds.sf", "snowflake", tables=["Scorecard"])],
        {"Scorecard": "csv_missing"},
        declarations=declarations,
    )
    report = _run(unit)
    assert report["status"] == ccf.STATUS_OK
    assert report["units"][0]["declared"] == 1


# --- committed fixture + CLI exit codes ------------------------------------------------------------


def _cli(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_connection_fidelity.py"), str(target), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_committed_fixture_reports_downgrade() -> None:
    """The committed SES-shaped fixture is a stable, inspectable DOWNGRADED case, exit 1."""
    result = _cli(FIXTURE_DIR / "silent-downgrade")
    assert result.returncode == ccf.EXIT_DOWNGRADED, result.stdout + result.stderr
    assert "DOWNGRADED" in result.stdout
    assert "Snowflake.*" in result.stdout


def test_cli_exit_codes_follow_the_house_ladder(tmp_path: Path) -> None:
    """0 pass / 1 findings / 3 not-checked, judged by exit code not printed text."""
    ok = _build_unit(tmp_path / "ok", [_live_source("ds.sf", "snowflake", tables=["S"])], {"S": "snowflake"})
    bad = _build_unit(tmp_path / "bad", [_live_source("ds.sf", "snowflake", tables=["S"])], {"S": "csv_missing"})
    skip = _build_unit(tmp_path / "skip", [], {"S": "csv_missing"}, write_spec=False)
    assert _cli(ok).returncode == ccf.EXIT_OK
    assert _cli(bad).returncode == ccf.EXIT_DOWNGRADED
    assert _cli(skip).returncode == ccf.EXIT_SKIPPED


def test_registered_in_check_unit_at_integration_scope() -> None:
    """The gate must be wired into the unit facade so a unit check runs it."""
    import check_unit as cu  # pylint: disable=import-outside-toplevel

    assert "connection-fidelity" in cu.INTEGRATION_CHECK_IDS
    gate = next(g for g in cu.GATES if g.check_id == "connection-fidelity")
    assert gate.finding_statuses == {ccf.STATUS_DOWNGRADED}
    assert gate.finding_exit_codes == {ccf.EXIT_DOWNGRADED}


# --- mutation testing: break the gate four ways, one per truth-table row ---------------------------


def _downgrade_unit(tmp_path: Path) -> Path:
    return _build_unit(tmp_path / "u", [_live_source("ds.sf", "snowflake", tables=["S"])], {"S": "csv_missing"})


def test_mutation_row3_disable_file_detection_survives_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 3 mutant: if file-backed partitions are invisible, the downgrade is missed -> a test fails."""
    model = ccf.load_model(_downgrade_unit(tmp_path) / "Unit.SemanticModel")
    assert model.file_backed == 1  # baseline
    monkeypatch.setattr(ccf, "FILE_CATEGORIES", frozenset())
    assert ccf.load_model(_downgrade_unit(tmp_path) / "Unit.SemanticModel").file_backed == 0  # mutation landed
    assert _run(_downgrade_unit(tmp_path))["status"] != ccf.STATUS_DOWNGRADED  # the row-3 test would fail


def test_mutation_row1_ignore_connector_survives_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 1 mutant: if a preserved connector is ignored, a connected source is misreported."""
    connected = _build_unit(tmp_path / "u", [_live_source("ds.sf", "snowflake", tables=["S"])], {"S": "snowflake"})
    assert _run(connected)["status"] == ccf.STATUS_OK  # baseline
    monkeypatch.setattr(ccf.Model, "has_connector", lambda self, token: False)
    assert ccf.load_model(connected / "Unit.SemanticModel").has_connector("Snowflake") is False  # landed
    assert _run(connected)["status"] != ccf.STATUS_OK  # the row-1 test would fail


def test_mutation_mode_keying_bug_survives_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discriminator mutant: the naive `mode`-keyed implementation the brief warns against.

    A gate that treats `mode == "extract"` as flat_file misclassifies the SES incident (Snowflake WITH
    an extract) and misses the downgrade. Keying off powerbi_target/class is exactly what defeats it.
    """
    unit = _downgrade_unit(tmp_path)  # snowflake, mode=extract, powerbi_target=live_source, shipped as CSV
    assert _run(unit)["status"] == ccf.STATUS_DOWNGRADED  # baseline: correctly caught

    def mode_keyed(conn: dict) -> tuple[str, str, str]:
        cls, mode = str(conn.get("class") or ""), str(conn.get("mode") or "")
        return (ccf.FLAT_FILE if mode == "extract" else ccf.LIVE_SOURCE), cls, mode

    monkeypatch.setattr(ccf, "_expected_target", mode_keyed)
    assert ccf._expected_target({"class": "snowflake", "mode": "extract"})[0] == ccf.FLAT_FILE  # landed
    assert _run(unit)["status"] == ccf.STATUS_SKIPPED  # the row-3 defect test would fail


def test_mutation_row4_ignore_escape_hatch_survives_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 4 mutant: if a recorded downgrade is ignored, a DECLARED source is misreported as a finding."""
    limitations = [
        {"item": "ds.sf", "issue": "deliberately materialised", "severity": "medium", "stage": "semantic_build"}
    ]
    unit = _build_unit(
        tmp_path / "u",
        [_live_source("ds.sf", "snowflake", tables=["S"])],
        {"S": "csv_missing"},
        limitations=limitations,
    )
    assert _run(unit)["status"] == ccf.STATUS_OK  # baseline: declared -> OK
    monkeypatch.setattr(ccf, "_declared_by_limitation", lambda lims, sid, tables: None)
    assert ccf._declared_by_limitation(limitations, "ds.sf", []) is None  # landed
    assert _run(unit)["status"] == ccf.STATUS_DOWNGRADED  # the escape-hatch test would fail
