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
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import assess_estate as ae  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_lineage as tl  # noqa: E402  # pylint: disable=wrong-import-position
from mocks import estate, tableau  # noqa: E402  # pylint: disable=wrong-import-position


@pytest.fixture(name="site")
def _site() -> tableau.TableauSite:
    return estate.build_site()


@pytest.fixture(name="served")
def _served(site):
    with tableau.serve(site) as base:
        yield site, base


def signed_in(site) -> str:
    """Authenticate through the router, retaining the actual sign-in identity assertion."""
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
    credentials = json.loads(payload)["credentials"]
    assert credentials["user"] == {"id": "user-1"}
    return credentials["token"]


def rest_get(site: tableau.TableauSite, path: str, token: str = "") -> tuple[int, dict]:
    """GET through the real router; missing tokens stay missing."""
    status, _headers, payload = site.handle(
        "GET",
        f"http://x/api/{site.rest_version}{path}",
        {"x-tableau-auth": token} if token else {},
        b"",
    )
    return status, json.loads(payload)


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
    """A PAT must not sign into a different site."""
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
    """The assessment client must read every server-capped page."""
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


# -------------------------------------------------------- REST authority subset


def test_user_detail_matches_the_signed_in_admin(site: tableau.TableauSite) -> None:
    """The signed-in identity has the least broad site-admin role admitted by provenance P."""
    status, payload = rest_get(site, f"/sites/{site.site_id}/users/user-1", signed_in(site))
    assert status == 200
    assert payload == {"user": {"id": "user-1", "siteRole": "SiteAdministratorExplorer"}}


@pytest.mark.parametrize("value, count", [("SalesMaster", 1), ("salesmaster", 0), ("SALESMASTER", 0), ("absent", 0)])
def test_datasource_filter_selects_exact_content_url(site: tableau.TableauSite, value: str, count: int) -> None:
    """An exact match or a complete zero, never display-name matching or a case-folded match."""
    shared = site.datasources[0]
    site.datasource("SalesMaster", site.projects[0], estate.FIXTURES / "standalone_datasource.tds", content_url="Other")
    query = urlencode({"filter": f"contentUrl:eq:{value}", "pageSize": 1000, "pageNumber": 1})
    status, payload = rest_get(site, f"/sites/{site.site_id}/datasources?{query}", signed_in(site))
    assert status == 200
    assert payload["pagination"] == {"pageNumber": "1", "pageSize": "1000", "totalAvailable": str(count)}
    rows = payload["datasources"]["datasource"]
    assert len(rows) == count
    if count:
        assert {key: rows[0][key] for key in ("id", "name", "contentUrl", "updatedAt")} == {
            "id": shared.luid,
            "name": "Corporate Cities",
            "contentUrl": "SalesMaster",
            "updatedAt": "2026-02-03T04:05:06Z",
        }


@pytest.mark.parametrize("single_row", [False, True])
def test_filtered_datasources_page_the_filtered_set(site: tableau.TableauSite, single_row: bool) -> None:
    """Filter before slicing; object-shaped single rows keep the same string-valued page totals."""
    first = site.datasources[0]
    other = site.datasource(
        "Other", site.projects[0], estate.FIXTURES / "standalone_datasource.tds", content_url="Other"
    )
    second = site.datasource("Second match", site.projects[0], estate.FIXTURES / "standalone_datasource.tds")
    site.page_size, site.single_row_as_object = 1, single_row
    token = signed_in(site)
    seen = []
    for number, expected in enumerate(([first.luid], [second.luid], []), start=1):
        query = urlencode({"filter": "contentUrl:eq:SalesMaster", "pageSize": 1000, "pageNumber": number})
        status, payload = rest_get(site, f"/sites/{site.site_id}/datasources?{query}", token)
        assert status == 200
        assert payload["pagination"] == {"pageNumber": str(number), "pageSize": "1", "totalAvailable": "2"}
        rows = payload["datasources"]["datasource"]
        assert isinstance(rows, dict) == (single_row and bool(expected))
        rows = [rows] if isinstance(rows, dict) else rows
        assert [row["id"] for row in rows] == expected
        seen.extend(row["id"] for row in rows)
    assert seen == [first.luid, second.luid]

    site.page_size, site.single_row_as_object = None, False
    status, payload = rest_get(site, f"/sites/{site.site_id}/datasources", token)
    assert status == 200
    assert [row["id"] for row in payload["datasources"]["datasource"]] == [first.luid, other.luid, second.luid]
    assert payload["pagination"] == {"pageNumber": "1", "pageSize": "100", "totalAvailable": "3"}


