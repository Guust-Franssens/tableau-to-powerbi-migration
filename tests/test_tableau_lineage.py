"""Tests for scripts/tableau_lineage.py - the model-first migration ORDER.

Issue #126: step 1 of the documented pipeline (`estate_survey.py`, REST) proved that 10 workbooks
hard-depend on published data sources, and minutes later step 3 (`tableau_lineage.py --plan`,
Metadata API) listed those same certified sources as having no downstream workbooks and said they
"may be abandoned". Acting on that at a customer decision point means migrating consumers before
their data sources, which rebuilds them as EMPTY REPORTS - the exact failure the model-first order
exists to prevent.

Root cause: the Metadata API is structurally blind to `sqlproxy` connections (a workbook embedding a
published data source), and the script had no way to be told otherwise - no `--survey` flag existed.

So these tests gate three separate things, because fixing only the first would leave the tool wrong
in a different way:

1. **Merge + precedence** - a survey edge promotes a data source the Metadata API called an orphan
   into phase 1, and a Metadata-API-only edge is never silently dropped in the other direction.
2. **Attribution** - the printed plan says which system made which claim, and states the precedence
   rule, so an operator reading it can tell.
3. **Wording** - without a survey the tool must not use the word "abandoned" at all; it can only
   support "no downstream usage visible to the Metadata API". The stronger claim needs a COMPLETE
   survey that also found no consumer.

All fixtures use synthetic names on purpose: this is a public repo.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
from tableau_lineage import build_order, build_plan, load_survey, main, print_plan

SALES_DS = "Lakeside Sales (Live Warehouse)"
TRIPS_DS = "Lakeside Trips (Live Lakehouse)"
UNUSED_DS = "Retired Pilot Extract"
REVENUE_WB = "Regional Revenue"
SEGMENTS_WB = "Customer Segments"
TRIPS_WB = "Trip Economics"
ADMIN_DS = "Site Usage"
ADMIN_WB = "Usage Starter"


def _metadata(name: str, downstream: list[str] | None = None, luid: str | None = None) -> dict[str, Any]:
    """One `publishedDatasources` node as the Metadata API returns it."""
    return {
        "id": luid or f"id-{name}",
        "luid": luid or f"luid-{name}",
        "name": name,
        "projectName": "Certified Sources",
        "hasExtracts": False,
        "downstreamWorkbooks": [{"luid": f"wb-{w}", "name": w, "projectName": "Reports"} for w in downstream or []],
    }


def _survey_workbook(name: str, dependencies: list[tuple[str, str | None]]) -> dict[str, Any]:
    """One workbook entry as `estate_survey.py --json` writes it."""
    return {
        "name": name,
        "luid": f"wb-{name}",
        "project": "Reports",
        "published_dependencies": [
            {
                "datasource_name": ds_name,
                "status": "resolved",
                "luid": luid or f"luid-{ds_name}",
                "project": "Certified Sources",
            }
            for ds_name, luid in dependencies
        ],
        "complexity_understated": bool(dependencies),
    }


def _survey_file(tmp_path: Path, workbooks: list[dict[str, Any]], **extra: Any) -> Path:
    """Write an `estate_survey.json` and return its path."""
    required = sorted(
        {dep["datasource_name"] for wb in workbooks for dep in wb["published_dependencies"]},
    )
    payload = {
        "workbooks": workbooks,
        "required_datasources": [
            {"datasource_name": name, "luid": f"luid-{name}", "project": "Certified Sources"} for name in required
        ],
        "unresolved_dependencies": [],
        "fetch_order": [],
        "summary": {"workbooks_total": len(workbooks)},
        "connection_read_errors": [],
        **extra,
    }
    path = tmp_path / "estate_survey.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sqlproxy_estate(tmp_path: Path) -> tuple[list[dict[str, Any]], Path]:
    """The shape of issue #126: the Metadata API sees NONE of the sqlproxy edges the survey saw."""
    datasources = [
        _metadata(SALES_DS),
        _metadata(TRIPS_DS),
        _metadata(UNUSED_DS),
        _metadata(ADMIN_DS, downstream=[ADMIN_WB]),
    ]
    survey = _survey_file(
        tmp_path,
        [
            _survey_workbook(REVENUE_WB, [(SALES_DS, None)]),
            _survey_workbook(SEGMENTS_WB, [(SALES_DS, None)]),
            _survey_workbook(TRIPS_WB, [(TRIPS_DS, None)]),
            _survey_workbook(ADMIN_WB, [(ADMIN_DS, None)]),
            _survey_workbook("Embedded Only", []),
        ],
    )
    return datasources, survey


