"""Tests for scripts/check_field_bindings.py - the cross-layer gate from issue #236.

Every test here names the mutation it kills, because a gate like this is exactly the kind that
"passes" by never finding anything. The two that matter most:

* `test_case_only_mismatch_is_a_near_miss_showing_both_spellings` - if near-misses were folded into
  `missing`, the output would still be red but would stop being *actionable*, which is the whole
  point of the category.
* `test_doubled_apostrophe_in_a_tmdl_name_is_not_a_missing_field` - measured against committed data:
  `examples/broadway-stage-to-screen` has `column 'Sondheim''s Work'`, and a naive `'[^']*'` name
  regex truncates it and reports a perfectly good binding as missing.

No network, no Power BI Desktop, no engine: every fixture is written into `tmp_path`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

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
    """A PBIR column reference node."""
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _measure(entity: str, prop: str) -> dict:
    """A PBIR measure reference node."""
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def _visual(*fields: dict) -> dict:
    """A minimal visual whose projections carry the given field nodes."""
    return {
        "name": "v1",
        "visual": {
            "visualType": "clusteredColumnChart",
            "query": {"queryState": {"Y": {"projections": [{"field": f} for f in fields]}}},
        },
    }


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
    model = pbip / model_name
    (model / "definition" / "tables").mkdir(parents=True)
    (model / "definition" / "tables" / "Sales.tmdl").write_text(model_tmdl, encoding="utf-8")
    if with_report:
        report = pbip / "Book.Report"
        (report / "definition" / "pages").mkdir(parents=True)
        (report / "definition.pbir").write_text(
            json.dumps({"datasetReference": {"byPath": {"path": f"../{model_name}"}}}),
            encoding="utf-8",
        )
        for index, visual in enumerate(visuals or []):
            folder = report / "definition" / "pages" / "p1" / "visuals" / f"v{index}"
            folder.mkdir(parents=True)
            (folder / "visual.json").write_text(json.dumps(visual), encoding="utf-8")
    return root


def _statuses(result: dict) -> list[str]:
    """Every finding status in the merged verdict."""
    return [f["status"] for report in result["reports"] for f in report["findings"]]


def test_exact_match_passes_and_exits_zero(tmp_path, capsys) -> None:
    """Mutation killed: a gate that reports every reference as unresolved (i.e. always says no)."""
    bundle = _write_bundle(
        tmp_path, visuals=[_visual(_column("Sales", "Order Date"), _measure("Sales", "Total Revenue"))]
    )
    result = cfb.scan(bundle)
    assert result["status"] == "OK", result
    assert result["reports"][0]["references"] == 2, "both the column AND the measure must be seen"
    assert cfb.main([str(bundle)]) == 0
    assert "OK" in capsys.readouterr().out


def test_case_only_mismatch_is_a_near_miss_showing_both_spellings(tmp_path, capsys) -> None:
    """Mutation killed: dropping the case-insensitive retry (would grade this `missing`).

    This is issue #236's exact signature - the report kept `SLA_ACPU_Down_Duration` while the model
    was folded to `SLA_ACPU_DOWN_DURATION`.
    """
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "SLA_ACPU_Down_Duration"))])
    result = cfb.scan(bundle)
    assert result["status"] == "UNRESOLVED"
    assert _statuses(result) == ["near_miss"]
    assert result["near_misses"] == 1 and result["missing"] == 0
    finding = result["reports"][0]["findings"][0]
    assert finding["report_spelling"] == "Sales[SLA_ACPU_Down_Duration]"
    assert finding["model_spelling"] == "Sales[SLA_ACPU_DOWN_DURATION]"

    assert cfb.main([str(bundle)]) == 1, "an unresolved reference must gate the bundle"
    out = capsys.readouterr().out
    assert "NEAR-MISS" in out, "the near-miss category must be labelled distinctly"
    assert "SLA_ACPU_Down_Duration" in out and "SLA_ACPU_DOWN_DURATION" in out, "BOTH spellings must print"


def test_genuinely_missing_field_is_reported_as_missing_not_near_miss(tmp_path, capsys) -> None:
    """Mutation killed: collapsing both failure kinds into one label."""
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Nope"))])
    result = cfb.scan(bundle)
    assert _statuses(result) == ["missing"]
    assert result["missing"] == 1 and result["near_misses"] == 0
    assert "model_spelling" not in result["reports"][0]["findings"][0]
    assert cfb.main([str(bundle)]) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out and "NEAR-MISS" not in out


def test_unknown_table_is_missing_but_a_case_only_table_is_a_near_miss(tmp_path) -> None:
    """Mutation killed: resolving the table name case-sensitively only, or not at all."""
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("sales", "Order Date"), _column("Ghost", "Order Date"))])
    findings = {f["entity"]: f for f in cfb.scan(bundle)["reports"][0]["findings"]}
    assert findings["sales"]["status"] == "near_miss"
    assert findings["sales"]["model_spelling"] == "Sales[Order Date]"
    assert findings["Ghost"]["status"] == "missing"


def test_bundle_with_no_report_skips_cleanly(tmp_path, capsys) -> None:
    """Mutation killed: treating "nothing to check" as a pass with reports, or as a failure."""
    bundle = _write_bundle(tmp_path, with_report=False)
    result = cfb.scan(bundle)
    assert result["status"] == "SKIPPED"
    assert result["reports_scanned"] == 0
    assert cfb.main([str(bundle)]) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_report_without_a_model_beside_it_is_skipped_not_failed(tmp_path) -> None:
    """A report with no model cannot be graded - refusing it would block on a tooling fact."""
    report = tmp_path / "pbip" / "Book" / "Book.Report" / "definition" / "pages"
    report.mkdir(parents=True)
    result = cfb.scan(tmp_path)
    assert result["status"] == "SKIPPED"
    assert result["skipped"] and "no semantic model" in result["skipped"][0]["reason"]
    assert cfb.main([str(tmp_path)]) == 0


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "name": "v1",
                "filterConfig": {"filters": [{"field": _column("Sales", "Nope"), "type": "Categorical"}]},
            },
            id="visual-level-filter",
        ),
        pytest.param(
            {
                "name": "v1",
                "visual": {
                    "visualType": "barChart",
                    "query": {
                        "sortDefinition": {"sort": [{"field": _column("Sales", "Nope"), "direction": "Descending"}]}
                    },
                },
            },
            id="sort-definition",
        ),
        pytest.param(
            {
                "name": "v1",
                "visual": {
                    "visualType": "barChart",
                    "objects": {
                        "dataPoint": [
                            {
                                "properties": {
                                    "fill": {
                                        "solid": {"color": {"expr": {"FillRule": {"Input": _measure("Sales", "Nope")}}}}
                                    }
                                }
                            }
                        ]
                    },
                },
            },
            id="conditional-formatting",
        ),
        pytest.param(
            {
                "name": "v1",
                "visual": {
                    "visualType": "barChart",
                    "query": {
                        "queryState": {
                            "Y": {
                                "projections": [
                                    {"field": {"Aggregation": {"Expression": _column("Sales", "Nope"), "Function": 0}}}
                                ]
                            }
                        }
                    },
                },
            },
            id="aggregation-wrapped-column",
        ),
    ],
)
def test_references_outside_plain_projections_are_seen(tmp_path, payload) -> None:
    """Mutation killed: walking only `visual.query.queryState` (the shape most people remember).

    Filters, sort definitions, conditional-formatting `FillRule` inputs and `Aggregation` wrappers
    all carry the same reference node, and a broken binding in any of them is the same Desktop
    modal.
    """
    bundle = _write_bundle(tmp_path, visuals=[payload])
    assert _statuses(cfb.scan(bundle)) == ["missing"], "the reference was not found at all"


def test_page_and_report_level_filters_are_scanned(tmp_path) -> None:
    """Mutation killed: globbing `visuals/*/visual.json` instead of every JSON in the definition."""
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Order Date"))])
    page = bundle / "pbip" / "Book" / "Book.Report" / "definition" / "pages" / "p1" / "page.json"
    page.write_text(
        json.dumps({"name": "p1", "filterConfig": {"filters": [{"field": _column("Sales", "Nope")}]}}),
        encoding="utf-8",
    )
    assert _statuses(cfb.scan(bundle)) == ["missing"]


def test_a_source_alias_resolves_through_the_query_from_clause(tmp_path) -> None:
    """Mutation killed: ignoring `From` aliases, which reports valid PBIR as an unknown table.

    A TopN filter refers to its tables by the alias its own `From` declares (`SourceRef.Source`),
    and a `Subquery` opens a nested scope - measured in
    `examples/wind-energy-utilization/.../fd605c135377d8dacb41/visual.json`.
    """
    aliased = {
        "name": "v1",
        "filterConfig": {
            "filters": [
                {
                    "field": _column("Sales", "Order Date"),
                    "filter": {
                        "From": [{"Name": "s", "Entity": "Sales", "Type": 0}],
                        "Where": [
                            {
                                "Condition": {
                                    "In": {
                                        "Expressions": [
                                            {
                                                "Column": {
                                                    "Expression": {"SourceRef": {"Source": "s"}},
                                                    "Property": "Order Date",
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        ],
                    },
                }
            ]
        },
    }
    result = cfb.scan(_write_bundle(tmp_path, visuals=[aliased]))
    assert result["status"] == "OK", result["reports"][0]["findings"]
    assert result["reports"][0]["references"] == 2, "the aliased reference must be graded, not dropped"


def test_an_alias_that_resolves_to_a_missing_field_still_fails(tmp_path) -> None:
    """Mutation killed: "resolving" an alias by silently skipping every aliased reference."""
    aliased = {
        "name": "v1",
        "filterConfig": {
            "filters": [
                {
                    "filter": {
                        "From": [{"Name": "s", "Entity": "Sales", "Type": 0}],
                        "Where": [
                            {
                                "Condition": {
                                    "Comparison": {
                                        "Left": {
                                            "Column": {
                                                "Expression": {"SourceRef": {"Source": "s"}},
                                                "Property": "Nope",
                                            }
                                        }
                                    }
                                }
                            }
                        ],
                    }
                }
            ]
        },
    }
    assert _statuses(cfb.scan(_write_bundle(tmp_path, visuals=[aliased]))) == ["missing"]


def test_hierarchy_levels_resolve_against_the_models_hierarchy(tmp_path) -> None:
    """Mutation killed: parsing `level` lines as plain columns, which would pass ANY level name."""
    good = {"HierarchyLevel": {"Expression": {"Hierarchy": _hierarchy()}, "Level": "Year"}}
    bad = {"HierarchyLevel": {"Expression": {"Hierarchy": _hierarchy()}, "Level": "Quarter"}}
    # A level name that happens to match a COLUMN must still fail: a hierarchy level is not a
    # column, and accepting it would let a real broken drill-down through.
    decoy = {"HierarchyLevel": {"Expression": {"Hierarchy": _hierarchy()}, "Level": "Order Date"}}
    result = cfb.scan(_write_bundle(tmp_path, visuals=[_visual(good, bad, decoy)]))
    findings = result["reports"][0]["findings"]
    assert sorted(f["property"] for f in findings) == ["Order Date", "Quarter"], findings
    assert {f["status"] for f in findings} == {"missing"}
    assert {f["hierarchy"] for f in findings} == {"Date Hierarchy"}


def _hierarchy() -> dict:
    """The `Hierarchy` node a `HierarchyLevel` wraps."""
    return {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Hierarchy": "Date Hierarchy"}


def test_doubled_apostrophe_in_a_tmdl_name_is_not_a_missing_field(tmp_path) -> None:
    """Mutation killed: a `'[^']*'` TMDL name regex.

    Measured on committed data: `examples/broadway-stage-to-screen` declares
    `column 'Sondheim''s Work'`, and truncating at the doubled quote made this gate report a
    shipping, correct report as broken.
    """
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Sondheim's Work"))])
    assert cfb.scan(bundle)["status"] == "OK"
    assert "Sondheim's Work" in cfb.parse_model(bundle / "pbip" / "Book" / "Book.SemanticModel").tables["Sales"].columns


def test_a_word_inside_a_multiline_dax_body_is_not_a_model_field(tmp_path) -> None:
    """Mutation killed: matching `column`/`measure` anywhere instead of at the block's own indent.

    A measure's continuation lines are indented deeper than its declaration; treating them as
    declarations would invent fields and quietly PASS a genuinely broken binding.
    """
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Phantom"))])
    assert _statuses(cfb.scan(bundle)) == ["missing"]


def test_the_model_is_taken_from_definition_pbir_not_a_guess(tmp_path) -> None:
    """Mutation killed: pairing a report with whatever model sits nearby.

    Grading a report against a model it does not ship with is worse than not checking at all.
    """
    bundle = _write_bundle(
        tmp_path,
        model_name="Renamed.SemanticModel",
        visuals=[_visual(_column("Sales", "Order Date"))],
    )
    report = bundle / "pbip" / "Book" / "Book.Report"
    assert cfb.model_for_report(report) == (bundle / "pbip" / "Book" / "Renamed.SemanticModel").resolve()
    assert cfb.scan(bundle)["status"] == "OK"


def test_repeated_defects_collapse_but_keep_every_file(tmp_path) -> None:
    """One rename breaks many visuals; the verdict must stay readable without losing locations."""
    broken = _visual(_column("Sales", "Nope"))
    result = cfb.scan(_write_bundle(tmp_path, visuals=[broken, broken]))
    findings = result["reports"][0]["findings"]
    assert len(findings) == 1, "one defect, not one line per visual"
    assert findings[0]["occurrences"] == 2
    assert len(findings[0]["files"]) == 2


def test_a_skipped_report_is_named_even_when_another_one_passes(tmp_path, capsys) -> None:
    """Mutation killed: rendering only the graded reports.

    "OK - 1 report(s)" on a two-report bundle reads as full coverage when half of it was never
    checked; the skipped half must be named in the verdict, not only in the JSON.
    """
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Order Date"))])
    orphan = bundle / "pbip" / "Book" / "Orphan.Report" / "definition" / "pages"
    orphan.mkdir(parents=True)
    assert cfb.main([str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "Orphan.Report" in out and "SKIPPED" in out


def test_explicit_model_and_report_pair(tmp_path) -> None:
    """The `--model` / `--report` entry point, and its usage guard."""
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "SLA_ACPU_Down_Duration"))])
    model = bundle / "pbip" / "Book" / "Book.SemanticModel"
    report = bundle / "pbip" / "Book" / "Book.Report"
    assert cfb.main(["--model", str(model), "--report", str(report), "--quiet"]) == 1
    with pytest.raises(SystemExit):
        cfb.main(["--model", str(model)])


def test_a_path_that_does_not_exist_never_reports_ok(tmp_path, capsys) -> None:
    """Mutation killed: skipping the existence guard, which turns a typo into a green gate.

    `rglob` on a missing folder yields nothing, so an unchecked `--report` typo grades as
    "0 references, all resolved" - indistinguishable from a genuinely clean bundle.
    """
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Order Date"))])
    model = bundle / "pbip" / "Book" / "Book.SemanticModel"
    for argv in (
        ["--model", str(model), "--report", str(tmp_path / "Gone.Report")],
        ["--model", str(tmp_path / "Gone.SemanticModel"), "--report", str(bundle / "pbip" / "Book" / "Book.Report")],
        [str(tmp_path / "no-such-bundle")],
    ):
        with pytest.raises(SystemExit) as exc:
            cfb.main(argv)
        assert exc.value.code != 0, argv
        assert "OK" not in capsys.readouterr().out, argv


def test_nothing_to_compare_is_skipped_never_ok(tmp_path, capsys) -> None:
    """Mutation killed: grading an EMPTY pair as OK.

    Review finding: with `--model` and `--report` transposed - a one-keystroke slip, both paths
    perfectly real - no model tables parse and no references are found, and the gate used to print
    "every PBIR field reference resolves" and exit 0 for a report it never opened.
    """
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Order Date"))])
    model = bundle / "pbip" / "Book" / "Book.SemanticModel"
    report = bundle / "pbip" / "Book" / "Book.Report"

    transposed = cfb.check_pair(model, report)
    assert transposed["status"] == "SKIPPED", transposed
    assert cfb.main(["--model", str(report), "--report", str(model)]) == 0
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "resolves in its model" not in out

    # An existing but empty report folder is the same shape: nothing was compared.
    empty = tmp_path / "Empty.Report"
    empty.mkdir()
    assert cfb.check_pair(empty, model)["status"] == "SKIPPED"
    merged = cfb._merge([cfb.check_pair(empty, model)], [])  # pylint: disable=protected-access
    assert merged["status"] == "SKIPPED" and merged["reports_scanned"] == 0


def test_warn_only_and_json_output(tmp_path) -> None:
    """`--warn-only` never gates; `--json` writes the same verdict a caller can act on."""
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Nope"))])
    out = tmp_path / "verdict.json"
    assert cfb.main([str(bundle), "--warn-only", "--quiet", "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "UNRESOLVED" and payload["missing"] == 1


def test_the_engine_baseline_reports_folder_is_not_scanned(tmp_path) -> None:
    """`<bundle>/reports/` has no model beside it; scanning it would cry wolf on every reference."""
    bundle = _write_bundle(tmp_path, visuals=[_visual(_column("Sales", "Order Date"))])
    # A faithful engine baseline: its own report AND model, whose bindings are broken. If the
    # sweep reached it, the bundle would be refused for a folder that never ships.
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
    assert result["status"] == "OK"
    assert result["reports_scanned"] == 1, "only the shipping pbip/ report is graded"


def test_committed_examples_still_resolve() -> None:
    """The gate must be quiet on the repo's own shipping deliverables, or nobody will trust it."""
    repo = Path(__file__).resolve().parents[1]
    checked = 0
    for pbir in sorted(repo.glob("examples/*/fabric/*.Report")):
        model = cfb.model_for_report(pbir)
        if model is None:
            continue
        checked += 1
        result = cfb.check_pair(pbir, model)
        assert result["status"] == "OK", f"{pbir.name}: {result['findings']}"
    assert checked >= 10, "the sweep must actually have found the committed examples"


