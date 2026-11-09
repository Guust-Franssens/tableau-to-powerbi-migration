"""Synthetic evidence, deterministic rendering, refusal and local-browser controls."""

from __future__ import annotations

from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import assessment_report as report  # noqa: E402  # pylint: disable=wrong-import-position
import assess_estate  # noqa: E402  # pylint: disable=wrong-import-position
import provision_tableau_estate  # noqa: E402  # pylint: disable=wrong-import-position
import run_status  # noqa: E402  # pylint: disable=wrong-import-position
import work_dirs  # noqa: E402  # pylint: disable=wrong-import-position

WB = [f"wb-{number}" for number in range(1, 6)]
VIEWS = [f"view-{number}" for number in range(1, 8)]
PROJECT_ADMIN = "project-admin"
PROJECT_CUSTOMER = "project-customer"
DS = ["ds-events", "ds-shared", "ds-other"]
NAMES = ["Book A", "Book B", "Book C", "Book D", "Book E"]
VIEWS_LIFETIME = [500, 300, 100, 60, 40]

EXPECTED_FULL = {
    "counts": {"workbooks": 5, "views": 7, "datasources": 3, "projects": 2},
    "assessed_at": "2026-10-01T10:00:00Z",
    "pareto": {"cut80": "2", "cut99": "5", "points": "5", "lifetime": True},
    "tiers": {"High": 2, "Medium": 2, "Low": 1},
    "activity": {
        "first_day": "2026-09-01",
        "last_day": "2026-09-02",
        "events": "9",
        "unmapped": "1",
        "mapped_rows": 9,
        "daily_days": "2",
        "daily_events": "9",
    },
    "lineage": {"edges": "2", "denominator": "3", "unresolved": "1", "embedded": "1"},
    "complexity": {"points": "5", "understated": "1"},
    "inventory": {"rows": 5, "first_views": "500", "ascending_first": "40"},
    "states": {
        4: ("partial", "lineage_is_partial"),
        6: ("not_delivered", "wave_analysis_deferred"),
        7: ("partial", "permission_mapping_not_delivered"),
    },
    "caveats": ("estate_survey.json", "assessment.json", "estate.db", "admin_insights.json", "not live-qualified"),
}

WEIGHTS = {"sheets": 1.0, "dashboards": 2.0, "calcs": 1.0, "lods": 5.0, "table_calcs": 5.0}


def _collector(*, outcome: str = "available", reason: str | None = None) -> dict:
    rows = []
    for index in range(9):
        rows.append(
            {
                "event_type": "view" if index < 7 else "authoring",
                "event_name": "View" if index < 7 else "Edit",
                "item_type": "Workbook",
                "item_luid": WB[index % 5],
                "workbook_luid": WB[index % 5],
                "day": "2026-09-01" if index < 5 else "2026-09-02",
                "events": 1,
            }
        )
    return {
        "schema": "admin-insights-aggregate/1",
        "outcome": outcome,
        "reason": reason,
        "datasource_luid": DS[0],
        "window": (
            {"first_day": "2026-09-01", "last_day": "2026-09-02"}
            if outcome == "available"
            else {"first_day": None, "last_day": None}
        ),
        "rows": rows if outcome == "available" else [],
        "unmapped_events": 1 if outcome == "available" else 0,
    }


