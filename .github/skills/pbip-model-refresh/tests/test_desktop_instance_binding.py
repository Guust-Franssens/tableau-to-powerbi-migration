"""Tests for binding a Desktop run to the RIGHT Power BI Desktop instance.

In a parallel batch the toolkit drives several Power BI Desktop instances at once, and two pieces of
state have to agree: the destination `<Name>.SemanticModel/.pbi/cache.abf` (resolved from the pid,
so always correct) and the Analysis Services port the data is read from. `discover_port` used to
silently widen to "any msmdsrv on the machine" whenever the pid-scoped lookup came back empty -
most likely because Desktop was up before its msmdsrv had bound a port - and then took the first
result. That writes a SIBLING migration's model into this migration's own correct `cache.abf`, and
every existing check still passes: a cache check can only see that the file exists, is non-empty and
was newly written, and `row_counts` queries that same wrong port, so both signals agree and both are
wrong. `cache.abf` is gitignored, so nothing downstream catches it either.

So these tests lock in the two defences: never widen a named pid (and never pick between ambiguous
candidates), and prove the connected model really is the one that owns the cache before touching it.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

# `conftest.py` next to this file puts the skill's own `scripts/` on `sys.path`, resolved relatively
# so the suite runs wherever this folder was copied.
# ruff: noqa: E402  (the conftest-provided path must be in place before these imports)
import probe_desktop_query
import refresh_pbip_model
from probe_desktop_query import discover_port, table_names
from refresh_pbip_model import same_model, tmdl_tables

SKILL_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_IDENTITY = probe_desktop_query.DesktopIdentity(111, "100", 222, "101", 52001)
OBSERVED_CATALOGUE = "11111111-2222-3333-4444-555555555555"

# Real Power BI auto date/time tables ALWAYS carry a canonical 8-4-4-4-12 GUID suffix, and the
# identity fingerprint now filters them by that exact shape rather than a name prefix (round-3
# blocker 4). Fixtures that stand in for the scaffolding must therefore use the real generated shape;
# a short stub like `LocalDateTable_abc` is (correctly) treated as a genuine user table now.
AUTO_DATE_LOCAL = "LocalDateTable_9f2c1e4a-1111-2222-3333-444455556666"
AUTO_DATE_TEMPLATE = "DateTableTemplate_0a1b2c3d-4e5f-6789-abcd-ef0123456789"


def test_the_suite_exercises_the_scripts_bundled_beside_it() -> None:
    """A copied skill must test ITS OWN scripts, not a same-named module from the host repo."""
    for module in (probe_desktop_query, refresh_pbip_model):
        assert Path(module.__file__).resolve().parent == SKILL_ROOT / "scripts", (
            f"{module.__name__} was imported from {module.__file__}, not from this skill's scripts/"
        )


def _stub_ports(monkeypatch, answers: dict[int | None, list[list[int]]]) -> list[int | None]:
    """Replace the msmdsrv lookup with scripted answers, recording every scope it was asked for."""
    asked: list[int | None] = []

    def fake(desktop_pid: int | None) -> list[int]:
        asked.append(desktop_pid)
        queue = answers.get(desktop_pid, [[]])
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(probe_desktop_query, "_msmdsrv_ports", fake)
    monkeypatch.setattr(probe_desktop_query, "PORT_DISCOVERY_INTERVAL_SECONDS", 0)
    return asked


def test_scoped_lookup_returns_the_childs_port(monkeypatch) -> None:
    """The happy path: msmdsrv is a child of Desktop, so the parent-pid match is ground truth."""
    asked = _stub_ports(monkeypatch, {111: [[52001]]})
    assert discover_port(111) == 52001
    assert asked == [111]


def test_a_named_pid_never_falls_back_to_another_instance(monkeypatch) -> None:
    """The bug: an empty scoped result silently became "any msmdsrv", i.e. a sibling's model."""
    asked = _stub_ports(monkeypatch, {111: [[]], None: [[52002]]})
    with pytest.raises(SystemExit) as exit_info:
        discover_port(111)
    assert exit_info.value.code == 2
    assert None not in asked, "a named pid must never widen to the machine-wide msmdsrv list"


