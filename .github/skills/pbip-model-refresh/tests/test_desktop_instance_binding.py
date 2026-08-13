"""Tests for binding a Desktop run to the RIGHT Power BI Desktop instance.

In a parallel batch the toolkit drives several Power BI Desktop instances at once, and two pieces of
state have to agree: the destination `<Name>.SemanticModel/.pbi/cache.abf` (resolved from the pid,
so always correct) and the Analysis Services port the data is read from. `discover_port` used to
silently widen to "any msmdsrv on the machine" whenever the pid-scoped lookup came back empty -
most likely because Desktop was up before its msmdsrv had bound a port - and then took the first
result. That writes a SIBLING migration's model into this migration's own correct `cache.abf`, and
every existing check still passes: `image_save` can only see that the file exists, is non-empty and
was newly written, and `row_counts` queries that same wrong port, so both signals agree and both are
wrong. `cache.abf` is gitignored, so nothing downstream catches it either.

So these tests lock in the two defences: never widen a named pid (and never pick between ambiguous
candidates), and prove the connected model really is the one that owns the cache before touching it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# `conftest.py` next to this file puts the skill's own `scripts/` on `sys.path`, resolved relatively
# so the suite runs wherever this folder was copied.
# ruff: noqa: E402  (the conftest-provided path must be in place before these imports)
import probe_desktop_query
import refresh_pbip_model
from probe_desktop_query import discover_port, table_names
from refresh_pbip_model import _instance, _resolve_pid, same_model, tmdl_tables

SKILL_ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL_PROBE_PS1 = SKILL_ROOT / "scripts" / "probe_desktop_credential.ps1"
REF_TABLE_RE = re.compile(r"^ref table\s+(?:'([^']+)'|(\S+))", re.MULTILINE)


def _example_models() -> list[Path]:
    """This repo's committed `examples/` corpus, if the host repo has one.

    Ground truth for the TMDL fingerprint has to come from real models, and this repo has 16 of them.
    They are a HOST-repo fixture, though, not part of the skill: a Qlik repo that copies this folder
    has no `examples/` tree, so the corpus test must skip with a reason there rather than fail (or,
    worse, silently parametrize to nothing). `tests/test_skills.py` in this repo asserts the corpus
    is non-empty HERE, so the skip cannot quietly turn into a no-op where it is meant to run.
    """
    for parent in Path(__file__).resolve().parents:
        models = sorted((parent / "examples").glob("*/fabric/*.SemanticModel"))
        if models:
            return models
    return []


EXAMPLE_MODELS = _example_models()


def test_the_suite_exercises_the_scripts_bundled_beside_it() -> None:
    """A copied skill must test ITS OWN scripts, not a same-named module from the host repo.

    This repo keeps forwarding shims at `scripts/probe_desktop_query.py` (so existing agent
    invocations do not break), which is exactly the shape that could shadow the real modules if
    anything put the host repo's `scripts/` on `sys.path` first. Then every test below would pass
    while proving nothing about the files that actually ship.
    """
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
    """The PowerShell boundary: the pid reaches it, and one port per process comes back.

    `Get-NetTCPConnection` can report the same listener twice (IPv4 + IPv6), which would otherwise
    look like "this pid owns two ports" and abort a perfectly good run.
    """
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return type("R", (), {"stdout": "WARNING: something\r\n52001\r\n52001\r\n"})()

    monkeypatch.setattr(probe_desktop_query.subprocess, "run", fake_run)
    assert probe_desktop_query._msmdsrv_ports(111) == [52001]
    assert seen["env"]["PID_FILTER"] == "111"
    assert probe_desktop_query._msmdsrv_ports(None) == [52001]
    assert seen["env"]["PID_FILTER"] == "", "an inherited PID_FILTER must not scope a machine-wide query"


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


def test_table_names_filters_the_auto_date_scaffolding_but_can_keep_hidden_tables() -> None:
    """A hidden table is part of a model's fingerprint; `LocalDateTable_*` never is (not in TMDL)."""
    rows = [("Sales", False), ("Bridge", True), ("LocalDateTable_abc", True), ("DateTableTemplate_x", True)]
    assert table_names(_FakeConnection(rows)) == ["Sales"]
    assert table_names(_FakeConnection(rows), include_hidden=True) == ["Sales", "Bridge"]


