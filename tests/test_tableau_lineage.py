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
4. **Completeness** - "complete" must be read from EVERY signal `estate_survey.py` publishes about
   itself (`degraded`, `listing_errors`, per-workbook `dependencies_unknown`, an empty or truncated
   workbook list), not the two that were convenient. A blind review found that a survey with
   `degraded: true` and a failed site listing - workbooks MISSING from it entirely - still reported
   `complete`, so the "abandoned" claim came back with MORE authority than issue #126's original
   ("Both the Metadata API and the survey found no consumer"). The missing workbook is exactly the
   consumer that would have disproved it.
5. **Identity** - a LUID is an identity and a name is not. Both matching paths are exercised,
   because the engine emits `luid: ""` for every dependency it could not resolve, which makes the
   name fallback the one carrying those edges in production.

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
SHARED_DS = "Quarterly Figures"
FINANCE_WB = "Finance Report"
MARKETING_WB = "Marketing Report"


def _metadata(
    name: str,
    downstream: list[str] | None = None,
    luid: str | None = None,
    project: str = "Certified Sources",
) -> dict[str, Any]:
    """One `publishedDatasources` node as the Metadata API returns it."""
    return {
        "id": luid or f"id-{name}",
        "luid": luid or f"luid-{name}",
        "name": name,
        "projectName": project,
        "hasExtracts": False,
        "downstreamWorkbooks": [{"luid": f"wb-{w}", "name": w, "projectName": "Reports"} for w in downstream or []],
    }


def _survey_workbook(
    name: str,
    dependencies: list[tuple[str, str | None]],
    project: str = "Certified Sources",
    unknown: bool = False,
    candidates: dict[str, list[tuple[str, str]]] | None = None,
) -> dict[str, Any]:
    """One workbook entry as `estate_survey.py --json` writes it.

    A dependency row is `dict(dep)` updated with `resolve_dependency`'s return, so the three shapes
    it can take are reproduced exactly:

    * RESOLVED  -> `luid`/`project` filled, `candidates` holding the ONE match;
    * AMBIGUOUS -> `luid: ""` AND `project: ""` (it "NEVER picks one"), `candidates` naming every
      data source that shares the name;
    * NOT_FOUND -> `luid: ""`, `project: ""`, `candidates: []`.

    Blanking `project` alongside `luid` is the detail a fixture is most tempted to skip, and it is
    load-bearing: on real output the ambiguity is visible ONLY in `candidates`, so a fixture that
    leaves `project` populated tests a shape the engine cannot emit.
    """
    matches = candidates or {}
    deps: list[dict[str, Any]] = []
    for ds_name, luid in dependencies:
        found = [
            {"luid": c_luid, "name": ds_name, "project": c_project} for c_luid, c_project in matches.get(ds_name, [])
        ]
        if luid is None or luid:
            resolved_luid = f"luid-{ds_name}" if luid is None else luid
            deps.append(
                {
                    "datasource_name": ds_name,
                    "status": "resolved",
                    "luid": resolved_luid,
                    "project": project,
                    "candidates": found or [{"luid": resolved_luid, "name": ds_name, "project": project}],
                }
            )
            continue
        deps.append(
            {
                "datasource_name": ds_name,
                "status": "ambiguous" if found else "not_found",
                "luid": "",
                "project": "",
                "candidates": found,
            }
        )
    return {
        "name": name,
        "luid": f"wb-{name}",
        "project": "Reports",
        "published_dependencies": deps,
        # `build_survey` stamps both of these on every workbook row; the fixtures carry them so a
        # gap check that reads them is exercised against the shape the engine actually writes.
        "dependencies_unknown": unknown,
        "complexity_understated": bool(dependencies) or unknown,
    }


