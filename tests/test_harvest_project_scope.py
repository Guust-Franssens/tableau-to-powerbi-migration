"""Scoped harvests: what gets selected, what file identity survives, and what progress claims.

Three of these guard silent failures rather than loud ones, which is why they exist as tests at all:
an asset that never reaches a parser is not counted as a failure (the sweep still prints
`ours failed 0, his failed 0`), a re-download is indistinguishable from a first run except by the
clock, and an ETA is never checked by anyone against the run it described.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import harvest_estate_assets as harvest  # noqa: E402  # pylint: disable=wrong-import-position

# The engine's own `_TRANSFER_UUID_PREFIX` (`migrate_estate.py`), copied so this suite stays offline.
# Verified against engine 2.126.0: `strip_transfer_uuid('<uuid>_Meridian_Revenue_by_Region')` ->
# `'Meridian_Revenue_by_Region'`, while `'Meridian_Revenue_by_Region--<uuid>'` comes back intact.
ENGINE_UUID_PREFIX = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}[-_ ]+")


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
    todo, selected, workbooks, in_project, pulled_in = harvest.scoped_todo(
        database, [], ["project-finance"], workbooks_only=False
    )
    assert selected == [("project-finance", "Finance")]
    assert (workbooks, in_project, pulled_in) == (1, 0, 1)
    assert todo == [("datasource", "ds-finance", "Ledger"), ("workbook", "wb-finance", "Monthly Report")]


def test_project_name_selects_all_matching_projects_without_name_keyed_dependencies(
    database: sqlite3.Connection,
) -> None:
    todo, selected, workbooks, in_project, pulled_in = harvest.scoped_todo(
        database, ["Finance"], [], workbooks_only=False
    )
    assert selected == [("project-archive", "Finance"), ("project-finance", "Finance")]
    assert (workbooks, in_project, pulled_in) == (2, 0, 2)
    assert {item[1] for item in todo} == {"wb-finance", "wb-archive", "ds-finance", "ds-archive"}


def test_unknown_project_is_an_explicit_error(database: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no projects matched"):
        harvest.scoped_todo(database, ["Missing"], [], workbooks_only=False)


def test_unresolved_edge_includes_all_same_named_datasource_candidates(database: sqlite3.Connection) -> None:
    database.execute(
        "UPDATE dependency SET datasource_luid = NULL, datasource_name = ' ledger ' WHERE workbook_luid = 'wb-archive'"
    )
    todo, _, workbooks, in_project, pulled_in = harvest.scoped_todo(
        database, [], ["project-archive"], workbooks_only=False
    )
    assert (workbooks, in_project, pulled_in) == (1, 0, 2)
    assert {item[1] for item in todo} == {"wb-archive", "ds-finance", "ds-archive"}


def test_a_project_holding_only_datasources_is_still_selectable(database: sqlite3.Connection) -> None:
    """The issue's own example: `--project "00 - Certified Sources"` is datasources and no workbooks.

    Selecting only what workbook edges pull in leaves this empty, so the model-first phase-1
    workflow -- migrate the certified sources first, then the reports -- cannot be scoped at all.
    """
    todo, _, workbooks, in_project, pulled_in = harvest.scoped_todo(
        database, ["Certified Sources"], [], workbooks_only=False
    )
    assert (workbooks, in_project, pulled_in) == (0, 2, 0)
    assert todo == [("datasource", "ds-archive", "Ledger"), ("datasource", "ds-finance", "Ledger")]


def test_a_datasource_that_is_both_in_project_and_pulled_in_is_counted_once(database: sqlite3.Connection) -> None:
    todo, _, workbooks, in_project, pulled_in = harvest.scoped_todo(
        database, [], ["project-finance", "project-certified"], workbooks_only=False
    )
    assert (workbooks, in_project, pulled_in) == (1, 2, 0)
    assert [item[1] for item in todo] == ["ds-archive", "ds-finance", "wb-finance"]


def test_workbooks_only_still_drops_the_datasources_that_live_in_the_project(database: sqlite3.Connection) -> None:
    todo, _, workbooks, in_project, pulled_in = harvest.scoped_todo(
        database, ["Certified Sources", "Finance"], [], workbooks_only=True
    )
    assert (workbooks, in_project, pulled_in) == (2, 0, 0)
    assert {item[0] for item in todo} == {"workbook"}


def test_local_asset_name_puts_the_luid_in_front_so_the_engine_strips_it(tmp_path: Path) -> None:
    """LUID-unique on disk, and invisible downstream -- a trailing `--<luid>` is neither."""
    luid = "a85bde90-9380-4a01-8b1e-2f9c3d4e5f60"
    finance = harvest.asset_path(tmp_path, "workbook", "Meridian Revenue by Region", luid)
    archive = harvest.asset_path(
        tmp_path, "workbook", "Meridian Revenue by Region", "b0000000-0000-4000-8000-" + 12 * "0"
    )
    assert finance.name == f"{luid}_Meridian_Revenue_by_Region.twbx"
    assert finance != archive
    # Without this the LUID becomes the stem of `bundle/pbip/<stem>/` and `migrations/<slug>/`.
    assert ENGINE_UUID_PREFIX.sub("", finance.stem) == "Meridian_Revenue_by_Region"


def test_a_twb_landing_where_a_twbx_was_requested_is_still_found(tmp_path: Path) -> None:
    """The extension fallback. `fetch_tds.py::save_outputs` writes `.twb` for a non-zip download.

    Measured across three real full harvests: `{'.tdsx': 17, '.twb': 18, '.twbx': 20}` -- 18 of 38
    workbooks (47%) land as `.twb`. Matching only the requested extension drops them, and the sweep
    still reports `ours failed 0, his failed 0` because they never reached a parser at all.
    """
    landed = tmp_path / "wb-finance_Monthly_Report.twb"
    landed.write_text("<workbook/>", encoding="utf-8")
    assert harvest.asset_path(tmp_path, "workbook", "Monthly Report", "wb-finance").suffix == ".twbx"
    assert harvest.existing_asset(tmp_path, "workbook", "Monthly Report", "wb-finance") == landed


def test_a_tds_landing_where_a_tdsx_was_requested_is_still_found(tmp_path: Path) -> None:
    landed = tmp_path / "ds-finance_Ledger.tds"
    landed.write_text("<datasource/>", encoding="utf-8")
    assert harvest.existing_asset(tmp_path, "datasource", "Ledger", "ds-finance") == landed


def test_the_packaged_download_wins_over_the_unpacked_document(tmp_path: Path) -> None:
    """A zip download writes BOTH; the `.twbx` is the one carrying the extract."""
    (tmp_path / "wb-finance_Monthly_Report.twb").write_text("<workbook/>", encoding="utf-8")
    packaged = tmp_path / "wb-finance_Monthly_Report.twbx"
    packaged.write_bytes(b"PK\x03\x04")
    assert harvest.existing_asset(tmp_path, "workbook", "Monthly Report", "wb-finance") == packaged


def test_an_assets_dir_from_before_the_luid_prefix_is_reused(tmp_path: Path) -> None:
    """Otherwise the first run after an upgrade re-downloads the estate at a sign-in per asset."""
    legacy = tmp_path / "Monthly_Report.twbx"
    legacy.write_bytes(b"PK\x03\x04")
    assert harvest.existing_asset(tmp_path, "workbook", "Monthly Report", "wb-finance") == legacy


def test_nothing_landed_is_still_reported_as_nothing(tmp_path: Path) -> None:
    assert harvest.existing_asset(tmp_path, "workbook", "Monthly Report", "wb-finance") is None


def test_the_eta_is_measured_on_finished_assets_so_it_reaches_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`elapsed / index` announced `ETA=46s` with 0 s of work left, and ~19 h on a 58-asset run."""
    monkeypatch.setattr(harvest.time, "perf_counter", lambda: 60.0)
    assert harvest.progress(6, 6, 0.0) == "elapsed=60s avg=10.0s ETA=0s"
    assert harvest.progress(3, 6, 0.0) == "elapsed=60s avg=20.0s ETA=60s"
    assert harvest.progress(0, 6, 0.0) == "elapsed=60s"