def _write_full(root: Path, *, degraded: bool = False, collector: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    survey_rows = [
        {
            "luid": luid,
            "name": name,
            "published_dependencies": (
                [{"datasource_name": "Shared", "luid": DS[1], "project": "Customers", "status": "resolved"}]
                if index < 2
                else []
            ),
        }
        for index, (luid, name) in enumerate(zip(WB, NAMES, strict=True))
    ]
    survey = {
        "degraded": degraded,
        "workbooks": survey_rows,
        "required_datasources": [{"luid": DS[1], "datasource_name": "Shared", "project": "Customers"}],
        "summary": {"workbooks_total": 5, "degraded": degraded},
    }
    (root / "estate_survey.json").write_text(json.dumps(survey), encoding="utf-8")
    tiers = ["High", "High", "Medium", "Medium", "Low"]
    cumulative = [0.5, 0.8, 0.9, 0.96, 1.0]
    assessed = []
    for index, (luid, name, views, tier, share) in enumerate(
        zip(WB, NAMES, VIEWS_LIFETIME, tiers, cumulative, strict=True), 1
    ):
        assessed.append(
            {
                "luid": luid,
                "name": name,
                "project": "Customers",
                "project_luid": PROJECT_CUSTOMER,
                "rank": index,
                "cumulative_share": share,
                "tier": tier,
                "tier_reason": f"synthetic {tier}",
                "complexity": float(index * 2),
                "complexity_understated": int(index == 1),
                "views_lifetime": views,
            }
        )
    assessment = {
        "workbooks": assessed,
        "listing_errors": ([{"listing": "structure", "severity": "primary", "status": 503}] if degraded else []),
        "degraded": degraded,
        "degraded_primary": degraded,
        "iam_hard_cases": [{"case": "per_view_grants"}, {"case": "explicit_deny"}],
        "datasource_hazards": {"cross_class": [], "duplicate_within_class": []},
        "summary": {
            "workbooks_total": 5,
            "listing_errors": int(degraded),
            "degraded": degraded,
            "degraded_primary": degraded,
        },
    }
    (root / "assessment.json").write_text(json.dumps(assessment), encoding="utf-8")
    if collector is not None:
        (root / "admin_insights.json").write_text(json.dumps(collector), encoding="utf-8")

    connection = sqlite3.connect(root / "estate.db")
    connection.executescript(
        """
        CREATE TABLE workbook (
          luid TEXT PRIMARY KEY, name TEXT, project TEXT, project_luid TEXT, owner_luid TEXT, size_mb INTEGER,
          created_at TEXT, updated_at TEXT, sheets INTEGER, dashboards INTEGER, calcs INTEGER, lods INTEGER,
          table_calcs INTEGER, has_user_reference INTEGER, complexity REAL, complexity_understated INTEGER,
          views_lifetime INTEGER, view_count INTEGER, subscriptions INTEGER, alerts INTEGER, custom_views INTEGER,
          rank INTEGER, cumulative_share REAL, tier TEXT, tier_reason TEXT);
        CREATE TABLE view (luid TEXT PRIMARY KEY, workbook_luid TEXT, name TEXT, content_url TEXT, views_lifetime INTEGER, updated_at TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project TEXT, project_luid TEXT, is_certified INTEGER, has_extracts INTEGER, extract_last_refresh TEXT);
        CREATE TABLE upstream_table (datasource_name TEXT, full_name TEXT);
        CREATE TABLE dependency (workbook_name TEXT, workbook_luid TEXT, datasource_name TEXT, datasource_luid TEXT, source TEXT);
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT, parent_luid TEXT, content_permissions TEXT);
        CREATE TABLE grp (luid TEXT PRIMARY KEY, name TEXT, domain TEXT, members INTEGER);
        CREATE TABLE permission (object_type TEXT, object_luid TEXT, object_name TEXT, grantee_type TEXT, grantee_luid TEXT, capability TEXT, mode TEXT);
        CREATE TABLE permission_collection (denied_listings INTEGER NOT NULL);
        CREATE TABLE flow (luid TEXT PRIMARY KEY, name TEXT, project TEXT);
        CREATE TABLE assessment_run (assessed_at TEXT, degraded INTEGER, degraded_primary INTEGER, workbooks_total INTEGER, listing_errors INTEGER);
        CREATE TABLE listing_error (listing TEXT, severity TEXT, status INTEGER, path TEXT, page INTEGER, attempts INTEGER, elapsed_sec REAL, transport INTEGER, error TEXT);
        """
    )
    connection.execute("INSERT INTO project VALUES (?, ?, NULL, 'ManagedByOwner')", (PROJECT_ADMIN, "Admin Insights"))
    connection.execute("INSERT INTO project VALUES (?, ?, NULL, 'Locked')", (PROJECT_CUSTOMER, "Customers"))
    connection.executemany(
        "INSERT INTO workbook (luid,name,project,project_luid,owner_luid,sheets,dashboards,calcs,lods,table_calcs,"
        "complexity,complexity_understated,views_lifetime,view_count,rank,cumulative_share,tier,tier_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                row["luid"],
                row["name"],
                row["project"],
                row["project_luid"],
                "OWNER-MUST-NOT-RENDER",
                2,
                1,
                3,
                0,
                0,
                row["complexity"],
                row["complexity_understated"],
                row["views_lifetime"],
                1,
                row["rank"],
                row["cumulative_share"],
                row["tier"],
                row["tier_reason"],
            )
            for row in assessed
        ],
    )
    connection.executemany(
        "INSERT INTO view VALUES (?, ?, ?, ?, ?, ?)",
        [
            (VIEWS[index], WB[index % 5], f"View {index + 1}", f"url-{index}", 100 - index, "2026-09-01")
            for index in range(7)
        ],
    )
    connection.executemany(
        "INSERT INTO datasource VALUES (?, ?, ?, ?, 0, 0, NULL)",
        [
            (DS[0], "TS Events", "Admin Insights", PROJECT_ADMIN),
            (DS[1], "Shared", "Customers", PROJECT_CUSTOMER),
            (DS[2], "Other", "Customers", PROJECT_CUSTOMER),
        ],
    )
    connection.executemany(
        "INSERT INTO dependency VALUES (?, ?, ?, ?, ?)",
        [
            ("Book A", WB[0], "Shared", DS[1], "survey"),
            ("Book B", WB[1], "Shared", DS[1], "survey"),
            ("Book C", WB[2], "Unknown", "", "embedded"),
        ],
    )
    connection.executemany("INSERT INTO permission VALUES (NULL, NULL, NULL, NULL, NULL, NULL, NULL)", [(), (), ()])
    connection.execute("INSERT INTO permission_collection VALUES (0)")
    connection.execute(
        "INSERT INTO assessment_run VALUES (?, ?, ?, ?, ?)",
        ("2026-10-01T10:00:00Z", int(degraded), int(degraded), 5, int(degraded)),
    )
    if degraded:
        connection.execute("INSERT INTO listing_error (listing,severity,status) VALUES ('structure','primary',503)")
    connection.commit()
    connection.close()
    return root


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sections: dict[int, tuple[str, str]] = {}
        self.metrics: dict[str, str] = {}
        self.text: list[str] = []
        self.tables: dict[str, list[list[str]]] = {}
        self._section: int | None = None
        self._metric: str | None = None
        self._table: str | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._metric_nested = False
        self._metric_value = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "section":
            self._section = int(values["data-section"])
            self.sections[self._section] = (str(values["data-state"]), str(values["data-reason"]))
        if tag == "div" and values.get("data-metric"):
            self._metric = values["data-metric"]
            self.metrics[self._metric] = ""
            self._metric_nested = False
        if tag == "b" and self._metric is not None:
            self._metric_nested = True
            self._metric_value = True
        if tag == "table" and values.get("id"):
            self._table = values["id"]
            self.tables[self._table] = []
        if tag == "tr" and self._table:
            self._row = []
        if tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._metric is not None and (not self._metric_nested or self._metric_value):
            self.metrics[self._metric] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        if tag == "tr" and self._row is not None and self._table:
            self.tables[self._table].append(self._row)
            self._row = None
        if tag == "table":
            self._table = None
        if tag == "section":
            self._section = None
        if tag == "div":
            self._metric = None
        if tag == "b":
            self._metric_value = False