def _by_name(plan: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in plan}


def _rendered(caplog: pytest.LogCaptureFixture, plan: list[dict[str, Any]], survey: Any = None) -> str:
    """Capture exactly what an operator reads on stdout."""
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="tableau_lineage"):
        print_plan(plan, survey)
    return "\n".join(record.getMessage() for record in caplog.records)


def test_survey_promotes_a_metadata_api_orphan_into_phase_1(tmp_path: Path) -> None:
    """The defect itself: a hard dependency the Metadata API reports as having no consumers.

    Both consumers come from the survey, so the plan must show 2 - not 0 - and the data source must
    appear in phase 1, ahead of the workbooks that bind to it.
    """
    datasources, survey_path = _sqlproxy_estate(tmp_path)
    plan = build_plan(datasources, "", load_survey(survey_path))
    sales = _by_name(plan)[SALES_DS]

    assert sales["downstream_count"] == 2
    assert sales["downstream_workbooks"] == [SEGMENTS_WB, REVENUE_WB]
    assert sales["metadata_count"] == 0
    assert sales["evidence"] == "survey"
    assert [step["name"] for step in build_order(plan) if step["kind"] == "datasource"][:1] == [SALES_DS]


def test_without_a_survey_the_same_estate_still_reports_the_orphans(tmp_path: Path) -> None:
    """The degraded path must stay HONEST, not silently wrong: the edges are simply not visible."""
    datasources, _ = _sqlproxy_estate(tmp_path)
    plan = build_plan(datasources, "")
    assert _by_name(plan)[SALES_DS]["downstream_count"] == 0
    assert _by_name(plan)[SALES_DS]["evidence"] == "none"
    assert _by_name(plan)[ADMIN_DS]["evidence"] == "metadata-api"


def test_survey_only_datasource_absent_from_the_metadata_api_is_still_planned(tmp_path: Path) -> None:
    """A data source the Metadata API never listed must not fall out of the plan.

    A required data source missing from the sequence is how its consumer gets rebuilt first - the
    empty-report failure this script exists to prevent - so absence from one source cannot delete it.
    """
    survey_path = _survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])])
    plan = build_plan([_metadata(ADMIN_DS, downstream=[ADMIN_WB])], "", load_survey(survey_path))

    sales = _by_name(plan)[SALES_DS]
    assert sales["downstream_workbooks"] == [REVENUE_WB]
    assert sales["luid"] == f"luid-{SALES_DS}"
    assert sales["project"] == "Certified Sources"


def test_a_metadata_only_edge_is_kept_never_dropped(tmp_path: Path) -> None:
    """Precedence settles the CLAIM; it does not license DELETING an edge.

    The survey wins where the two disagree, but "wins" must not mean discarding a consumer only the
    Metadata API saw - that is the same defect pointing the other way, and it would drop a real
    dependency from the order.
    """
    datasources = [_metadata(SALES_DS, downstream=[SEGMENTS_WB])]
    survey_path = _survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])])
    sales = _by_name(build_plan(datasources, "", load_survey(survey_path)))[SALES_DS]

    assert sales["downstream_workbooks"] == [SEGMENTS_WB, REVENUE_WB]
    assert sales["metadata_only"] == [SEGMENTS_WB]
    assert sales["survey_only"] == [REVENUE_WB]
    assert sales["edge_origin"] == {SEGMENTS_WB: "metadata-api", REVENUE_WB: "survey"}


