"""Tests for scripts/check_connection_fidelity.py - the silent live->flat-file downgrade gate (#328).

Every positive assertion has its negative beside it: the gate must FIRE on a live source shipped as a
flat file, and must stay SILENT on a source that is legitimately a file. The final block mutation-tests
the gate itself - it breaks the production logic four ways (one per truth-table row) and asserts a test
would fail each time, after first asserting the mutation actually landed.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

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
        '\t\t\t    Source = Snowflake.Databases("acme-fixture.snowflakecomputing.com", "WH"),\n'
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
    """the field shape: a downgraded snowflake source AND a legitimate csv source in one unit.

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
    """The committed field-shaped fixture is a stable, inspectable DOWNGRADED case, exit 1."""
    result = _cli(FIXTURE_DIR / "silent-downgrade")
    assert result.returncode == ccf.EXIT_DOWNGRADED, result.stdout + result.stderr
    assert "DOWNGRADED" in result.stdout
    assert "Snowflake.*" in result.stdout


@pytest.mark.parametrize(
    ("state", "expected_exit"),
    [
        ("live-preserved", ccf.EXIT_OK),
        ("mixed-live-and-flat-file", ccf.EXIT_OK),
        ("declared-downgrade", ccf.EXIT_OK),
        ("silent-downgrade", ccf.EXIT_DOWNGRADED),
    ],
)
def test_every_committed_state_holds_its_verdict(state: str, expected_exit: int) -> None:
    """Each committed state is inspectable by hand and pinned by exit code.

    The `tmp_path` tests above already cover the same logic, so these are not redundant coverage -
    they are the version a human can READ. A reviewer asking "what does a silent downgrade actually
    look like?" gets a real spec + real TMDL instead of assembling one from test helpers, and can run
    `python scripts/check_connection_fidelity.py tests/fixtures/connection-fidelity/<state>` directly.
    """
    result = _cli(FIXTURE_DIR / state)
    assert result.returncode == expected_exit, result.stdout + result.stderr