@pytest.mark.skipif(not EXAMPLE_MODELS, reason="no examples/*/fabric/*.SemanticModel corpus in this repo")
@pytest.mark.parametrize("model_dir", EXAMPLE_MODELS or [None], ids=lambda p: p.parts[-3] if p else "no-corpus")
def test_tmdl_tables_matches_every_example_models_own_ref_list(model_dir: Path) -> None:
    """Ground truth for the fingerprint: `model.tmdl` lists exactly the tables it `ref`s.

    Reading the declarations rather than the file stems is what makes quoted names ('CO2 Savings')
    and unquoted ones (Turbine) both resolve - a fingerprint that mis-reads names would either
    refuse every save or verify nothing.
    """
    declared = tmdl_tables(model_dir)
    text = (model_dir / "definition" / "model.tmdl").read_text(encoding="utf-8")
    referenced = {quoted or bare for quoted, bare in REF_TABLE_RE.findall(text)}
    assert declared == referenced


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
    """Desktop writes TMDL with a BOM, and a BOM before `table` would empty the whole fingerprint.

    That fails OPEN - an empty fingerprint reports "identity unverified", which the gate lets
    through - so it would silently switch the check off on exactly the real-world files it guards.
    `check_datamodel.py` already strips `\\ufeff` for the same file set, so this shape is not theoretical.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"], bom=True)
    assert tmdl_tables(cache.parent.parent) == {"Orders"}


def test_a_bom_prefixed_model_still_refuses_a_siblings_data(monkeypatch, tmp_path: Path) -> None:
    """The consequence that matters: BOM handling is what keeps the gate armed."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"], bom=True)
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Turbine"})
    ok, _ = same_model(52001, cache)
    assert not ok


def test_auto_date_tables_on_disk_do_not_look_like_a_stranger(monkeypatch, tmp_path: Path) -> None:
    """A PBIP with auto date/time on serializes `LocalDateTable_*.tmdl` into `definition/tables/`.

    The live side already strips them, so leaving them in the disk side would report them as
    "missing from the connected model" and abort every such migration with WRONG_MODEL.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders", "LocalDateTable_9f2c", "DateTableTemplate_1a"])
    assert tmdl_tables(cache.parent.parent) == {"Orders"}
    _stub_live_to_match_disk(monkeypatch, cache)
    ok, message = same_model(52001, cache)
    assert ok, message


def test_same_model_accepts_an_exact_case_insensitive_match(monkeypatch, tmp_path: Path) -> None:
    """The model that OWNS the cache matches the TMDL table-for-table (case-insensitively).

    An exactly-matching set - names differing only by case (`orders` vs `Orders`) - is the one thing
    that earns "confirmed". This is the legitimate flow the fail-closed gate must NOT block.
    """
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
    """A sibling holding ALL your tables PLUS more is a different model, not a lagging TMDL.

    This test used to assert the opposite (`test_same_model_accepts_the_model_that_owns_the_cache`
    expected ok=True and "more not in TMDL"). That fail-open subset match is the exact #114 hole: the
    check only required the TMDL tables to be PRESENT in the engine, so a richer sibling passed and
    was reported "confirmed". Concurrent Desktop instances are supported, so that sibling is
    reachable, not hypothetical - and once its model is confirmed, its rows get written into this
    project's cache.abf. An exact match now refuses it.
    """
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
    """'Cannot verify' must FAIL CLOSED, because this gate guards a write into a project.

    No model folder resolved, or no TMDL tables to fingerprint, is 'unknown' - and unknown is not
    'fine'. This test used to assert the opposite (ok=True + "unverified"): a hiccup that left the
    destination unresolved let the write straight through. That fail-open path is #114; both shapes
    now return False.
    """
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Whatever"})
    ok, message = same_model(52001, None)
    assert not ok and "UNVERIFIED" in message.upper()

    empty = tmp_path / "Empty.SemanticModel" / ".pbi" / "cache.abf"
    empty.parent.mkdir(parents=True)
    ok, message = same_model(52001, empty)
    assert not ok and "UNVERIFIED" in message.upper()


def test_same_model_refuses_when_a_column_differs(monkeypatch, tmp_path: Path) -> None:
    """Matching table NAMES but a different column is a different model - tables alone are not enough.

    Once `cache_file`'s binding is authoritative (round-2 blocker 3a), a same-named sibling is
    reachable on a widened/wrong port, so the table-only fingerprint stops being defence-in-depth and
    becomes the ONLY control - and a sibling sharing table names but differing in columns passes it.
    The fingerprint therefore reaches columns (#114, round-2 blocker 3): here the tables match exactly
    but the connected model has `Orders[Y]` where the TMDL has `Orders[X]`, and it must be refused.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])  # disk column: Orders[X]
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Orders"})
    monkeypatch.setattr(
        refresh_pbip_model, "_live_columns_measures", lambda port: ({("Orders", "Y")}, set()), raising=False
    )
    ok, message = same_model(52001, cache)
    assert not ok
    assert "column" in message