def test_startup_lag_is_retried_before_failing(monkeypatch) -> None:
    """The likeliest trigger is timing - Desktop is up before msmdsrv has bound its port."""
    asked = _stub_ports(monkeypatch, {111: [[], [], [52003]]})
    assert discover_port(111) == 52003
    assert asked == [111, 111, 111]


def test_two_ports_for_one_pid_is_an_error_not_a_coin_flip(monkeypatch) -> None:
    """Taking `ports[0]` from an ambiguous answer is the same silent guess, one level down."""
    _stub_ports(monkeypatch, {111: [[52004, 52005]]})
    with pytest.raises(SystemExit) as exit_info:
        discover_port(111)
    assert exit_info.value.code == 2


def test_several_instances_without_a_pid_is_an_error(monkeypatch) -> None:
    """With no pid the caller did not name an instance - so there must be exactly one to name."""
    _stub_ports(monkeypatch, {None: [[52006, 52007]]})
    with pytest.raises(SystemExit) as exit_info:
        discover_port(None)
    assert exit_info.value.code == 2


def test_single_instance_without_a_pid_still_works(monkeypatch) -> None:
    """The interactive one-instance case must stay as easy as it was."""
    _stub_ports(monkeypatch, {None: [[52008]]})
    assert discover_port(None) == 52008


def test_msmdsrv_lookup_scopes_by_pid_and_dedupes_its_output(monkeypatch) -> None:
    """The PowerShell boundary: the pid reaches it, and one port per process comes back."""
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return type("R", (), {"stdout": "WARNING: something\r\n52001\r\n52001\r\n"})()

    monkeypatch.setattr(probe_desktop_query.subprocess, "run", fake_run)
    assert probe_desktop_query._msmdsrv_ports(111) == [52001]
    assert seen["env"]["PID_FILTER"] == "111"
    assert probe_desktop_query._msmdsrv_ports(None) == [52001]
    assert seen["env"]["PID_FILTER"] == "", "an inherited PID_FILTER must not scope a machine-wide query"


def test_observation_identity_uses_exact_parent_start_child_and_unique_port(monkeypatch):
    calls = []
    monkeypatch.setattr(probe_desktop_query, "os", SimpleNamespace(name="nt", environ={}))

    def observe(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps(asdict(OBSERVED_IDENTITY)))

    monkeypatch.setattr(probe_desktop_query.subprocess, "run", observe)
    assert probe_desktop_query.desktop_identity(111, 52001) == OBSERVED_IDENTITY
    assert calls[0][1]["env"]["OBSERVATION_PID"] == "111"
    assert calls[0][1]["timeout"] == 30 and calls[0][1]["check"] is True
    script = calls[0][0][-1]
    assert "ParentProcessId -eq $p.ProcessId" in script
    assert "$a.Count -ne 1" in script and "$ports.Count -ne 1" in script
    assert "Select-Object -First" not in script
    with pytest.raises(probe_desktop_query.ObservationUnavailable, match="^WRONG_PID_PORT$"):
        probe_desktop_query.desktop_identity(111, 52002)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", True),
        ("port", 65536),
        ("process_start", ""),
        ("as_process_start", "1"),
    ],
)
def test_observation_identity_rejects_missing_or_coerced_fields(field, value):
    payload = asdict(OBSERVED_IDENTITY)
    payload[field] = value
    with pytest.raises(ValueError):
        probe_desktop_query._validate_identity(payload, 111)
    with pytest.raises(ValueError):
        probe_desktop_query._validate_identity({}, 111)


class _FakeReader:
    """Minimal stand-in for an ADOMD data reader (PascalCase because the real one is .NET)."""

    def __init__(self, rows: list[tuple[str, bool]]) -> None:
        self._rows = list(rows)
        self._row: tuple[str, bool] | None = None
        self.closed = False

    def Read(self) -> bool:  # noqa: N802
        if not self._rows:
            return False
        self._row = self._rows.pop(0)
        return True

    def GetValue(self, index: int):  # noqa: N802
        return self._row[index]

    def Close(self) -> None:  # noqa: N802
        self.closed = True


