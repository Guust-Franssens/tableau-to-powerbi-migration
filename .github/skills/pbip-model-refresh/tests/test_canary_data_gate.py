"""The data gate must never certify a whole model from one arbitrary table (#115).

`DATA_OK` says "every live source loaded". Emitting it from an IMPLICIT first-table probe is a false
positive: a static parameter/CSV table can return rows while every live source failed to load, and
"first non-hidden table" is often exactly such a table. So:

- with explicit canaries (one per distinct live source) an all-non-zero result earns `DATA_OK`;
- with none, only the first queryable table is probed and the verdict is downgraded to
  `TABLE_OK '<table>'`, naming the single table actually read;
- any empty table is `NO_DATA`, naming it.

These tests drive both surfaces that emit the verdict: `probe_desktop_query.probe` (the read-only
preflight) and `refresh_pbip_model.row_counts` (the refresh's own data check).
"""

from __future__ import annotations

import re

# `conftest.py` next to this file puts the skill's own `scripts/` on `sys.path`.
# ruff: noqa: E402  (the conftest-provided path must be in place before these imports)
import probe_desktop_query
import refresh_pbip_model


class _Reader:
    """A minimal ADOMD data reader (PascalCase because the real one is .NET)."""

    def __init__(self, columns: list[str], rows: list[list]) -> None:
        self._columns = columns
        self._rows = list(rows)
        self._cur: list | None = None

    @property
    def FieldCount(self) -> int:  # noqa: N802
        return len(self._columns)

    def GetName(self, index: int) -> str:  # noqa: N802
        return self._columns[index]

    def Read(self) -> bool:  # noqa: N802
        if not self._rows:
            return False
        self._cur = self._rows.pop(0)
        return True

    def GetValue(self, index: int):  # noqa: N802
        return self._cur[index]

    def Close(self) -> None:  # noqa: N802
        self._cur = None


class _Command:  # pylint: disable=too-few-public-methods
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self.CommandText = ""  # noqa: N815

    def ExecuteReader(self) -> _Reader:  # noqa: N802
        return self._conn.reader_for(self.CommandText)


class _Conn:
    """A fake connection that answers the three DAX/DMV shapes these functions send."""

    def __init__(self, tables: list[tuple[str, bool]], counts: dict[str, int]) -> None:
        self._tables = tables
        self._counts = counts

    def Open(self) -> None:  # noqa: N802
        pass

    def Close(self) -> None:  # noqa: N802
        pass

    def CreateCommand(self) -> _Command:  # noqa: N802
        return _Command(self)

    def reader_for(self, text: str) -> _Reader:
        if "TMSCHEMA_TABLES" in text:
            return _Reader(["Name", "IsHidden"], [[name, hidden] for name, hidden in self._tables])
        topn = re.search(r"TOPN\(1, '([^']+)'\)", text)
        if topn:
            has_rows = self._counts.get(topn.group(1), 0) > 0
            return _Reader(["Sales Amount"], [["value"]] if has_rows else [])
        countrows = re.search(r"COUNTROWS\('([^']+)'\)", text)
        if countrows:
            return _Reader(["n"], [[self._counts.get(countrows.group(1), 0)]])
        raise AssertionError(f"unexpected DAX/DMV: {text}")


def _wire(monkeypatch, module, conn: _Conn) -> None:
    monkeypatch.setattr(module, "_load_adomd", lambda: lambda _dsn: conn)


# --- probe_desktop_query.probe: the read-only preflight verdict ---------------------------------


def test_probe_with_no_canary_is_table_ok_not_data_ok(monkeypatch, capsys) -> None:
    """The #115 defect: an implicit probe of the first table 'Parameters' must NOT claim DATA_OK."""
    conn = _Conn([("Parameters", False), ("Orders", False)], {"Parameters": 1, "Orders": 5})
    _wire(monkeypatch, probe_desktop_query, conn)

    exit_code = probe_desktop_query.probe(52001, None)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "PREFLIGHT: TABLE_OK 'Parameters'" in out
    assert "PREFLIGHT: DATA_OK" not in out, "an implicit single-table probe must not certify the model"