def _survey_file(tmp_path: Path, workbooks: list[dict[str, Any]], **extra: Any) -> Path:
    """Write an `estate_survey.json` and return its path.

    The defaults mirror a run of `estate_survey.py::survey_site` field for field - including
    `degraded`, `listing_errors` and the `summary` counters, and including the rule that only a
    RESOLVED dependency reaches `required_datasources` while every other one is echoed in
    `unresolved_dependencies`. Fixtures that omitted those are how the degraded-survey hole got
    through review the first time: a gap check cannot be tested against a flag no fixture ever sets.
    """
    deps = [(wb, dep) for wb in workbooks for dep in wb["published_dependencies"]]
    required: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for workbook, dep in deps:
        if dep["status"] == "resolved":
            required.setdefault(
                dep["datasource_name"],
                {"datasource_name": dep["datasource_name"], "luid": dep["luid"], "project": dep["project"]},
            )
        else:
            unresolved.append(
                {
                    "workbook": workbook["name"],
                    "datasource_name": dep["datasource_name"],
                    "status": dep["status"],
                    "candidates": dep["candidates"],
                }
            )
    payload: dict[str, Any] = {
        "workbooks": workbooks,
        "required_datasources": [required[name] for name in sorted(required)],
        "unresolved_dependencies": unresolved,
        "fetch_order": [],
        "connection_read_errors": [],
        "listing_errors": [],
        "degraded": False,
        "summary": {
            "workbooks_total": len(workbooks),
            "required_datasources": len(required),
            "unresolved_dependencies": len(unresolved),
            "connection_read_errors": 0,
            "listing_errors": 0,
            "dependencies_unknown": sum(1 for wb in workbooks if wb.get("dependencies_unknown")),
            "degraded": False,
        },
        **extra,
    }
    path = tmp_path / "estate_survey.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# `build_survey()` alone returns these four summary keys and nothing else - verified against engine
# 2.126.0. `survey_site()` is what adds `degraded`, `listing_errors`, `connection_read_errors` and
# `summary.dependencies_unknown` on top.
_OFFLINE_SUMMARY_KEYS = frozenset(
    {"workbooks_total", "workbooks_with_published_dependency", "required_datasources", "unresolved_dependencies"}
)


def _offline_survey(tmp_path: Path, workbooks: list[dict[str, Any]], **extra: Any) -> Path:
    """A survey as `build_survey()` writes it WITHOUT `survey_site()` - i.e. no error bookkeeping.

    This is not a hypothetical: `build_survey` is the no-network assembly entry point, and every
    survey written before engine 2.117.0 (2026-08-10) has this shape too. It is also the only shape
    in which the per-workbook `dependencies_unknown` flag is the SOLE evidence that a workbook went
    unread - which is exactly why a fixture must be able to produce it. A fixture that stamps
    `summary.dependencies_unknown` supplies the very signal the test claims to be testing.
    """
    payload = json.loads(_survey_file(tmp_path, workbooks, **extra).read_text("utf-8"))
    for key in ("degraded", "listing_errors", "connection_read_errors"):
        payload.pop(key, None)
    payload["summary"] = {k: v for k, v in payload["summary"].items() if k in _OFFLINE_SUMMARY_KEYS}
    path = tmp_path / "offline_survey.json"
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
    assert "the Metadata API alone saw 1 data source(s) feeding 1 workbook(s); the survey raised that to 3 and 4." in (
        output
    )


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
    """A dependency the survey could not resolve leaves a hole in the graph; say so.

    This is the PARSED direction on its own: the declared list is emptied, so only the workbook row
    carries the evidence. The declared direction has its own test below - the two must not be able
    to cover for each other, because a listing failure is exactly when they disagree.
    """
    workbook = _survey_workbook(REVENUE_WB, [(SALES_DS, "")])
    survey = load_survey(
        _survey_file(
            tmp_path,
            [workbook],
            unresolved_dependencies=[],
            summary={"workbooks_total": 1, "unresolved_dependencies": 0, "degraded": False},
        )
    )

    assert not survey.complete
    assert [gap for gap in survey.gaps if "resolve" in gap] == [
        "1 dependency(ies) did not resolve to a published data source"
    ]


