"""Fidelity self-tests for the offline Tableau mock.

Same rule as the Fabric mock's suite: each test names the evidence behind the behaviour it pins, and
the ASSUMED ones say so. The bulk of the value here is that the REAL clients in ``scripts/`` -
``assess_estate.Site`` and ``tableau_lineage`` - are driven against it unmodified, so a
mock-vs-client mismatch is a test failure rather than a surprise on the day.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import assess_estate as ae  # noqa: E402
import tableau_lineage as tl  # noqa: E402
from mocks import estate, tableau  # noqa: E402


@pytest.fixture(name="site")
def _site() -> tableau.TableauSite:
    return estate.build_site()


@pytest.fixture(name="served")
def _served(site):
    with tableau.serve(site) as base:
        yield site, base


def signed_in(site) -> str:
    status, _headers, payload = site.handle(
        "POST",
        f"http://x/api/{site.rest_version}/auth/signin",
        {},
        json.dumps(
            {
                "credentials": {
                    "personalAccessTokenName": "mock-pat",
                    "personalAccessTokenSecret": site.pat_credentials["mock-pat"],
                    "site": {"contentUrl": site.content_url},
                }
            }
        ).encode(),
    )
    assert status == 200
    return json.loads(payload)["credentials"]["token"]


# ------------------------------------------------------------------ transport


def test_sign_in_requires_both_halves_of_the_pat(site):
    """The right PAT NAME with the wrong secret must fail, and vice versa.

    This is the single most common credential mistake this pipeline hits, and a mock that accepted
    any non-empty pair would make the error message untestable.
    """
    body = json.dumps(
        {
            "credentials": {
                "personalAccessTokenName": "mock-pat",
                "personalAccessTokenSecret": "wrong",
                "site": {"contentUrl": site.content_url},
            }
        }
    ).encode()
    status, _headers, _payload = site.handle("POST", f"http://x/api/{site.rest_version}/auth/signin", {}, body)
    assert status == 401


def test_an_unknown_site_content_url_is_a_404(site):
    body = json.dumps(
        {
            "credentials": {
                "personalAccessTokenName": "mock-pat",
                "personalAccessTokenSecret": site.pat_credentials["mock-pat"],
                "site": {"contentUrl": "some-other-site"},
            }
        }
    ).encode()
    status, _headers, _payload = site.handle("POST", f"http://x/api/{site.rest_version}/auth/signin", {}, body)
    assert status == 404


def test_a_lost_session_answers_the_literal_code_the_client_looks_for(site):
    """MEASURED, and load-bearing: ``assess_estate.Site.get`` re-authenticates only when the response
    body contains the string ``401002``. A mock that answered a bare 401 would make the re-auth path
    dead code that nobody notices until a long survey drops its session."""
    token = signed_in(site)
    site.expire_session()

    status, _headers, payload = site.handle(
        "GET",
        f"http://x/api/{site.rest_version}/sites/{site.site_id}/projects",
        {"x-tableau-auth": token},
        b"",
    )
    assert status == 401
    assert "401002" in payload.decode()


def test_the_real_assess_client_recovers_from_a_dropped_session(served):
    """The recovery path, driven through the REAL client rather than asserted about."""
    site, base = served
    client = ae.Site(tableau.env_for(site, base))
    client.sign_in()
    path = f"/sites/{client.site_id}/projects"
    assert client.paged(path, "projects", "project")

    site.expire_session()
    projects, _continuation = client.paged(path, "projects", "project")
    assert len(projects) == len(site.projects), "the client must re-auth and finish the page"
    assert client.reauths == 1, "the recovery must be recorded, not silent"


def test_pagination_numbers_are_strings_because_tableau_emits_strings(site):
    """MEASURED quirk of the REST API: ``pageNumber``/``pageSize``/``totalAvailable`` are strings.

    Emitting ints would let a client that never coerces them pass here and then compare ``"1" < 2``
    against the site.
    """
    token = signed_in(site)
    _status, _headers, payload = site.handle(
        "GET", f"http://x/api/{site.rest_version}/sites/{site.site_id}/projects", {"x-tableau-auth": token}, b""
    )
    pagination = json.loads(payload)["pagination"]
    assert all(isinstance(value, str) for value in pagination.values()), pagination


def test_paging_actually_pages(site):
    """A page size smaller than the collection must require a second request."""
    site.page_size = 2
    token = signed_in(site)
    seen = []
    for number in (1, 2):
        _status, _headers, payload = site.handle(
            "GET",
            f"http://x/api/{site.rest_version}/sites/{site.site_id}/projects?pageNumber={number}&pageSize=100",
            {"x-tableau-auth": token},
            b"",
        )
        seen += json.loads(payload)["projects"]["project"]
    assert len(seen) == len(site.projects)


def test_the_real_client_follows_pagination_to_the_end(served):
    site, base = served
    site.page_size = 1
    client = ae.Site(tableau.env_for(site, base))
    client.sign_in()
    rows, _continuation = client.paged(f"/sites/{client.site_id}/workbooks", "workbooks", "workbook")
    assert len(rows) == len(site.workbooks)
    assert len([r for _m, r in site.requests if "/workbooks?" in r]) == len(site.workbooks), "one request per page"


def test_usage_statistics_are_absent_unless_requested(site):
    """MEASURED: the ``usage`` block appears only with ``includeUsageStatistics=true``.

    Always returning it would hide a client that forgot the flag and would then read zero traffic on
    a live site - the input to the tiering decision.
    """
    token = signed_in(site)
    base = f"http://x/api/{site.rest_version}/sites/{site.site_id}/views"
    _s, _h, without = site.handle("GET", base, {"x-tableau-auth": token}, b"")
    _s, _h, with_usage = site.handle("GET", base + "?includeUsageStatistics=true", {"x-tableau-auth": token}, b"")

    assert all("usage" not in row for row in json.loads(without)["views"]["view"])
    assert any("usage" in row for row in json.loads(with_usage)["views"]["view"])


# ------------------------------------------------------------------- download


def test_content_download_returns_a_real_packaged_workbook(site):
    """Real bytes are the whole point: the parser has to do genuine work downstream."""
    token = signed_in(site)
    workbook = site.workbooks[0]
    status, headers, payload = site.handle(
        "GET",
        f"http://x/api/{site.rest_version}/sites/{site.site_id}/workbooks/{workbook.luid}/content",
        {"x-tableau-auth": token},
        b"",
    )

    assert status == 200
    assert payload[:2] == b"PK", "a .twbx is a zip"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert any(name.endswith(".twb") for name in archive.namelist())
    assert "Content-Disposition" in headers


def test_the_download_header_uses_tableaus_non_standard_name_form(site):
    """MEASURED: Tableau sends ``Content-Disposition: name="X.twbx"`` with NO ``filename=``.

    The engine's ``fetch_tds.derive_filename`` has a fallback precisely because of this. Serving the
    standard ``filename=`` form would make that fallback untested and let a regression through.
    """
    token = signed_in(site)
    _status, headers, _payload = site.handle(
        "GET",
        f"http://x/api/{site.rest_version}/sites/{site.site_id}/workbooks/{site.workbooks[0].luid}/content",
        {"x-tableau-auth": token},
        b"",
    )
    disposition = headers["Content-Disposition"]
    assert "name=" in disposition
    assert "filename=" not in disposition


def test_a_downloaded_workbook_parses_with_the_real_parser(site, tmp_path):
    """End of the honesty chain: served bytes -> file -> this repo's own parser."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from parse_tableau import parse_workbook  # noqa: PLC0415

    target = tmp_path / "wb.twbx"
    target.write_bytes(site.workbooks[0].content)
    spec = parse_workbook(target)
    assert spec.get("worksheets")


