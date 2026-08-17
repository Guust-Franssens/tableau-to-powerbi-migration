"""Project-scoped harvests keep LUID identity and include model dependencies."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import harvest_estate_assets as harvest  # noqa: E402  # pylint: disable=wrong-import-position


@pytest.fixture(name="database")
def database_fixture() -> sqlite3.Connection:
    """A minimal assessment database with duplicate project and asset display names."""
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE dependency (workbook_luid TEXT, datasource_luid TEXT, datasource_name TEXT);
        """
    )
    con.executemany(
        "INSERT INTO project VALUES (?, ?)",
        [("project-finance", "Finance"), ("project-archive", "Finance"), ("project-certified", "Certified Sources")],
    )
    con.executemany(
        "INSERT INTO workbook VALUES (?, ?, ?)",
        [("wb-finance", "Monthly Report", "project-finance"), ("wb-archive", "Monthly Report", "project-archive")],
    )
    con.executemany(
        "INSERT INTO datasource VALUES (?, ?, ?)",
        [("ds-finance", "Ledger", "project-certified"), ("ds-archive", "Ledger", "project-certified")],
    )
    con.executemany(
        "INSERT INTO dependency VALUES (?, ?, ?)",
        [("wb-finance", "ds-finance", "Ledger"), ("wb-archive", "ds-archive", "Ledger")],
    )
    return con


def test_project_id_selects_one_same_named_project_and_its_dependency(database: sqlite3.Connection) -> None:
    todo, selected, workbooks, datasources = harvest.scoped_todo(
        database, [], ["project-finance"], workbooks_only=False
    )
    assert selected == [("project-finance", "Finance")]
    assert (workbooks, datasources) == (1, 1)
    assert todo == [("datasource", "ds-finance", "Ledger"), ("workbook", "wb-finance", "Monthly Report")]


def test_project_name_selects_all_matching_projects_without_name_keyed_dependencies(
    database: sqlite3.Connection,
) -> None:
    todo, selected, workbooks, datasources = harvest.scoped_todo(database, ["Finance"], [], workbooks_only=False)
    assert selected == [("project-archive", "Finance"), ("project-finance", "Finance")]
    assert (workbooks, datasources) == (2, 2)
    assert {item[1] for item in todo} == {"wb-finance", "wb-archive", "ds-finance", "ds-archive"}


def test_unknown_project_is_an_explicit_error(database: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no projects matched"):
        harvest.scoped_todo(database, ["Missing"], [], workbooks_only=False)


def test_unresolved_edge_includes_all_same_named_datasource_candidates(database: sqlite3.Connection) -> None:
    database.execute("UPDATE dependency SET datasource_luid = NULL WHERE workbook_luid = 'wb-archive'")
    todo, _, workbooks, datasources = harvest.scoped_todo(database, [], ["project-archive"], workbooks_only=False)
    assert (workbooks, datasources) == (1, 2)
    assert {item[1] for item in todo} == {"wb-archive", "ds-finance", "ds-archive"}


def test_local_asset_name_includes_the_luid(tmp_path: Path) -> None:
    finance = harvest.asset_path(tmp_path, "workbook", "Monthly Report", "wb-finance")
    archive = harvest.asset_path(tmp_path, "workbook", "Monthly Report", "wb-archive")
    assert finance != archive
    assert finance.name == "Monthly_Report--wb-finance.twbx"