# --- the survey's OWN completeness signals -------------------------------------------------------
# `estate_survey.py::survey_site` publishes several, and every one of them means the same thing in
# its own words: this survey "did NOT see the whole estate, so its 'no dependency' answers are not
# evidence of independence". Reading a subset is how the strong claim came back with MORE authority
# than issue #126's original, so each signal gets its own test.


def test_a_survey_that_reports_itself_degraded_cannot_support_the_abandoned_claim(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`degraded` is the engine's own verdict on itself, and it is authoritative on its own.

    It is honoured directly rather than re-derived from the error lists: a new failure class added
    upstream would flip this flag while every list this script knows about stayed empty.
    """
    datasources, _ = _sqlproxy_estate(tmp_path)
    survey_path = _survey_file(
        tmp_path,
        [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])],
        degraded=True,
    )
    survey = load_survey(survey_path)
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert not survey.complete
    assert "DEGRADED" in " ".join(survey.gaps)
    assert "abandon" not in output.lower()
    assert "UNCONFIRMED" in output


def test_a_failed_site_listing_means_workbooks_are_missing_from_the_survey_entirely(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`listing_errors` is the worst gap of all: the consumer may never have been listed.

    `paged_list` returns the rows it managed to read plus an error, so the survey is a PARTIAL
    listing - a data source can look consumer-less purely because the page naming its consumer
    never arrived.
    """
    datasources, _ = _sqlproxy_estate(tmp_path)
    survey_path = _survey_file(
        tmp_path,
        [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])],
        listing_errors=[{"path": "/sites/s/workbooks", "page": 2, "error": "500"}],
        degraded=True,
    )
    survey = load_survey(survey_path)
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert not survey.complete
    assert "MISSING from this survey entirely" in " ".join(survey.gaps)
    assert "abandon" not in output.lower()


