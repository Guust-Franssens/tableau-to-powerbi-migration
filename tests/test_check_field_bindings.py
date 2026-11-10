"""Tests for scripts/check_field_bindings.py - the cross-layer gate from issue #236.

A gate like this "passes" by never finding anything, so each family pins the branch a mutation
would remove. No network, no Desktop, no engine: every fixture is written into `tmp_path`, except
the committed-corpus anti-vacuity proofs at the end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import check_field_bindings as cfb  # noqa: E402  # pylint: disable=wrong-import-position

MODEL_TMDL = """table Sales

\tcolumn 'Order Date'
\t\tdataType: dateTime

\tcolumn SLA_ACPU_DOWN_DURATION
\t\tdataType: double

\tcolumn 'Sondheim''s Work' = CONTAINSSTRINGEXACT('Sales'[Notes], "Sondheim")

\tmeasure 'Total Revenue' = SUM(Sales[Amount])

\tmeasure 'Multi Line' =
\t\t\tVAR _x = 1
\t\t\tcolumn Phantom
\t\t\tRETURN _x

\thierarchy 'Date Hierarchy'

\t\tlevel Year
\t\t\tcolumn: Year
"""


def _column(entity: str, prop: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _measure(entity: str, prop: str) -> dict:
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _visual(*fields: dict, visual_type: str = "tableEx", role: str = "Values") -> dict:
    """A minimal visual whose projections carry the given field nodes."""
    return {
        "name": "v1",
        "visual": {
            "visualType": visual_type,
            "query": {"queryState": {role: {"projections": [{"field": f} for f in fields]}}},
        },
    }


def _aliased_column(alias: str, prop: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Source": alias}}, "Property": prop}}


def _aliased_visual(*pairs: tuple[str, str, str]) -> dict:
    """Projections reaching their tables through `query.From` ALIASES: `(alias, entity, property)`."""
    return {
        "name": "v1",
        "visual": {
            "visualType": "tableEx",
            "query": {
                "From": [{"Name": alias, "Entity": entity} for alias, entity, _ in pairs],
                "queryState": {"Values": {"projections": [{"field": _aliased_column(a, p)} for a, _, p in pairs]}},
            },
        },
    }


def _write_report(pbip: Path, model_name: str, visuals: list[dict]) -> None:
    report = pbip / "Book.Report"
    (report / "definition" / "pages").mkdir(parents=True)
    (report / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": f"../{model_name}"}}}), encoding="utf-8"
    )
    for index, visual in enumerate(visuals):
        folder = report / "definition" / "pages" / "p1" / "visuals" / f"v{index}"
        folder.mkdir(parents=True)
        (folder / "visual.json").write_text(json.dumps(visual), encoding="utf-8")


def _write_bundle(
    root: Path,
    *,
    model_tmdl: str = MODEL_TMDL,
    visuals: list[dict] | None = None,
    model_name: str = "Book.SemanticModel",
    with_report: bool = True,
) -> Path:
    """Write a `pbip/`-shaped bundle and return its root."""
    pbip = root / "pbip" / "Book"
    (pbip / model_name / "definition" / "tables").mkdir(parents=True)
    (pbip / model_name / "definition" / "tables" / "Sales.tmdl").write_text(model_tmdl, encoding="utf-8")
    if with_report:
        _write_report(pbip, model_name, visuals or [])
    return root


def _statuses(result: dict) -> list[str]:
    return [f["status"] for report in result["reports"] for f in report["findings"]]


def test_field_resolution_categories_are_distinct_and_gate(tmp_path, capsys) -> None:
    """Exact is OK; a case-only miss is a NEAR-MISS printing both spellings; a real miss is MISSING."""
    ok = _write_bundle(
        tmp_path / "ok", visuals=[_visual(_column("Sales", "Order Date"), _measure("Sales", "Total Revenue"))]
    )
    result = cfb.scan(ok)
    assert (result["status"], result["reports"][0]["references"]) == ("OK", 2)
    assert cfb.main([str(ok)]) == 0 and "OK" in capsys.readouterr().out

    near = _write_bundle(tmp_path / "near", visuals=[_visual(_column("Sales", "SLA_ACPU_Down_Duration"))])
    result = cfb.scan(near)
    assert (result["status"], _statuses(result), result["near_misses"], result["missing"]) == (
        "UNRESOLVED",
        ["near_miss"],
        1,
        0,
    )
    finding = result["reports"][0]["findings"][0]
    assert finding["report_spelling"] == "Sales[SLA_ACPU_Down_Duration]"
    assert finding["model_spelling"] == "Sales[SLA_ACPU_DOWN_DURATION]"
    assert cfb.main([str(near)]) == 1
    out = capsys.readouterr().out
    assert "NEAR-MISS" in out and "SLA_ACPU_Down_Duration" in out and "SLA_ACPU_DOWN_DURATION" in out

    missing = _write_bundle(tmp_path / "missing", visuals=[_visual(_column("Sales", "Nope"))])
    result = cfb.scan(missing)
    assert (_statuses(result), result["missing"], result["near_misses"]) == (["missing"], 1, 0)
    assert "model_spelling" not in result["reports"][0]["findings"][0]
    assert cfb.main([str(missing)]) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out and "NEAR-MISS" not in out

    tables = _write_bundle(
        tmp_path / "tables", visuals=[_visual(_column("sales", "Order Date"), _column("Ghost", "Order Date"))]
    )
    findings = {f["entity"]: f for f in cfb.scan(tables)["reports"][0]["findings"]}
    assert (findings["sales"]["status"], findings["sales"]["model_spelling"]) == ("near_miss", "Sales[Order Date]")
    assert findings["Ghost"]["status"] == "missing"


def test_nothing_graded_is_skipped_and_named_never_ok(tmp_path, capsys) -> None:
    """No report, a report with no model, an empty or transposed pair: SKIPPED, and named beside a pass."""
    bare = _write_bundle(tmp_path / "bare", with_report=False)
    result = cfb.scan(bare)
    assert (result["status"], result["reports_scanned"]) == ("SKIPPED", 0)
    assert cfb.main([str(bare)]) == 0 and "SKIPPED" in capsys.readouterr().out

    lonely = tmp_path / "lonely"
    (lonely / "pbip" / "Book" / "Book.Report" / "definition" / "pages").mkdir(parents=True)
    result = cfb.scan(lonely)
    assert result["status"] == "SKIPPED" and "no semantic model" in result["skipped"][0]["reason"]

    bundle = _write_bundle(tmp_path / "pair", visuals=[_visual(_column("Sales", "Order Date"))])
    (bundle / "pbip" / "Book" / "Orphan.Report" / "definition" / "pages").mkdir(parents=True)
    assert cfb.main([str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "Orphan.Report" in out and "SKIPPED" in out

    model = bundle / "pbip" / "Book" / "Book.SemanticModel"
    report = bundle / "pbip" / "Book" / "Book.Report"
    assert cfb.check_pair(model, report)["status"] == "SKIPPED"  # transposed: a one-keystroke slip
    assert cfb.main(["--model", str(report), "--report", str(model)]) == 0
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "resolves in its model" not in out
    empty = tmp_path / "Empty.Report"
    empty.mkdir()
    merged = cfb._merge([cfb.check_pair(empty, model)], [])  # pylint: disable=protected-access
    assert (merged["status"], merged["reports_scanned"]) == ("SKIPPED", 0)


FILL_RULE = {"properties": {"fill": {"solid": {"color": {"expr": {"FillRule": {"Input": _measure("Sales", "Nope")}}}}}}}
AGGREGATED = {"field": {"Aggregation": {"Expression": _column("Sales", "Nope"), "Function": 0}}}
SORTED = {"sort": [{"field": _column("Sales", "Nope"), "direction": "Descending"}]}
OUTSIDE_PROJECTIONS = {
    "visual-filter": {
        "name": "v1",
        "filterConfig": {"filters": [{"field": _column("Sales", "Nope"), "type": "Categorical"}]},
    },
    "sort": {"name": "v1", "visual": {"visualType": "barChart", "query": {"sortDefinition": SORTED}}},
    "fill-rule": {"name": "v1", "visual": {"visualType": "barChart", "objects": {"dataPoint": [FILL_RULE]}}},
    "aggregation": {
        "name": "v1",
        "visual": {"visualType": "barChart", "query": {"queryState": {"Y": {"projections": [AGGREGATED]}}}},
    },
}


def test_references_outside_plain_projections_and_visuals_are_seen(tmp_path) -> None:
    """Filters, sorts, FillRule inputs, Aggregation wrappers and page.json all carry reference nodes."""
    for name, payload in OUTSIDE_PROJECTIONS.items():
        assert _statuses(cfb.scan(_write_bundle(tmp_path / name, visuals=[payload]))) == ["missing"], name
    bundle = _write_bundle(tmp_path / "page", visuals=[_visual(_column("Sales", "Order Date"))])
    page = bundle / "pbip" / "Book" / "Book.Report" / "definition" / "pages" / "p1" / "page.json"
    page.write_text(
        json.dumps({"name": "p1", "filterConfig": {"filters": [{"field": _column("Sales", "Nope")}]}}),
        encoding="utf-8",
    )
    assert _statuses(cfb.scan(bundle)) == ["missing"]


def _aliased_filter(prop: str, *, field: dict | None = None) -> dict:
    where = {"Condition": {"In": {"Expressions": [_aliased_column("s", prop)]}}}
    entry: dict = {"filter": {"From": [{"Name": "s", "Entity": "Sales", "Type": 0}], "Where": [where]}}
    if field:
        entry["field"] = field
    return {"name": "v1", "filterConfig": {"filters": [entry]}}


def test_a_source_alias_resolves_through_the_query_from_clause(tmp_path) -> None:
    """A filter names its table by its own `From` alias; it is graded, and a bad target still fails."""
    good = _aliased_filter("Order Date", field=_column("Sales", "Order Date"))
    result = cfb.scan(_write_bundle(tmp_path / "ok", visuals=[good]))
    assert (result["status"], result["reports"][0]["references"]) == ("OK", 2)
    assert _statuses(cfb.scan(_write_bundle(tmp_path / "bad", visuals=[_aliased_filter("Nope")]))) == ["missing"]


def test_model_parsing_edges(tmp_path) -> None:
    """Hierarchy levels are not columns; `''` does not end a name; DAX continuation lines are not
    declarations; and the model comes from definition.pbir, never a neighbour guess."""
    hierarchy = {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Hierarchy": "Date Hierarchy"}
    levels = [
        {"HierarchyLevel": {"Expression": {"Hierarchy": hierarchy}, "Level": level}}
        for level in ("Year", "Quarter", "Order Date")
    ]
    findings = cfb.scan(_write_bundle(tmp_path / "h", visuals=[_visual(*levels)]))["reports"][0]["findings"]
    assert sorted(f["property"] for f in findings) == ["Order Date", "Quarter"]
    assert {f["status"] for f in findings} == {"missing"}
    assert {f["hierarchy"] for f in findings} == {"Date Hierarchy"}

    bundle = _write_bundle(tmp_path / "q", visuals=[_visual(_column("Sales", "Sondheim's Work"))])
    assert cfb.scan(bundle)["status"] == "OK"
    columns = cfb.parse_model(bundle / "pbip" / "Book" / "Book.SemanticModel").tables["Sales"].columns
    assert "Sondheim's Work" in columns
    phantom = _write_bundle(tmp_path / "dax", visuals=[_visual(_column("Sales", "Phantom"))])
    assert _statuses(cfb.scan(phantom)) == ["missing"]

    renamed = _write_bundle(
        tmp_path / "r", model_name="Renamed.SemanticModel", visuals=[_visual(_column("Sales", "Order Date"))]
    )
    report = renamed / "pbip" / "Book" / "Book.Report"
    assert cfb.model_for_report(report) == (renamed / "pbip" / "Book" / "Renamed.SemanticModel").resolve()
    assert cfb.scan(renamed)["status"] == "OK"


def test_cli_entry_points_guards_and_outputs(tmp_path, capsys) -> None:
    """`--model/--report`, its usage guard, nonexistent paths never OK, `--warn-only` and `--json`."""
    bundle = _write_bundle(tmp_path / "b", visuals=[_visual(_column("Sales", "SLA_ACPU_Down_Duration"))])
    model = bundle / "pbip" / "Book" / "Book.SemanticModel"
    report = bundle / "pbip" / "Book" / "Book.Report"
    assert cfb.main(["--model", str(model), "--report", str(report), "--quiet"]) == 1
    with pytest.raises(SystemExit):
        cfb.main(["--model", str(model)])
    for argv in (
        ["--model", str(model), "--report", str(tmp_path / "Gone.Report")],
        ["--model", str(tmp_path / "Gone.SemanticModel"), "--report", str(report)],
        [str(tmp_path / "no-such-bundle")],
    ):
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            cfb.main(argv)
        assert exc.value.code != 0 and "OK" not in capsys.readouterr().out, argv
    out = tmp_path / "verdict.json"
    assert cfb.main([str(bundle), "--warn-only", "--quiet", "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert (payload["status"], payload["near_misses"]) == ("UNRESOLVED", 1)


def test_the_engine_baseline_reports_folder_is_not_scanned(tmp_path) -> None:
    """`<bundle>/reports/` never ships; a broken baseline there must not refuse the bundle."""
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Order Date"))])
    baseline_report = bundle / "reports" / "Book.Report"
    baseline = baseline_report / "definition" / "pages" / "p1" / "visuals" / "v0"
    baseline.mkdir(parents=True)
    (baseline / "visual.json").write_text(json.dumps(_visual(_column("Sales", "Nope"))), encoding="utf-8")
    (baseline_report / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": "../Book.SemanticModel"}}}), encoding="utf-8"
    )
    baseline_model = bundle / "reports" / "Book.SemanticModel" / "definition" / "tables"
    baseline_model.mkdir(parents=True)
    (baseline_model / "Sales.tmdl").write_text(MODEL_TMDL, encoding="utf-8")
    result = cfb.scan(bundle)
    assert (result["status"], result["reports_scanned"]) == ("OK", 1)


# --- Issue #258: table agreement. Every reference RESOLVES; the defect is WHICH table. ---
# `Serial_Number` lives on four tables; Installs-Aircraft are joined, Config and Scrap are stranded.
AIRCRAFT_TMDL = """table Installs