def test_an_edge_both_sources_saw_is_labelled_both(tmp_path: Path) -> None:
    """Agreement has to be visible too, or 'survey' would look like the only trustworthy label."""
    datasources = [_metadata(ADMIN_DS, downstream=[ADMIN_WB])]
    survey_path = _survey_file(tmp_path, [_survey_workbook(ADMIN_WB, [(ADMIN_DS, None)])])
    admin = _by_name(build_plan(datasources, "", load_survey(survey_path)))[ADMIN_DS]

    assert admin["evidence"] == "both"
    assert admin["edge_origin"] == {ADMIN_WB: "both"}
    assert not admin["survey_only"] and not admin["metadata_only"]


def test_a_renamed_datasource_is_matched_by_luid(tmp_path: Path) -> None:
    """Matching by name alone would double-count a data source renamed since the survey ran."""
    datasources = [_metadata("Lakeside Sales (renamed)", luid=f"luid-{SALES_DS}")]
    survey_path = _survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])])
    plan = build_plan(datasources, "", load_survey(survey_path))

    assert len(plan) == 1
    assert plan[0]["downstream_workbooks"] == [REVENUE_WB]


def test_migration_order_places_every_datasource_before_its_consumers(tmp_path: Path) -> None:
    """The ordering guarantee, asserted on the emitted sequence rather than on two phase headings."""
    datasources, survey_path = _sqlproxy_estate(tmp_path)
    plan = build_plan(datasources, "", load_survey(survey_path))
    order = build_order(plan)
    position = {(step["kind"], step["name"]): i for i, step in enumerate(order)}

    for entry in plan:
        for workbook in entry["downstream_workbooks"]:
            assert position[("datasource", entry["name"])] < position[("workbook", workbook)], (
                f"{entry['name']} must be migrated before {workbook}"
            )
    assert ("workbook", REVENUE_WB) in position
    assert ("datasource", UNUSED_DS) not in position


def test_the_printed_plan_states_the_precedence_rule_and_attributes_each_edge(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator must be able to see WHICH source produced WHICH claim, without re-running it."""
    datasources, survey_path = _sqlproxy_estate(tmp_path)
    survey = load_survey(survey_path)
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert "PRECEDENCE: where the survey and the Metadata API disagree, the SURVEY WINS" in output
    assert "SOURCES: Metadata API (GraphQL) + survey" in output
    assert "DISAGREEMENTS" in output
    assert f"{SALES_DS}: Metadata API saw 0 consumer(s), survey saw 2" in output
    assert f"-> {REVENUE_WB:<44} [survey]" in output
    assert f"-> {ADMIN_WB:<44} [both]" in output


def test_the_precedence_rule_is_stated_even_when_the_two_sources_agree(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The rule is what tells a reader how to weigh the plan, so it cannot depend on a conflict.

    An estate where nothing disagrees today still needs the rule printed, otherwise the only place
    it appears is the disagreement block that this estate does not have.
    """
    datasources = [_metadata(ADMIN_DS, downstream=[ADMIN_WB])]
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(ADMIN_WB, [(ADMIN_DS, None)])]))
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert "PRECEDENCE: where the survey and the Metadata API disagree, the SURVEY WINS" in output
    assert "agree on every data source" in output
    assert "DISAGREEMENTS" not in output