def test_a_survey_listing_no_workbooks_at_all_is_evidence_of_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Zero workbooks means zero consumer edges were OBSERVED, not that none exist."""
    datasources, _ = _sqlproxy_estate(tmp_path)
    survey = load_survey(_survey_file(tmp_path, []))
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert not survey.complete
    assert "no workbooks at all" in " ".join(survey.gaps)
    assert "abandon" not in output.lower()


def test_a_workbook_with_unknown_dependencies_is_a_gap_with_no_error_list_present(tmp_path: Path) -> None:
    """`build_survey` marks the row; only `survey_site` adds the error list. Read the row too.

    The fixture here is `build_survey()`'s output with NONE of `survey_site`'s bookkeeping - no
    `connection_read_errors`, no `summary.dependencies_unknown` - so the per-workbook flag is the
    only evidence in the file. That matters: with the summary counter present the scan is never the
    sole signal, and deleting it leaves the suite green (measured). "Unknown" is the opposite of
    "none", which is the engine's own stated reason for the flag.
    """
    survey = load_survey(
        _offline_survey(
            tmp_path,
            [_survey_workbook(REVENUE_WB, [(SALES_DS, None)]), _survey_workbook("Unreadable", [], unknown=True)],
        )
    )

    assert "dependencies_unknown" not in json.loads(survey.path.read_text("utf-8"))["summary"]
    assert not survey.complete
    assert "1 workbook connection(s) could not be read" in " ".join(survey.gaps)


def test_the_summary_counter_is_read_even_when_no_workbook_row_carries_the_flag(tmp_path: Path) -> None:
    """The mirror of the test above: the SUMMARY on its own, with clean rows.

    `survey_site` computes `summary.dependencies_unknown` from the LUIDs whose connections it could
    not read, while the per-row flag is stamped by `build_survey` - so the two can disagree exactly
    when it matters, e.g. when the listing that would have carried the row failed as well. Each
    source needs a test in which it is the only evidence, or one of them can quietly stop counting.
    """
    survey = load_survey(
        _survey_file(
            tmp_path,
            [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])],
            summary={"workbooks_total": 1, "dependencies_unknown": 1, "degraded": False},
        )
    )

    assert not survey.complete
    assert "1 workbook connection(s) could not be read" in " ".join(survey.gaps)


def test_a_survey_carrying_no_degraded_flag_cannot_claim_it_saw_the_estate(tmp_path: Path) -> None:
    """Completeness needs positive evidence; the absence of the flag is not it.

    Every survey the canonical engine writes today carries `degraded` (`main()` dumps
    `survey_site`'s dict whole). A JSON without it is either older than the flag or not the
    engine's - and neither can license the strongest claim this tool makes.

    Measured on the three survey artifacts on this machine: the 2026-08-13 operator run carries
    `degraded: false`, empty error lists and 38/38 workbooks, and is classified COMPLETE - a
    healthy survey is unaffected by this gate. The two older dry-run artifacts (2026-08-12 and
    2026-08-11) carry no `degraded` and no `listing_errors` key at all, so the pre-fix code was
    reading two fields out of files that could not have contained the others.
    """
    payload = json.loads(_survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])]).read_text("utf-8"))
    del payload["degraded"]
    del payload["summary"]["degraded"]
    path = tmp_path / "no_flag.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    survey = load_survey(path)

    assert not survey.complete
    assert "no 'degraded' flag" in " ".join(survey.gaps)


def test_an_otherwise_clean_survey_with_no_flag_is_told_the_likely_cause(tmp_path: Path) -> None:
    """Name the cheap explanation instead of sending an operator after a phantom failure.

    `degraded` and `listing_errors` arrived in engine 2.117.0 (upstream commit 72f983a8,
    2026-08-10), so every survey this repo took before that date now reads INCOMPLETE. The gate
    stays exactly as strict - a pre-2.117.0 survey that lost a listing call is genuinely
    indistinguishable from a healthy one, which is why the flag was added - but the remedy it
    prints ("re-run estate_survey.py") needs live Tableau credentials, so an operator who cannot
    get them deserves to know the likely reason before spending the afternoon on it.
    """
    survey = load_survey(_offline_survey(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])]))
    gaps = " ".join(survey.gaps)

    assert not survey.complete
    assert "predates engine 2.117.0 (2026-08-10)" in gaps


def test_the_version_hint_is_withheld_when_the_survey_shows_a_REAL_failure(tmp_path: Path) -> None:
    """An old survey that ALSO lost a listing call is not merely old; do not explain it away."""
    survey = load_survey(
        _offline_survey(
            tmp_path,
            [_survey_workbook(REVENUE_WB, [(SALES_DS, None)]), _survey_workbook("Unreadable", [], unknown=True)],
        )
    )
    gaps = " ".join(survey.gaps)

    assert "no 'degraded' flag" in gaps
    assert "predates engine" not in gaps


def test_a_workbook_list_shorter_than_the_surveys_own_count_is_a_gap(tmp_path: Path) -> None:
    """A survey that contradicts its own summary was truncated somewhere; do not trust the rows."""
    workbooks = [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])]
    survey = load_survey(
        _survey_file(
            tmp_path,
            workbooks,
            summary={"workbooks_total": 38, "degraded": False},
        )
    )

    assert not survey.complete
    assert "lists 1 of the 38 workbook(s)" in " ".join(survey.gaps)


def test_one_failure_recorded_three_ways_is_reported_once(tmp_path: Path) -> None:
    """`survey_site` records an unread workbook in three places; three gap lines would be noise."""
    survey = load_survey(
        _survey_file(
            tmp_path,
            [_survey_workbook(REVENUE_WB, [(SALES_DS, None)]), _survey_workbook("Unreadable", [], unknown=True)],
            connection_read_errors=[{"workbook": "Unreadable", "luid": "wb-Unreadable", "error": "403"}],
            degraded=True,
        )
    )

    unread = [gap for gap in survey.gaps if "could not be read" in gap]
    assert unread == ["1 workbook connection(s) could not be read"]


def test_an_incomplete_survey_says_so_even_when_there_is_nothing_to_warn_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The gaps have to REACH the operator, not merely exist on the object.

    With no orphan data sources there is no UNCONFIRMED heading to soften, so the header's
    'SURVEY IS INCOMPLETE' line is the only place the gaps are printed at all. Silencing it left
    the whole suite green (measured), which means every other completeness test was asserting on
    `survey.gaps` rather than on what an operator actually reads.
    """
    datasources = [_metadata(SALES_DS, downstream=[REVENUE_WB])]
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])], degraded=True))
    output = _rendered(caplog, build_plan(datasources, "", survey), survey)

    assert "UNCONFIRMED" not in output
    assert "SURVEY IS INCOMPLETE: the survey reports itself DEGRADED" in output