\tcolumn Serial_Number
\t\tdataType: string

\tcolumn Install_Count
\t\tdataType: int64

\tmeasure 'Total Installs' = SUM(Installs[Install_Count])

table Aircraft

\tcolumn Serial_Number
\t\tdataType: string

\tcolumn Aircraft_Type
\t\tdataType: string

table Config

\tcolumn Serial_Number
\t\tdataType: string

\tcolumn Config_Code
\t\tdataType: string

table Scrap

\tcolumn Serial_Number
\t\tdataType: string
"""

AIRCRAFT_RELATIONSHIPS = """relationship Installs_Aircraft
\tfromColumn: Installs.Serial_Number
\ttoColumn: Aircraft.Serial_Number
"""


def _write_star(
    root: Path,
    *,
    visuals: list[dict],
    model_tmdl: str = AIRCRAFT_TMDL,
    relationships: str | None = AIRCRAFT_RELATIONSHIPS,
) -> Path:
    """A `pbip/` bundle whose model has several tables and a relationships file."""
    pbip = root / "pbip" / "Book"
    definition = pbip / "Book.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True)
    (definition / "tables" / "model-tables.tmdl").write_text(model_tmdl, encoding="utf-8")
    if relationships is not None:
        (definition / "relationships.tmdl").write_text(relationships, encoding="utf-8")
    _write_report(pbip, "Book.SemanticModel", visuals)
    return root


def _coherence(result: dict) -> list[dict]:
    return [f for report in result["reports"] for f in report["coherence"]]


CONFIG_INSTALLS = _visual(_column("Config", "Serial_Number"), _column("Installs", "Install_Count"))


def test_a_field_bound_to_the_wrong_table_of_the_same_name_is_caught(tmp_path, capsys) -> None:
    """THE #258 reproduction: every field resolves, Desktop says InvalidUnconstrainedJoin."""
    bundle = _write_star(tmp_path, visuals=[CONFIG_INSTALLS])
    result = cfb.scan(bundle)
    assert (result["status"], result["missing"], result["near_misses"]) == ("INCOHERENT", 0, 0)
    finding = _coherence(result)[0]
    assert finding["status"] == "unrelated_tables"
    assert sorted(sorted(g) for g in finding["table_groups"]) == [["Config"], ["Installs"]]
    assert finding["ambiguous"] == [{"report_spelling": "Config[Serial_Number]", "also_on": ["Aircraft", "Installs"]}]
    assert cfb.main([str(bundle)]) == 1
    out = capsys.readouterr().out
    assert "UNRELATED TABLES" in out and "Config" in out and "Installs" in out and "Serial_Number" in out