def test_a_missing_luid_is_a_404_not_an_empty_download(site):
    token = signed_in(site)
    status, _headers, _payload = site.handle(
        "GET",
        f"http://x/api/{site.rest_version}/sites/{site.site_id}/workbooks/nope/content",
        {"x-tableau-auth": token},
        b"",
    )
    assert status == 404


# -------------------------------------------------------------------- GraphQL


def test_an_unsupported_graphql_query_is_an_error_not_an_empty_result(site):
    """The strictest choice available, and it is the right one.

    An empty ``{"data": {...}}`` is how a caller concludes "this estate has no dependencies" and
    sequences the migration wrong. An ``errors`` array cannot be mistaken for an answer.
    """
    token = signed_in(site)
    status, _headers, payload = site.handle(
        "POST",
        "http://x/api/metadata/graphql",
        {"x-tableau-auth": token},
        json.dumps({"query": "{ somethingWeDoNotServe { id } }"}).encode(),
    )
    assert status == 200, "GraphQL reports errors with HTTP 200, as the real API does"
    assert json.loads(payload)["errors"]


def test_the_structure_query_is_derived_from_the_served_bytes(site):
    """The mock reads the workbook XML itself rather than repeating a hard-coded answer.

    Independent of ``parse_tableau`` on purpose: if both sides shared the parser, a parser bug would
    be invisible because the expectation would move with it.
    """
    token = signed_in(site)
    _status, _headers, payload = site.handle(
        "POST",
        "http://x/api/metadata/graphql",
        {"x-tableau-auth": token},
        json.dumps({"query": ae.STRUCTURE_QUERY}).encode(),
    )
    workbooks = {w["name"]: w for w in json.loads(payload)["data"]["workbooks"]}

    assert workbooks["Sales Review"]["sheets"], "minimal.twb has worksheets"
    assert not workbooks["Ops Dashboard"]["sheets"], "federated_multi_connection.twb has none"