def _parser_assertions(payload: bytes) -> None:
    parsed = _Page()
    parsed.feed(payload.decode())
    assert set(parsed.sections) == set(range(1, 10))
    assert parsed.sections[1] == ("ok", "")
    assert parsed.sections[3] == ("ok", "")
    assert parsed.metrics["workbooks"] == "5"
    assert parsed.metrics["published views"] == "7"
    assert parsed.metrics["datasources"] == "3"
    assert parsed.metrics["projects"] == "2"
    assert parsed.metrics["permission entries"] == "3"
    assert parsed.metrics["assessed_at"] == EXPECTED_FULL["assessed_at"]
    assert parsed.metrics["cut80"] == EXPECTED_FULL["pareto"]["cut80"]
    assert parsed.metrics["cut99"] == EXPECTED_FULL["pareto"]["cut99"]
    assert parsed.metrics["pareto-points"] == EXPECTED_FULL["pareto"]["points"]
    assert parsed.metrics["first-day"] == EXPECTED_FULL["activity"]["first_day"]
    assert parsed.metrics["last-day"] == EXPECTED_FULL["activity"]["last_day"]
    assert parsed.metrics["event-total"] == EXPECTED_FULL["activity"]["events"]
    assert parsed.metrics["unmapped-events"] == EXPECTED_FULL["activity"]["unmapped"]
    assert parsed.metrics["daily-days"] == EXPECTED_FULL["activity"]["daily_days"]
    assert parsed.metrics["daily-events-total"] == EXPECTED_FULL["activity"]["daily_events"]
    assert parsed.metrics["scatter-points"] == EXPECTED_FULL["complexity"]["points"]
    assert parsed.metrics["understated-count"] == EXPECTED_FULL["complexity"]["understated"]
    assert parsed.metrics["dependency-edges"] == EXPECTED_FULL["lineage"]["edges"]
    assert parsed.metrics["datasource-denominator"] == EXPECTED_FULL["lineage"]["denominator"]
    assert parsed.metrics["unresolved-identities"] == EXPECTED_FULL["lineage"]["unresolved"]
    assert parsed.metrics["embedded-dependencies"] == EXPECTED_FULL["lineage"]["embedded"]
    for section, expected in EXPECTED_FULL["states"].items():
        assert parsed.sections[section] == expected
    text = " ".join(parsed.text)
    for caveat in EXPECTED_FULL["caveats"]:
        assert caveat in text
    for tier, count in EXPECTED_FULL["tiers"].items():
        assert f"{tier}: {count}" in text
    assert "OWNER-MUST-NOT-RENDER" not in payload.decode()
    assert parsed.tables["inventory"][1][2] == EXPECTED_FULL["inventory"]["first_views"]
    assert "INCOMPLETE" not in text