def test_an_incomplete_survey_still_contributes_every_edge_it_did_see(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Withholding the CLAIM must not mean discarding the EVIDENCE.

    A degraded survey is still the only system that can see a sqlproxy edge, so it must keep
    promoting a Metadata-API "orphan" into phase 1. Rejecting a degraded survey outright would fix
    the wording by re-breaking the order - the more expensive half of issue #126.
    """
    datasources, _ = _sqlproxy_estate(tmp_path)
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])], degraded=True))
    plan = build_plan(datasources, "", survey)
    output = _rendered(caplog, plan, survey)

    assert _by_name(plan)[SALES_DS]["downstream_workbooks"] == [REVENUE_WB]
    assert [step["name"] for step in build_order(plan) if step["kind"] == "datasource"][:1] == [SALES_DS]
    assert "SURVEY WINS" in output


# --- identity: a LUID is one, a name is not ------------------------------------------------------


def test_a_dependency_the_survey_could_not_luid_resolve_is_matched_by_name(tmp_path: Path) -> None:
    """The name fallback is load-bearing, not decorative.

    `resolve_dependency` returns `luid: ""` for every AMBIGUOUS or NOT_FOUND dependency, so on a
    real site the survey's edges for those data sources carry no LUID at all. Without the fallback
    they match nothing, the data source drops back to zero consumers, and issue #126 returns.
    """
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, "")])]))
    assert survey.by_luid == {}

    key, how = survey.match(f"luid-{SALES_DS}", SALES_DS)
    assert (key, how) == (SALES_DS.lower(), "name")

    plan = build_plan([_metadata(SALES_DS)], "", survey)
    assert _by_name(plan)[SALES_DS]["downstream_workbooks"] == [REVENUE_WB]
    assert _by_name(plan)[SALES_DS]["matched_via"] == "name"


def test_the_name_fallback_matches_across_a_case_and_whitespace_difference(tmp_path: Path) -> None:
    """Tableau round-trips display names; a case difference must not orphan a real dependency."""
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, "")])]))
    key, how = survey.match(None, f"  {SALES_DS.upper()}  ")

    assert (key, how) == (SALES_DS.lower(), "name")


def test_a_luid_match_beats_a_name_match_and_says_so(tmp_path: Path) -> None:
    """LUID first, because it is the only identity Tableau guarantees across a rename."""
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])]))

    assert survey.match(f"luid-{SALES_DS}", "Renamed Since The Survey") == (SALES_DS.lower(), "luid")
    assert survey.match("luid-nothing-like-it", "Not A Data Source") == (None, None)


# --- a shared name is merged, but never silently -------------------------------------------------


def test_two_projects_sharing_a_datasource_name_are_merged_and_flagged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The merge is kept - it over-migrates rather than orphans - but it is stated, not hidden.

    `estate_survey.py::resolve_dependency` REFUSES this ambiguity because it is deciding an
    identity. This script is deciding an order, where the safe direction is the opposite one: build
    both data sources before the workbook. What it must not do is present the per-project
    attribution as if it were known.
    """
    datasources = [
        _metadata(SHARED_DS, luid="luid-finance", project="Finance"),
        _metadata(SHARED_DS, luid="luid-marketing", project="Marketing"),
    ]
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(FINANCE_WB, [(SHARED_DS, "")])]))
    plan = build_plan(datasources, "", survey)
    output = _rendered(caplog, plan, survey)

    assert [entry["downstream_workbooks"] for entry in plan] == [[FINANCE_WB], [FINANCE_WB]]
    assert [entry["name_collision"] for entry in plan] == [["Finance", "Marketing"]] * 2
    assert "NAME COLLISION" in output
    assert "a name is not an identity" in output
    assert f"{SHARED_DS!r} exists in 2 project(s) (Finance, Marketing)" in output