def test_the_lineage_query_reports_two_downstream_workbooks(served):
    """Driven through the REAL ``tableau_lineage`` client, not asserted about the payload."""
    site, base = served
    session = tl.sign_in(base, site.content_url, "mock-pat", site.pat_credentials["mock-pat"], site.rest_version)
    plan = tl.build_plan(tl.fetch_lineage(session), site.content_url)

    assert [row["name"] for row in plan] == ["Corporate Cities"]
    assert plan[0]["downstream_count"] == 2, "migration ORDER depends on this number"


# ------------------------------------------------------- the real assess run


def test_the_real_assessment_runs_end_to_end_against_the_mock(served, tmp_path):
    """``assess_estate``'s three passes, its scoring, and its SQLite store - all offline."""
    site, base = served
    client = ae.Site(tableau.env_for(site, base))
    client.sign_in()
    raw = ae.collect(client, None)
    assembled = ae.assemble(raw, 0.99)
    db = ae.write_store(tmp_path, raw, assembled)

    assert db.is_file()
    names = {row["name"] for row in assembled["workbooks"]}
    assert names == {"Sales Review", "Ops Dashboard", "Attic Copy"}
    assert assembled["iam_hard_cases"], "a Read/ViewUnderlyingData split is an IAM hard case"


def test_the_estate_db_carries_the_nested_project_tree(served, tmp_path):
    """The deploy step mirrors folders FROM this table, so the nesting has to survive the write."""
    import sqlite3  # noqa: PLC0415

    site, base = served
    client = ae.Site(tableau.env_for(site, base))
    client.sign_in()
    raw = ae.collect(client, None)
    db = ae.write_store(tmp_path, raw, ae.assemble(raw, 0.99))

    with sqlite3.connect(db) as connection:
        rows = dict(connection.execute("select name, parent_luid from project"))
        luids = dict(connection.execute("select name, luid from project"))
    assert rows["Q1.2026"] == luids["Finance"], "Q1.2026 is nested under Finance"
    assert rows["Finance"] is None


# --------------------------------------------------------------- the loud gate


def test_running_an_engine_script_without_the_pat_variable_fails_loudly(monkeypatch):
    """MEASURED, and it cost 13 minutes of a real session.

    ``estate_survey.py`` resolves its secret through ``credential_resolver``, whose last layer is a
    ``getpass`` prompt. With ``TABLEAU_PAT_VALUE`` unset it does not fail - it blocks forever with no
    output. The harness refuses to launch instead, which turns a silent hang into an error.
    """
    with pytest.raises(SystemExit) as raised:
        estate.run_engine_script("estate_survey.py", [], env={"TABLEAU_PAT_SECRET": "ours"})
    assert "TABLEAU_PAT_VALUE" in str(raised.value)


def test_env_for_exports_both_the_ours_and_engine_names(site):
    """Our scripts read ``TABLEAU_PAT_SECRET``; the engine reads ``TABLEAU_PAT_VALUE``.

    The bridge in ``tableau_env.engine_child_env`` only reaches an engine script OUR python spawns,
    so anything else needs both names set explicitly.
    """
    env = tableau.env_for(site, "http://127.0.0.1:1")
    assert env["TABLEAU_PAT_SECRET"] == env["TABLEAU_PAT_VALUE"]
    assert env["TABLEAU_SERVER_URL"].startswith("http://127.0.0.1")


def test_the_mock_never_points_at_a_real_host(site):
    """A guard against the worst possible harness bug: talking to production by accident."""
    env = tableau.env_for(site, "http://127.0.0.1:9")
    assert "127.0.0.1" in env["TABLEAU_SERVER_URL"]
    assert "online.tableau.com" not in json.dumps(env)


def test_the_server_really_is_loopback_only(served):
    site, base = served
    del site
    assert base.startswith("http://127.0.0.1:")