def test_full_evidence_html_is_deterministic_and_matches_expected(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    assert report.main(["--run", str(root)]) == 0
    first = (root / "assessment-report.html").read_bytes()
    assert report.main(["--run", str(root)]) == 0
    assert (root / "assessment-report.html").read_bytes() == first
    assert f'content="{report.evidence_state(root)["evidence_sha256"]}"'.encode() in first
    assert report.evidence_state(root)["report"] == "current"
    _parser_assertions(first)


def test_local_constants_are_pinned_to_existing_producers() -> None:
    assert report.WEIGHTS == WEIGHTS == assess_estate.WEIGHTS
    assert {
        "Admin Insights",
        "Samples",
        "External Assets Default Project",
    } == provision_tableau_estate.TABLEAU_MANAGED_PROJECTS - {"default"}


@pytest.mark.parametrize(
    "collector,expected_admin,expected_reason,expected_exit",
    [
        (_collector(outcome="unavailable", reason="vds_unavailable"), "unavailable", "vds_unavailable", 0),
        (
            _collector(outcome="cannot_establish", reason="transport_failure"),
            "cannot_establish",
            "transport_failure",
            1,
        ),
    ],
)
def test_admin_outcomes_share_reader_and_report_state(
    tmp_path, collector, expected_admin, expected_reason, expected_exit
):
    run = work_dirs.allocate_run("assessment", repo_root=tmp_path, extra_manifest={"intent": "assessment"})
    root = _write_full(run.root / "assessment", collector=collector)
    assert report.main(["--run", str(root)]) == 0
    state = report.evidence_state(root)
    assert state["admin_insights"] == expected_admin
    assert state["admin_insights_reason"] == expected_reason
    assert state["report"] == "current"
    html = (root / "assessment-report.html").read_text(encoding="utf-8")
    section_state = "ok" if expected_admin == "available" else expected_admin
    assert f'data-state="{section_state}" data-reason="{expected_reason}"' in html
    status, exit_code = run_status.build_status(run.root)
    assert status["assessment"]["admin_insights"] == expected_admin
    assert status["assessment"]["admin_insights_reason"] == expected_reason
    assert exit_code == expected_exit


@pytest.mark.parametrize(
    "mutation",
    ["zero_workbooks", "mismatched_count", "bad_flag", "bad_run_count", "mismatched_workbook_fields"],
)
def test_invalid_or_inconsistent_evidence_is_refused_without_html(tmp_path: Path, mutation: str) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    path = root / "assessment.json"
    assessment = json.loads(path.read_text())
    if mutation == "zero_workbooks":
        assessment["workbooks"] = []
        assessment["summary"]["workbooks_total"] = 0
    elif mutation == "mismatched_count":
        assessment["summary"]["workbooks_total"] = 4
    elif mutation == "bad_flag":
        assessment["summary"]["degraded_primary"] = "false"
    elif mutation == "bad_run_count":
        with sqlite3.connect(root / "estate.db") as connection:
            connection.execute("INSERT INTO assessment_run VALUES ('another',0,0,5,0)")
    else:
        assessment["workbooks"][0]["complexity"] += 1
    path.write_text(json.dumps(assessment), encoding="utf-8")
    assert report.main(["--run", str(root)]) == 1
    assert not (root / "assessment-report.html").exists()
    state = report.evidence_state(root)
    assert state["assessment"] == "unrecognized"


def test_primary_degradation_is_partial_and_does_not_make_zero_complexity_a_measurement(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", degraded=True)
    assessment = json.loads((root / "assessment.json").read_text())
    for row in assessment["workbooks"]:
        row["complexity"] = 0
    (root / "assessment.json").write_text(json.dumps(assessment), encoding="utf-8")
    with sqlite3.connect(root / "estate.db") as connection:
        connection.execute("UPDATE workbook SET complexity=0")
    assert report.main(["--run", str(root)]) == 0
    text = (root / "assessment-report.html").read_text()
    assert "INCOMPLETE" in text
    assert 'data-state="partial" data-reason="degraded_primary"' in text
    assert 'data-metric="permission entries" data-completeness="unknown"><b>unknown</b>' in text
    assert "500</td><td>unknown" in text
    assert report.evidence_state(root)["admin_insights_reason"] == "degraded_primary"


def test_secondary_degradation_is_disclosed_and_withholds_no_consumer_claim(tmp_path: Path) -> None:
    run = work_dirs.allocate_run("assessment", repo_root=tmp_path, extra_manifest={"intent": "assessment"})
    root = _write_full(run.root / "assessment", collector=_collector())
    assessment_path = root / "assessment.json"
    assessment = json.loads(assessment_path.read_text())
    assessment["degraded"] = True
    assessment["listing_errors"] = [{"listing": "group_membership", "severity": "secondary", "status": 403}]
    assessment["summary"]["degraded"] = True
    assessment["summary"]["listing_errors"] = 1
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
    with sqlite3.connect(root / "estate.db") as connection:
        connection.execute("UPDATE assessment_run SET degraded=1, listing_errors=1")
        connection.execute(
            "INSERT INTO listing_error (listing, severity, status) VALUES ('group_membership', 'secondary', 403)"
        )

    assert report.main(["--run", str(root)]) == 0
    html = (root / "assessment-report.html").read_text(encoding="utf-8")
    assert "Secondary listing degradation was recorded" in html
    assert 'data-metric="permission entries" data-completeness="ok"><b>3</b>' in html
    assert "No-consumer conclusion unknown" in html
    assert "No-consumer candidates:" not in html
    status, code = run_status.build_status(run.root)
    assert code == 0
    assert status["assessment"]["assessment"] == "established"


def test_failed_grant_collection_marks_zero_permission_rows_as_partial(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    assessment_path = root / "assessment.json"
    assessment = json.loads(assessment_path.read_text())
    grant_error = {"listing": "workbook grants: PRIVATE-OBJECT", "severity": "secondary", "status": 503}
    assessment["listing_errors"] = [grant_error]
    assessment["degraded"] = True
    assessment["summary"]["listing_errors"] = 1
    assessment["summary"]["degraded"] = True
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
    with sqlite3.connect(root / "estate.db") as connection:
        connection.execute("DELETE FROM permission")
        connection.execute("UPDATE assessment_run SET degraded=1, listing_errors=1")
        connection.execute(
            "INSERT INTO listing_error (listing, severity, status) VALUES (?, ?, ?)",
            (grant_error["listing"], grant_error["severity"], grant_error["status"]),
        )

    assert report.main(["--run", str(root)]) == 0
    html = (root / "assessment-report.html").read_text(encoding="utf-8")
    assert 'data-metric="permission entries" data-completeness="partial"><b>0 observed</b>' in html
    assert "permission entries (partial)" in html


def test_auth_denied_grant_listing_makes_permission_count_partial(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    with sqlite3.connect(root / "estate.db") as connection:
        connection.execute("DELETE FROM permission")
        connection.execute("UPDATE permission_collection SET denied_listings=1")

    assert report.main(["--run", str(root)]) == 0
    html = (root / "assessment-report.html").read_text(encoding="utf-8")
    assert 'data-metric="permission entries" data-completeness="partial"><b>0 observed</b>' in html


def test_missing_permission_collection_evidence_is_not_assumed_complete(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    with sqlite3.connect(root / "estate.db") as connection:
        connection.execute("DROP TABLE permission_collection")

    assert report.main(["--run", str(root)]) == 0
    html = (root / "assessment-report.html").read_text(encoding="utf-8")
    assert 'data-metric="permission entries" data-completeness="unknown"><b>unknown</b>' in html


def test_primary_grant_listing_name_is_redacted_from_report(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", degraded=True, collector=_collector())
    private_listing = "workbook grants: PRIVATE-OBJECT"
    assessment_path = root / "assessment.json"
    assessment = json.loads(assessment_path.read_text())
    assessment["listing_errors"] = [{"listing": private_listing, "severity": "primary", "status": 403}]
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
    with sqlite3.connect(root / "estate.db") as connection:
        connection.execute("DELETE FROM listing_error")
        connection.execute(
            "INSERT INTO listing_error (listing, severity, status) VALUES (?, 'primary', 403)", (private_listing,)
        )

    assert report.main(["--run", str(root)]) == 0
    html = (root / "assessment-report.html").read_text(encoding="utf-8")
    assert "PRIVATE-OBJECT" not in html
    assert "permission grants" in html


def test_survey_without_workbook_luid_is_unrecognized(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    survey = json.loads((root / "estate_survey.json").read_text())
    del survey["workbooks"][0]["luid"]
    (root / "estate_survey.json").write_text(json.dumps(survey), encoding="utf-8")
    assert report.main(["--run", str(root)]) == 1
    assert not (root / "assessment-report.html").exists()
    assert report.evidence_state(root)["assessment"] == "unrecognized"


def test_script_shaped_source_text_is_escaped_and_html_is_current(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    path = root / "assessment.json"
    assessment = json.loads(path.read_text())
    assessment["workbooks"][0]["name"] = '<script>alert("x")</script><!--'
    path.write_text(json.dumps(assessment), encoding="utf-8")
    with sqlite3.connect(root / "estate.db") as connection:
        connection.execute("UPDATE workbook SET name=? WHERE luid=?", (assessment["workbooks"][0]["name"], WB[0]))
    assert report.main(["--run", str(root)]) == 0
    rendered = (root / "assessment-report.html").read_text()
    assert "<script>alert" not in rendered
    assert "&lt;script&gt;" in rendered and "&lt;!--" in rendered
    assert report.evidence_state(root)["report"] == "current"


def test_report_digest_detects_stale_bytes_and_status_reads_only(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    assert report.main(["--run", str(root)]) == 0
    before = (root / "assessment-report.html").read_bytes()
    (root / "admin_insights.json").write_text(json.dumps(_collector(outcome="unavailable", reason="vds_unavailable")))
    assert report.evidence_state(root)["report"] == "stale"
    assert (root / "assessment-report.html").read_bytes() == before


def test_prepublication_failure_leaves_no_final_report_or_temporary_file(tmp_path: Path, monkeypatch) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())

    class BrokenOutput:
        def flush(self):
            raise OSError("flush failure")

    old_stdout, old_stderr = sys.stdout, sys.stderr
    monkeypatch.setattr(sys, "stdout", BrokenOutput())
    monkeypatch.setattr(sys, "stderr", old_stderr)

    assert report.main(["--run", str(root)]) == 1
    monkeypatch.setattr(sys, "stdout", old_stdout)
    assert not (root / "assessment-report.html").exists()
    assert not list(root.glob(".assessment-report-*.tmp"))


def test_postreplace_print_and_log_failure_does_not_change_renderer_return(tmp_path: Path, monkeypatch) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    target = root / "assessment-report.html"
    real_replace = os.replace
    old_stdout, old_stderr = sys.stdout, sys.stderr

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

    monkeypatch.setattr(report.os, "replace", replace_and_arm)

    assert report.main(["--run", str(root)]) == 0
    assert target.is_file()
    assert report.evidence_state(root)["report"] == "current"
    monkeypatch.setattr(sys, "stdout", old_stdout)
    monkeypatch.setattr(sys, "stderr", old_stderr)


def test_browser_file_render_and_interactions(tmp_path: Path) -> None:
    root = _write_full(tmp_path / "assessment", collector=_collector())
    assert report.main(["--run", str(root)]) == 0
    try:
        from playwright.sync_api import sync_playwright

        playwright_context = sync_playwright()
        playwright = playwright_context.start()
    except Exception as exc:  # noqa: BLE001 - optional local skip, mandatory CI failure.
        if os.environ.get("T2P_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail(f"Playwright required but unavailable: {type(exc).__name__}: {exc}")
        pytest.skip("Chromium unavailable for optional browser test")
    try:
        browser = playwright.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - optional local skip, mandatory CI failure.
        playwright.stop()
        if os.environ.get("T2P_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail(f"Chromium required but unavailable: {type(exc).__name__}: {exc}")
        pytest.skip("Chromium unavailable for optional browser test")
    requests: list[str] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    try:
        page = browser.new_page()
        page.on(
            "request", lambda request: requests.append(request.url) if not request.url.startswith("file://") else None
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.goto((root / "assessment-report.html").as_uri())
        assert page.locator("section[data-section]").count() == 9
        assert page.locator('[data-metric="event-total"]').inner_text() == EXPECTED_FULL["activity"]["events"]
        assert page.locator('[data-metric="daily-days"]').inner_text() == EXPECTED_FULL["activity"]["daily_days"]
        assert (
            page.locator('[data-metric="unresolved-identities"]').inner_text() == EXPECTED_FULL["lineage"]["unresolved"]
        )
        assert page.locator("#inventory tbody tr").count() == EXPECTED_FULL["inventory"]["rows"]
        page.locator("#sort-views").click()
        assert (
            page.locator("#inventory tbody tr").first.locator("td").nth(2).inner_text()
            == EXPECTED_FULL["inventory"]["ascending_first"]
        )
        page.locator("#tier-filter").select_option("Low")
        assert page.locator("#inventory tbody tr:not([hidden])").count() == EXPECTED_FULL["tiers"]["Low"]
        assert not requests
        assert not page_errors
        assert not console_errors
    finally:
        try:
            browser.close()
        finally:
            playwright.stop()