def test_unfiltered_datasources_keep_blank_page_defaults(site: tableau.TableauSite) -> None:
    """Retaining blank filters for rejection must not change the existing blank paging defaults."""
    status, payload = rest_get(site, f"/sites/{site.site_id}/datasources?pageSize=&pageNumber=", signed_in(site))
    assert status == 200
    assert payload["pagination"] == {"pageNumber": "1", "pageSize": "100", "totalAvailable": "1"}
    assert payload["datasources"]["datasource"][0]["id"] == site.datasources[0].luid


@pytest.mark.parametrize(
    "query",
    [
        "filter",
        "filter=",
        "filter=name:eq:SalesMaster",
        "filter=contenturl:eq:SalesMaster",
        "filter=contentUrl:in:SalesMaster",
        "filter=contentUrl:EQ:SalesMaster",
        "filter=contentUrl:eq",
        "filter=contentUrl:eq:",
        "filter=contentUrl:eq:%20",
        "filter=contentUrl:eq:SalesMaster:extra",
        "filter=contentUrl:eq:SalesMaster,contentUrl:eq:Other",
        "filter=contentUrl:eq:SalesMaster%26contentUrl:eq:Other",
        "filter=contentUrl:eq:SalesMaster&filter=contentUrl:eq:Other",
        "filter=contentUrl:eq:SalesMaster&filter=",
        "Filter=contentUrl:eq:SalesMaster",
    ],
)
def test_unsupported_datasource_filters_are_bad_requests(site: tableau.TableauSite, query: str) -> None:
    """Unsupported syntax cannot silently become an unfiltered, apparently authoritative catalog."""
    status, payload = rest_get(site, f"/sites/{site.site_id}/datasources?{query}", signed_in(site))
    assert status == 400
    assert "error" in payload
    assert "datasources" not in payload


def test_datasource_detail_agrees_with_list_and_tracks_current_row(site: tableau.TableauSite) -> None:
    """Detail/list agreement includes current identity and timestamp, not a stale copied row."""
    shared = site.datasources[0]
    token = signed_in(site)
    path = f"/sites/{site.site_id}/datasources"
    for content_url, updated_at in (("SalesMaster", shared.updated_at), ("RenamedMaster", "2026-03-04T05:06:07Z")):
        shared.content_url, shared.updated_at = content_url, updated_at
        status, listing = rest_get(site, path + "?" + urlencode({"filter": f"contentUrl:eq:{content_url}"}), token)
        detail_status, detail = rest_get(site, f"{path}/{shared.luid}", token)
        assert status == detail_status == 200
        row = listing["datasources"]["datasource"][0]
        assert detail == {"datasource": row}
        assert (row["id"], row["contentUrl"], row["updatedAt"]) == (shared.luid, content_url, updated_at)


def test_fixture_content_url_matches_preserved_workbook_authority(site: tableau.TableauSite) -> None:
    """Independently read vendor XML: the synthetic metadata edges are not byte-level authority."""
    fixture_names = ("minimal.twb", "federated_multi_connection.twb", "published_datasource.twb")
    for workbook, fixture_name in zip(site.workbooks, fixture_names, strict=True):
        with zipfile.ZipFile(io.BytesIO(workbook.content)) as archive:
            assert archive.read(fixture_name) == (estate.FIXTURES / fixture_name).read_bytes()
    with zipfile.ZipFile(io.BytesIO(site.workbooks[2].content)) as archive:
        root = ElementTree.fromstring(archive.read("published_datasource.twb"))
    location = root.find("./datasources/datasource/repository-location")
    assert location is not None
    content_url = urlparse(location.attrib["derived-from"]).path.rsplit("/", 1)[1]
    assert content_url == "SalesMaster"
    assert site.datasources[0].row()["contentUrl"] == content_url
    assert location.attrib["id"] != content_url
    assert site.datasources[0].name == "Corporate Cities"
    assert site.datasources[0].downstream == ["Sales Review", "Ops Dashboard"]


@pytest.mark.parametrize(
    "path",
    [
        "/sites/wrong/users/user-1",
        "/sites/{site}/users/wrong",
        "/sites/{site}/users/USER-1",
        "/sites/{site}/users/user-1/extra",
        "/sites/{site}/users//user-1",
        "/sites/wrong/datasources",
        "/sites/wrong/datasources/{luid}",
        "/sites/{site}/datasources/unknown",
        "/sites/{site}/datasources/{upper_luid}",
        "/sites/{site}/datasources/{luid}/extra",
        "/sites/{site}/datasources/{luid}/content/extra",
        "/sites/{site}/datasources//{luid}",
        "/sites/{site}-wrong/datasources/{luid}",
        "/sites/wrong/api/{version}/sites/{site}/users/user-1",
        "/sites/{site}/groups/{luid}/content",
    ],
)
def test_authority_routes_reject_wrong_identity_or_extra_segments(site: tableau.TableauSite, path: str) -> None:
    """Exact site, collection, user/LUID and path arity; no suffix or fallback route."""
    luid = site.datasources[0].luid
    path = path.format(site=site.site_id, luid=luid, upper_luid=luid.upper(), version=site.rest_version)
    status, payload = rest_get(site, path, signed_in(site))
    assert status == 404
    assert "error" in payload