FIELD_PARAMETER = (
    "\ntable Switcher\n\n\tcolumn Switcher\n\t\tsourceColumn: [Value1]\n"
    "\n\tpartition Switcher = calculated\n"
    '\t\tsource = {("Installs", NAMEOF(Installs[Install_Count]), 0)}\n'
)
CALCULATION_GROUP = (
    "\ntable Switcher\n\n\tcalculationGroup\n\n\t\tcalculationItem Current = SELECTEDMEASURE()\n"
    "\n\tcolumn Switcher\n\t\tdataType: string\n"
)
MANUFACTURER = "\ntable Manufacturer\n\n\tcolumn Aircraft_Type\n\t\tdataType: string\n"
SNOWFLAKE_RELS = AIRCRAFT_RELATIONSHIPS + (
    "\nrelationship Aircraft_Manufacturer\n"
    "\tfromColumn: Aircraft.Aircraft_Type\n"
    "\ttoColumn: Manufacturer.Aircraft_Type\n"
)
REGION = "\ntable 'Region Parameter'\n\n\tcolumn Region\n\t\tdataType: string\n"
WITH_FILTER = _visual(_column("Installs", "Install_Count"))
WITH_FILTER["filterConfig"] = {"filters": [{"field": _column("Config", "Config_Code")}]}
SWITCHER = _visual(_column("Switcher", "Switcher"), _column("Installs", "Install_Count"))
AIRCRAFT_INSTALLS = _visual(_column("Aircraft", "Aircraft_Type"), _column("Installs", "Install_Count"))
RELS = AIRCRAFT_RELATIONSHIPS