class _FakeCommand:
    def __init__(self, reader: _FakeReader) -> None:
        self._reader = reader
        self.CommandText = ""

    def ExecuteReader(self) -> _FakeReader:  # noqa: N802
        return self._reader


class _FakeConnection:
    def __init__(self, rows: list[tuple[str, bool]]) -> None:
        self.reader = _FakeReader(rows)

    def CreateCommand(self) -> _FakeCommand:  # noqa: N802
        return _FakeCommand(self.reader)


@pytest.mark.parametrize("catalogues", [[], [OBSERVED_CATALOGUE] * 2, ["named-model"]])
def test_observation_catalogue_is_one_exact_id_and_reader_is_closed(catalogues):
    connection = _FakeConnection([(name, False) for name in catalogues])
    if catalogues == [OBSERVED_CATALOGUE]:
        assert probe_desktop_query.catalogue_id(connection) == OBSERVED_CATALOGUE
    else:
        with pytest.raises(probe_desktop_query.ObservationUnavailable, match="^CATALOGUE_UNESTABLISHED$"):
            probe_desktop_query.catalogue_id(connection)
    assert connection.reader.closed


def test_table_names_filters_the_auto_date_scaffolding_but_can_keep_hidden_tables() -> None:
    """A hidden table is part of a model's fingerprint; `LocalDateTable_*` never is (not in TMDL)."""
    rows = [("Sales", False), ("Bridge", True), (AUTO_DATE_LOCAL, True), (AUTO_DATE_TEMPLATE, True)]
    assert table_names(_FakeConnection(rows)) == ["Sales"]
    assert table_names(_FakeConnection(rows), include_hidden=True) == ["Sales", "Bridge"]


def _model_folder(root: Path, name: str, tables: list[str], *, bom: bool = False) -> Path:
    """A minimal `<Name>.SemanticModel` on disk, returning the cache.abf destination inside it."""
    tables_dir = root / f"{name}.SemanticModel" / "definition" / "tables"
    tables_dir.mkdir(parents=True)
    for table in tables:
        head = "\ufeff" if bom else "/// doc\n"
        (tables_dir / f"{table}.tmdl").write_text(f"{head}table '{table}'\n\n\tcolumn X\n", encoding="utf-8")
    return root / f"{name}.SemanticModel" / ".pbi" / "cache.abf"


def _stub_live_to_match_disk(monkeypatch, cache: Path) -> None:
    """Stub `same_model`'s live side to mirror the on-disk model exactly (a legitimate exact match).

    `same_model` reads the connected model's tables, columns AND measures off the AS port; with no
    Desktop in a unit test all three are faked. Deriving the fake from the SAME on-disk model via the
    real parsers keeps both sides in lockstep, so a test that only cares about the main() flow gets a
    clean identity pass without hand-copying a schema. `raising=False` on the columns/measures stub is
    what lets the revert experiment run: round-1 has no `_live_columns_measures`, and there the stub is
    simply set and never consulted.
    """
    model_dir = cache.parent.parent
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: set(tmdl_tables(model_dir)))
    monkeypatch.setattr(
        refresh_pbip_model,
        "_live_columns_measures",
        lambda port: refresh_pbip_model.tmdl_columns_measures(model_dir),
        raising=False,
    )


def test_tmdl_tables_reads_through_a_byte_order_mark(tmp_path: Path) -> None:
    """Desktop writes TMDL with a BOM, and a BOM before `table` would empty the whole fingerprint."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"], bom=True)
    assert tmdl_tables(cache.parent.parent) == {"Orders"}


def test_auto_date_tables_on_disk_do_not_look_like_a_stranger(monkeypatch, tmp_path: Path) -> None:
    """A PBIP with auto date/time on serializes `LocalDateTable_*.tmdl` into `definition/tables/`."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders", AUTO_DATE_LOCAL, AUTO_DATE_TEMPLATE])
    assert tmdl_tables(cache.parent.parent) == {"Orders"}
    _stub_live_to_match_disk(monkeypatch, cache)
    ok, message = same_model(52001, cache)
    assert ok, message