def test_a_shared_name_still_sequences_both_data_sources_before_the_workbook(tmp_path: Path) -> None:
    """The over-migrate direction has to actually hold in the emitted order, not just in prose."""
    datasources = [
        _metadata(SHARED_DS, luid="luid-finance", project="Finance"),
        _metadata(SHARED_DS, luid="luid-marketing", project="Marketing"),
    ]
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(FINANCE_WB, [(SHARED_DS, "")])]))
    order = build_order(build_plan(datasources, "", survey))

    kinds = [step["kind"] for step in order]
    assert kinds == ["datasource", "datasource", "workbook"]
    # De-duplicated: two data sources that merely share a name are indistinguishable in a list of
    # names, so "after: Quarterly Figures, Quarterly Figures" tells the reader nothing actionable.
    assert order[-1]["requires"] == [SHARED_DS]


def test_a_declared_unresolved_dependency_is_a_gap_even_when_every_row_resolved(tmp_path: Path) -> None:
    """The top-level list is read on its own, not inferred from the workbook rows.

    `survey_site` records an unresolvable dependency in `unresolved_dependencies` as well as on the
    row, and the two can disagree - a row can be missing entirely when the listing that would have
    carried it failed. Reading only the rows would then miss a hole the survey is explicitly
    declaring. They are counted as ONE failure, not two: on a listing failure both carry the same
    38 dependencies, and printing them under two different sentences reads as 76.
    """
    survey = load_survey(
        _survey_file(
            tmp_path,
            [_survey_workbook(REVENUE_WB, [(SALES_DS, None)])],
            unresolved_dependencies=[
                {"workbook": "Elsewhere", "datasource_name": "Missing Source", "status": "not_found", "candidates": []}
            ],
        )
    )

    assert not survey.complete
    assert [gap for gap in survey.gaps if "resolve" in gap] == [
        "1 dependency(ies) did not resolve to a published data source"
    ]


def test_one_unresolved_dependency_declared_and_parsed_is_reported_once(tmp_path: Path) -> None:
    """The same hole seen from both sides is one hole. `_count`'s contract says ONCE."""
    survey = load_survey(_survey_file(tmp_path, [_survey_workbook(REVENUE_WB, [(SALES_DS, "")])]))

    assert [gap for gap in survey.gaps if "resolve" in gap] == [
        "1 dependency(ies) did not resolve to a published data source"
    ]