# (name, extra tmdl, relationships, visuals, status, expected detached_ok or None)
COHERENCE_CASES = [
    ("star-silent", "", RELS, [_visual(_column("Aircraft", "Aircraft_Type"), _measure("Installs", "Total Installs")), AIRCRAFT_INSTALLS], "OK", None),
    ("transitive", MANUFACTURER, SNOWFLAKE_RELS, [_visual(_column("Manufacturer", "Aircraft_Type"), _column("Installs", "Install_Count"))], "OK", None),
    ("measure-home", "\ntable _Measures\n\n\tmeasure 'Install Rate' = 1\n", RELS, [_visual(_column("Aircraft", "Aircraft_Type"), _measure("_Measures", "Install Rate"))], "OK", None),
    ("field-parameter", FIELD_PARAMETER, RELS, [SWITCHER], "OK", {"Switcher": "field parameter"}),
    ("calculation-group", CALCULATION_GROUP, RELS, [SWITCHER], "OK", {"Switcher": "calculation group"}),
    ("plain-slicer", REGION, RELS, [_visual(_column("Region Parameter", "Region"), _column("Installs", "Install_Count"))], "INCOHERENT", {}),
    ("filters-do-not-group", "", RELS, [WITH_FILTER], "OK", None),
    ("relationships-in-table-file", "\n" + RELS, None, [AIRCRAFT_INSTALLS], "OK", None),
]  # fmt: skip


