"""Tests for scripts/assess_estate.py.

Weighted deliberately toward the four REFUSALS and the two defects a live run against a real
Tableau Cloud site exposed, because those are the failures that produce a confident wrong answer
rather than an error:

* a dependency-key guess that parsed ZERO edges and reported "order unknown" (the exact failure
  the survey exists to prevent),
* a sparse-usage guard written as ``== 0`` that stayed silent on an estate with ONE view event
  and printed a coverage curve built on nothing.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("assess_estate", SCRIPTS / "assess_estate.py")
assess_estate = importlib.util.module_from_spec(spec)
sys.modules["assess_estate"] = assess_estate
spec.loader.exec_module(assess_estate)


def _live(views=0, subscriptions=0, alerts=0, custom_views=0, view_count=1):
    return {
        "views_lifetime": views,
        "view_count": view_count,
        "subscriptions": subscriptions,
        "alerts": alerts,
        "custom_views": custom_views,
    }


def _row(name="wb", views=0, complexity=0.0, share=1.0, understated=False, tier_name="migrate"):
    return {
        "luid": f"luid-{name}",
        "name": name,
        "rank": 1,
        "views_lifetime": views,
        "view_count": 1,
        "cumulative_share": share,
        "complexity": complexity,
        "complexity_understated": understated,
        "tier": tier_name,
        "tier_reason": "test",
    }


# --- refusal 1: never retire on a metric alone --------------------------------------------------


@pytest.mark.parametrize("signal", ["subscriptions", "alerts", "custom_views"])
def test_deliberate_use_outranks_zero_views(signal):
    """A quarterly board pack has near-zero views and is business critical."""
    destination, why = assess_estate.tier(_live(views=0, **{signal: 1}), 0.0, 1.0, 0.99)
    assert destination == "migrate"
    assert "deliberate use" in why


def test_unused_and_simple_is_only_a_CANDIDATE():
    destination, why = assess_estate.tier(_live(), 0.0, 1.0, 0.99)
    assert destination == "retire-candidate"
    assert "CONFIRM WITH THE OWNER" in why


def test_unused_but_complex_goes_to_a_human():
    assert assess_estate.tier(_live(), 40.0, 1.0, 0.99)[0] == "review"


def test_used_but_outside_the_cut_is_archived_not_retired():
    assert assess_estate.tier(_live(views=3), 0.0, 1.0, 0.99)[0] == "archive"


# --- refusal 2: dependencies are never guessed ---------------------------------------------------


def test_dependencies_read_the_documented_survey_key():
    survey = {
        "workbooks": [
            {
                "name": "Sales",
                "luid": "wb-sales",
                "published_dependencies": [
                    {"datasource_name": "Finance Master", "luid": "ds-finance", "status": "resolved"}
                ],
            }
        ]
    }
    _, rows = assess_estate._parse_dependencies(survey)
    assert rows == [
        {
            "workbook_name": "Sales",
            "workbook_luid": "wb-sales",
            "datasource_name": "Finance Master",
            "datasource_luid": "ds-finance",
            "source": "sqlproxy/resolved",
        }
    ]


def test_declared_but_unparsable_dependencies_RAISE():
    """A guess that yields nothing is indistinguishable from a genuine absence.

    This is the live defect: an earlier version read ``datasource``/``name``, parsed zero edges,
    and reported "order unknown" for an estate that had nine real ones.
    """
    survey = {"workbooks": [{"name": "Sales", "published_dependencies": [{"datasource": "Finance Master"}]}]}
    with pytest.raises(RuntimeError, match="none parsed"):
        assess_estate._parse_dependencies(survey)


def test_no_survey_yields_no_edges_without_raising():
    assert assess_estate._parse_dependencies(None) == (set(), [])


def test_understated_complexity_is_carried_from_the_survey():
    survey = {"workbooks": [{"name": "Sales", "complexity_understated": True}]}
    required, _ = assess_estate._parse_dependencies(survey)
    assert required == {"Sales"}


def test_store_keeps_project_and_dependency_luids_for_scoped_harvest(tmp_path: Path):
    raw = {
        "workbooks": [{"id": "wb-sales", "name": "Sales", "project": {"id": "p-finance", "name": "Finance"}}],
        "views": [],
        "datasources": [{"id": "ds-finance", "name": "Finance Master", "project": {"id": "p-certified"}}],
        "projects": [
            {"id": "p-finance", "name": "Finance", "parentProjectId": None, "contentPermissions": "ManagedByOwner"},
            {
                "id": "p-certified",
                "name": "Certified Sources",
                "parentProjectId": None,
                "contentPermissions": "ManagedByOwner",
            },
        ],
        "groups": [],
        "flows": [],
        "subscriptions": [],
        "alerts": [],
        "custom_views": [],
        "structure": {"publishedDatasources": []},
        "structure_by_name": {},
        "permissions": [],
        "survey": {
            "workbooks": [
                {
                    "name": "Sales",
                    "luid": "wb-sales",
                    "published_dependencies": [
                        {"datasource_name": "Finance Master", "luid": "ds-finance", "status": "resolved"}
                    ],
                }
            ]
        },
    }
    store = assess_estate.write_store(tmp_path, raw, assess_estate.assemble(raw, 0.99))
    con = assess_estate.sqlite3.connect(store)
    assert con.execute("SELECT project_luid FROM workbook").fetchone() == ("p-finance",)
    assert con.execute("SELECT project_luid FROM datasource").fetchone() == ("p-certified",)
    assert con.execute("SELECT workbook_luid, datasource_luid FROM dependency").fetchone() == ("wb-sales", "ds-finance")


# --- refusal 3: never claim a usage window we do not have ---------------------------------------


def test_sparse_usage_warns_even_when_it_is_not_zero():
    """One view event across thirteen workbooks is not evidence. ``== 0`` missed this live."""
    rows = [_row(name=f"wb{i}") for i in range(13)]
    rows[0]["views_lifetime"] = 1
    report = "\n".join(assess_estate._render_curve(rows, 0.99))
    assert "too sparse to tier on" in report
    assert "unproven" in report


def test_real_usage_does_not_trip_the_sparse_warning():
    rows = [_row(name=f"wb{i}", views=500) for i in range(3)]
    assert "too sparse" not in "\n".join(assess_estate._render_curve(rows, 0.99))


def test_liveness_names_the_count_lifetime():
    """Tableau Cloud has no usage WINDOW - the field name must not imply one."""
    live = assess_estate.liveness([{"usage": {"totalViewCount": 7}}], {})
    assert live["views_lifetime"] == 7


# --- refusal 4: IAM is exported, never mapped ----------------------------------------------------


def test_explicit_deny_is_a_hard_case():
    cases = assess_estate.iam_hard_cases([{"mode": "Deny", "object_type": "project", "capability": "Read"}], [])
    assert [c["case"] for c in cases] == ["explicit_deny"]


def test_per_view_grants_flag_a_report_split():
    cases = assess_estate.iam_hard_cases([{"mode": "Allow", "object_type": "view", "capability": "Read"}], [])
    assert cases[0]["case"] == "per_view_grants"


def test_local_groups_are_named_so_an_identity_owner_can_be_found():
    cases = assess_estate.iam_hard_cases([], [{"name": "Analysts", "domain": {"name": "local"}}])
    assert cases[0]["names"] == ["Analysts"]


def test_iam_section_refuses_to_map():
    body = "\n".join(assess_estate._render_iam({"iam_hard_cases": []}, {"flows": []}))
    assert "exported, not mapped" in body


# --- the report distinguishes UNKNOWN from NONE --------------------------------------------------


def _sequencing(dependencies, survey_supplied):
    assembled = {"dependencies": dependencies, "survey_supplied": survey_supplied}
    return "\n".join(assess_estate._render_sequencing(assembled, [_row()]))


def test_no_survey_reports_order_UNKNOWN():
    assert "ORDER is unknown" in _sequencing([], survey_supplied=False)


def test_survey_with_zero_edges_reports_NONE_not_unknown():
    body = _sequencing([], survey_supplied=True)
    assert "No published-datasource dependencies" in body
    assert "ORDER is unknown" not in body


def test_edges_are_reported_as_a_sequencing_constraint():
    body = _sequencing([{"workbook_name": "Sales", "datasource_name": "Finance"}], survey_supplied=True)
    assert "1 hard dependency edge(s)" in body


def test_understated_complexity_is_surfaced_in_the_report():
    assembled = {"dependencies": [], "survey_supplied": True}
    body = "\n".join(assess_estate._render_sequencing(assembled, [_row(understated=True)]))
    assert "UNDERSTATED complexity" in body


# --- scoring and the curve -----------------------------------------------------------------------


def test_score_counts_lods_and_table_calcs_separately():
    node = {
        "sheets": [{}, {}],
        "dashboards": [{}],
        "embeddedDatasources": [
            {
                "fields": [
                    {"__typename": "CalculatedField", "formula": "{FIXED [Region] : SUM([Sales])}"},
                    {"__typename": "CalculatedField", "formula": "WINDOW_SUM(SUM([Sales]))"},
                    {"__typename": "CalculatedField", "formula": "[a] + [b]"},
                ]
            }
        ],
    }
    counts = assess_estate.score_workbook(node)
    assert (counts["sheets"], counts["dashboards"], counts["calcs"]) == (2, 1, 3)
    assert (counts["lods"], counts["table_calcs"]) == (1, 1)


def test_score_ignores_plain_columns():
    """Only ``CalculatedField`` carries a formula - a ColumnField must never inflate the score."""
    node = {"embeddedDatasources": [{"fields": [{"__typename": "ColumnField", "name": "Sales"}]}]}
    assert assess_estate.score_workbook(node)["calcs"] == 0


def test_score_of_an_unresolved_workbook_is_zero_not_an_error():
    counts = assess_estate.score_workbook({})
    assert counts["complexity"] == 0


def test_curve_is_ordered_by_usage_and_accumulates_to_one():
    rows = [_row(name="a", views=10), _row(name="b", views=90)]
    curve = assess_estate.coverage_curve(rows)
    assert [r["name"] for r in curve] == ["b", "a"]
    assert curve[0]["cumulative_share"] == pytest.approx(0.9)
    assert curve[-1]["cumulative_share"] == pytest.approx(1.0)


def test_curve_does_not_divide_by_zero_on_an_unused_estate():
    curve = assess_estate.coverage_curve([_row(name="a"), _row(name="b")])
    assert all(r["cumulative_share"] == 0 for r in curve)


# --- signal roll-up -------------------------------------------------------------------------------


def test_a_signal_on_a_VIEW_counts_for_its_workbook():
    """The signal attaches to a view; the migration decision is taken per workbook."""
    raw = {
        "views": [{"id": "v1", "workbook": {"id": "wb1"}}],
        "subscriptions": [{"content": {"id": "v1"}}],
        "alerts": [{"view": {"id": "v1"}}],
        "custom_views": [],
    }
    views_by_wb, wb_signals = assess_estate._aggregate_signals(raw)
    assert list(views_by_wb) == ["wb1"]
    assert wb_signals["wb1"] == {"subscriptions": 1, "alerts": 1}