# --- the same two failures, end to end through main() -------------------------------------------


def estate_db(path: Path) -> Path:
    """One workbook, in the shape `assess_estate.py --survey` writes."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE dependency (workbook_luid TEXT, datasource_luid TEXT, datasource_name TEXT);
        INSERT INTO project VALUES ('project-finance', 'Finance');
        INSERT INTO workbook VALUES ('wb-finance', 'Monthly Report', 'project-finance');
        """
    )
    con.commit()
    con.close()
    return path


@pytest.fixture(name="offline_sweep")
def offline_sweep_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path]:
    """Stub the engine, the `.env` and both parsers; the file bookkeeping under test stays real."""
    parsed: list[Path] = []

    def fake_parse(path: Path, scripts: Path) -> tuple[dict, dict]:  # pylint: disable=unused-argument
        parsed.append(Path(path))
        return {"ok": True, "sheets": 1}, {"ok": True, "relations": 1}

    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(harvest, "parse_asset", fake_parse)
    return parsed


def run_sweep(monkeypatch: pytest.MonkeyPatch, out: Path, db: Path, *extra: str) -> list[dict]:
    """Run the sweep and return `parse-sweep.json`."""
    monkeypatch.setattr(sys, "argv", ["harvest_estate_assets.py", "--out", str(out), "--db", str(db), *extra])
    assert harvest.main() == 0
    return json.loads((out / "parse-sweep.json").read_text(encoding="utf-8"))