@pytest.mark.parametrize(
    "route", ["users/user-1", "datasources?filter=contentUrl:eq:SalesMaster", "datasources/{luid}"]
)
@pytest.mark.parametrize("state, expected", [("missing", 401), ("invalid", 401), ("expired", 401), ("forbidden", 403)])
def test_authority_routes_keep_auth_failures(site: tableau.TableauSite, route: str, state: str, expected: int) -> None:
    """New authority routes must still enforce the mock's token and permission controls."""
    path = f"/sites/{site.site_id}/" + route.format(luid=site.datasources[0].luid)
    token = signed_in(site)
    if state in {"missing", "invalid"}:
        token = "" if state == "missing" else "not-a-token"
    elif state == "expired":
        site.expire_session()
    else:
        site.forbid(path.split("?")[0])
    status, payload = rest_get(site, path, token)
    assert status == expected
    assert "error" in payload
    if state == "expired":
        assert payload["error"]["code"] == "401002"


@pytest.mark.parametrize("route", ["users/user-1", "datasources", "datasources/{luid}"])
def test_authority_routes_do_not_accept_other_http_methods(site: tableau.TableauSite, route: str) -> None:
    """A valid path must not turn an unsupported method into a successful read."""
    route = route.format(luid=site.datasources[0].luid)
    status, _headers, _body = site.handle(
        "POST",
        f"http://x/api/{site.rest_version}/sites/{site.site_id}/{route}",
        {"x-tableau-auth": signed_in(site)},
        b"",
    )
    assert status == 405


def test_real_client_reaches_user_filtered_list_and_datasource_detail(served) -> None:
    """Unmodified assess client -> urllib -> loopback HTTP -> the same strict router."""
    site, base = served
    shared = site.datasources[0]
    site.datasource("SalesMaster", site.projects[0], estate.FIXTURES / "standalone_datasource.tds", content_url="Other")
    client = ae.Site(tableau.env_for(site, base))
    client.sign_in()
    prefix = f"/sites/{client.site_id}"
    user_path = f"{prefix}/users/user-1"
    list_path = f"{prefix}/datasources?" + urlencode(
        {"filter": "contentUrl:eq:SalesMaster", "pageSize": 1000, "pageNumber": 1}
    )
    detail_path = f"{prefix}/datasources/{shared.luid}"
    assert client.get(user_path) == {"user": {"id": "user-1", "siteRole": "SiteAdministratorExplorer"}}
    listing = client.get(list_path)
    assert listing["pagination"] == {"pageNumber": "1", "pageSize": "1000", "totalAvailable": "1"}
    rows = listing["datasources"]["datasource"]
    assert len(rows) == 1
    assert (rows[0]["id"], rows[0]["contentUrl"], rows[0]["updatedAt"]) == (
        shared.luid,
        "SalesMaster",
        "2026-02-03T04:05:06Z",
    )
    assert client.get(detail_path) == {"datasource": rows[0]}
    missing = client.get(f"{prefix}/datasources?filter=contentUrl:eq:salesmaster&pageSize=1000&pageNumber=1")
    assert missing == {
        "pagination": {"pageNumber": "1", "pageSize": "1000", "totalAvailable": "0"},
        "datasources": {"datasource": []},
    }
    all_rows, error = client.paged(f"{prefix}/datasources", "datasources", "datasource")
    assert error is None
    assert len(all_rows) == 2
    for path in (user_path, list_path, detail_path):
        assert ("GET", f"/api/{site.rest_version}{path}") in site.requests
    client.sign_out()


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
    from parse_tableau import parse_workbook  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    target = tmp_path / "wb.twbx"
    target.write_bytes(site.workbooks[0].content)
    spec = parse_workbook(target)
    assert spec.get("worksheets")


def test_a_missing_luid_is_a_404_not_an_empty_download(site):
    """Content downloads must not manufacture bytes for unknown workbook identities."""
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
    import sqlite3  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

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


def test_running_an_engine_script_without_the_pat_variable_fails_loudly():
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
    """No externally reachable bind address is used by the HTTP fixture."""
    site, base = served
    del site
    assert base.startswith("http://127.0.0.1:")
