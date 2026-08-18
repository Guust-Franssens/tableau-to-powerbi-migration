"""Regression tests for TMDL identifier quoting in the probe scaffold `scripts/probe_live_source.py`.

The probe hand-writes a throwaway one-table TMDL model and hands it to Power BI Desktop. It used to
interpolate the table/column name RAW into every TMDL header, so a name with a space produced
`ref table Custom SQL Query` - which the TOM deserializer Power BI Desktop uses rejects with
`InvalidObjectHeader` ("the object name is followed by an invalid token"). That is not an edge case:
the deterministic conversion engine names unresolved custom-SQL relations `Custom SQL Query` (two
spaces), a shape a live customer estate carried in ~72% of its assets, so the probe crashed Desktop
far more often than it worked.

The fix quotes every TMDL identifier unconditionally (a quoted identifier is always valid TMDL) and
escapes an embedded single quote by doubling it - exactly what Power BI's own serializer emits. Ground
truth these tests mirror: `examples/wind-energy-utilization/.../CO2 Savings.tmdl`
(`table 'CO2 Savings'`, `column 'Turbine Id'`, `partition 'CO2 Savings'`), its `model.tmdl`
(`ref table 'CO2 Savings'`), and `examples/broadway-stage-to-screen/.../1 Films.tmdl`
(`column 'Sondheim''s Work'` - the doubled-quote escape).

Two identifiers that deliberately stay UN-quoted, each with its own test below:
* `sourceColumn:` is a plain string property (runs to end of line, e.g. `displayFolder: 01 Fleet KPIs`
  in ground truth), and it must byte-match the raw name the partition's M query selects - quoting it
  would break that match.
* the on-disk FILENAME is a separate string from the identifier; it is sanitised for the characters
  Windows forbids in a path, while the `table` header keeps the real quoted name.

The authoritative structural check is TOM's `TmdlSerializer.DeserializeDatabaseFromFolder` (the offline
`examples/*/fabric/_validation/tmdl_validate`). It was run out-of-band against the emitted models for
each case here and returned PASS (and FAILED on the old un-quoted output), but it needs the .NET SDK +
a Windows-only TOM package, so it is not wired into this cross-platform suite; these tests assert the
emitted TMDL text instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import probe_live_source  # noqa: E402  # pylint: disable=wrong-import-position

# The real 72%-of-the-estate case: the engine's unresolved custom-SQL relation name.
TABLE = "Custom SQL Query"
COLUMN = "Some Column"
# A name whose own apostrophes exercise the doubling escape.
APOS_TABLE = "O'Brien's Orders"
APOS_COLUMN = "Sondheim's Work"
# A name carrying characters Windows forbids in a filename (`:` and `/`).
PATH_TABLE = "dbo:staging/tmp"

TABLES_PREFIX = "Probe.SemanticModel/definition/tables/"
MODEL_KEY = "Probe.SemanticModel/definition/model.tmdl"
SIMPLE_M = "let\n    Source = #table({}, {})\nin\n    Source"


def _scaffold(table: str, column: str, m: str = SIMPLE_M) -> dict[str, str]:
    return probe_live_source._pbip_files("Probe", m, table, column)  # pylint: disable=protected-access


def _table_key(files: dict[str, str]) -> str:
    return next(k for k in files if k.startswith(TABLES_PREFIX))


def _lines(text: str) -> list[str]:
    """The TMDL as stripped lines, so an assertion pins a whole header, not a loose substring."""
    return [ln.strip() for ln in text.splitlines()]


# --- the four header sites, one test each (so quoting three of four is still caught) --------------


def test_ref_table_header_is_quoted() -> None:
    """`model.tmdl` must reference the table with a quoted identifier."""
    lines = _lines(_scaffold(TABLE, COLUMN)[MODEL_KEY])
    assert f"ref table '{TABLE}'" in lines
    assert f"ref table {TABLE}" not in lines


def test_table_header_is_quoted() -> None:
    """The table file's `table` header must be quoted."""
    files = _scaffold(TABLE, COLUMN)
    lines = _lines(files[_table_key(files)])
    assert f"table '{TABLE}'" in lines
    assert f"table {TABLE}" not in lines


def test_column_header_is_quoted() -> None:
    """The `column` header must be quoted."""
    files = _scaffold(TABLE, COLUMN)
    lines = _lines(files[_table_key(files)])
    assert f"column '{COLUMN}'" in lines
    assert f"column {COLUMN}" not in lines