def test_the_sweep_parses_a_twb_that_landed_for_a_twbx_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offline_sweep: list[Path]
) -> None:
    out = tmp_path / "_sweep"
    (out / "assets").mkdir(parents=True)
    landed = out / "assets" / "wb-finance_Monthly_Report.twb"
    landed.write_text("<workbook/>", encoding="utf-8")

    rows = run_sweep(monkeypatch, out, estate_db(tmp_path / "estate.db"), "--skip-download")

    assert offline_sweep == [landed]
    assert [row.get("download_error") for row in rows] == [None]
    assert rows[0]["file"] == str(landed)
    assert rows[0]["ours"]["ok"] and rows[0]["theirs"]["ok"]


def test_the_sweep_does_not_re_download_an_assets_dir_from_before_the_luid_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offline_sweep: list[Path]
) -> None:
    out = tmp_path / "_sweep"
    (out / "assets").mkdir(parents=True)
    legacy = out / "assets" / "Monthly_Report.twbx"
    legacy.write_bytes(b"PK\x03\x04")

    def refuse_download(*args: object, **kwargs: object) -> tuple[bool, str]:
        raise AssertionError(f"re-downloaded an asset that is already on disk: {args} {kwargs}")

    monkeypatch.setattr(harvest, "download", refuse_download)
    rows = run_sweep(monkeypatch, out, estate_db(tmp_path / "estate.db"))

    assert offline_sweep == [legacy]
    assert rows[0]["file"] == str(legacy)


# --- a project URL pasted out of the browser (issue #191) ---------------------------------------
#
# The numeric id in Tableau's own web-UI route is a legacy internal identifier with NO public API
# surface: verified against a live site 2026-08-17, REST `GET /sites/{id}/projects` returns only
# GUID `id`s and the Metadata API answers `FieldUndefined` for it. So the ONE thing these guard is
# that an unresolvable paste is a loud usage error rather than a scope that quietly selects nothing
# -- an empty selection reads as "that project has no content", not "we could not resolve you".

PROJECT_GUID = "a85bde90-9380-4a01-8b1e-2f9c3d4e5f60"
OTHER_GUID = "b0000000-0000-4000-8000-000000000000"