def test_a_user_table_named_like_the_auto_date_prefix_stays_in_the_disk_fingerprint(tmp_path: Path) -> None:
    """A GENUINE user table like `LocalDateTableSales` must NOT be mistaken for auto date/time scaffolding."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders", "LocalDateTableSales", "DateTableTemplateArchive"])
    assert tmdl_tables(cache.parent.parent) == {"Orders", "LocalDateTableSales", "DateTableTemplateArchive"}


def test_same_model_accepts_an_exact_case_insensitive_match(monkeypatch, tmp_path: Path) -> None:
    """The model that OWNS the cache matches the TMDL table-for-table, case-insensitively."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders", "Date Table"])
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"orders", "date table"})
    monkeypatch.setattr(
        refresh_pbip_model,
        "_live_columns_measures",
        lambda port: ({("orders", "X"), ("date table", "X")}, set()),
        raising=False,
    )
    ok, message = same_model(52001, cache)
    assert ok, message
    assert "exact match" in message


def test_same_model_refuses_a_superset_schema(monkeypatch, tmp_path: Path) -> None:
    """A sibling holding ALL your tables PLUS more is a different model, not a lagging TMDL (#114)."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders", "Date Table"])
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"orders", "Date Table", "Extra"})
    ok, message = same_model(52001, cache)
    assert not ok
    assert "Extra" in message, "the extra live table must be named as the reason for refusal"


def test_same_model_refuses_a_siblings_model(monkeypatch, tmp_path: Path) -> None:
    """The only check that can catch a wrong-instance bind - file metadata never can."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders", "Date Table"])
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Turbine", "CO2 Savings"})
    ok, message = same_model(52001, cache)
    assert not ok
    assert "Orders" in message and "MyMigration.SemanticModel" in message


def test_same_model_fails_closed_when_it_cannot_verify(monkeypatch, tmp_path: Path) -> None:
    """'Cannot verify' must FAIL CLOSED, because this gate guards a write into a project (#114)."""
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Whatever"})
    ok, message = same_model(52001, None)
    assert not ok and "UNVERIFIED" in message.upper()

    empty = tmp_path / "Empty.SemanticModel" / ".pbi" / "cache.abf"
    empty.parent.mkdir(parents=True)
    ok, message = same_model(52001, empty)
    assert not ok and "UNVERIFIED" in message.upper()