def test_a_legitimate_flat_file_is_not_flagged_beside_a_measured_live_source() -> None:
    """The discrimination the whole gate turns on, as a committed artifact.

    In the estate that prompted this gate, 3 CSV-backed tables were legitimately CSV in the Tableau
    source while 3 others were live Snowflake silently materialised to CSV. A gate that flagged CSV
    would fire on the correct half, get muted, and then miss the real one - so `mixed-...` deliberately
    pairs BOTH in one unit. Note it must contain a live source too: with only a flat file there is
    nothing to measure and the gate honestly SKIPs, which would prove nothing about discrimination.
    """
    result = _cli(FIXTURE_DIR / "mixed-live-and-flat-file")
    assert result.returncode == ccf.EXIT_OK, result.stdout + result.stderr
    assert "DOWNGRADED" not in result.stdout
    assert "REGIONAL_TARGETS" not in result.stdout, "the legitimate flat file must not be named as a finding"


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

    A gate that treats `mode == "extract"` as flat_file misclassifies that incident (Snowflake WITH
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


# --------------------------------------------------------------------------- blind-review findings
#
# Both were FALSE PASSES - the one direction that matters for a gate whose whole purpose is catching a
# silent failure. A false finding costs an engineer a minute; a false pass ships stale data.


def test_a_partial_downgrade_is_not_checked_never_connected() -> None:
    """One live table + one file-backed table in the same source must NOT read as connected.

    This is the shape of the incident that motivated the gate: three Snowflake tables were
    materialised to CSV. If any OTHER table of that source had kept its connector, the pre-fix gate
    returned CONNECTED and hid every downgraded table.
    """
    source = _live_source("ds.sf", "snowflake", tables=["A", "B"])
    model = ccf.Model(
        "m",
        "m",
        ({"table": "A", "category": "live"}, {"table": "B", "category": "file_ok"}),
        'Snowflake.Databases("s","d")',
    )
    verdict = ccf._judge_source(source, "live_source", "snowflake", "extract", ccf.UnitContext((model,), (), ()))
    assert verdict.verdict == ccf.SOURCE_NOT_CHECKED
    assert "PARTIAL" in verdict.detail


def test_a_connector_named_only_in_a_comment_does_not_count_as_connected() -> None:
    """`// prior source was Snowflake.Databases(...)` is prose, not a connection."""
    source = _live_source("ds.sf", "snowflake", tables=["A"])
    model = ccf.Model(
        "m", "m", ({"table": "A", "category": "file_ok"},), '// prior source was Snowflake.Databases("s","d")'
    )
    verdict = ccf._judge_source(source, "live_source", "snowflake", "extract", ccf.UnitContext((model,), (), ()))
    assert verdict.verdict != ccf.SOURCE_CONNECTED


def test_a_genuinely_connected_source_still_passes() -> None:
    """The negative case beside the positive one: the fix must not make everything NOT_CHECKED."""
    source = _live_source("ds.sf", "snowflake", tables=["A", "B"])
    model = ccf.Model(
        "m",
        "m",
        ({"table": "A", "category": "live"}, {"table": "B", "category": "live"}),
        'Snowflake.Databases("s","d")',
    )
    verdict = ccf._judge_source(source, "live_source", "snowflake", "extract", ccf.UnitContext((model,), (), ()))
    assert verdict.verdict == ccf.SOURCE_CONNECTED


def test_a_file_table_belonging_to_ANOTHER_source_does_not_taint_this_one() -> None:
    """Per-source scoping, which is why the partial rule cannot simply be model-wide.

    `mixed-live-and-flat-file` depends on exactly this: a legitimately-CSV source sits in the same
    model as a preserved live one, and must not drag it to NOT_CHECKED.
    """
    source = _live_source("ds.sf", "snowflake", tables=["A"])
    model = ccf.Model(
        "m",
        "m",
        ({"table": "A", "category": "live"}, {"table": "ZZ_OTHER", "category": "file_ok"}),
        'Snowflake.Databases("s","d")',
    )
    verdict = ccf._judge_source(source, "live_source", "snowflake", "extract", ccf.UnitContext((model,), (), ()))
    assert verdict.verdict == ccf.SOURCE_CONNECTED


# --------------------------------------------------------------------------- round-2 review finding
#
# The round-1 fix scoped the FILE side per-source but left connector detection model-wide. Blind
# review showed that is its own false PASS: with two live sources of the same class, source A's
# preserved connector vouched for source B, and if B's declared table name did not match the emitted
# one, B had no file evidence either - so a downgraded source reported CONNECTED and the unit exited 0.
# A fix that moves the failure boundary instead of removing it is the thing to watch for.


def _two_source_unit(declared_b: str) -> tuple[list[dict], ccf.UnitContext]:
    """Two same-class live sources; ORDERS stays live, FLIGHTS is materialised to a file."""
    sources = [
        _live_source("ds.a", "snowflake", tables=["ORDERS"]),
        _live_source("ds.b", "snowflake", tables=[declared_b] if declared_b else []),
    ]
    model = ccf.Model(
        "m",
        "m",
        ({"table": "ORDERS", "category": "live"}, {"table": "FLIGHTS", "category": "file_ok"}),
        'Snowflake.Databases("s","d")',
    )
    return sources, ccf.UnitContext((model,), (), ())


def test_one_sources_connector_does_not_vouch_for_another() -> None:
    """The round-2 finding: a downgraded source must not inherit a sibling's connector."""
    sources, ctx = _two_source_unit("FLIGHTS")
    verdicts = [ccf._judge_source(s, "live_source", "snowflake", "live", ctx) for s in sources]
    assert [v.verdict for v in verdicts] == [ccf.SOURCE_CONNECTED, ccf.SOURCE_DOWNGRADED]


@pytest.mark.parametrize("declared", ["flights", "FLIGHTS", "[Flights]"])
def test_benign_table_name_differences_still_attribute(declared: str) -> None:
    """CASE and bracket-quoting differ freely between a spec and emitted TMDL on correct input.

    If those broke attribution the gate would go quiet on ordinary migrations, so they are normalised
    rather than treated as a mismatch. QUALIFIED names are deliberately NOT normalised - see
    test_a_shared_table_leaf_does_not_let_one_source_borrow_another for why, and the measurement
    showing 58 of 65 real declared names are unqualified while all 7 dotted ones are CSV filenames.
    """
    sources, ctx = _two_source_unit(declared)
    assert ccf._judge_source(sources[1], "live_source", "snowflake", "live", ctx).verdict == ccf.SOURCE_DOWNGRADED


def test_a_renamed_table_is_unattributable_and_says_so() -> None:
    """A genuine rename is honestly unknowable - and must not fall back to a sibling's PASS.

    The DETAIL is asserted, not just the verdict. Mutation testing showed why: deleting the
    attribution guard entirely still yields NOT_CHECKED, because an unattributable source has neither
    connector nor file evidence and falls through to the generic branch. So a verdict-only assertion
    could not fail, and the guard's real value - telling an operator this is a spec-to-TMDL NAMING
    mismatch rather than an empty model - was untested.
    """
    sources, ctx = _two_source_unit("Flights_Renamed")
    verdict = ccf._judge_source(sources[1], "live_source", "snowflake", "live", ctx)
    assert verdict.verdict == ccf.SOURCE_NOT_CHECKED
    assert "declared tables match an emitted model partition" in verdict.detail
    assert "borrow another source's verdict" in verdict.detail


def test_a_source_declaring_no_tables_is_unattributable() -> None:
    """Different reason, same safe verdict - and worth pinning separately.

    Round 3 changed this deliberately. It used to mean "every emitted table belongs to me", which let
    such a source borrow a sibling's connector and report CONNECTED. Nothing to scope by now means
    unattributable.
    """
    sources, ctx = _two_source_unit("")
    verdict = ccf._judge_source(sources[1], "live_source", "snowflake", "live", ctx)
    assert verdict.verdict == ccf.SOURCE_NOT_CHECKED
    assert "declared tables match an emitted model partition" in verdict.detail


# --------------------------------------------------------------------------- round-3 review findings


def _qualified_collision_unit():
    """Two sources whose table names share a leaf; the HR one was renamed and materialised to CSV."""
    sources = [
        _live_source("ds.sales", "snowflake", tables=["SALES.ORDERS"]),
        _live_source("ds.hr", "snowflake", tables=["HR.ORDERS"]),
    ]
    model = ccf.Model(
        "m",
        "m",
        ({"table": "SALES.ORDERS", "category": "live"}, {"table": "HR_ORDERS_EXPORT", "category": "file_ok"}),
        'Snowflake.Databases("s","d")',
    )
    return sources, ccf.UnitContext((model,), (), ())


def test_a_shared_table_leaf_does_not_let_one_source_borrow_another() -> None:
    """SALES.ORDERS and HR.ORDERS are different tables and must not collapse to `orders`.

    The earlier leaf-matching fallback certified the downgraded HR source as CONNECTED off the
    preserved SALES partition. Exact matching removed the whole class - and measurement showed the
    fallback protected nothing: across every spec in this repo 58 of 65 declared table names are
    unqualified, and all 7 containing a dot are CSV filenames.
    """
    sources, ctx = _qualified_collision_unit()
    verdicts = [ccf._judge_source(s, "live_source", "snowflake", "live", ctx) for s in sources]
    assert [v.verdict for v in verdicts] == [ccf.SOURCE_CONNECTED, ccf.SOURCE_NOT_CHECKED]


def test_an_unattributable_source_keeps_the_unit_off_a_clean_pass() -> None:
    """A unit must not exit 0 while announcing a pass over a source it could not examine."""
    sources, ctx = _qualified_collision_unit()
    verdicts = [ccf._judge_source(s, "live_source", "snowflake", "live", ctx) for s in sources]
    unit = ccf._finalize_unit(Path("migration-spec.json"), verdicts)
    assert unit["status"] == ccf.STATUS_SKIPPED
    assert ccf.merge([unit])["status"] != ccf.STATUS_OK
    assert "partial coverage, not a pass" in unit["detail"]


def test_csv_filenames_are_not_collapsed_to_their_extension() -> None:
    """`tree.csv` and `orders.csv` share the leaf `csv` - leaf matching would have merged every one."""
    source = _live_source("ds.t", "snowflake", tables=["tree.csv"])
    model = ccf.Model(
        "m",
        "m",
        ({"table": "tree.csv", "category": "file_ok"}, {"table": "orders.csv", "category": "live"}),
        'Snowflake.Databases("s","d")',
    )
    verdict = ccf._judge_source(source, "live_source", "snowflake", "live", ccf.UnitContext((model,), (), ()))
    assert verdict.verdict == ccf.SOURCE_DOWNGRADED


def test_a_source_with_no_declared_tables_cannot_borrow_a_sibling_connector() -> None:
    """No declared tables means nothing to scope by - not "every table belongs to me"."""
    source = _live_source("ds.empty", "snowflake", tables=[])
    model = ccf.Model("m", "m", ({"table": "UNRELATED", "category": "live"},), 'Snowflake.Databases("s","d")')
    verdict = ccf._judge_source(source, "live_source", "snowflake", "live", ccf.UnitContext((model,), (), ()))
    assert verdict.verdict == ccf.SOURCE_NOT_CHECKED


# --- blind review round 4: duplicate table names made the verdict order-dependent ------------------

_CONN_M = 'Snowflake.Databases("acme-fixture.snowflakecomputing.com", "WH")'
_COMMENTED_M = f"// Source = {_CONN_M}"


def _model(name: str, parts: tuple[tuple[str, str], ...], m_text: str) -> ccf.Model:
    """A Model from (table, category) pairs - the shape load_model produces."""
    return ccf.Model(name, name, tuple({"table": t, "category": c} for t, c in parts), m_text)


def _judge(
    models: tuple[ccf.Model, ...],
    tables: tuple[str, ...] = ("ORDERS",),
    limitations: tuple[dict, ...] = (),
    declarations: tuple[dict, ...] = (),
    connection_class: str = "snowflake",
) -> ccf.SourceVerdict:
    source = _live_source("ds.sf", connection_class, tables=list(tables))
    ctx = ccf.UnitContext(models, limitations, declarations)
    return ccf._judge_source(source, "live_source", connection_class, "live", ctx)


@pytest.mark.parametrize("swap", [False, True])
def test_one_table_live_in_one_model_and_file_in_another_is_order_independent(swap: bool) -> None:
    """Both orders must agree, and neither may be a clean pass.

    An earlier `_attribute` keyed a dict by normalised table name, so the second partition with a
    given name overwrote the first. Visiting the file-backed model first therefore reported
    CONNECTED and exited 0; visiting it second reported DOWNGRADED. Same inputs, opposite verdicts.
    """
    live = _model("live", (("ORDERS", "live"),), _CONN_M)
    flat = _model("flat", (("ORDERS", "file_ok"),), "")
    verdict = _judge((flat, live) if swap else (live, flat))
    assert verdict.verdict == ccf.SOURCE_NOT_CHECKED
    assert "PARTIAL" in verdict.detail


@pytest.mark.parametrize("swap", [False, True])
def test_two_partitions_of_one_table_are_order_independent(swap: bool) -> None:
    """The same collapse happened WITHIN a model, across partitions of a single table."""
    parts = (("ORDERS", "file_ok"), ("ORDERS", "live")) if swap else (("ORDERS", "live"), ("ORDERS", "file_ok"))
    verdict = _judge((_model("m", parts, _CONN_M),))
    assert verdict.verdict == ccf.SOURCE_NOT_CHECKED


def test_every_permutation_of_models_yields_one_verdict() -> None:
    """Order-independence asserted mechanically rather than on the two orders I happened to try."""
    models = [
        _model("m0", (("ORDERS", "file_ok"),), ""),
        _model("m1", (("ORDERS", "live"),), _CONN_M),
        _model("m2", (("ORDERS", "file_ok"),), _CONN_M),
        _model("m3", (("OTHER", "live"),), _CONN_M),
    ]
    outcomes = {(v.verdict, tuple(v.tables)) for v in (_judge(perm) for perm in itertools.permutations(models))}
    assert len(outcomes) == 1, f"verdict depends on model order: {outcomes}"


def test_duplicate_table_that_is_file_backed_everywhere_is_still_a_finding() -> None:
    """Aggregating all partitions must not blunt the gate: file-only across two models is a downgrade."""
    flat_a = _model("a", (("ORDERS", "file_ok"),), "")
    flat_b = _model("b", (("ORDERS", "file_ok"),), _CONN_M)
    assert _judge((flat_a, flat_b)).verdict == ccf.SOURCE_DOWNGRADED


# --- a finding may rest on partial coverage; a pass may not ----------------------------------------
#
# Found by probing the enumeration's own dimensions rather than by review: `_DECLARED` topped out at
# ONE table, so a partial match was unreachable and this whole class was invisible. 9% of the data
# sources in this repo's specs declare more than one table, and none of those are live -- but a
# multi-table live source is entirely ordinary in the field, which is exactly the profile of a defect
# that ships silently.


def test_a_finding_survives_partial_coverage() -> None:
    """Adding an unmatched table to the spec must not silence a positively-evidenced downgrade.

    Measured before the fix: declaring ORDERS+CUSTOMERS against a model emitting a file-backed ORDERS
    returned NOT_CHECKED, while the identical model with a single-table source returned DOWNGRADED -
    and the detail claimed "none of this source's declared tables match" when ORDERS matched exactly.
    """
    flat = _model("m", (("ORDERS", "file_ok"),), "")
    partial = _judge((flat,), tables=("ORDERS", "CUSTOMERS"))
    assert partial.verdict == ccf.SOURCE_DOWNGRADED
    assert "partial coverage" in partial.detail
    assert "CUSTOMERS" in partial.detail
    assert _judge((flat,), tables=("ORDERS",)).verdict == ccf.SOURCE_DOWNGRADED


def test_a_pass_does_not_survive_partial_coverage() -> None:
    """A clean bill of health covers the WHOLE source, so an unfound table blocks it."""
    live = _model("m", (("ORDERS", "live"),), _CONN_M)
    assert _judge((live,), tables=("ORDERS", "CUSTOMERS")).verdict == ccf.SOURCE_NOT_CHECKED
    assert _judge((live,), tables=("ORDERS",)).verdict == ccf.SOURCE_CONNECTED
    both = _model("m", (("ORDERS", "live"), ("OTHER", "live")), _CONN_M)
    assert _judge((both,), tables=("ORDERS", "OTHER")).verdict == ccf.SOURCE_CONNECTED


def test_the_unattributable_message_is_only_used_when_nothing_matched() -> None:
    """The old message was factually false whenever one declared table matched."""
    flat = _model("m", (("ORDERS", "file_ok"),), "")
    assert "none of this source's declared tables match" not in _judge((flat,), tables=("ORDERS", "CUSTOMERS")).detail
    assert "none of this source's declared tables match" in _judge((flat,), tables=("NOPE",)).detail


@pytest.mark.parametrize(
    "label,limitations,declarations",
    [
        ("generated-edit target", (), ({"target": "ORDERS.tmdl"},)),
        ("limitation naming a table", ({"stage": "semantic_build", "item": "ORDERS", "issue": "x"},), ()),
    ],
)
def test_a_table_scoped_declaration_cannot_certify_an_unexamined_sibling(
    label: str, limitations: tuple, declarations: tuple
) -> None:
    """DECLARED is a pass, so it needs complete coverage too.

    Blind review round 8: "a pass requires complete evidence" had been enforced only on the CONNECTED
    path, so an explicit decision about ORDERS made the unit OK / exit 0 while CUSTOMERS was never
    examined. Both sanctioned record types are table-scoped, and both did it.
    """
    flat = _model("m", (("ORDERS", "file_ok"),), "")
    verdict = _judge((flat,), tables=("ORDERS", "CUSTOMERS"), limitations=limitations, declarations=declarations)
    assert verdict.verdict == ccf.SOURCE_NOT_CHECKED, label
    assert "CUSTOMERS" in verdict.detail
    unit = ccf._finalize_unit(Path("u/migration-spec.json"), [verdict])
    assert ccf.merge([unit])["status"] != ccf.STATUS_OK


def test_a_source_scoped_declaration_still_stands_on_incomplete_coverage() -> None:
    """A record naming the SOURCE attests to the whole source, so it is not blocked.

    Measured across every spec in this repo: 4 decision-stage limitations name a data-source id and 0
    name a table. Refusing every declaration on incomplete coverage would therefore add noise to the
    only shape that actually occurs, while the dangerous table-scoped shape occurs zero times.
    """
    flat = _model("m", (("ORDERS", "file_ok"),), "")
    lims = ({"stage": "semantic_build", "item": "ds.sf", "issue": "materialised on purpose"},)
    assert _judge((flat,), tables=("ORDERS", "CUSTOMERS"), limitations=lims).verdict == ccf.SOURCE_DECLARED


def test_a_table_scoped_declaration_still_passes_when_coverage_is_complete() -> None:
    """The round-8 fix must not add noise to the ordinary complete case."""
    flat = _model("m", (("ORDERS", "file_ok"),), "")
    verdict = _judge((flat,), tables=("ORDERS",), declarations=({"target": "ORDERS.tmdl"},))
    assert verdict.verdict == ccf.SOURCE_DECLARED


def test_a_source_id_like_item_declares_nothing() -> None:
    """`ds.sf_archive` must not speak for `ds.sf` - not as scope, and not as association either.

    Blind review rounds 9 and 10 walked through the two imprecise predicates in turn. Round 9 was the
    source-wide SCOPE test; round 10 was the ASSOCIATION test, which still used normalised-substring
    matching and so let an unrelated archive record certify a complete, positively-evidenced
    downgrade as DECLARED / OK / exit 0. Both now go through `_declaration_scope`, which accepts only
    precise forms, so an archive record declares nothing and the downgrade stands as a FINDING.
    """
    assert ccf._declaration_scope("ds.sf_archive", "ds.sf", ["ORDERS"]) is None
    assert ccf._declaration_scope("ds.sf", "ds.sf", ["ORDERS"]) == "source"
    assert ccf._declaration_scope("ORDERS", "ds.sf", ["ORDERS"]) == "table"
    assert ccf._declaration_scope("ds.sf__ORDERS", "ds.sf", ["ORDERS"]) == "table"
    flat = _model("m", (("ORDERS", "file_ok"),), "")
    lims = ({"stage": "semantic_build", "item": "ds.sf_archive", "issue": "archive-table decision"},)
    verdict = _judge((flat,), tables=("ORDERS",), limitations=lims)
    assert verdict.verdict == ccf.SOURCE_DOWNGRADED
    unit = ccf._finalize_unit(Path("u/migration-spec.json"), [verdict])
    assert ccf.merge([unit])["status"] != ccf.STATUS_OK


def test_sibling_source_ids_do_not_declare_for_each_other() -> None:
    """Grounded in a committed example, not a hypothetical.

    The airline workbook ships two near-duplicate sources whose ids differ only by a `_1` suffix, so
    the shorter is a substring of the longer. Under the old matcher a decision recorded about one
    silently vouched for the other.
    """
    assert ccf._declaration_scope("ds.airline_x_2022_2025_1", "ds.airline_x_2022_2025", ["T"]) is None
    assert ccf._declaration_scope("ds.airline_x_2022_2025", "ds.airline_x_2022_2025", ["T"]) == "source"


def test_enumeration_is_falsifiable_via_loose_declaration_matching() -> None:
    """Restore substring association and the space must go red."""
    original = ccf._declaration_scope

    def loose(item, source_id, table_names):
        if ccf._item_names_source(item, source_id, table_names):
            return "source" if item.strip().casefold() != source_id.strip().casefold() else "source"
        return None

    try:
        ccf._declaration_scope = loose
        assert _violations(), "the space cannot reach an imprecise declaration record"
    finally:
        ccf._declaration_scope = original
    assert _violations() == []


# --- the decision space, enumerated -----------------------------------------------------------------
#
# Three review rounds found five defects because each pass tested the cases its author thought of.
# This enumerates the whole finite space instead and asserts invariants over every point in it. The
# dimensions below are the input axes; `test_enumeration_*_is_falsifiable` proves the harness can
# actually fail, because a green result from a check that cannot go red is worth nothing.
#
# ⚠️ I6 exists because the first draft of this harness could NOT catch the round-4 defect, and the
# falsifiability test is what exposed that. I1 and I4 reason about `mine` - the attribution computed
# by `_attribute`, the very function that was broken - so under the collapsing mutation the oracle
# collapsed with it: the file-backed partition simply vanished from `mine`, and "CONNECTED while
# owning a file-backed partition" was true of nothing. An invariant evaluated through the code under
# test cannot see that code's blind spot. I6 is computed from the raw models instead, so it holds
# independently of any attribution logic.


class _Point(NamedTuple):
    """One combination in the decision space, with its verdict and the raw inputs that produced it."""

    label: str
    verdict: ccf.SourceVerdict
    mine: list[dict[str, str]] | None
    m_text: str
    lims: tuple[dict, ...]
    models: tuple[ccf.Model, ...]
    declared: tuple[str, ...]
    decls: tuple[dict, ...]
    cls: str


_CLASSES = ("snowflake", "mysteryengine")  # mappable / unmappable
_DECLARED = (
    ("ORDERS",),  # attributable, complete
    ("MISSING",),  # nothing matches
    (),  # nothing to scope by
    ("ORDERS", "CUSTOMERS"),  # PARTIAL: one matches, one does not
    ("ORDERS", "OTHER"),  # both match (OTHER is emitted by one partition set)
)
_M_TEXTS = (_CONN_M, _COMMENTED_M, "")
_PARTITION_SETS = (
    ((("ORDERS", "live"),),),
    ((("ORDERS", "file_ok"),),),
    ((("ORDERS", "live"), ("ORDERS", "file_ok")),),
    ((("ORDERS", "live"),), (("ORDERS", "file_ok"),)),
    ((("ORDERS", "file_ok"),), (("ORDERS", "live"),)),
    ((("ORDERS", "file_ok"),), (("ORDERS", "file_ok"),)),
    ((("ORDERS", "live"), ("OTHER", "live")),),  # lets a MULTI-table source match completely
    ((("ORDERS", "live"), ("OTHER", "file_ok")),),  # multi-table, one live one file
)
_DECLS = (
    ((), ()),
    (({"stage": "semantic_build", "item": "ds.sf", "issue": "materialised on purpose"},), ()),
    (({"stage": "parse", "item": "ds.sf", "issue": "parse-stage note"},), ()),
    ((), ({"target": "ORDERS.tmdl"},)),
    # A source-id-LIKE item that is not the source. Round 9: `_item_names_source` matched it by
    # normalised substring, so `ds.sf_archive` claimed source-wide scope over `ds.sf` and certified an
    # unexamined sibling table. The space had no such item, so I7 could not reach the class at all.
    (({"stage": "semantic_build", "item": "ds.sf_archive", "issue": "archive-table decision"},), ()),
)


def _enumerate_space():
    """Yield a Point for every combination in the space.

    The raw inputs are yielded alongside the verdict deliberately. An earlier draft recovered them by
    substring-matching the label, and the truncation in that label made one invariant vacuously true -
    the exact class of silently-unfalsifiable check this harness exists to avoid.
    """
    for cls, declared, m_text, part_sets, (lims, decls) in itertools.product(
        _CLASSES, _DECLARED, _M_TEXTS, _PARTITION_SETS, _DECLS
    ):
        models = tuple(_model(f"m{i}", parts, m_text) for i, parts in enumerate(part_sets))
        verdict = _judge(models, tables=declared, limitations=lims, declarations=decls, connection_class=cls)
        label = f"cls={cls} declared={declared} m={m_text!r} parts={part_sets} decl={(lims, decls)}"
        yield _Point(label, verdict, ccf._attribute(models, set(declared)), m_text, lims, models, declared, decls, cls)


def _raw_partitions(models: tuple[ccf.Model, ...], declared: tuple[str, ...]) -> list[dict[str, str]]:
    """Partitions matching this source's declared tables, computed WITHOUT `_attribute`.

    Deliberately a separate implementation. I1's whole job is to police the attribution, so reading
    its evidence from `_attribute` output makes it blind to any regression that drops partitions:
    blind review round 6 mutated `_attribute` to filter out file-backed partitions, the gate certified
    a live+file source as CONNECTED/OK, and the harness stayed green because its oracle had been
    filtered too. An oracle must not be computed by the code it judges.
    """
    keys = {name.strip("[]\"'").casefold() for name in declared}
    return [p for m in models for p in m.partitions if p["table"].strip("[]\"'").casefold() in keys]


def _precise_forms(declared: tuple[str, ...]) -> set[str]:
    """Every item string that legitimately declares something about the enumeration's source.

    Written out independently of production: the source id, each declared table, and each
    source-qualified table. Anything else is imprecise and must not produce DECLARED.
    """
    forms = {"ds.sf"}
    for name in declared:
        folded = name.strip().casefold()
        forms.add(folded)
        forms.update(f"ds.sf{delimiter}{folded}" for delimiter in (".", "__", "_"))
    return forms


def _raw_unmatched(models: tuple[ccf.Model, ...], declared: tuple[str, ...]) -> list[str]:
    """Declared tables with no emitted counterpart - again computed independently of `_attribute`."""
    emitted = {p["table"].strip("[]\"'").casefold() for m in models for p in m.partitions}
    return [name for name in declared if name.strip("[]\"'").casefold() not in emitted]


def _violations() -> list[str]:
    """Invariant breaches across the whole space. Empty list == the gate held everywhere."""
    bad = []
    for pt in _enumerate_space():
        connected = pt.verdict.verdict == ccf.SOURCE_CONNECTED
        raw = _raw_partitions(pt.models, pt.declared)
        if connected and any(p["category"] in ccf.FILE_CATEGORIES for p in raw):
            bad.append(f"I1 CONNECTED while owning a file-backed partition :: {pt.label}")
        if connected and pt.m_text == _COMMENTED_M:
            bad.append(f"I2 CONNECTED on a commented-out connector :: {pt.label}")
        if pt.verdict.connection_class == "mysteryengine" and pt.verdict.verdict != ccf.SOURCE_NOT_CHECKED:
            bad.append(f"I3 unmappable class certified as {pt.verdict.verdict} :: {pt.label}")
        if connected and not raw:
            bad.append(f"I4 CONNECTED while nothing could be attributed :: {pt.label}")
        # The stage name is hard-coded, NOT read from ccf.DECISION_STAGES. Reading the module constant
        # made this invariant vacuous: a mutation that adds "parse" to DECISION_STAGES also flipped
        # this test to False, so the oracle excused the very behaviour it exists to forbid. Measured:
        # 0 own-tag violations under its own mutation. Fourth instance of oracle-coupling in this file.
        parse_only = bool(pt.lims) and all(entry.get("stage") == "parse" for entry in pt.lims)
        if pt.verdict.verdict == ccf.SOURCE_DECLARED and parse_only:
            bad.append(f"I5 parse-stage limitation excused a downgrade :: {pt.label}")
        # I7: a PASS is a statement about the whole source, so it needs evidence about every declared
        # table. Round 8 showed why this must cover EVERY pass-equivalent verdict, not just CONNECTED:
        # enforcing it on the connected branch alone let a table-scoped declaration certify an
        # unexamined sibling table and exit 0. The pass set is written out literally here rather than
        # read from a production constant - reading `ccf.DECISION_STAGES` is exactly what made I5 dead.
        passing = pt.verdict.verdict in {ccf.SOURCE_CONNECTED, ccf.SOURCE_DECLARED}
        source_wide = any(
            str(entry.get("item") or "") == "ds.sf"
            and str(entry.get("stage")) in {"semantic_build", "validate", "deploy"}
            for entry in pt.lims
        )
        if passing and not source_wide and _raw_unmatched(pt.models, pt.declared):
            bad.append(f"I7 {pt.verdict.verdict} while coverage is incomplete :: {pt.label}")
        # I8: DECLARED is a pass, so it needs a PRECISE record - exact source id, or exact declared
        # table name, optionally source-qualified. Rounds 9 and 10 were the same imprecision at two
        # different sites (scope, then association), so this asserts the property directly instead of
        # trusting whichever matcher happens to be wired in. Computed here, not read from production.
        if pt.verdict.verdict == ccf.SOURCE_DECLARED and not pt.decls:
            precise = any(
                str(e.get("item") or "").strip().casefold() in _precise_forms(pt.declared)
                and str(e.get("stage")) in {"semantic_build", "validate", "deploy"}
                for e in pt.lims
            )
            if not precise:
                bad.append(f"I8 DECLARED on an imprecise record :: {pt.label}")
        outcomes = {
            (v.verdict, tuple(v.tables))
            for v in (
                _judge(perm, tables=pt.declared, limitations=pt.lims, declarations=pt.decls, connection_class=pt.cls)
                for perm in itertools.permutations(pt.models)
            )
        }
        if len(outcomes) > 1:
            bad.append(f"I6 verdict depends on model order {sorted(outcomes)} :: {pt.label}")
    return bad


def test_decision_space_holds_every_invariant() -> None:
    """The full cross-product of input axes, seven invariants, zero tolerated breaches."""
    expected = len(_CLASSES) * len(_DECLARED) * len(_M_TEXTS) * len(_PARTITION_SETS) * len(_DECLS)
    assert len(list(_enumerate_space())) == expected
    assert _violations() == []


def test_enumeration_is_falsifiable_via_comment_stripping() -> None:
    """Proof the harness can go red: a connector named only inside an M comment must not count."""
    original = ccf._strip_m_comments
    try:
        ccf._strip_m_comments = lambda text: text  # mutation: stop stripping comments
        assert ccf.Model("m", "m", (), _COMMENTED_M).has_connector("Snowflake"), "mutation did not land"
        assert _violations(), "harness reported clean under a mutation that should break it"
    finally:
        ccf._strip_m_comments = original
    assert _violations() == []


def test_enumeration_is_falsifiable_via_the_round4_collapse() -> None:
    """Proof the harness now covers the axis round 4 found - restore the collapse, it must go red."""
    original = ccf._attribute

    def collapsing(models, tables):
        if not tables:
            return [], []
        by_exact = {ccf._normalise_table(p["table"]): p for m in models for p in m.partitions}
        mine = [by_exact[k] for k in (ccf._normalise_table(t) for t in tables) if k in by_exact]
        unmatched = [t for t in sorted(tables) if ccf._normalise_table(t) not in by_exact]
        return (mine, unmatched) if len(mine) == len(tables) - len(unmatched) else ([], unmatched)

    try:
        ccf._attribute = collapsing
        assert _violations(), "the enumeration does not cover duplicate table names"
    finally:
        ccf._attribute = original
    assert _violations() == []


def test_enumeration_is_falsifiable_via_dropped_file_partitions() -> None:
    """Round 6: a regression that stably HIDES file-backed partitions must still be caught.

    This is the mutation that proved I1 was vacuous. It is order-independent, so I6 cannot see it,
    and it filters the very evidence I1 used to read from `_attribute` - so before `_raw_partitions`
    existed, the gate certified a live+file source as CONNECTED/OK with the harness fully green.
    """
    original = ccf._attribute

    def drop_file_parts(models, tables):
        matched, unmatched = original(models, tables)
        return [p for p in matched if p["category"] not in ccf.FILE_CATEGORIES], unmatched

    try:
        ccf._attribute = drop_file_parts
        source = _live_source("ds.sf", "snowflake", tables=["ORDERS"])
        flat = _model("file", (("ORDERS", "file_ok"),), "")
        live = _model("live", (("ORDERS", "live"),), _CONN_M)
        mutated = ccf._judge_source(source, "live_source", "snowflake", "live", ccf.UnitContext((flat, live), (), ()))
        assert mutated.verdict == ccf.SOURCE_CONNECTED, "mutation did not land"
        assert _violations(), "harness is blind to a stable loss of file-backed evidence"
    finally:
        ccf._attribute = original
    assert _violations() == []


# --- blind review round 5: the same false green, one level up at the root merge ---------------------
#
# I6 permutes models WITHIN one unit, so it is structurally unable to see an aggregation defect. This
# block enumerates the merge separately. The lesson is the round-4 lesson again: a harness only covers
# the layer it calls, and "the enumeration is green" says nothing about a layer it never enters.


def _unit(status: str) -> dict:
    return {"status": status, "downgraded": 0, "declared": 0, "connected": 0}


_STATUSES = (ccf.STATUS_OK, ccf.STATUS_DOWNGRADED, ccf.STATUS_SKIPPED)


def _merge_violations() -> list[str]:
    """A clean OK must mean every scanned unit was checked and passed - nothing weaker."""
    bad = []
    for size in range(4):
        for combo in itertools.product(_STATUSES, repeat=size):
            merged = ccf.merge([_unit(s) for s in combo])["status"]
            if merged == ccf.STATUS_OK and not (combo and all(s == ccf.STATUS_OK for s in combo)):
                bad.append(f"OK merged from {combo}")
            if ccf.STATUS_DOWNGRADED in combo and merged != ccf.STATUS_DOWNGRADED:
                bad.append(f"a downgrade was outranked: {combo} -> {merged}")
    return bad


def test_one_unchecked_unit_keeps_the_whole_root_off_a_clean_pass() -> None:
    """One clean unit beside one unattributable unit must not report OK / exit 0."""
    merged = ccf.merge([_unit(ccf.STATUS_OK), _unit(ccf.STATUS_SKIPPED)])
    assert merged["status"] == ccf.STATUS_SKIPPED
    assert merged["units_unchecked"] == 1


def test_partial_coverage_is_not_rendered_as_nothing_measured() -> None:
    """The operator must not be told the opposite of what happened.

    A root with one checked unit and one unattributable unit is SKIPPED, but "nothing measured" is
    false: one unit WAS measured and passed. Blind review round 6 found the CLI saying exactly that.
    """
    source = _live_source("ds.sf", "snowflake", tables=["ORDERS"])
    live = _model("live", (("ORDERS", "live"),), _CONN_M)
    renamed = _model("m", (("RENAMED", "file_ok"),), "")
    checked = ccf._finalize_unit(
        Path("ok/migration-spec.json"),
        [ccf._judge_source(source, "live_source", "snowflake", "live", ccf.UnitContext((live,), (), ()))],
    )
    unchecked = ccf._finalize_unit(
        Path("un/migration-spec.json"),
        [ccf._judge_source(source, "live_source", "snowflake", "live", ccf.UnitContext((renamed,), (), ()))],
    )
    text = ccf.render(ccf.merge([checked, unchecked]))
    assert "nothing measured" not in text
    assert "partial coverage" in text
    assert "1 of 2 unit(s) checked" in text
    assert "un/migration-spec.json" in text or "un" in text

    # the genuinely-empty case must keep its original wording
    assert "nothing measured" in ccf.render(ccf.merge([]))


def test_merge_precedence_holds_across_every_combination() -> None:
    """40 combinations of up to three units: DOWNGRADED > SKIPPED > OK, with no exceptions."""
    assert _merge_violations() == []


def test_merge_enumeration_is_falsifiable() -> None:
    """Restore the ok-before-skipped precedence; the enumeration must go red."""
    original = ccf.merge

    def ok_wins(units):
        report = original(units)
        if report["status"] == ccf.STATUS_SKIPPED and any(u["status"] == ccf.STATUS_OK for u in units):
            report["status"] = ccf.STATUS_OK
        return report

    try:
        ccf.merge = ok_wins
        assert _merge_violations(), "the merge enumeration cannot detect an OK-outranks-SKIPPED bug"
    finally:
        ccf.merge = original
    assert _merge_violations() == []


# --- every invariant proven non-vacuous INDIVIDUALLY -----------------------------------------------
#
# The falsifiability tests above each prove that SOME invariant fires. That is weaker than it looks:
# it cannot distinguish a working invariant from a dead one standing next to a working neighbour.
# Running this per-invariant probe found I5 dead - its `parse_only` test read `ccf.DECISION_STAGES`,
# the very constant its mutation changes, so the oracle excused the behaviour it exists to forbid.
# That was the FOURTH oracle-coupling defect in this file, and the first one found here rather than
# by review. Hence this test: each invariant must fire under a mutation aimed squarely at it.


def _mutations():
    """(tag, apply, restore) for a mutation that must trip exactly that invariant."""

    def swap(module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        return lambda: setattr(module, name, original)

    def drop_file_parts():
        original = ccf._attribute

        def dropped(models, tables):
            matched, unmatched = original(models, tables)
            return [p for p in matched if p["category"] not in ccf.FILE_CATEGORIES], unmatched

        setattr(ccf, "_attribute", dropped)
        return lambda: setattr(ccf, "_attribute", original)

    def loose_connectivity():
        original = ccf._connectivity

        def loose(models, token, tables):
            cov = original(models, token, tables)
            if not cov.attributable:
                return ccf.Coverage(any(m.has_connector(token) for m in models), False, [], True, [])
            return cov

        setattr(ccf, "_connectivity", loose)
        return lambda: setattr(ccf, "_connectivity", original)

    def collapse():
        original = ccf._attribute

        def collapsing(models, tables):
            if not tables:
                return [], []
            by_exact = {ccf._normalise_table(p["table"]): p for m in models for p in m.partitions}
            mine = [by_exact[k] for k in (ccf._normalise_table(t) for t in tables) if k in by_exact]
            unmatched = [t for t in sorted(tables) if ccf._normalise_table(t) not in by_exact]
            return (mine, unmatched) if mine else ([], unmatched)

        setattr(ccf, "_attribute", collapsing)
        return lambda: setattr(ccf, "_attribute", original)

    def ignore_incompleteness():
        """Certify a source whose coverage is incomplete - the round-8 defect, restored."""
        original = ccf._attribute

        def hide_unmatched(models, tables):
            matched, _ = original(models, tables)
            return matched, []

        setattr(ccf, "_attribute", hide_unmatched)
        return lambda: setattr(ccf, "_attribute", original)

    def loose_declaration():
        """Restore substring association (rounds 9/10) so I8 must fire."""
        original = ccf._declaration_scope

        def loose(item, source_id, table_names):
            return "source" if ccf._item_names_source(item, source_id, table_names) else None

        setattr(ccf, "_declaration_scope", loose)
        return lambda: setattr(ccf, "_declaration_scope", original)

    return [
        ("I1", drop_file_parts),
        ("I2", lambda: swap(ccf, "_strip_m_comments", lambda text: text)),
        ("I3", lambda: swap(ccf, "CLASS_TO_CONNECTOR", {**ccf.CLASS_TO_CONNECTOR, "mysteryengine": "Snowflake"})),
        ("I4", loose_connectivity),
        ("I5", lambda: swap(ccf, "DECISION_STAGES", frozenset({*ccf.DECISION_STAGES, "parse"}))),
        ("I6", collapse),
        ("I7", ignore_incompleteness),
        ("I8", loose_declaration),
    ]


@pytest.mark.parametrize("tag,mutate", _mutations(), ids=[t for t, _ in _mutations()])
def test_each_invariant_fires_under_its_own_mutation(tag: str, mutate) -> None:
    """A dead invariant is worse than a missing one: it is counted as coverage."""
    assert _violations() == [], "baseline must be clean or the result below means nothing"
    restore = mutate()
    try:
        own = [v for v in _violations() if v.startswith(tag)]
        assert own, f"{tag} is VACUOUS - it did not fire under a mutation aimed at it"
    finally:
        restore()
    assert _violations() == []