@pytest.mark.parametrize(
    "url",
    [
        f"https://tableau.example.com/#/projects/{PROJECT_GUID}",
        f"https://tableau.example.com/#/site/finance/projects/{PROJECT_GUID}",
        f"https://tableau.example.com/#/site/finance/projects/{PROJECT_GUID}/",
        f"https://tableau.example.com/#/projects/{PROJECT_GUID}?:origin=card_share_link",
        f"https://tableau.example.com/api/3.19/sites/{OTHER_GUID}/projects/{PROJECT_GUID}",
        f"http://tableau.example.com/#/projects/{PROJECT_GUID}",
        f"tableau.example.com/#/projects/{PROJECT_GUID}",
        f"https://tableau.example.com/%23/site/finance/projects/{PROJECT_GUID}",
    ],
)
def test_a_guid_url_normalises_into_the_exact_project_id_path(url: str) -> None:
    """Including the REST shape, whose SITE guid sits in the same URL and must not be mistaken for it."""
    assert harvest.project_ids_from_urls([url], []) == [PROJECT_GUID]


@pytest.mark.parametrize(
    "url",
    [
        "https://tableau.example.com/#/projects/35",
        "https://tableau.example.com/#/projects/35/",
        "https://tableau.example.com/#/site/finance/projects/35?:origin=card_share_link",
        "https://tableau.example.com/#/projects/3%35",  # percent-encoded, same legacy id
    ],
)
def test_a_numeric_url_is_refused_with_the_id_echoed(url: str) -> None:
    with pytest.raises(harvest.ProjectUrlError) as raised:
        harvest.project_ids_from_urls([url], [])
    message = str(raised.value)
    assert "35" in message
    assert "no public REST or Metadata API mapping" in message
    assert "--project" in message  # says what to pass instead, not just that it failed


@pytest.mark.parametrize(
    "url",
    [
        "https://tableau.example.com/#/site/finance/views/Monthly/Sheet1",
        "https://tableau.example.com/",
        "",
        "not a url at all",
    ],
)
def test_a_url_with_no_project_segment_is_a_usage_error(url: str) -> None:
    with pytest.raises(harvest.ProjectUrlError, match="no `/projects/<id>` segment"):
        harvest.project_ids_from_urls([url], [])


def test_a_non_http_url_is_refused_as_unsupported() -> None:
    with pytest.raises(harvest.ProjectUrlError, match="only http"):
        harvest.project_ids_from_urls([f"file:///c:/projects/{PROJECT_GUID}"], [])


def test_a_project_segment_that_is_neither_luid_nor_numeric_is_a_usage_error() -> None:
    """A project NAME in the route is not silently accepted: `--project` matches exactly, this does not."""
    with pytest.raises(harvest.ProjectUrlError, match="neither a LUID nor a numeric id"):
        harvest.project_ids_from_urls(["https://tableau.example.com/#/projects/Certified%20Sources"], [])


def test_two_project_segments_in_one_url_are_ambiguous_rather_than_a_best_guess() -> None:
    url = f"https://tableau.example.com/#/projects/{PROJECT_GUID}/projects/{OTHER_GUID}"
    with pytest.raises(harvest.ProjectUrlError, match="ambiguous"):
        harvest.project_ids_from_urls([url], [])


def test_the_same_project_named_twice_is_one_project() -> None:
    url = f"https://tableau.example.com/#/projects/{PROJECT_GUID}"
    assert harvest.project_ids_from_urls([url, url], []) == [PROJECT_GUID]
    assert harvest.project_ids_from_urls([url], [PROJECT_GUID]) == [PROJECT_GUID]


def test_two_different_guid_urls_scope_to_both_projects_like_repeated_project_id() -> None:
    """`--project-url` is repeatable and ADDITIVE, exactly as `--project-id` already is."""
    urls = [f"https://tableau.example.com/#/projects/{PROJECT_GUID}", f"https://x.example.com/#/projects/{OTHER_GUID}"]
    assert harvest.project_ids_from_urls(urls, []) == [PROJECT_GUID, OTHER_GUID]


def test_a_url_composes_with_the_existing_project_id_flag() -> None:
    resolved = harvest.project_ids_from_urls([f"https://tableau.example.com/#/projects/{PROJECT_GUID}"], [OTHER_GUID])
    assert resolved == [OTHER_GUID, PROJECT_GUID]