def test_same_model_refuses_when_a_column_differs(monkeypatch, tmp_path: Path) -> None:
    """Matching table NAMES but a different column is a different model - tables alone are not enough (#114)."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])  # disk column: Orders[X]
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Orders"})
    monkeypatch.setattr(
        refresh_pbip_model, "_live_columns_measures", lambda port: ({("Orders", "Y")}, set()), raising=False
    )
    ok, message = same_model(52001, cache)
    assert not ok
    assert "column" in message


def test_same_model_refuses_when_a_measure_differs(monkeypatch, tmp_path: Path) -> None:
    """Matching tables and columns but a different measure is still a different model (#114)."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])  # disk: Orders[X], no measures
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Orders"})
    monkeypatch.setattr(
        refresh_pbip_model,
        "_live_columns_measures",
        lambda port: ({("Orders", "X")}, {("Orders", "Total Sales")}),
        raising=False,
    )
    ok, message = same_model(52001, cache)
    assert not ok
    assert "measure" in message


def test_tmdl_columns_measures_parses_quoted_and_calculated_names(tmp_path: Path) -> None:
    """The disk fingerprint reads column/measure NAMES only - quoted or not - never the expression."""
    tables_dir = tmp_path / "M.SemanticModel" / "definition" / "tables"
    tables_dir.mkdir(parents=True)
    (tables_dir / "Sales.tmdl").write_text(
        "table Sales\n"
        "\n"
        "\tcolumn 'Order Date'\n"
        "\tcolumn Qty\n"
        "\tcolumn Margin = [Revenue] - [Cost]\n"  # calculated column: name before '='
        "\n"
        "\tmeasure 'Total Sales' = SUM(Sales[Revenue])\n"
        "\tmeasure Count = COUNTROWS(Sales)\n",
        encoding="utf-8",
    )
    columns, measures = refresh_pbip_model.tmdl_columns_measures(tmp_path / "M.SemanticModel")
    assert columns == {("Sales", "Order Date"), ("Sales", "Qty"), ("Sales", "Margin")}
    assert measures == {("Sales", "Total Sales"), ("Sales", "Count")}


def test_tmdl_columns_measures_skips_auto_date_tables(tmp_path: Path) -> None:
    """Auto date/time scaffolding is filtered on the disk side, mirroring the live-side filter."""
    tables_dir = tmp_path / "M.SemanticModel" / "definition" / "tables"
    tables_dir.mkdir(parents=True)
    (tables_dir / "Sales.tmdl").write_text("table Sales\n\n\tcolumn Amount\n", encoding="utf-8")
    (tables_dir / f"{AUTO_DATE_LOCAL}.tmdl").write_text(
        f"table {AUTO_DATE_LOCAL}\n\n\tcolumn Year\n\tmeasure M = 1\n", encoding="utf-8"
    )
    columns, measures = refresh_pbip_model.tmdl_columns_measures(tmp_path / "M.SemanticModel")
    assert columns == {("Sales", "Amount")}
    assert measures == set()


class _FingerprintReader:  # pylint: disable=too-few-public-methods
    """A minimal positional ADOMD reader (PascalCase mirrors the .NET one)."""

    def __init__(self, rows: list[list]) -> None:
        self._rows = [list(row) for row in rows]
        self._cur: list | None = None

    def Read(self) -> bool:  # noqa: N802
        if not self._rows:
            return False
        self._cur = self._rows.pop(0)
        return True

    def GetValue(self, index: int):  # noqa: N802
        return self._cur[index]

    def Close(self) -> None:  # noqa: N802
        self._cur = None


class _FingerprintCommand:  # pylint: disable=too-few-public-methods
    def __init__(self, conn: _FingerprintConn) -> None:
        self._conn = conn
        self.CommandText = ""  # noqa: N815

    def ExecuteReader(self) -> _FingerprintReader:  # noqa: N802
        return self._conn.reader_for(self.CommandText)


class _FingerprintConn:  # pylint: disable=too-few-public-methods
    """Answers the DMV queries `column_names`/`measure_names` send: table-id map, columns, measures."""

    def __init__(self, *, tables: dict[str, str], columns: list[tuple], measures: list[tuple]) -> None:
        self._tables = tables
        self._columns = columns
        self._measures = measures

    def CreateCommand(self) -> _FingerprintCommand:  # noqa: N802
        return _FingerprintCommand(self)

    def reader_for(self, text: str) -> _FingerprintReader:
        if "TMSCHEMA_TABLES" in text:
            return _FingerprintReader([[tid, name] for tid, name in self._tables.items()])
        if "TMSCHEMA_COLUMNS" in text:
            return _FingerprintReader(self._columns)
        if "TMSCHEMA_MEASURES" in text:
            return _FingerprintReader(self._measures)
        raise AssertionError(f"unexpected DMV: {text}")


def test_column_names_filters_rownumber_and_auto_date_columns() -> None:
    """The live column fingerprint drops the auto RowNumber index and the auto date/time scaffolding (#114)."""
    conn = _FingerprintConn(
        tables={"t1": "Orders", "t2": AUTO_DATE_LOCAL},
        columns=[
            ("t1", "Amount", "Amount", 1),  # data column -> kept
            ("t1", "", "Qty", 2),  # calculated, no explicit name -> InferredName
            ("t1", "RowNumber-2b", "RowNumber", 3),  # RowNumber (Type 3) -> filtered
            ("t2", "Date", "Date", 1),  # auto date-table column -> filtered
        ],
        measures=[],
    )
    assert probe_desktop_query.column_names(conn) == {("Orders", "Amount"), ("Orders", "Qty")}


def test_measure_names_filters_auto_date_measures() -> None:
    """Measures on the auto date/time tables are filtered too, mirroring the disk side."""
    conn = _FingerprintConn(
        tables={"t1": "Sales", "t2": AUTO_DATE_TEMPLATE},
        columns=[],
        measures=[("t1", "Total Sales"), ("t2", "Some Auto Measure")],
    )
    assert probe_desktop_query.measure_names(conn) == {("Sales", "Total Sales")}


def _reach_the_instance(monkeypatch, pbip: Path) -> None:
    """Route `main` past the bridge and observer preflights to the pid-111 Desktop that has `pbip` open."""
    instance = {"pid": 111, "currentFilePath": str(pbip)}
    monkeypatch.setattr(refresh_pbip_model, "_ui_bridge_preflight", lambda *_args: instance)
    monkeypatch.setattr(
        refresh_pbip_model,
        "_ui_refresh_preflight",
        lambda pid, inst, *_args, **_kwargs: (inst, refresh_pbip_model._credential_state(pid)),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)


def test_main_aborts_before_refreshing_a_wrong_bound_instance(monkeypatch, tmp_path: Path, capsys) -> None:
    """End to end: a wrong port must never reach Desktop's Refresh, Save or the row count."""
    _model_folder(tmp_path, "MyMigration", ["Orders"])
    _reach_the_instance(monkeypatch, tmp_path / "MyMigration.pbip")
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Turbine"})
    for never in ("invoke_desktop_action", "_ui_refresh_and_save", "row_counts"):
        monkeypatch.setattr(refresh_pbip_model, never, _explode(never))

    assert refresh_pbip_model.main(["--pid", "111"]) == 2
    assert "REFRESH: WRONG_MODEL" in capsys.readouterr().out


def _project_with_two_models(root: Path, bypath: str | None, *, stem_decoy: bool) -> Path:
    """Two `.SemanticModel` folders, a `.pbip` and its report; `bypath` is the pbir binding (or None).

    Returns the `.pbip` path (stem `Proj`). The BOUND model is always `Zed`, which sorts LAST, so a
    fixture that resolved to it cannot have done so by grabbing the first-sorted sibling. `stem_decoy`
    controls the other folder: `Proj` (matching the `.pbip` stem) exposes the #114 round-2 blocker 3a
    hole - the name heuristic must NOT short-circuit the binding - while `Alpha` (no stem match) is
    the genuinely-ambiguous case where, absent a binding, `cache_file` must refuse to guess.
    """
    other = "Proj" if stem_decoy else "Alpha"
    for name in (other, "Zed"):
        tables = root / f"{name}.SemanticModel" / "definition" / "tables"
        tables.mkdir(parents=True)
        (tables / "T.tmdl").write_text("table 'T'\n\n\tcolumn X\n", encoding="utf-8")
    report = root / "Proj.Report"
    report.mkdir(parents=True)
    dataset_ref = {"byPath": {"path": bypath}} if bypath else {}
    (report / "definition.pbir").write_text(
        json.dumps({"version": "1.0", "datasetReference": dataset_ref}), encoding="utf-8"
    )
    pbip = root / "Proj.pbip"
    pbip.write_text(
        json.dumps({"version": "1.0", "artifacts": [{"report": {"path": "Proj.Report"}}]}), encoding="utf-8"
    )
    return pbip


def test_cache_file_refuses_to_guess_when_the_binding_is_ambiguous(tmp_path: Path) -> None:
    """No name match and no resolvable binding => None, so the identity gate fails closed."""
    pbip = _project_with_two_models(tmp_path, None, stem_decoy=False)
    assert refresh_pbip_model.cache_file(111, instance={"pid": 111, "currentFilePath": str(pbip)}) is None


def test_cache_file_uses_the_stem_only_when_no_binding_resolves(tmp_path: Path) -> None:
    """The stem heuristic is a legitimate FALLBACK - it just must never run ahead of the binding."""
    pbip = _project_with_two_models(tmp_path, None, stem_decoy=True)
    resolved = refresh_pbip_model.cache_file(111, instance={"pid": 111, "currentFilePath": str(pbip)})
    assert resolved == tmp_path / "Proj.SemanticModel" / ".pbi" / "cache.abf"


def _project_with_binding_outside_the_folder(root: Path, *, target_exists: bool) -> Path:
    """A `.pbip` whose report binds to a model OUTSIDE its own folder, plus ONE adjacent decoy model.

    Structure (returns the `.pbip`, stem `Proj`)::

        root/proj/Proj.pbip                      -> artifacts: Proj.Report
        root/proj/Proj.Report/definition.pbir    -> byPath: ../../elsewhere/Real.SemanticModel
        root/proj/Decoy.SemanticModel/...         (the single adjacent sibling - a DECOY)
        root/elsewhere/Real.SemanticModel/...     (the bound model, present only if target_exists)

    This is the round-3 mandate case: with exactly ONE `.SemanticModel` beside the `.pbip`, the
    round-2 `len(models)==1` shortcut returned that lone sibling BEFORE consulting the binding, so a
    binding pointing outside the folder was ignored and the decoy was written into - a wrong-project
    write reachable even with a single adjacent model.
    """
    proj = root / "proj"
    decoy = proj / "Decoy.SemanticModel" / "definition" / "tables"
    decoy.mkdir(parents=True)
    (decoy / "T.tmdl").write_text("table 'T'\n\n\tcolumn X\n", encoding="utf-8")
    if target_exists:
        real = root / "elsewhere" / "Real.SemanticModel" / "definition" / "tables"
        real.mkdir(parents=True)
        (real / "T.tmdl").write_text("table 'T'\n\n\tcolumn X\n", encoding="utf-8")
    report = proj / "Proj.Report"
    report.mkdir(parents=True)
    (report / "definition.pbir").write_text(
        json.dumps({"version": "1.0", "datasetReference": {"byPath": {"path": "../../elsewhere/Real.SemanticModel"}}}),
        encoding="utf-8",
    )
    pbip = proj / "Proj.pbip"
    pbip.write_text(
        json.dumps({"version": "1.0", "artifacts": [{"report": {"path": "Proj.Report"}}]}), encoding="utf-8"
    )
    return pbip


def test_cache_file_follows_a_binding_that_points_outside_the_folder(tmp_path: Path) -> None:
    """A binding to a model OUTSIDE the `.pbip` folder wins over the lone adjacent decoy (round-3 mandate)."""
    pbip = _project_with_binding_outside_the_folder(tmp_path, target_exists=True)
    resolved = refresh_pbip_model.cache_file(111, instance={"pid": 111, "currentFilePath": str(pbip)})
    assert resolved == tmp_path / "elsewhere" / "Real.SemanticModel" / ".pbi" / "cache.abf"
    assert resolved != tmp_path / "proj" / "Decoy.SemanticModel" / ".pbi" / "cache.abf"


def test_cache_file_fails_closed_when_the_binding_points_at_a_missing_model(tmp_path: Path) -> None:
    """A binding present but UNRESOLVABLE (its target is gone) fails closed - it must NOT fall to the decoy."""
    pbip = _project_with_binding_outside_the_folder(tmp_path, target_exists=False)
    assert refresh_pbip_model.cache_file(111, instance={"pid": 111, "currentFilePath": str(pbip)}) is None


def test_a_mismatched_port_aborts_before_any_read_or_write(monkeypatch, tmp_path: Path, capsys) -> None:
    """`--port` must EQUAL the pid-derived port, or the run aborts before touching anything (#114)."""
    _model_folder(tmp_path, "MyMigration", ["Orders"])
    _reach_the_instance(monkeypatch, tmp_path / "MyMigration.pbip")
    for never in ("_load_adomd", "same_model", "invoke_desktop_action", "row_counts"):
        monkeypatch.setattr(refresh_pbip_model, never, _explode(never))

    exit_code = refresh_pbip_model.main(["--pid", "111", "--port", "59999"])
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "59999" in out and "52001" in out, "the error must name both the given and the discovered port"
    assert "does not match" in out


def test_a_matching_port_is_accepted(monkeypatch, tmp_path: Path, capsys) -> None:
    """An explicit `--port` that EQUALS the pid-derived port is fine - the guard blocks only mismatch."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _reach_the_instance(monkeypatch, tmp_path / "MyMigration.pbip")
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "row_counts", lambda port, tables: ([("Orders", 5)], False))

    exit_code = refresh_pbip_model.main(["--pid", "111", "--canaries", "Orders", "--port", "52001", "--verify-only"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "REFRESH: DATA_OK" in out


@pytest.mark.parametrize(
    "arguments",
    [
        [],
    ],
)
def test_the_repo_root_credential_probe_is_a_retirement_shim(arguments) -> None:
    """Every legacy parameter set refuses with fixed output; none can reach the raw backend."""
    root_probe = next(
        (
            parent / "scripts" / "probe_desktop_credential.ps1"
            for parent in SKILL_ROOT.parents
            if (parent / "scripts" / "probe_desktop_credential.ps1").is_file()
            and (parent / ".github" / "skills" / "pbip-model-refresh").resolve() == SKILL_ROOT
        ),
        None,
    )
    if root_probe is None:
        pytest.skip("no host repo scripts/ folder next to this bundle")
    if os.name != "nt":
        pytest.skip("Desktop UIA qualification is Windows-only")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(root_probe), *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 3 and result.stdout.splitlines()[0] == "VERDICT: UNKNOWN"
    assert "no observation or Refresh performed" in result.stdout
    assert "111" not in result.stdout and "CREDENTIAL_PRESENT" not in result.stdout
    assert not result.stderr


def _explode(name: str):
    def boom(*_args, **_kwargs):
        raise AssertionError(f"{name}() must not run once the identity check has failed")

    return boom


def _reporting_instance(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Use real identity/reporting logic with a bound model and an independently timed old cache."""
    cache = _model_folder(root, "MyMigration", ["Orders"])
    cache.parent.mkdir()
    cache.write_bytes(b"old-cache")
    os.utime(cache, (1_700_000_000, 1_700_000_000))
    _reach_the_instance(monkeypatch, root / "MyMigration.pbip")
    _stub_live_to_match_disk(monkeypatch, cache)
    return cache


@pytest.mark.parametrize("advanced", [False, True], ids=["unchanged", "newer"])
def test_unrequested_persistence_never_attributes_external_cache_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], advanced: bool
) -> None:
    """An external cache update cannot become evidence of an invocation's unrequested save."""
    cache = _reporting_instance(monkeypatch, tmp_path)
    before_stamp = cache.stat().st_mtime
    monkeypatch.setattr(refresh_pbip_model, "invoke_desktop_action", _explode("invoke_desktop_action"))

    def row_counts(port: int, tables: list[str]) -> tuple[list[tuple[str, int]], bool]:
        assert port == 52001 and tables == ["Orders"]
        if advanced:
            cache.write_bytes(b"external-cache")
            os.utime(cache, (before_stamp + 60, before_stamp + 60))
        return [("Orders", 42)], False

    monkeypatch.setattr(refresh_pbip_model, "row_counts", row_counts)
    assert refresh_pbip_model.main(["--pid", "111", "--canaries", "Orders", "--verify-only"]) == 0
    out = capsys.readouterr().out
    assert (cache.stat().st_mtime > before_stamp) is advanced
    assert cache.read_bytes() == (b"external-cache" if advanced else b"old-cache")
    assert [line.strip() for line in out.splitlines() if line.strip().startswith("cache  :")] == [
        "cache  : persistence not requested (--verify-only; no refresh or save was run)"
    ]
    assert [line for line in out.splitlines() if line.startswith("REFRESH:")] == ["REFRESH: DATA_OK"]
    assert "PERSISTED" not in out and "byte-identical" not in out


@pytest.mark.parametrize(
    "retired",
    [
        ["--ui-save"],
        ["--tables", "Orders"],
        ["--calculate-only"],
        ["--measures-only"],
        ["--no-progress"],
        ["--progress-liveness-seconds", "5"],
    ],
    ids=lambda retired: retired[0],
)
def test_retired_flags_are_rejected_before_any_desktop_contact(monkeypatch, capsys, retired: list[str]) -> None:
    """T16: a retired flag is an argparse error (exit 2) before any bridge, port or ADOMD call."""
    for never in (
        "_ui_bridge_preflight",
        "_wait_ui_bridge",
        "discover_port",
        "_load_adomd",
        "invoke_desktop_action",
        "row_counts",
        "cache_file",
    ):
        monkeypatch.setattr(refresh_pbip_model, never, _explode(never))
    with pytest.raises(SystemExit) as stop:
        refresh_pbip_model.main(["--pid", "111", *retired])
    assert stop.value.code == 2
    assert f"unrecognized arguments: {' '.join(retired)}" in capsys.readouterr().err