def test_a_survey_listing_one_name_under_two_identities_is_flagged_too(tmp_path: Path) -> None:
    """The collision can arrive from the SURVEY side, not just the Metadata API's.

    This is the shape `resolve_dependency` ACTUALLY emits for a duplicated name: `status:
    "ambiguous"`, `luid: ""`, `project: ""` and a `candidates` list naming both data sources. It
    "NEVER picks one", so neither identity field carries the evidence - only `candidates` does, and
    a fixture that hands over two populated LUIDs instead is testing a shape the engine cannot
    produce. Here the Metadata API lists ONE row for the name, so `len(rows) >= 2` cannot fire
    either: without reading `candidates` the merge happens with nothing printed.
    """
    both = {SHARED_DS: [("luid-finance", "Finance"), ("luid-marketing", "Marketing")]}
    survey = load_survey(
        _survey_file(
            tmp_path,
            [
                _survey_workbook(FINANCE_WB, [(SHARED_DS, "")], candidates=both),
                _survey_workbook(MARKETING_WB, [(SHARED_DS, "")], candidates=both),
            ],
        )
    )
    entry = survey.datasources[SHARED_DS.lower()]

    assert entry.ambiguous
    assert entry.projects == {"Finance", "Marketing"}
    assert entry.luids == {"luid-finance", "luid-marketing"}
    # Evidence only: the survey could not choose, so neither does this. A row with no LUID is
    # skipped by the download step instead of fetching whichever candidate was listed first.
    assert (entry.luid, entry.project) == (None, None)

    plan = build_plan([_metadata(SHARED_DS, luid="luid-finance", project="Finance")], "", survey)
    assert plan[0]["downstream_workbooks"] == [FINANCE_WB, MARKETING_WB]
    assert plan[0]["name_collision"] == ["Finance", "Marketing"]


def test_a_luid_only_name_collision_without_projects_is_still_announced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Candidate LUIDs are collision evidence even when the REST payload has no project."""
    both = {SHARED_DS: [("luid-finance", ""), ("luid-marketing", "")]}
    survey = load_survey(
        _survey_file(
            tmp_path,
            [_survey_workbook(FINANCE_WB, [(SHARED_DS, "")], candidates=both)],
        )
    )
    entry = survey.datasources[SHARED_DS.lower()]
    plan = build_plan(
        [
            {
                "name": SHARED_DS,
                "luid": None,
                "projectName": "",
                "hasExtracts": False,
                "downstreamWorkbooks": [],
            }
        ],
        "",
        survey,
    )
    output = _rendered(caplog, plan, survey)

    assert entry.luids == {"luid-finance", "luid-marketing"}
    assert not entry.projects
    assert entry.ambiguous
    assert plan[0]["name_collision"] == ["?"]
    assert "NAME COLLISION" in output
    assert f"{SHARED_DS!r} exists in 1 project(s) (?)" in output


def test_a_collision_the_metadata_api_cannot_see_at_all_is_still_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The silent-merge hole: no Metadata API row for the name, ambiguous survey dependencies.

    A `sqlproxy` data source can be invisible to the Metadata API entirely - that IS issue #126 -
    so the row is survey-only and `len(rows) >= 2` never fires. The merge is still the right call
    (it over-migrates rather than orphans), but it is the one decision that must never be made
    quietly, so the survey-only row is checked for the collision too.
    """
    both = {SHARED_DS: [("luid-finance", "Finance"), ("luid-marketing", "Marketing")]}
    survey = load_survey(
        _survey_file(
            tmp_path,
            [
                _survey_workbook(FINANCE_WB, [(SHARED_DS, "")], candidates=both),
                _survey_workbook(MARKETING_WB, [(SHARED_DS, "")], candidates=both),
            ],
        )
    )
    plan = build_plan([], "", survey)
    output = _rendered(caplog, plan, survey)

    assert [entry["matched_via"] for entry in plan] == ["survey-only"]
    assert plan[0]["name_collision"] == ["Finance", "Marketing"]
    assert "NAME COLLISION" in output
    assert f"{SHARED_DS!r} exists in 2 project(s) (Finance, Marketing)" in output


def test_a_unique_name_reports_no_collision(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The collision block must stay silent on an unambiguous estate, or it is just noise."""
    datasources, survey_path = _sqlproxy_estate(tmp_path)
    survey = load_survey(survey_path)
    plan = build_plan(datasources, "", survey)

    assert all(not entry["name_collision"] for entry in plan)
    assert "NAME COLLISION" not in _rendered(caplog, plan, survey)


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
