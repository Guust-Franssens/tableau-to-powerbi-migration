"""Tests for scripts/assess_estate.py."""

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


def test_tiering_never_retires_on_a_metric_alone():
    """A quarterly board pack has near-zero views and is business critical."""
    for signal in ("subscriptions", "alerts", "custom_views"):
        destination, why = assess_estate.tier(_live(views=0, **{signal: 1}), 0.0, 1.0, 0.99)
        assert (destination, "deliberate use" in why) == ("migrate", True), signal
    destination, why = assess_estate.tier(_live(), 0.0, 1.0, 0.99)
    assert destination == "retire-candidate" and "CONFIRM WITH THE OWNER" in why
    assert assess_estate.tier(_live(), 40.0, 1.0, 0.99)[0] == "review"
    # Tableau Cloud has no usage WINDOW - the field name must not imply one.
    used = assess_estate.liveness([{"usage": {"totalViewCount": 7}}], {})
    assert used["views_lifetime"] == 7
    assert assess_estate.tier(used, 0.0, 1.0, 0.99)[0] == "archive"


# --- refusal 2: dependencies are never guessed ---------------------------------------------------


def test_dependencies_read_the_documented_survey_key():
    survey = {
        "workbooks": [
            {
                "name": "Sales",
                "luid": "wb-sales",
                "complexity_understated": True,
                "published_dependencies": [
                    {"datasource_name": "Finance Master", "luid": "ds-finance", "status": "resolved"}
                ],
            }
        ]
    }
    required, rows = assess_estate._parse_dependencies(survey)
    assert rows == [
        {
            "workbook_name": "Sales",
            "workbook_luid": "wb-sales",
            "datasource_name": "Finance Master",
            "datasource_luid": "ds-finance",
            "source": "sqlproxy/resolved",
        }
    ]
    assert required == {"Sales"}
    assert assess_estate._parse_dependencies(None) == (set(), [])


def test_declared_but_unparsable_dependencies_RAISE():
    """A guess that yields nothing is indistinguishable from a genuine absence."""
    survey = {"workbooks": [{"name": "Sales", "published_dependencies": [{"datasource": "Finance Master"}]}]}
    with pytest.raises(RuntimeError, match="none parsed"):
        assess_estate._parse_dependencies(survey)


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
    rows = [_row(name=f"wb{i}", views=500) for i in range(3)]
    assert "too sparse" not in "\n".join(assess_estate._render_curve(rows, 0.99))


# --- refusal 4: IAM is exported, never mapped ----------------------------------------------------


def test_iam_hard_cases_are_exported_never_mapped():
    def cases(grants, groups):
        return assess_estate.iam_hard_cases(grants, groups)

    deny = cases([{"mode": "Deny", "object_type": "project", "capability": "Read"}], [])
    assert [c["case"] for c in deny] == ["explicit_deny"]
    assert cases([{"mode": "Allow", "object_type": "view", "capability": "Read"}], [])[0]["case"] == "per_view_grants"
    # Local groups are named so an identity owner can be found.
    assert cases([], [{"name": "Analysts", "domain": {"name": "local"}}])[0]["names"] == ["Analysts"]
    body = "\n".join(assess_estate._render_iam({"iam_hard_cases": []}, {"flows": []}))
    assert "exported, not mapped" in body


# --- the report distinguishes UNKNOWN from NONE --------------------------------------------------


def test_sequencing_distinguishes_unknown_none_and_real_edges():
    def render(dependencies, survey_supplied, row):
        assembled = {"dependencies": dependencies, "survey_supplied": survey_supplied}
        return "\n".join(assess_estate._render_sequencing(assembled, [row]))

    assert "ORDER is unknown" in render([], False, _row())
    none = render([], True, _row())
    assert "No published-datasource dependencies" in none and "ORDER is unknown" not in none
    edge = render([{"workbook_name": "Sales", "datasource_name": "Finance"}], True, _row())
    assert "1 hard dependency edge(s)" in edge
    assert "UNDERSTATED complexity" in render([], True, _row(understated=True))


# --- scoring and the curve -----------------------------------------------------------------------


def test_score_counts_lods_and_table_calcs_separately_and_ignores_plain_columns():
    """Only ``CalculatedField`` carries a formula - a ColumnField must never inflate the score."""
    node = {
        "sheets": [{}, {}],
        "dashboards": [{}],
        "embeddedDatasources": [
            {
                "fields": [
                    {"__typename": "CalculatedField", "formula": "{FIXED [Region] : SUM([Sales])}"},
                    {"__typename": "CalculatedField", "formula": "WINDOW_SUM(SUM([Sales]))"},
                    {"__typename": "CalculatedField", "formula": "[a] + [b]"},
                    {"__typename": "ColumnField", "name": "Sales"},
                ]
            }
        ],
    }
    counts = assess_estate.score_workbook(node)
    assert (counts["sheets"], counts["dashboards"], counts["calcs"]) == (2, 1, 3)
    assert (counts["lods"], counts["table_calcs"]) == (1, 1)
    # An unresolved workbook scores zero rather than erroring.
    assert assess_estate.score_workbook({})["complexity"] == 0


def test_curve_is_ordered_by_usage_and_never_divides_by_zero():
    curve = assess_estate.coverage_curve([_row(name="a", views=10), _row(name="b", views=90)])
    assert [r["name"] for r in curve] == ["b", "a"]
    assert curve[0]["cumulative_share"] == pytest.approx(0.9)
    assert curve[-1]["cumulative_share"] == pytest.approx(1.0)
    unused = assess_estate.coverage_curve([_row(name="a"), _row(name="b")])
    assert all(r["cumulative_share"] == 0 for r in unused)


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