def test_one_valid_url_beside_one_numeric_url_refuses_the_whole_invocation() -> None:
    """No partial scope: a run that silently drops half the requested scope is the worse failure."""
    urls = [f"https://tableau.example.com/#/projects/{PROJECT_GUID}", "https://tableau.example.com/#/projects/35"]
    with pytest.raises(harvest.ProjectUrlError) as raised:
        harvest.project_ids_from_urls(urls, [])
    assert "35" in str(raised.value)


def test_the_diagnostic_never_echoes_userinfo_query_or_fragment_secrets() -> None:
    url = "https://admin:hunter2@tableau.example.com/#/site/finance/projects/35?:token=SEKRIT-TOKEN"
    with pytest.raises(harvest.ProjectUrlError) as raised:
        harvest.project_ids_from_urls([url], [])
    message = str(raised.value)
    assert "tableau.example.com" in message and "35" in message
    for secret in ("hunter2", "admin", "SEKRIT-TOKEN", ":token"):
        assert secret not in message


# --- the same refusal, end to end through main() -------------------------------------------------


def guid_estate_db(path: Path) -> Path:
    """The same one-workbook estate, with the project keyed by a LUID a URL could carry."""
    con = sqlite3.connect(path)
    con.executescript(
        f"""
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE dependency (workbook_luid TEXT, datasource_luid TEXT, datasource_name TEXT);
        INSERT INTO project VALUES ('{PROJECT_GUID}', 'Finance');
        INSERT INTO workbook VALUES ('wb-finance', 'Monthly Report', '{PROJECT_GUID}');
        """
    )
    con.commit()
    con.close()
    return path