# ---------------------------------------------------------------------------------------------
# Issue #258 - table agreement. Every reference below RESOLVES; the defect is which table it
# resolves ON, which the pre-#258 gate could not see.
# ---------------------------------------------------------------------------------------------

# The customer's shape, reduced: `Serial_Number` exists on THREE tables. `Installs` and `Aircraft`
# are joined; `Config` is a stranded lookup that nothing relates to. Binding the grouping column to
# `Config` still resolves, so the old gate said OK - and Desktop said InvalidUnconstrainedJoin.
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
    model = pbip / "Book.SemanticModel"
    (model / "definition" / "tables").mkdir(parents=True)
    (model / "definition" / "tables" / "model-tables.tmdl").write_text(model_tmdl, encoding="utf-8")
    if relationships is not None:
        (model / "definition" / "relationships.tmdl").write_text(relationships, encoding="utf-8")
    report = pbip / "Book.Report"
    (report / "definition.pbir").parent.mkdir(parents=True, exist_ok=True)
    (report / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": "../Book.SemanticModel"}}}), encoding="utf-8"
    )
    for index, visual in enumerate(visuals):
        folder = report / "definition" / "pages" / "p1" / "visuals" / f"v{index}"
        folder.mkdir(parents=True)
        (folder / "visual.json").write_text(json.dumps(visual), encoding="utf-8")
    return root