def test_same_model_refuses_when_a_measure_differs(monkeypatch, tmp_path: Path) -> None:
    """Matching tables and columns but a different measure is still a different model.

    The measure dimension is the second half of the finer fingerprint (#114, round-2 blocker 3): two
    models can agree on every table and column yet differ in their measures. Here the connected model
    carries a `Total Sales` measure the TMDL does not, so `same_model` must refuse.
    """
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
    (tables_dir / "LocalDateTable_x.tmdl").write_text(
        "table LocalDateTable_x\n\n\tcolumn Year\n\tmeasure M = 1\n", encoding="utf-8"
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
    """The live column fingerprint drops the auto RowNumber index and the auto date/time scaffolding.

    Those never appear in TMDL, so comparing them would make a legitimate model fail its own identity
    gate - the false-negative the reviewer flagged (#114, round-2 blocker 3). Everything else is kept,
    preferring the explicit model name over the engine's inferred one.
    """
    conn = _FingerprintConn(
        tables={"t1": "Orders", "t2": "LocalDateTable_abc"},
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
        tables={"t1": "Sales", "t2": "DateTableTemplate_1"},
        columns=[],
        measures=[("t1", "Total Sales"), ("t2", "Some Auto Measure")],
    )
    assert probe_desktop_query.measure_names(conn) == {("Sales", "Total Sales")}


def _stub_bridge(monkeypatch, instances: list[dict]) -> None:
    monkeypatch.setattr(refresh_pbip_model, "_bridge_status", lambda: {"instances": instances})


def test_resolve_pid_refuses_to_choose_between_instances(monkeypatch) -> None:
    """`_instance(None)` returning the first instance is fine interactively, ambiguous in a batch."""
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": "a.pbip"}, {"pid": 222, "currentFilePath": "b.pbip"}])
    assert _resolve_pid(None) is None
    assert _instance(None) is None
    assert _resolve_pid(222) == 222  # an explicitly named instance is always honoured
    assert _instance(222) == {"pid": 222, "currentFilePath": "b.pbip"}
    assert _instance(333) is None


def test_resolve_pid_keeps_the_single_instance_shortcut(monkeypatch) -> None:
    """One Desktop open is unambiguous, so the interactive call must not need --pid."""
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": "a.pbip"}])
    assert _resolve_pid(None) == 111
    _stub_bridge(monkeypatch, [])
    assert _resolve_pid(None) is None


def test_main_aborts_before_refreshing_a_wrong_bound_instance(monkeypatch, tmp_path: Path, capsys) -> None:
    """End to end: a wrong port must never reach refresh, ImageSave or the row count.

    Those three are exactly the checks that agree with each other while all being wrong, so the
    identity gate has to run first - not as one more opinion afterwards.
    """
    _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Turbine"})
    for never in ("refresh", "image_save", "save", "row_counts"):
        monkeypatch.setattr(refresh_pbip_model, never, _explode(never))

    assert refresh_pbip_model.main(["--pid", "111"]) == 2
    assert "REFRESH: WRONG_MODEL" in capsys.readouterr().out


def test_main_still_refreshes_and_persists_the_right_instance(monkeypatch, tmp_path: Path, capsys) -> None:
    """The gate must not cry wolf: the normal single-migration flow has to stay green.

    Passes explicit `--tables Orders` (both the refresh scope and the canary set, so an all-non-zero
    result earns the model-level DATA_OK - #115) and a legacy `--save` (now an accepted no-op, since
    persisting is the default - #113), proving both still work and still persist.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_counts", lambda port, tables: ([("Orders", 42)], False))

    def fake_image_save(port: int, cache_path: Path, model_dir=None):
        # Signature mirrors the real image_save EXACTLY. A stub that lags it raises TypeError, which
        # main() swallows into the UI-save fallback - so a drifted stub would go green against a
        # broken call.
        del port, model_dir
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"cache")
        return True, "persisted"

    monkeypatch.setattr(refresh_pbip_model, "image_save", fake_image_save)

    assert refresh_pbip_model.main(["--pid", "111", "--tables", "Orders", "--save"]) == 0
    out = capsys.readouterr().out
    assert "REFRESH: DATA_OK + PERSISTED" in out
    assert cache.read_bytes() == b"cache"


def test_no_save_leaves_the_project_byte_identical(monkeypatch, tmp_path: Path, capsys) -> None:
    """With `--no-save`, a refresh must NOT write `cache.abf` or touch the project.

    Persisting is the DEFAULT (it is this script's stated purpose), so the opt-OUT is what needs
    pinning. `--no-save` exists for read-only consumers: the validator is read-only by contract, and
    saving rewrites `database.tmdl`'s declared compatibilityLevel - so a validator that forgot this
    flag would mutate the very artifact it is judging.

    This test used to assert the opposite (`test_not_saving_is_the_default`). That default came from
    a real but MIS-ATTRIBUTED finding: a persisted cache was believed to make the PBIP unopenable,
    when the actual cause was a compatibility-level mismatch between the cache and the project
    (root-caused 2026-08-07). `image_save` now aligns them the way Desktop's own Save does.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_counts", lambda port, tables: ([("Orders", 42)], False))
    monkeypatch.setattr(refresh_pbip_model, "image_save", _explode("image_save must not run"))
    monkeypatch.setattr(refresh_pbip_model, "save", _explode("save must not run"))

    assert refresh_pbip_model.main(["--pid", "111", "--tables", "Orders", "--no-save"]) == 0
    out = capsys.readouterr().out
    assert "REFRESH: DATA_OK" in out
    assert "PERSISTED" not in out
    assert not cache.exists(), "--no-save must not write cache.abf"


def test_persisting_is_the_default(monkeypatch, tmp_path: Path, capsys) -> None:
    """With no flags at all, a refresh MUST persist.

    This is the script's stated purpose - "so the next agent (and the next Desktop open) sees real
    data instead of an empty model" - and it was off for a while only because of a misdiagnosed
    defect. Pinned because the failure it prevents is silent and expensive: an agent handed an
    unrefreshed model queries it, gets nothing, and reports findings about an empty model.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_counts", lambda port, tables: ([("Orders", 42)], False))

    def fake_image_save(port: int, cache_path: Path, model_dir=None):
        del port, model_dir
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"cache")
        return True, "persisted"

    monkeypatch.setattr(refresh_pbip_model, "image_save", fake_image_save)
    monkeypatch.setattr(refresh_pbip_model, "save", _explode("the UI fallback must not run"))

    assert refresh_pbip_model.main(["--pid", "111", "--tables", "Orders"]) == 0
    out = capsys.readouterr().out
    assert "REFRESH: DATA_OK + PERSISTED" in out
    assert cache.exists(), "a default refresh must persist cache.abf"


def test_compatibility_alignment_declares_generated_edit(tmp_path: Path) -> None:
    """A normal persisted refresh may rewrite database.tmdl; tamper needs structured evidence."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    database_tmdl.write_text("compatibilityLevel: 1604\n", encoding="utf-8")
    before_hash = refresh_pbip_model.sha256_file(database_tmdl)
    (tmp_path / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 1,
                    "run_id": "engine-run",
                    "recorded_at": "2026-08-10T08:00:00+00:00",
                    "report_sha256": "report-hash",
                    "files": {"MyMigration.SemanticModel/definition/database.tmdl": before_hash},
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeDatabase:
        CompatibilityLevel = 1702

    note = refresh_pbip_model._align_compatibility(model_dir, int(FakeDatabase.CompatibilityLevel))

    declarations = json.loads((tmp_path / "_build" / "generated-edit-declarations.json").read_text(encoding="utf-8"))
    declaration = declarations["declarations"][0]
    assert "1604 -> 1702" in note
    assert declaration["run_id"] == "engine-run"
    assert declaration["target"] == "MyMigration.SemanticModel/definition/database.tmdl"
    assert declaration["baseline_sha256"] == before_hash
    assert declaration["expected_sha256"] == refresh_pbip_model.sha256_file(database_tmdl)
    assert declaration["script_identity"] == "pbip-model-refresh/refresh_pbip_model.py"


def test_a_timeout_does_not_assert_a_credential_modal(monkeypatch, tmp_path: Path, capsys) -> None:
    """A refresh timeout must report the cause as UNKNOWN, never as "this needs a human".

    Regression test for the most expensive defect found on 2026-08-04. The old message named a
    data-source sign-in modal and said "THIS NEEDS A HUMAN. Do not retry" on ANY timeout - the one
    blocker an agent is forbidden to retry - so a false positive converted a transient slowdown into
    a permanent dead end. It fired on 2 of 5 Desktop instances opened on the SAME bundle that
    refreshed cleanly on the other 3 (38.8s good run, >87s slow run, 90s ceiling).
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "refresh", _explode("The XML for Analysis request timed out"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out
    assert exit_code == 3
    assert "REFRESH: TIMEOUT" in out
    assert "CAUSE UNKNOWN" in out
    assert "THIS NEEDS A HUMAN" not in out, "a timeout heuristic must not emit a stop-word instruction"
    assert "probe_desktop_credential" in out, "it must name the arbiter that settles the cause"
    # #118: the arbiter it names must actually ship in the bundle, and the message must print its
    # bundled absolute path - a runtime instruction to run a file that is not here breaks the
    # bundle's self-containment claim.
    arbiter = refresh_pbip_model.CREDENTIAL_PROBE
    assert str(arbiter) in out, "the timeout must print the arbiter's absolute, bundled path"
    assert arbiter.is_file(), "the named arbiter must actually ship in the bundle"
    assert arbiter.parent == SKILL_ROOT / "scripts", "the arbiter must live inside this skill's scripts/"


def test_an_unresolved_destination_fails_closed_not_into_a_blind_write(monkeypatch, tmp_path: Path, capsys) -> None:
    """An unresolved destination must fail CLOSED at the gate, before any write - and be resolved once.

    `cache_file` is a Desktop Bridge round trip and returns None when the pid maps to no single model
    (an ambiguous `.SemanticModel` folder, or a bridge hiccup). This test used to assert that the run
    then proceeded to a `NOT_PERSISTED` exit 1 - i.e. the fail-OPEN gate let a None destination
    through as "unverified -> fine". Under #114 the gate fails closed: None aborts with WRONG_MODEL
    (exit 2) and refresh/save/image_save never run. `cache_file` is also resolved EXACTLY once -
    re-deriving it after the gate would verify one path and write another.
    """
    calls: list[int] = []

    def fake_cache_file(pid: int) -> None:
        calls.append(pid)
        return None

    written: list[Path] = []

    def _record(port: int, cache_path: Path, model_dir=None) -> tuple[bool, str]:
        written.append(cache_path)
        return True, "saved via ImageSave"

    monkeypatch.setattr(refresh_pbip_model, "cache_file", fake_cache_file)
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Turbine"})
    monkeypatch.setattr(refresh_pbip_model, "refresh", _explode("refresh must not run on an unverified destination"))
    monkeypatch.setattr(refresh_pbip_model, "save", _explode("save must not run"))
    monkeypatch.setattr(refresh_pbip_model, "image_save", _record)

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    assert exit_code == 2
    assert "REFRESH: WRONG_MODEL" in capsys.readouterr().out
    assert written == []
    assert calls == [111], "cache_file must be resolved exactly once, not re-derived after the gate"


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


def test_cache_file_resolves_the_bound_model_when_several_exist(monkeypatch, tmp_path: Path) -> None:
    """With several `.SemanticModel` folders, the destination comes from the `.pbip` BINDING - first.

    The bound model here is `Zed` while a same-named `Proj` sibling matches the `.pbip` stem. Round-1
    consulted the name heuristic BEFORE the binding, so that `Proj` decoy short-circuited it and the
    run bound to the wrong sibling (#114, round-2 blocker 3a). The binding names the real owner
    (`Zed`), and `cache_file` must follow it even when a stem-matching decoy exists. `Zed` also sorts
    last, so this cannot pass by the old `models[0]` accident either.
    """
    pbip = _project_with_two_models(tmp_path, "../Zed.SemanticModel", stem_decoy=True)
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(pbip)}])
    resolved = refresh_pbip_model.cache_file(111)
    assert resolved == tmp_path / "Zed.SemanticModel" / ".pbi" / "cache.abf"


def test_cache_file_refuses_to_guess_when_the_binding_is_ambiguous(monkeypatch, tmp_path: Path) -> None:
    """No name match and no resolvable binding => None, so the identity gate fails closed.

    Neither `Alpha` nor `Zed` matches the `.pbip` stem `Proj`, and there is no binding, so nothing
    names the owner. The old `models[0]` fallback would have grabbed `Alpha` (first sorted) with no
    evidence; returning None is what makes the downstream gate refuse.
    """
    pbip = _project_with_two_models(tmp_path, None, stem_decoy=False)
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(pbip)}])
    assert refresh_pbip_model.cache_file(111) is None


def test_cache_file_uses_the_stem_only_when_no_binding_resolves(monkeypatch, tmp_path: Path) -> None:
    """The stem heuristic is a legitimate FALLBACK - it just must never run ahead of the binding.

    With no binding but a single stem-matching sibling (`Proj`), naming it is the best available
    evidence, so `cache_file` may return it. This pins that the round-2 reordering did not delete the
    fallback, only demoted it below the authoritative binding.
    """
    pbip = _project_with_two_models(tmp_path, None, stem_decoy=True)
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(pbip)}])
    resolved = refresh_pbip_model.cache_file(111)
    assert resolved == tmp_path / "Proj.SemanticModel" / ".pbi" / "cache.abf"


def test_a_mismatched_port_aborts_before_any_read_or_write(monkeypatch, tmp_path: Path, capsys) -> None:
    """`--port` must EQUAL the pid-derived port, or the run aborts before touching anything.

    A `--port` pointing at another instance is the wrong-instance write in one argument: the
    destination is resolved from the pid, so a stray port reads (and, via ImageSave, persists) a
    sibling's model into this project's own correct cache.abf. #114 closes it by refusing any
    mismatch up front - the identity probe, refresh, row count and save must never run.
    """
    _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    for never in ("same_model", "refresh", "image_save", "save", "row_counts"):
        monkeypatch.setattr(refresh_pbip_model, never, _explode(never))

    exit_code = refresh_pbip_model.main(["--pid", "111", "--port", "59999"])
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "59999" in out and "52001" in out, "the error must name both the given and the discovered port"
    assert "does not match" in out


def test_a_matching_port_is_accepted(monkeypatch, tmp_path: Path, capsys) -> None:
    """An explicit `--port` that EQUALS the pid-derived port is fine - the guard blocks only mismatch."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_counts", lambda port, tables: ([("Orders", 5)], False))

    def fake_image_save(port: int, cache_path: Path, model_dir=None):
        del port, model_dir
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"cache")
        return True, "persisted"

    monkeypatch.setattr(refresh_pbip_model, "image_save", fake_image_save)

    exit_code = refresh_pbip_model.main(["--pid", "111", "--tables", "Orders", "--port", "52001"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "REFRESH: DATA_OK + PERSISTED" in out


def test_an_implicit_probe_downgrades_to_table_ok_but_still_persists(monkeypatch, tmp_path: Path, capsys) -> None:
    """No `--tables`: only the first queryable table is probed, so the verdict names that one table.

    A model-level `DATA_OK` from an implicit single-table probe is the #115 defect: a static
    parameter/CSV table (here 'Parameters') can return rows while every live source failed to load.
    The verdict must be `TABLE_OK 'Parameters'`, never `DATA_OK`. Persistence is a separate axis and
    is unaffected.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_counts", lambda port, tables: ([("Parameters", 1)], True))

    def fake_image_save(port: int, cache_path: Path, model_dir=None):
        del port, model_dir
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"cache")
        return True, "persisted"

    monkeypatch.setattr(refresh_pbip_model, "image_save", fake_image_save)

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "REFRESH: TABLE_OK 'Parameters'" in out
    assert "REFRESH: DATA_OK" not in out, "an implicit single-table probe must never claim a model-level DATA_OK"
    assert cache.exists(), "the downgrade is about the VERDICT, not about suppressing persistence"


def test_an_empty_canary_reports_no_data_naming_the_table(monkeypatch, tmp_path: Path, capsys) -> None:
    """A named canary returning 0 rows is a source that never loaded - NO_DATA, naming the table.

    With explicit canaries, EVERY named source must return rows; one empty table (a live source whose
    credential failed) is the exact failure #115 wants surfaced, not averaged away by the others.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_counts", lambda port, tables: ([("Orders", 42), ("Live", 0)], False))

    def fake_image_save(port: int, cache_path: Path, model_dir=None):
        del port, model_dir
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"cache")
        return True, "persisted"

    monkeypatch.setattr(refresh_pbip_model, "image_save", fake_image_save)

    exit_code = refresh_pbip_model.main(["--pid", "111", "--tables", "Orders", "Live"])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "REFRESH: NO_DATA" in out
    assert "Live" in out, "the empty canary must be named"


def _explode(name: str):
    def boom(*_args, **_kwargs):
        raise AssertionError(f"{name}() must not run once the identity check has failed")

    return boom


def test_timeout_recovery_command_quotes_a_path_with_spaces(monkeypatch, tmp_path: Path, capsys) -> None:
    """The printed recovery command must QUOTE the arbiter path, or a space breaks `powershell -File`.

    Verified experimentally in the round-2 review: `powershell -File C:\\Program Files\\...` parses as
    `-File 'C:\\Program'`. The arbiter is bundled and can sit under a spaced directory (a user profile
    with a space is the common case), so the command must read `-File "<path>"` (#118, round-2 major 4).
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    spaced = tmp_path / "Program Files" / "probe_desktop_credential.ps1"
    spaced.parent.mkdir(parents=True)
    spaced.write_text("# stub arbiter\n", encoding="utf-8")
    monkeypatch.setattr(refresh_pbip_model, "CREDENTIAL_PROBE", spaced)
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    _stub_live_to_match_disk(monkeypatch, cache)
    monkeypatch.setattr(refresh_pbip_model, "refresh", _explode("The XML for Analysis request timed out"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out
    assert exit_code == 3
    assert f'-File "{spaced}"' in out, "the arbiter path must be quoted for powershell -File"


def test_credential_arbiter_enumerates_all_windows_and_fails_closed() -> None:
    """The credential arbiter (#118) must not fail OPEN, and must inspect EVERY window for the pid.

    It became "the arbiter" the timeout message points at, so a fail-open answer is worse than none
    (round-2 major 5). The script needs live UIAutomation + Desktop to execute, so these two fixes are
    pinned structurally: it must enumerate all top-level windows (`Get-PidWindows`/`FindAll`, not the
    old single-window `FindFirst`), and when no Refresh control was ever invoked it must report
    UNKNOWN rather than asserting a credential is present.
    """
    text = CREDENTIAL_PROBE_PS1.read_text(encoding="utf-8")
    assert "Get-PidWindows" in text, "must enumerate windows via the all-windows helper"
    assert "FindAll" in text, "must use FindAll (every top-level window)"
    assert "FindFirst" not in text, "the single-window FindFirst scan was the fail-open enumeration bug"
    assert re.search(r"-not\s+\$invoked", text), "must guard the case where no Refresh was invoked"
    assert re.search(r"VERDICT:\s*UNKNOWN", text), "the not-invoked case must report UNKNOWN, not CREDENTIAL_PRESENT"