def test_table_agreement_verdicts(tmp_path) -> None:
    """Reachability not adjacency; measures and query-time-substituted tables never constrain;
    only the two substituted shapes are exempt; and a relationship after a table is not swallowed."""
    for name, extra, rels, visuals, status, detached in COHERENCE_CASES:
        bundle = _write_star(tmp_path / name, model_tmdl=AIRCRAFT_TMDL + extra, relationships=rels, visuals=visuals)
        result = cfb.scan(bundle)
        assert result["status"] == status, (name, _coherence(result))
        model = cfb.parse_model(bundle / "pbip" / "Book" / "Book.SemanticModel")
        assert detached is None or model.detached_ok == detached, name
        if rels is None:
            assert len(model.relationships) == 1
            assert model.tables["Config"].columns == {"Serial_Number", "Config_Code"}


def test_inactive_relationship_aliases_and_bookmarks(tmp_path, capsys) -> None:
    """`isActive: false` is not a join (and has its own fix); aliased projections still vote; a
    bookmark carrying a visual-shaped payload is not what renders."""
    inactive = (
        "relationship Installs_Config\n\tfromColumn: Installs.Serial_Number\n"
        "\ttoColumn: Config.Serial_Number\n\tisActive: false\n"
    )
    bundle = _write_star(
        tmp_path / "inactive",
        relationships=inactive,
        visuals=[_visual(_column("Config", "Config_Code"), _column("Installs", "Install_Count"))],
    )
    result = cfb.scan(bundle)
    assert result["status"] == "INCOHERENT" and _coherence(result)[0]["inactive_only"] is True
    assert cfb.main([str(bundle)]) == 1 and "INACTIVE" in capsys.readouterr().out

    aliased_visual = _aliased_visual(("c", "Config", "Serial_Number"), ("i", "Installs", "Install_Count"))
    aliased = cfb.scan(_write_star(tmp_path / "aliased", visuals=[aliased_visual]))
    assert (aliased["status"], aliased["incoherent_visuals"]) == ("INCOHERENT", 1)

    bundle = _write_star(tmp_path / "bookmark", visuals=[_visual(_column("Installs", "Install_Count"))])
    stowaway = bundle / "pbip" / "Book" / "Book.Report" / "definition" / "bookmarks"
    stowaway.mkdir(parents=True)
    (stowaway / "b1.json").write_text(json.dumps(CONFIG_INSTALLS), encoding="utf-8")
    assert cfb.scan(bundle)["status"] == "OK"


