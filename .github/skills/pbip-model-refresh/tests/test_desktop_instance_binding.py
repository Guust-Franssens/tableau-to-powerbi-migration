"""Tests for binding a Desktop run to the RIGHT Power BI Desktop instance.

In a parallel batch the toolkit drives several Power BI Desktop instances at once, and two pieces of
state have to agree: the destination `<Name>.SemanticModel/.pbi/cache.abf` (resolved from the pid,
so always correct) and the Analysis Services port the data is read from. `discover_port` used to
silently widen to "any msmdsrv on the machine" whenever the pid-scoped lookup came back empty -
most likely because Desktop was up before its msmdsrv had bound a port - and then took the first
result. That writes a SIBLING migration's model into this migration's own correct `cache.abf`, and
every existing check still passes: `image_save` can only see that the file exists, is non-empty and
was newly written, and `row_count` queries that same wrong port, so both signals agree and both are
wrong. `cache.abf` is gitignored, so nothing downstream catches it either.

So these tests lock in the two defences: never widen a named pid (and never pick between ambiguous
candidates), and prove the connected model really is the one that owns the cache before touching it.
"""

from __future__ import annotations

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


def test_tmdl_tables_reads_through_a_byte_order_mark(tmp_path: Path) -> None:
    """Desktop writes TMDL with a BOM, and a BOM before `table` would empty the whole fingerprint.

    That fails OPEN - an empty fingerprint reports "identity unverified", which the gate lets
    through - so it would silently switch the check off on exactly the real-world files it guards.
    `check_m_syntax.py` already strips `\\ufeff` for the same file set, so this shape is not theoretical.
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
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Orders"})
    ok, message = same_model(52001, cache)
    assert ok, message


def test_same_model_accepts_the_model_that_owns_the_cache(monkeypatch, tmp_path: Path) -> None:
    """Extra tables in the engine are normal (auto date tables, in-memory edits); missing ones are not."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders", "Date Table"])
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"orders", "Date Table", "Extra"})
    ok, message = same_model(52001, cache)
    assert ok, message
    assert "more not in TMDL" in message, "TMDL that lags the engine is worth reporting, not refusing"


def test_same_model_refuses_a_siblings_model(monkeypatch, tmp_path: Path) -> None:
    """The only check that can catch a wrong-instance bind - file metadata never can."""
    cache = _model_folder(tmp_path, "MyMigration", ["Orders", "Date Table"])
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Turbine", "CO2 Savings"})
    ok, message = same_model(52001, cache)
    assert not ok
    assert "Orders" in message and "MyMigration.SemanticModel" in message


def test_same_model_says_so_when_it_cannot_verify(monkeypatch, tmp_path: Path) -> None:
    """No model folder / no TMDL is 'unknown', not 'wrong' - it must not block a legitimate save."""
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Whatever"})
    ok, message = same_model(52001, None)
    assert ok and "unverified" in message

    empty = tmp_path / "Empty.SemanticModel" / ".pbi" / "cache.abf"
    empty.parent.mkdir(parents=True)
    ok, message = same_model(52001, empty)
    assert ok and "unverified" in message


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
    for never in ("refresh", "image_save", "save", "row_count"):
        monkeypatch.setattr(refresh_pbip_model, never, _explode(never))

    assert refresh_pbip_model.main(["--pid", "111"]) == 2
    assert "REFRESH: WRONG_MODEL" in capsys.readouterr().out