def test_headline_counts_come_from_the_merged_graph(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The summary line is what gets quoted at a decision point, so it must reflect the merge."""
    datasources, survey_path = _sqlproxy_estate(tmp_path)
    survey = load_survey(survey_path)
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert "3 published data source(s) feed 4 workbook(s). 1 are SHARED by more than one workbook." in output
    assert "the Metadata API alone saw 1 data source(s) feeding 1 workbook(s); the survey raised that to 3 and 4."


def test_without_a_survey_the_output_never_says_abandoned(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The wording guard. Without a survey the tool cannot support the word at all.

    The Metadata API's silence about a data source is not a statement about usage, because it cannot
    see sqlproxy connections in the first place. The claim it CAN support is the weaker one.
    """
    datasources, _ = _sqlproxy_estate(tmp_path)
    output = _rendered(caplog, build_plan(datasources, ""))

    assert "abandon" not in output.lower()
    assert "no downstream usage VISIBLE TO THE METADATA API" in output
    assert "NO --survey WAS SUPPLIED" in output
    assert "python scripts/tableau_lineage.py --plan --survey" in output


def test_with_a_complete_survey_a_genuine_orphan_may_be_called_abandoned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With both sources agreeing there is no consumer, the stronger claim IS supported."""
    datasources, survey_path = _sqlproxy_estate(tmp_path)
    survey = load_survey(survey_path)
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert "NO downstream workbooks in EITHER source" in output
    assert "these may be abandoned" in output
    orphans = output.split("EITHER source")[1]
    assert UNUSED_DS in orphans
    assert SALES_DS not in orphans


def test_an_incomplete_survey_withholds_the_stronger_claim(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A survey that could not read every workbook cannot prove a data source is unused.

    The workbook whose connections failed to read may be the very consumer, so "abandoned" is
    withheld and the gap is named instead.
    """
    datasources, _ = _sqlproxy_estate(tmp_path)
    survey_path = _survey_file(
        tmp_path,
        [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])],
        connection_read_errors=[{"workbook": "Unreadable", "error": "403"}],
    )
    survey = load_survey(survey_path)
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert not survey.complete
    assert "abandon" not in output.lower()
    assert "UNCONFIRMED" in output
    assert "1 workbook connection(s) could not be read" in output


def test_an_unresolved_dependency_also_counts_as_a_gap(tmp_path: Path) -> None:
    """A dependency the survey could not resolve leaves a hole in the graph; say so."""
    workbook = _survey_workbook(REVENUE_WB, [(SALES_DS, None)])
    workbook["published_dependencies"][0]["status"] = "ambiguous"
    survey = load_survey(_survey_file(tmp_path, [workbook]))

    assert not survey.complete
    assert "did not resolve" in " ".join(survey.gaps)


def test_a_survey_whose_schema_moved_raises_instead_of_reporting_no_dependencies(tmp_path: Path) -> None:
    """Silently parsing zero edges would re-create the defect under a green run.

    "No edges parsed" and "this estate has no dependencies" are indistinguishable downstream, and
    one of them sequences the migration wrong - so refuse rather than guess.
    """
    path = tmp_path / "estate_survey.json"
    path.write_text(
        json.dumps({"workbooks": [{"name": REVENUE_WB, "published_dependencies": [{"datasource": SALES_DS}]}]}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="schema has changed"):
        load_survey(path)


def test_a_file_that_is_not_a_survey_is_rejected(tmp_path: Path) -> None:
    """Pointing --survey at the wrong JSON must fail loudly, not plan from an empty graph."""
    path = tmp_path / "lineage.json"
    path.write_text(json.dumps({"site": "", "datasources": []}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not look like estate_survey"):
        load_survey(path)


def test_an_estate_with_no_dependencies_at_all_stays_quiet(tmp_path: Path) -> None:
    """A survey that genuinely found nothing is legal and must not raise."""
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook("Embedded Only", [])]))
    assert survey.complete
    assert build_plan([], "", survey) == []


def test_cli_runs_offline_with_from_json_plus_survey(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """End to end through argv, the way the runbook invokes it - no server, no credentials."""
    datasources, survey_path = _sqlproxy_estate(tmp_path)
    lineage = tmp_path / "lineage.json"
    lineage.write_text(json.dumps({"site": "", "datasources": datasources}), encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="tableau_lineage"):
        exit_code = main(["--plan", "--from-json", str(lineage), "--survey", str(survey_path)])
    output = "\n".join(record.getMessage() for record in caplog.records)

    assert exit_code == 0
    assert "SURVEY WINS" in output
    assert f"-> {REVENUE_WB:<44} [survey]" in output


def test_cli_fails_loudly_when_the_survey_cannot_be_read(tmp_path: Path) -> None:
    """A --survey that will not load must not degrade to the plan the operator asked to avoid."""
    lineage = tmp_path / "lineage.json"
    lineage.write_text(json.dumps({"site": "", "datasources": []}), encoding="utf-8")
    missing = tmp_path / "nope.json"

    assert main(["--plan", "--from-json", str(lineage), "--survey", str(missing)]) == 1