def test_missing_fields_and_disagreements_interact_in_one_run(tmp_path, capsys) -> None:
    """Both defects land in ONE pass (no masked second round); a non-existent field does not also vote."""
    both = _write_star(tmp_path / "both", visuals=[_visual(_column("Installs", "Nope")), CONFIG_INSTALLS])
    result = cfb.scan(both)
    assert (result["status"], result["missing"], result["incoherent_visuals"]) == ("UNRESOLVED", 1, 1)
    assert cfb.main([str(both)]) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out and "UNRELATED TABLES" in out
    ghost_visual = _visual(_column("Aircraft", "Aircraft_Type"), _column("Config", "Nope"))
    ghost = cfb.scan(_write_star(tmp_path / "ghost", visuals=[ghost_visual]))
    assert (ghost["status"], ghost["missing"], ghost["incoherent_visuals"]) == ("UNRESOLVED", 1, 0)


def test_repeated_defects_collapse_but_keep_every_location(tmp_path) -> None:
    """One rename or one copy-pasted visual is one fix, not N lines - but every file is kept."""
    missing = cfb.scan(_write_bundle(tmp_path / "f", visuals=[_visual(_column("Sales", "Nope"))] * 2))
    findings = missing["reports"][0]["findings"]
    assert len(findings) == 1 and findings[0]["occurrences"] == 2 and len(findings[0]["files"]) == 2
    result = cfb.scan(_write_star(tmp_path / "c", visuals=[CONFIG_INSTALLS] * 3))
    findings = _coherence(result)
    assert len(findings) == 1 and findings[0]["occurrences"] == 3 and len(findings[0]["files"]) == 3
    assert result["incoherent_visuals"] == 3
    out = tmp_path / "verdict.json"
    assert cfb.main([str(tmp_path / "c"), "--warn-only", "--quiet", "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert (payload["status"], payload["incoherent_visuals"]) == ("INCOHERENT", 3)
    assert payload["reports"][0]["coherence"][0]["status"] == "unrelated_tables"


def test_relationship_endpoints_split_on_the_right_dot() -> None:
    """Mutation killed: `value.partition('.')`, which mis-splits every quoted name with a dot."""
    cases = {
        "'Sample Superstore'.'Order Date 2017'": ("Sample Superstore", "Order Date 2017"),
        "Date.Date": ("Date", "Date"),
        "'Sondheim''s Work'.Id": ("Sondheim's Work", "Id"),
        "Table.'Col.With.Dots'": ("Table", "Col.With.Dots"),
        "'unterminated.Id": None,
        "NoDotAtAll": None,
    }
    assert {e: cfb._split_qualified(e) for e in cases} == cases  # pylint: disable=protected-access


# --- Issue #429: role-type compatibility and direct color measure bindings. ---

ROLE_TEST_TMDL = """table Metrics

\tcolumn Health_Status
\t\tdataType: string

\tcolumn Weight
\t\tdataType: double

\tcolumn Color_Code
\t\tdataType: string

\tcolumn Flag
\t\tdataType: boolean

\tcolumn Created_Date
\t\tdataType: datetime

\tcolumn Unknown_Col
\t\tdataType: Unknown

\tcolumn Variant_Col
\t\tdataType: Variant

\tmeasure 'Health Status Measure' = "Healthy"
\t\tdataType: string

\tmeasure 'Numeric Revenue' = SUM(Metrics[Weight])
\t\tdataType: double

\tmeasure 'Untyped Measure' = SUM(Metrics[Weight])

\tmeasure 'Unknown Measure' = SUM(Metrics[Weight])
\t\tdataType: Unknown

\tmeasure 'Variant Measure' = SUM(Metrics[Weight])
\t\tdataType: Variant

\tmeasure 'Color Hex Measure' = "#FF0000"
\t\tdataType: string

\tmeasure 'Boolean Measure' = TRUE()
\t\tdataType: boolean

\tmeasure 'DateTime Measure' = NOW()
\t\tdataType: datetime

table Dups

\tcolumn AgreedNum
\t\tdataType: int64
\t\tdataType: Int64

\tmeasure AgreedStr = "test"
\t\tdataType: string
\t\tdataType: String

\tcolumn IntThenStr
\t\tdataType: int64
\t\tdataType: string

\tcolumn StrThenInt
\t\tdataType: string
\t\tdataType: int64
"""

MEASURES = {
    "Health Status Measure",
    "Numeric Revenue",
    "Untyped Measure",
    "Unknown Measure",
    "Variant Measure",
    "Color Hex Measure",
    "Boolean Measure",
    "DateTime Measure",
    "AgreedStr",
}


def _ref(prop: str, table: str = "Metrics") -> dict:
    return (_measure if prop in MEASURES else _column)(table, prop)


def _y(prop: str, table: str = "Metrics", visual_type: str = "clusteredColumnChart", role: str = "Y") -> dict:
    return _visual(_ref(prop, table), visual_type=visual_type, role=role)


def _fill(expr: dict, namespace: str = "dataPoint") -> dict:
    """A chart with a numeric Y and an explicit `<namespace>.fill.solid.color.expr`."""
    visual = _y("Numeric Revenue")
    visual["visual"]["objects"] = {namespace: [{"properties": {"fill": {"solid": {"color": {"expr": expr}}}}}]}
    return visual


COMPARISON = {
    "ComparisonKind": 0,
    "Left": _measure("Metrics", "Numeric Revenue"),
    "Right": {"Literal": {"Value": "100D"}},
}
CONDITIONAL = {
    "Conditional": {
        "Cases": [{"Condition": {"Comparison": COMPARISON}, "Value": {"Literal": {"Value": "'#00FF00'"}}}],
        "Else": {"Literal": {"Value": "'#FF0000'"}},
    }
}
NON_NUMERIC = "non_numeric_role"
FILL = "objects.dataPoint.fill"

# (name, visual, violation status or None, cannot-assess role or None)
ROLE_CASES = [
    ("numeric-measure-y", _y("Numeric Revenue"), None, None),
    ("numeric-column-y", _y("Weight"), None, None),
    ("text-on-category", _y("Health_Status", role="Category"), None, None),
    ("text-on-table", _y("Health_Status", visual_type="tableEx", role="Values"), None, None),
    ("text-on-card", _y("Health Status Measure", visual_type="card", role="Values"), None, None),
    ("untyped-y", _y("Untyped Measure"), None, "Y"),
    ("conditional-fill", _fill(CONDITIONAL), None, None),
    ("legend-fill", _fill(_ref("Color Hex Measure"), "legend"), None, None),
    ("labels-fill", _fill(_ref("Color Hex Measure"), "labels"), None, None),
    ("direct-color", _fill(_ref("Color Hex Measure")), "direct_color_measure", None),
    ("agreed-int", _y("AgreedNum", "Dups"), None, None),
    ("agreed-string", _y("AgreedStr", "Dups"), NON_NUMERIC, None),
    ("int-then-str", _y("IntThenStr", "Dups"), None, "Y"),
    ("str-then-int", _y("StrThenInt", "Dups"), None, "Y"),
    *[
        (f"{vt}-{role}", _y("Health Status Measure", visual_type=vt, role=role), NON_NUMERIC, None)
        for vt, role in [("scatterChart", "X"), ("scatterChart", "Size"), ("azureMap", "Size"), ("treemap", "Values")]
    ],
    *[(f"y-{p}", _y(p), None, "Y") for p in ["Unknown Measure", "Variant Measure", "Unknown_Col", "Variant_Col"]],
    *[
        (f"fill-{p}", _fill(_ref(p)), None, None)
        for p in ["Boolean Measure", "DateTime Measure", "Numeric Revenue", "Flag", "Created_Date", "Weight"]
    ],
    *[(f"fill-{p}", _fill(_ref(p)), None, FILL) for p in ["Untyped Measure", "Unknown Measure", "Variant Measure"]],
]


def test_role_type_verdicts(tmp_path) -> None:
    """Only a known non-numeric type on a numeric role, or a direct color measure on dataPoint.fill,
    is a violation; Unknown/Variant/untyped/conflicting types are cannot_assess and never fail."""
    wrong = []
    for name, visual, violation, cannot_role in ROLE_CASES:
        result = cfb.scan(_write_bundle(tmp_path / name, model_tmdl=ROLE_TEST_TMDL, visuals=[visual]))
        report = result["reports"][0]
        got = (
            result["status"],
            [f["status"] for f in report["role_type_findings"]],
            [f["role"] for f in report["cannot_assess_findings"]],
        )
        want = (
            "NON_NUMERIC_ROLE" if violation else "OK",
            [violation] if violation else [],
            [cannot_role] if cannot_role else [],
        )
        if got != want:
            wrong.append((name, got, want))
    assert not wrong


def test_role_violation_and_cannot_assess_details_reach_the_cli(tmp_path, capsys) -> None:
    """The finding names role, type and field, and the CLI gates violations but not cannot_assess."""
    bundle = _write_bundle(tmp_path / "y", model_tmdl=ROLE_TEST_TMDL, visuals=[_y("Health Status Measure")])
    result = cfb.scan(bundle)
    assert result["role_type_violations"] == 1
    finding = result["reports"][0]["role_type_findings"][0]
    assert (finding["role"], finding["data_type"], finding["entity"], finding["property"]) == (
        "Y",
        "string",
        "Metrics",
        "Health Status Measure",
    )
    assert cfb.main([str(bundle)]) == 1
    out = capsys.readouterr().out
    assert "NON_NUMERIC_ROLE" in out and "ROLE TYPE VIOLATION" in out and "Health Status Measure" in out
    color = _write_bundle(tmp_path / "c", model_tmdl=ROLE_TEST_TMDL, visuals=[_fill(_ref("Color Hex Measure"))])
    assert cfb.main([str(color)]) == 1 and "Color Hex Measure" in capsys.readouterr().out
    untyped = _write_bundle(tmp_path / "u", model_tmdl=ROLE_TEST_TMDL, visuals=[_y("Untyped Measure")])
    result = cfb.scan(untyped)
    assert (result["role_type_violations"], result["role_type_cannot_assess"]) == (0, 1)
    finding = result["reports"][0]["cannot_assess_findings"][0]
    assert (finding["status"], finding["property"]) == ("cannot_assess", "Untyped Measure")
    assert cfb.main([str(untyped)]) == 0


# --- Controlled experiments against the COMMITTED corpus: "0 findings" is only evidence if the
# check can fire at all. ---


def test_committed_corpus_is_clean_and_the_rules_are_load_bearing() -> None:
    """Every shipping example resolves; its multi-table visual is silent BECAUSE of the relationship
    graph (strip it and it fires); and the broadway field parameter would be a false positive
    without the `_classify_detached` exemption."""
    pairs = [(r, cfb.model_for_report(r)) for r in sorted(REPO.glob("examples/*/fabric/*.Report"))]
    pairs = [(r, m) for r, m in pairs if m is not None]
    assert len(pairs) >= 10
    exercised = fired = 0
    for report_dir, model_dir in pairs:
        result = cfb.check_pair(report_dir, model_dir)
        assert (result["status"], result["coherence"]) == ("OK", []), (report_dir.name, result["findings"])
        model = cfb.parse_model(model_dir)
        queries = list(cfb.iter_visual_queries(report_dir))
        exercised += sum(1 for q in queries if len(cfb._grouping_tables(model, q)) >= 2)  # pylint: disable=protected-access
        stripped = cfb.ModelFields(tables=model.tables, detached_ok=model.detached_ok)
        fired += sum(1 for q in queries if cfb.check_visual_coherence(stripped, q))
    assert exercised >= 1, "the corpus no longer exercises the table-agreement rule at all"
    assert fired >= 1, "removing every relationship changed nothing - the graph is not consulted"

    report_dir = next(REPO.glob("examples/broadway-stage-to-screen/fabric/*.Report"))
    model = cfb.parse_model(cfb.model_for_report(report_dir))
    assert model.detached_ok == {"Breakdown by": "field parameter"}
    unexempt = cfb.ModelFields(tables=model.tables, relationships=model.relationships)
    assert len([q for q in cfb.iter_visual_queries(report_dir) if cfb.check_visual_coherence(unexempt, q)]) == 1