@pytest.fixture(name="refuse_all_work")
def refuse_all_work_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step main() takes AFTER argument handling, wired to fail loudly if it is reached."""

    def refuse(name: str):
        def boom(*args: object, **kwargs: object):
            raise AssertionError(f"{name} ran before the project URL was refused: {args} {kwargs}")

        return boom

    monkeypatch.setattr(harvest, "refuse_unignored_output", refuse("the --out guard"))
    monkeypatch.setattr(harvest, "engine_scripts_dir", refuse("engine resolution"))
    monkeypatch.setattr(harvest, "resolve_env", refuse("the .env / credential read"))
    monkeypatch.setattr(harvest, "require", refuse("the credential check"))
    monkeypatch.setattr(harvest, "download", refuse("a download"))


@pytest.mark.parametrize(
    "urls",
    [
        ["https://tableau.example.com/#/projects/35"],
        [f"https://tableau.example.com/#/projects/{PROJECT_GUID}", "https://tableau.example.com/#/projects/35"],
    ],
)
def test_the_cli_refuses_a_numeric_project_url_before_any_session_or_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    urls: list[str],
    refuse_all_work: None,  # pylint: disable=unused-argument
) -> None:
    """The mutation control: drop the refusal from `main()` and this stops raising SystemExit at all.

    Both cases matter -- the numeric URL alone, and a numeric URL BESIDE a usable one, which must
    still select nothing rather than quietly harvest half the requested scope.
    """
    argv = ["harvest_estate_assets.py", "--out", str(tmp_path / "_sweep"), "--db", str(tmp_path / "estate.db")]
    for url in urls:
        argv += ["--project-url", url]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as raised:
        harvest.main()

    assert raised.value.code == 2  # argparse's usage-error convention, unchanged
    stderr = capsys.readouterr().err
    assert "35" in stderr and "no public REST or Metadata API mapping" in stderr
    assert not (tmp_path / "_sweep").exists()  # nothing was created, so nothing was partially scoped


def test_the_cli_treats_a_guid_url_exactly_as_the_project_id_it_carries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offline_sweep: list[Path]
) -> None:
    """Byte for byte the same selection, so the URL really is normalised into the existing path."""
    db = guid_estate_db(tmp_path / "estate.db")

    def sweep(out: Path, *extra: str) -> list[dict]:
        (out / "assets").mkdir(parents=True)
        (out / "assets" / "wb-finance_Monthly_Report.twbx").write_bytes(b"PK\x03\x04")
        rows = run_sweep(monkeypatch, out, db, "--skip-download", *extra)
        return [{key: row[key] for key in ("name", "kind", "luid")} for row in rows]

    by_id = sweep(tmp_path / "_by_id", "--project-id", PROJECT_GUID)
    by_url = sweep(tmp_path / "_by_url", "--project-url", f"https://tableau.example.com/#/projects/{PROJECT_GUID}")

    assert by_url == by_id == [{"name": "Monthly Report", "kind": "workbook", "luid": "wb-finance"}]
    assert len(offline_sweep) == 2


# --- review round 1: three URL classes that reached a WRONG outcome, not merely an ugly one ------
#
# All three were reproduced against the first commit before being fixed, and all three share one
# shape: the failure was invisible. A raw `ValueError` became exit 1, which in this script MEANS
# "nothing could be assessed"; an uppercase GUID and a `%0A`-suffixed GUID both became a perfectly
# well-formed `--project-id` matching no row at all.

MALFORMED_AUTHORITY = [
    "https://[::1/#/projects/35",  # unterminated IPv6 literal -> urlsplit ValueError
    "https://admin:hunter2@[::1/#/projects/35?:tok=SEKRIT-TOKEN",
    # `netloc '...' contains invalid characters under NFKC normalization` -- the ONE urllib message
    # that quotes the netloc back, and the netloc is exactly where `user:password@` sits.
    "https://admin:hunter2@exa\u2100mple.com/#/projects/35?:tok=SEKRIT-TOKEN",
]


@pytest.mark.parametrize("url", MALFORMED_AUTHORITY)
def test_a_malformed_authority_is_a_sanitized_usage_error_not_a_raw_valueerror(url: str) -> None:
    """`urlsplit` raises on a malformed authority; unguarded that reached the CLI as a traceback."""
    with pytest.raises(harvest.ProjectUrlError) as raised:
        harvest.project_ids_from_urls([url], [])
    message = str(raised.value)
    assert "not a parseable URL" in message
    for leak in ("hunter2", "admin", "SEKRIT-TOKEN", "exa", "[::1"):
        assert leak not in message
    assert raised.value.__cause__ is None and raised.value.__context__ is None  # no chained raw text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://x.example.com/#/projects/{PROJECT_GUID.upper()}", PROJECT_GUID),
        (f"https://x.example.com/#/projects/{{{PROJECT_GUID}}}", PROJECT_GUID),
        (f"https://x.example.com/#/projects/%7B{PROJECT_GUID.upper()}%7D", PROJECT_GUID),
        # The same project written two ways in ONE url is one project, not an ambiguity.
        (f"https://x.example.com/#/projects/{PROJECT_GUID}/projects/{PROJECT_GUID.upper()}", PROJECT_GUID),
    ],
)
def test_an_accepted_guid_is_canonicalised_to_lowercase_unbraced_form(url: str, expected: str) -> None:
    """Tableau stores LUIDs lowercase, so a verbatim uppercase GUID matched NO row -- silently."""
    assert harvest.project_ids_from_urls([url], []) == [expected]


@pytest.mark.parametrize(
    ("url", "codepoint"),
    [
        (f"https://x.example.com/#/projects/{PROJECT_GUID}%0A", "U+000A"),  # `$` matches before it
        (f"https://x.example.com/#/projects/{PROJECT_GUID}%00", "U+0000"),
        (f"https://x.example.com/#/projects/{PROJECT_GUID}%E2%80%8B", "U+200B"),  # zero-width space
        ("https://x.example.com/#/projects/35%0A", "U+000A"),
    ],
)
def test_a_decoded_control_character_is_refused_rather_than_silently_carried(url: str, codepoint: str) -> None:
    with pytest.raises(harvest.ProjectUrlError) as raised:
        harvest.project_ids_from_urls([url], [])
    assert "non-printable" in str(raised.value) and codepoint in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        f"https://x.example.com/#/projects/urn:uuid:{PROJECT_GUID}",  # uuid.UUID would take these,
        "https://x.example.com/#/projects/a85bde9093804a018b1e2f9c3d4e5f60",  # the regex gate does not
        f"https://x.example.com/#/projects/%20{PROJECT_GUID}%20",
        # A printable suffix: the reason BOTH matches are `fullmatch`. A prefix match returns the
        # whole segment, so `<luid>x` would have been forwarded verbatim as a project id.
        f"https://x.example.com/#/projects/{PROJECT_GUID}x",
        f"https://x.example.com/#/projects/{PROJECT_GUID}%20extra",
        "https://x.example.com/#/projects/35x",
        "https://x.example.com/#/projects/35;jsessionid=SEKRIT-TOKEN",
    ],
)
def test_canonicalisation_did_not_widen_what_counts_as_a_luid(url: str) -> None:
    """`uuid.UUID` is the normaliser, not the gate: it accepts undashed and `urn:` forms, we do not."""
    with pytest.raises(harvest.ProjectUrlError) as raised:
        harvest.project_ids_from_urls([url], [])
    assert "SEKRIT-TOKEN" not in str(raised.value)  # the generic refusal never echoes the segment


# --- the same three classes, end to end through main() -------------------------------------------


@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        ([MALFORMED_AUTHORITY[1]], "not a parseable URL"),
        ([f"https://x.example.com/#/projects/{PROJECT_GUID}%0A"], "non-printable"),
        # valid + invalid: still refuses everything, so no partial scope survives the correction.
        (
            [f"https://x.example.com/#/projects/{PROJECT_GUID}", MALFORMED_AUTHORITY[0]],
            "not a parseable URL",
        ),
        (
            [f"https://x.example.com/#/projects/{PROJECT_GUID}", f"https://x.example.com/#/projects/{PROJECT_GUID}%0A"],
            "non-printable",
        ),
        # A LUID with a printable suffix: refused, never truncated to the LUID and never forwarded
        # whole. Exit 1 here would be the script claiming the estate could not be assessed.
        ([f"https://x.example.com/#/projects/{PROJECT_GUID}x"], "neither a LUID nor a numeric id"),
    ],
)
def test_the_cli_refuses_a_malformed_or_control_bearing_url_as_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    urls: list[str],
    expected: str,
    refuse_all_work: None,  # pylint: disable=unused-argument
) -> None:
    """Exit 2 and a sanitized message -- NOT exit 1, which is this script's "nothing assessed"."""
    argv = ["harvest_estate_assets.py", "--out", str(tmp_path / "_sweep"), "--db", str(tmp_path / "estate.db")]
    for url in urls:
        argv += ["--project-url", url]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as raised:
        harvest.main()

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert expected in stderr
    assert "Traceback" not in stderr
    for leak in ("hunter2", "SEKRIT-TOKEN"):
        assert leak not in stderr
    assert not (tmp_path / "_sweep").exists()


@pytest.mark.parametrize(
    "url",
    [
        f"https://tableau.example.com/#/projects/{PROJECT_GUID.upper()}",
        f"https://tableau.example.com/#/projects/%7B{PROJECT_GUID.upper()}%7D",
    ],
)
def test_the_cli_selects_the_real_project_from_an_uppercase_or_braced_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str, offline_sweep: list[Path]
) -> None:
    """The production control for canonicalisation: `estate.db` holds the LUID lowercase.

    Without the `uuid.UUID` pass this selects nothing, `scoped_todo` raises `no projects matched`,
    and the run exits 1 -- an invented "that project is empty" for a URL naming a real project.
    """
    out = tmp_path / "_sweep"
    (out / "assets").mkdir(parents=True)
    (out / "assets" / "wb-finance_Monthly_Report.twbx").write_bytes(b"PK\x03\x04")

    rows = run_sweep(monkeypatch, out, guid_estate_db(tmp_path / "estate.db"), "--skip-download", "--project-url", url)

    assert [(row["kind"], row["luid"]) for row in rows] == [("workbook", "wb-finance")]
    assert len(offline_sweep) == 1