def test_partition_header_is_quoted() -> None:
    """The `partition` header must be quoted."""
    files = _scaffold(TABLE, COLUMN)
    lines = _lines(files[_table_key(files)])
    assert f"partition '{TABLE}' = m" in lines
    assert f"partition {TABLE} = m" not in lines


def test_no_header_identifier_is_left_bare() -> None:
    """Belt-and-suspenders: no known header may carry the raw, un-quoted name anywhere."""
    files = _scaffold(TABLE, COLUMN)
    model_lines = _lines(files[MODEL_KEY])
    table_lines = _lines(files[_table_key(files)])
    for bare in (f"ref table {TABLE}", f"table {TABLE}", f"column {COLUMN}", f"partition {TABLE} = m"):
        assert bare not in model_lines
        assert bare not in table_lines


# --- escaping -------------------------------------------------------------------------------------


def test_embedded_apostrophe_is_doubled() -> None:
    """A name containing a single quote is wrapped and the quote doubled (`'` -> `''`)."""
    files = _scaffold(APOS_TABLE, APOS_COLUMN)
    model_lines = _lines(files[MODEL_KEY])
    table_lines = _lines(files[_table_key(files)])
    assert "ref table 'O''Brien''s Orders'" in model_lines
    assert "table 'O''Brien''s Orders'" in table_lines
    assert "column 'Sondheim''s Work'" in table_lines
    assert "partition 'O''Brien''s Orders' = m" in table_lines


def test_tmdl_ident_helper_quotes_and_escapes() -> None:
    """Unit-level: the helper quotes unconditionally and doubles embedded quotes."""
    ident = probe_live_source._tmdl_ident  # pylint: disable=protected-access
    assert ident("T") == "'T'"  # unconditional: a bare-legal name is still quoted
    assert ident("Custom SQL Query") == "'Custom SQL Query'"
    assert ident("a'b") == "'a''b'"
    assert ident("a'b'c") == "'a''b''c'"  # every embedded quote doubled


# --- sourceColumn stays raw so it matches the M query --------------------------------------------


def test_source_column_stays_raw_to_match_m_query() -> None:
    """`sourceColumn:` must be the RAW column name; quoting it would break the M `SelectColumns` match."""
    conn = {"class": "sqlserver", "server": "srv", "database": "db", "schema": "dbo"}
    m_query, _ = probe_live_source.build_m_query(conn, TABLE, COLUMN)
    files = probe_live_source._pbip_files("Probe", m_query, TABLE, COLUMN)  # pylint: disable=protected-access
    table_lines = _lines(files[_table_key(files)])

    assert f"sourceColumn: {COLUMN}" in table_lines
    assert f"sourceColumn: '{COLUMN}'" not in table_lines
    # The M query selects the same raw, unquoted name - that correspondence is the whole point.
    assert f'{{"{COLUMN}"}}' in m_query


# --- the filename is a separate, sanitised string -------------------------------------------------


def test_table_filename_sanitised_for_path_breaking_chars() -> None:
    """A table name with `:`/`/` must not break the file path; the header keeps the real name."""
    files = _scaffold(PATH_TABLE, COLUMN)
    key = _table_key(files)
    stem = key[len(TABLES_PREFIX) : -len(".tmdl")]

    assert not any(c in stem for c in '<>:"/\\|?*'), stem
    assert f"table '{PATH_TABLE}'" in _lines(files[key])
    assert f"ref table '{PATH_TABLE}'" in _lines(files[MODEL_KEY])


def test_spaced_table_filename_keeps_spaces() -> None:
    """Spaces are legal in a Windows filename, so the common case keeps a readable name."""
    files = _scaffold(TABLE, COLUMN)
    assert _table_key(files) == f"{TABLES_PREFIX}{TABLE}.tmdl"


def test_tmdl_filename_stem_helper() -> None:
    """Unit-level: forbidden characters become `_`; an empty result falls back to a constant."""
    stem = probe_live_source._tmdl_filename_stem  # pylint: disable=protected-access
    assert stem("Custom SQL Query") == "Custom SQL Query"
    assert stem("dbo:staging/tmp") == "dbo_staging_tmp"
    assert stem('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert stem("  ..") == "probe_table"
    assert stem("tab\there") == "tab_here"