def test_probe_with_explicit_canaries_is_data_ok(monkeypatch, capsys) -> None:
    """One canary per live source, all non-zero, is the case that legitimately earns DATA_OK."""
    conn = _Conn([("Orders", False)], {"Orders": 5, "Customers": 3})
    _wire(monkeypatch, probe_desktop_query, conn)

    exit_code = probe_desktop_query.probe(52001, ["Orders", "Customers"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "PREFLIGHT: DATA_OK" in out


def test_probe_reports_no_data_when_a_canary_is_empty(monkeypatch, capsys) -> None:
    """A named live source returning 0 rows is NO_DATA, and must be named - not hidden by a sibling."""
    conn = _Conn([("Orders", False)], {"Orders": 5, "Live": 0})
    _wire(monkeypatch, probe_desktop_query, conn)

    exit_code = probe_desktop_query.probe(52001, ["Orders", "Live"])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "PREFLIGHT: NO_DATA" in out
    assert "Live" in out


def test_probe_single_explicit_table_earns_data_ok(monkeypatch, capsys) -> None:
    """An EXPLICIT single canary is the caller's own choice, so it still earns a model verdict.

    Only the IMPLICIT arbitrary first-table pick is downgraded; naming a table is opting in to
    certifying on it, which is exactly what #115 asks callers to do.
    """
    conn = _Conn([("Orders", False)], {"Orders": 9})
    _wire(monkeypatch, probe_desktop_query, conn)

    exit_code = probe_desktop_query.probe(52001, ["Orders"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "PREFLIGHT: DATA_OK" in out
    assert "TABLE_OK" not in out


def test_main_maps_the_tables_flag_to_the_canary_list(monkeypatch) -> None:
    """`--tables A B` reaches `probe` as ["A", "B"]; the port is the discovered one."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", lambda port, tables: seen.update(port=port, tables=tables) or 0)
    assert probe_desktop_query.main(["--pid", "111", "--tables", "A", "B"]) == 0
    assert seen == {"port": 52001, "tables": ["A", "B"]}


def test_main_maps_the_legacy_single_table_flag(monkeypatch) -> None:
    """`--table X` (legacy) becomes a one-name canary list, so old callers keep a model verdict."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", lambda port, tables: seen.update(tables=tables) or 0)
    assert probe_desktop_query.main(["--pid", "111", "--table", "Orders"]) == 0
    assert seen["tables"] == ["Orders"]


def test_main_maps_no_table_to_none_so_the_probe_downgrades(monkeypatch) -> None:
    """With neither flag, `probe` receives None and is responsible for the TABLE_OK downgrade."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", lambda port, tables: seen.update(tables=tables) or 0)
    assert probe_desktop_query.main(["--pid", "111"]) == 0
    assert seen["tables"] is None


# --- refresh_pbip_model.row_counts: the refresh's own data check --------------------------------


def test_row_counts_probes_each_named_canary(monkeypatch) -> None:
    """Explicit canaries: one COUNTROWS per table, in order, and `implicit` is False."""
    conn = _Conn([("Orders", False)], {"Orders": 5, "Customers": 3})
    _wire(monkeypatch, refresh_pbip_model, conn)

    results, implicit = refresh_pbip_model.row_counts(52001, ["Orders", "Customers"])
    assert results == [("Orders", 5), ("Customers", 3)]
    assert implicit is False


def test_row_counts_falls_back_to_first_table_and_flags_implicit(monkeypatch) -> None:
    """No canaries: probe the first queryable table only, and flag it implicit so the caller
    downgrades the verdict to TABLE_OK rather than DATA_OK."""
    conn = _Conn([("Parameters", False), ("Orders", False)], {"Parameters": 1})
    _wire(monkeypatch, refresh_pbip_model, conn)

    results, implicit = refresh_pbip_model.row_counts(52001, None)
    assert results == [("Parameters", 1)]
    assert implicit is True