def _coherence(result: dict) -> list[dict]:
    """Every table-agreement finding in the merged verdict."""
    return [f for report in result["reports"] for f in report["coherence"]]


def test_a_field_bound_to_the_wrong_table_of_the_same_name_is_caught(tmp_path, capsys) -> None:
    """THE issue #258 reproduction: every field resolves, and the visual still cannot render.

    Verbatim from the field report on `IA_Aircraft_Installs`: "a field existing under the identical
    name on BOTH the referenced table and the correct table produces no gate warning - it only
    surfaces later as InvalidUnconstrainedJoin". Pre-#258 this bundle graded `OK`, exit 0.
    """
    bundle = _write_star(
        tmp_path,
        visuals=[_visual(_column("Config", "Serial_Number"), _column("Installs", "Install_Count"))],
    )
    result = cfb.scan(bundle)

    assert result["status"] == "INCOHERENT", result
    assert result["missing"] == 0 and result["near_misses"] == 0, "every reference resolves - that is the point"
    finding = _coherence(result)[0]
    assert finding["status"] == "unrelated_tables"
    assert sorted(sorted(g) for g in finding["table_groups"]) == [["Config"], ["Installs"]]
    # The ambiguity is itself the finding: name the tables the same column name also lives on.
    assert finding["ambiguous"] == [{"report_spelling": "Config[Serial_Number]", "also_on": ["Aircraft", "Installs"]}]

    assert cfb.main([str(bundle)]) == 1, "a visual that cannot render must gate the bundle"
    out = capsys.readouterr().out
    assert "UNRELATED TABLES" in out
    assert "Config" in out and "Installs" in out
    assert "Serial_Number" in out


def test_a_star_schema_visual_spanning_fact_and_dimension_stays_silent(tmp_path, capsys) -> None:
    """The false-positive that would destroy trust: a measure plus a related dimension attribute.

    This is the single most common visual in any star schema. If it warns, the gate is worse than
    useless - people will switch it off and lose the real finding with it.
    """
    bundle = _write_star(
        tmp_path,
        visuals=[
            _visual(_column("Aircraft", "Aircraft_Type"), _measure("Installs", "Total Installs")),
            _visual(_column("Aircraft", "Aircraft_Type"), _column("Installs", "Install_Count")),
        ],
    )
    result = cfb.scan(bundle)
    assert result["status"] == "OK", _coherence(result)
    assert cfb.main([str(bundle)]) == 0
    assert "UNRELATED TABLES" not in capsys.readouterr().out