def test_main_still_refreshes_and_persists_the_right_instance(monkeypatch, tmp_path: Path, capsys) -> None:
    """The gate must not cry wolf: the normal single-migration flow has to stay green.

    Passes `--save` explicitly: persisting became opt-in on 2026-08-04 (a present `cache.abf` makes
    the PBIP unopenable), so this exercises the save path deliberately rather than by default.
    """
    cache = _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Orders"})
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_count", lambda port: (42, "Orders"))

    def fake_image_save(port: int, cache_path: Path, model_dir=None, align_compat: bool = False):
        # Signature mirrors the real image_save, including the compatibility-level guard arguments
        # added 2026-08-07. A stub that lags the real signature raises TypeError, which main()
        # swallows into the UI-save fallback - so this test would go green against a broken call.
        del port, model_dir, align_compat
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"cache")
        return True, "persisted"

    monkeypatch.setattr(refresh_pbip_model, "image_save", fake_image_save)

    assert refresh_pbip_model.main(["--pid", "111", "--save"]) == 0
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
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Orders"})
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_count", lambda port: (42, "Orders"))
    monkeypatch.setattr(refresh_pbip_model, "image_save", _explode("image_save must not run"))
    monkeypatch.setattr(refresh_pbip_model, "save", _explode("save must not run"))

    assert refresh_pbip_model.main(["--pid", "111", "--no-save"]) == 0
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
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Orders"})
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_count", lambda port: (42, "Orders"))

    def fake_image_save(port: int, cache_path: Path, model_dir=None):
        del port, model_dir
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"cache")
        return True, "persisted"

    monkeypatch.setattr(refresh_pbip_model, "image_save", fake_image_save)
    monkeypatch.setattr(refresh_pbip_model, "save", _explode("the UI fallback must not run"))

    assert refresh_pbip_model.main(["--pid", "111"]) == 0
    out = capsys.readouterr().out
    assert "REFRESH: DATA_OK + PERSISTED" in out
    assert cache.exists(), "a default refresh must persist cache.abf"


def test_a_timeout_does_not_assert_a_credential_modal(monkeypatch, tmp_path: Path, capsys) -> None:
    """A refresh timeout must report the cause as UNKNOWN, never as "this needs a human".

    Regression test for the most expensive defect found on 2026-08-04. The old message named a
    data-source sign-in modal and said "THIS NEEDS A HUMAN. Do not retry" on ANY timeout - the one
    blocker an agent is forbidden to retry - so a false positive converted a transient slowdown into
    a permanent dead end. It fired on 2 of 5 Desktop instances opened on the SAME bundle that
    refreshed cleanly on the other 3 (38.8s good run, >87s slow run, 90s ceiling).
    """
    _model_folder(tmp_path, "MyMigration", ["Orders"])
    _stub_bridge(monkeypatch, [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}])
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Orders"})
    monkeypatch.setattr(refresh_pbip_model, "refresh", _explode("The XML for Analysis request timed out"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out
    assert exit_code == 3
    assert "REFRESH: TIMEOUT" in out
    assert "CAUSE UNKNOWN" in out
    assert "THIS NEEDS A HUMAN" not in out, "a timeout heuristic must not emit a stop-word instruction"
    assert "probe_desktop_credential" in out, "it must name the arbiter that settles the cause"


def test_persisting_uses_the_exact_cache_path_the_gate_verified(monkeypatch, tmp_path: Path, capsys) -> None:
    """Re-deriving the destination after the gate means writing somewhere never verified.

    `cache_file` is a Desktop Bridge round trip, and `_bridge_status` returns `{}` on any non-JSON
    output - so a hiccup at gate time (path `None`, "identity unverified", gate passes as a no-op)
    followed by a successful call afterwards would have persisted an unchecked model. The write must
    never reach a path the gate did not see.
    """
    written: list[Path] = []

    def _record(port: int, path: Path) -> tuple[bool, str]:
        written.append(path)
        return True, "saved via ImageSave"

    resolved = [None, _model_folder(tmp_path, "MyMigration", ["Orders"])]
    monkeypatch.setattr(refresh_pbip_model, "cache_file", lambda pid: resolved.pop(0) if resolved else None)
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda pid: 52001)
    monkeypatch.setattr(refresh_pbip_model, "_live_tables", lambda port: {"Turbine"})
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda port, tables, timeout: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "row_count", lambda port: (7, "Orders"))
    monkeypatch.setattr(refresh_pbip_model, "save", lambda pid: (True, "saved via UI"))
    monkeypatch.setattr(refresh_pbip_model, "image_save", _record)

    exit_code = refresh_pbip_model.main(["--pid", "111", "--save"])
    assert written == []
    assert exit_code == 1
    assert "NOT_PERSISTED" in capsys.readouterr().out


def _explode(name: str):
    def boom(*_args, **_kwargs):
        raise AssertionError(f"{name}() must not run once the identity check has failed")

    return boom
