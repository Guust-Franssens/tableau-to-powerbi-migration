"""Loopback-only controls for the assumed aggregate Admin Insights contract."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import assess_estate  # noqa: E402  # pylint: disable=wrong-import-position
import admin_insights_collector as collector  # noqa: E402  # pylint: disable=wrong-import-position
from mocks.tableau import TableauSite, serve  # noqa: E402  # pylint: disable=wrong-import-position

PAT = "SYNTHETIC_PAT_SENTINEL"
WORKBOOK = "workbook-1"
VIEW = "view-1"
DATASOURCE = "datasource-1"
FIELDS = ["item_luid", "event_type", "event_name", "item_type", "day"]
EVENT = {
    "item_luid": VIEW,
    "event_type": "view",
    "event_name": "View",
    "item_type": "Workbook",
    "day": "2026-09-01",
    "events": 2,
    "actor_email": "MUST-NOT-PERSIST",
    "extra_response_column": "MUST-NOT-PERSIST",
}


def _db(path: Path, *, include_datasource: bool = True, duplicate: bool = False, nested: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE project (luid TEXT, name TEXT, parent_luid TEXT);"
        "CREATE TABLE datasource (luid TEXT, name TEXT, project_luid TEXT);"
        "CREATE TABLE view (luid TEXT, workbook_luid TEXT);"
    )
    connection.execute("INSERT INTO project VALUES ('admin', 'Admin Insights', NULL)")
    connection.execute("INSERT INTO project VALUES ('customer', 'Customers', NULL)")
    if nested:
        connection.execute("INSERT INTO project VALUES ('nested', 'Admin Insights', 'customer')")
    if include_datasource:
        connection.execute("INSERT INTO datasource VALUES (?, 'TS Events', 'admin')", (DATASOURCE,))
    if duplicate:
        connection.execute("INSERT INTO datasource VALUES ('datasource-2', 'TS Events', 'admin')")
    if nested:
        connection.execute("INSERT INTO datasource VALUES ('nested-events', 'TS Events', 'nested')")
    connection.execute("INSERT INTO view VALUES (?, ?)", (VIEW, WORKBOOK))
    connection.commit()
    connection.close()


def _site(*, vds: dict | None = None, nested: bool = False) -> TableauSite:
    site = TableauSite(content_url="synthetic", vds=vds)
    site.project("Admin Insights")
    site.project("Customers")
    if nested:
        site.project("Admin Insights", parent=site.projects[1])
    return site


def _run(
    site: TableauSite, database: Path, root: Path, monkeypatch, *, pat: str = PAT, valid_pat: bool = True
) -> tuple[int, dict, str, str]:
    secret_name = "mock-pat"
    if valid_pat:
        site.pat_credentials[secret_name] = pat
    with serve(site) as url:
        for key, value in {
            "TABLEAU_SERVER_URL": url,
            "TABLEAU_SITE": site.content_url,
            "TABLEAU_PAT_NAME": secret_name,
            "TABLEAU_PAT_SECRET": pat,
            "TABLEAU_REST_API_VERSION": site.rest_version,
        }.items():
            monkeypatch.setenv(key, value)
        out = root / "admin_insights.json"
        code = collector.main(["--env", str(root / "absent.env"), "--db", str(database), "--out", str(out)])
    return code, json.loads(out.read_text()), out.read_text(), str(out)


def test_available_query_uses_allowlist_and_luid_join_only(tmp_path: Path, monkeypatch, capsys) -> None:
    database = tmp_path / "estate.db"
    _db(database)
    site = _site(
        vds={
            "datasource_luid": DATASOURCE,
            "fields": FIELDS,
            "rows": [EVENT, {**EVENT, "item_luid": "no-view", "events": 3}],
        }
    )
    code, value, text, output = _run(site, database, tmp_path, monkeypatch)
    captured = capsys.readouterr()
    assert code == 0
    assert value["outcome"] == "available" and value["reason"] is None
    mapped = next(row for row in value["rows"] if row["item_luid"] == VIEW)
    unmapped = next(row for row in value["rows"] if row["item_luid"] == "no-view")
    assert mapped["workbook_luid"] == WORKBOOK
    assert unmapped["workbook_luid"] is None
    assert value["unmapped_events"] == 3
    assert set(value) == {"schema", "outcome", "reason", "datasource_luid", "window", "rows", "unmapped_events"}
    assert set(value["rows"][0]) == {
        "event_type",
        "event_name",
        "item_type",
        "item_luid",
        "workbook_luid",
        "day",
        "events",
    }
    assert len(site.vds_requests) == 2
    assert [row[1] for row in site.vds_requests] == [
        "/api/v1/vizql-data-service/metadata",
        "/api/v1/vizql-data-service/query",
    ]
    assert all(
        "actor" not in json.dumps(row[2]).lower() and "email" not in json.dumps(row[2]).lower()
        for row in site.vds_requests
    )
    assert "actor_email" not in text and "MUST-NOT-PERSIST" not in text and PAT not in text
    assert PAT not in captured.out + captured.err
    assert captured.out == captured.err == ""
    assert Path(output).is_file()
    assert [path for _method, path in site.requests if "vizql-data-service" in path] == [
        "/api/v1/vizql-data-service/metadata",
        "/api/v1/vizql-data-service/query",
    ]


def test_credential_reflection_is_refused_before_persisting(tmp_path, monkeypatch, capsys) -> None:
    database = tmp_path / "estate.db"
    _db(database)
    site = _site(
        vds={
            "datasource_luid": DATASOURCE,
            "fields": FIELDS,
            "rows": [{**EVENT, "extra_response_column": PAT}],
        }
    )

    code, value, text, output = _run(site, database, tmp_path, monkeypatch)
    captured = capsys.readouterr()

    assert code == 3
    assert value["outcome"] == "cannot_establish" and value["reason"] == "malformed_response"
    assert len(site.vds_requests) == 2
    assert PAT not in text + captured.out + captured.err + Path(output).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "status,route,expected,code,requests",
    [
        (403, "metadata_status", "permission_denied", 0, 1),
        (404, "metadata_status", "vds_unavailable", 0, 1),
        (401, "metadata_status", "transport_failure", 3, 1),
        (429, "metadata_status", "transport_failure", 3, 1),
        (500, "metadata_status", "transport_failure", 3, 1),
        (403, "query_status", "permission_denied", 0, 2),
        (404, "query_status", "vds_unavailable", 0, 2),
        (401, "query_status", "transport_failure", 3, 2),
        (429, "query_status", "transport_failure", 3, 2),
        (500, "query_status", "transport_failure", 3, 2),
    ],
)
def test_vds_status_mapping_has_no_retry(tmp_path, monkeypatch, status, route, expected, code, requests):
    database = tmp_path / "estate.db"
    _db(database)
    site = _site(vds={"datasource_luid": DATASOURCE, "fields": FIELDS, "rows": [EVENT], route: status})
    actual, value, _text, _output = _run(site, database, tmp_path, monkeypatch)
    assert (actual, value["outcome"], value["reason"]) == (
        code,
        "unavailable" if code == 0 else "cannot_establish",
        expected,
    )
    assert len(site.vds_requests) == requests
    assert len([row for row in site.requests if row[1].endswith("/auth/signin")]) == 1


@pytest.mark.parametrize(
    "include,duplicate,reason,code,expected_requests",
    [
        (False, False, "datasource_not_found", 0, 0),
        (True, True, "ambiguous_datasource", 3, 0),
    ],
)
def test_datasource_identity_refuses_missing_or_ambiguous_without_vds(
    tmp_path, monkeypatch, include, duplicate, reason, code, expected_requests
):
    database = tmp_path / "estate.db"
    _db(database, include_datasource=include, duplicate=duplicate)
    site = _site(vds={"datasource_luid": DATASOURCE, "fields": FIELDS, "rows": [EVENT]})
    actual, value, _text, _output = _run(site, database, tmp_path, monkeypatch)
    assert (actual, value["outcome"], value["reason"]) == (
        code,
        "unavailable" if code == 0 else "cannot_establish",
        reason,
    )
    assert len(site.vds_requests) == expected_requests
    assert len([row for row in site.requests if "vizql-data-service" in row[1]]) == expected_requests


def test_nested_admin_insights_does_not_create_ambiguous_identity(tmp_path, monkeypatch) -> None:
    database = tmp_path / "estate.db"
    _db(database, nested=True)
    site = _site(vds={"datasource_luid": DATASOURCE, "fields": FIELDS, "rows": [EVENT]}, nested=True)
    code, result, _text, _output = _run(site, database, tmp_path, monkeypatch)
    assert code == 0 and result["datasource_luid"] == DATASOURCE
    assert len(site.vds_requests) == 2


def test_required_metadata_fields_missing_skips_query(tmp_path, monkeypatch) -> None:
    database = tmp_path / "estate.db"
    _db(database)
    site = _site(vds={"datasource_luid": DATASOURCE, "fields": ["item_luid", "day"], "rows": [EVENT]})
    code, result, _text, _output = _run(site, database, tmp_path, monkeypatch)
    assert code == 0 and result["reason"] == "required_fields_missing"
    assert result["outcome"] == "unavailable"
    assert len(site.vds_requests) == 1
    assert site.vds_requests[0][1].endswith("/metadata")


@pytest.mark.parametrize("where", ["metadata", "query"])
def test_malformed_vds_response_is_cannot_establish(tmp_path, monkeypatch, where) -> None:
    database = tmp_path / "estate.db"
    _db(database)
    response_key = "metadata_response" if where == "metadata" else "query_response"
    bad_response = {"fields": "not-a-list"} if where == "metadata" else {"rows": [None]}
    site = _site(
        vds={
            "datasource_luid": DATASOURCE,
            "fields": FIELDS,
            "rows": [EVENT],
            response_key: bad_response,
        }
    )
    code, result, _text, _output = _run(site, database, tmp_path, monkeypatch)
    assert code == 3
    assert result["outcome"] == "cannot_establish" and result["reason"] == "malformed_response"
    assert len(site.vds_requests) == (1 if where == "metadata" else 2)


def test_forbid_precedes_vds_and_default_mock_route_is_404(tmp_path, monkeypatch) -> None:
    database = tmp_path / "estate.db"
    _db(database)
    site = _site(vds=None)
    code, result, _text, _output = _run(site, database, tmp_path, monkeypatch)
    assert code == 0 and result["reason"] == "vds_unavailable"
    assert len([row for row in site.requests if "vizql-data-service" in row[1]]) == 1

    site = _site(vds={"datasource_luid": DATASOURCE, "fields": FIELDS, "rows": [EVENT]})
    site.forbid("vizql-data-service")
    code, result, _text, _output = _run(site, database, tmp_path, monkeypatch)
    assert code == 0 and result["reason"] == "permission_denied"
    assert not site.vds_requests
    assert len([row for row in site.requests if "vizql-data-service" in row[1]]) == 1


def test_mock_rejects_wrong_luid_method_and_missing_token(tmp_path) -> None:
    site = _site(vds={"datasource_luid": DATASOURCE, "fields": FIELDS, "rows": [EVENT]})
    token = site.mint_token()
    base = "http://127.0.0.1/api/v1/vizql-data-service/metadata"
    assert (
        site.handle("POST", base, {"x-tableau-auth": token}, json.dumps({"datasource_luid": "wrong"}).encode())[0]
        == 404
    )
    assert site.handle("GET", base, {"x-tableau-auth": token}, b"")[0] == 405
    assert site.handle("POST", base, {}, json.dumps({"datasource_luid": DATASOURCE}).encode())[0] == 401
    assert len(site.vds_requests) == 1


def test_sign_in_failure_is_classified_without_retry(tmp_path, monkeypatch) -> None:
    database = tmp_path / "estate.db"
    _db(database)
    site = _site(vds={"datasource_luid": DATASOURCE, "fields": FIELDS, "rows": [EVENT]})
    code, result, _text, _output = _run(site, database, tmp_path, monkeypatch, pat="wrong-secret", valid_pat=False)
    assert code == 3 and result["outcome"] == "cannot_establish" and result["reason"] == "sign_in_failed"
    assert len([row for row in site.requests if row[1].endswith("/auth/signin")]) == 1
    assert not site.vds_requests


def test_response_size_cap() -> None:
    with pytest.raises(ValueError, match="response_too_large"):
        collector._payload(b"x" * (collector.MAX_RESPONSE_BYTES + 1))


def test_prepublication_failure_leaves_no_final_collector_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / "admin_insights.json"

    class BrokenOutput:
        def flush(self):
            raise OSError("flush failure")

    old_stdout, old_stderr = sys.stdout, sys.stderr
    monkeypatch.setattr(sys, "stdout", BrokenOutput())
    monkeypatch.setattr(sys, "stderr", old_stderr)
    with pytest.raises(OSError):
        collector._publish(target, {"schema": collector.SCHEMA}, 0)
    monkeypatch.setattr(sys, "stdout", old_stdout)
    assert not target.exists()
    assert not list(tmp_path.glob(".admin-insights-*.tmp"))


def test_postreplace_print_failure_does_not_change_collector_return(tmp_path, monkeypatch) -> None:
    code = 3
    target = tmp_path / "admin_insights.json"
    real_replace = os.replace

    class BrokenOutput:
        def flush(self):
            raise OSError("postpublication flush failure")

    class BrokenHandler(logging.Handler):
        def flush(self):
            raise OSError("postpublication log failure")

    def replace_and_arm(source, destination):
        real_replace(source, destination)
        monkeypatch.setattr(sys, "stdout", BrokenOutput())
        monkeypatch.setattr(sys, "stderr", BrokenOutput())
        monkeypatch.setattr(logging.getLogger(), "handlers", [BrokenHandler()])

    monkeypatch.setattr(collector.os, "replace", replace_and_arm)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    assert collector._publish(target, {"schema": collector.SCHEMA}, code) == code
    assert target.is_file()
    assert sys.stdout is not old_stdout
    assert sys.stderr is not old_stderr
    monkeypatch.setattr(sys, "stdout", old_stdout)
    monkeypatch.setattr(sys, "stderr", old_stderr)


@pytest.mark.parametrize(("forbid_metadata", "expected_exit"), [(True, 3), (False, 0)])
def test_assessor_metadata_failure_keeps_rest_workbook(
    tmp_path: Path, monkeypatch, forbid_metadata: bool, expected_exit: int
) -> None:
    site = TableauSite(content_url="synthetic")
    project = site.project("Customers")
    workbook = site.workbook("Synthetic workbook", project, ROOT / "tests" / "fixtures" / "minimal.twb")
    if forbid_metadata:
        site.forbid("/api/metadata/graphql")

    survey_path = tmp_path / "estate_survey.json"
    survey_path.write_text(
        json.dumps(
            {
                "degraded": False,
                "workbooks": [{"luid": workbook.luid, "name": workbook.name, "published_dependencies": []}],
                "required_datasources": [],
                "summary": {"workbooks_total": 1, "degraded": False},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "assessment"
    secret = "synthetic-only-secret"

    with serve(site) as url:
        env = {
            "TABLEAU_SERVER_URL": url,
            "TABLEAU_SITE": site.content_url,
            "TABLEAU_PAT_NAME": "mock-pat",
            "TABLEAU_PAT_SECRET": secret,
            "TABLEAU_REST_API_VERSION": site.rest_version,
        }
        site.pat_credentials["mock-pat"] = secret
        monkeypatch.setattr(assess_estate, "resolve_env", lambda _path: dict(env))
        monkeypatch.setattr(assess_estate, "env_source", lambda *_args: "test")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "assess_estate.py",
                "--env",
                str(tmp_path / "absent.env"),
                "--out",
                str(output),
                "--survey",
                str(survey_path),
            ],
        )
        assert assess_estate.main() == expected_exit

    assessment = json.loads((output / "assessment.json").read_text(encoding="utf-8"))
    assert assessment["summary"]["workbooks_total"] == 1
    assert assessment["workbooks"][0]["luid"] == workbook.luid
    assert assessment["summary"]["degraded_primary"] is forbid_metadata
    assert any(path.endswith("/api/metadata/graphql") for _method, path in site.requests)
