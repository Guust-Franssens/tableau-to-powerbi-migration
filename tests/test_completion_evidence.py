"""Slice-A direct controls. All business rows/processes here are synthetic, never native proof."""

from __future__ import annotations

import copy
import hashlib
import json
import struct
import sys
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / ".github" / "skills" / "pbip-model-refresh" / "scripts"))

import completion_evidence as ce  # noqa: E402
import iteration_receipt as receipt  # noqa: E402

pdq, refresh = ce._skill_modules()

CATALOGUE = "11111111-2222-3333-4444-555555555555"
WORKBOOK = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
VIEW = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
IDENTITY = pdq.DesktopIdentity(1234, "100000000000000001", 5678, "100000000000000002", 52001)
BOUND = pdq.BoundDesktop(IDENTITY, CATALOGUE)
NUMBER = Decimal("9007199254740993.12345678901234567890123456789")
ABF = (
    "This backup was created using XPress9 compression.".encode("utf-16-le")
    + b"\0\0"
    + struct.pack("<II", 512, 12)
    + bytes.fromhex("2ad7864e")
    + b"fixture1"
)


def blob(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def write(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else blob(content))


def model_fixture(mode="import") -> dict:
    return {
        "name": "Model",
        "culture": "en-US",
        "tables": [
            {
                "name": table,
                "columns": [{"name": "x", "dataType": "int64"}],
                "measures": [{"name": "Amount", "expression": f"SUM('{table}'[x])"}],
                "partitions": [
                    {
                        "name": table,
                        "mode": mode,
                        "source": {
                            "type": "m",
                            "expression": (
                                f'let\nSource = Sql.Database("{host}", "db"),\n'
                                f'Data = Source{{[Schema="dbo", Item="{table}"]}}[Data]\nin\nData'
                            ),
                        },
                    }
                ],
            }
            for table, host in (("Orders", "first.invalid"), ("Customers", "second.invalid"))
        ],
        "relationships": [
            {
                "name": "r",
                "fromTable": "Orders",
                "fromColumn": "x",
                "toTable": "Customers",
                "toColumn": "x",
                "crossFilteringBehavior": "oneDirection",
            }
        ],
        "roles": [
            {
                "name": "Readers",
                "modelPermission": "read",
                "tablePermissions": [{"name": "Orders", "filterExpression": "[x] > 0"}],
            }
        ],
        "expressions": [{"name": "Helper", "kind": "m", "expression": "1"}],
        "cultures": [
            {
                "name": "en-US",
                "linguisticMetadata": {
                    "content": {"Version": "2.0.0", "Language": "en-US", "CustomInstructions": "Use Amount."},
                    "contentType": "json",
                },
            }
        ],
    }


def spec_fixture() -> dict:
    return {
        "data_sources": [
            {
                "id": table,
                "connection": {
                    "class": "sqlserver",
                    "server": host,
                    "database": "db",
                    "schema": "dbo",
                    "mode": "live",
                    "powerbi_target": "live_source",
                },
                "tables": [{"name": table}],
            }
            for table, host in (("Orders", "first.invalid"), ("Customers", "second.invalid"))
        ]
    }


def plan_fixture() -> dict:
    return {
        "schema_version": 1,
        "cases": [
            {
                "id": "case-1",
                "page_id": "page-1",
                "visual_id": "visual-1",
                "projection": "Orders.Amount",
                "view_luid": VIEW,
                "view_kind": "worksheet",
                "metric": {"entity": "Orders", "property": "Amount", "kind": "Measure"},
                "columns": [
                    {"source": "Amount", "target": "[value]", "kind": "decimal", "scale": "1", "blank": "empty"}
                ],
                "grain": [],
                "context": {"kpi": "Amount", "period": "all", "filters": [], "parameters": []},
                "normalization": {"row_order": "ordered", "absolute_tolerance": "0", "relative_tolerance": "0"},
            }
        ],
    }


def certified_record(content: bytes) -> dict:
    return {
        "view_luid": VIEW,
        "workbook_luid": WORKBOOK,
        "view_type": "worksheet",
        "data": {
            "status": "ok",
            "certification": "certified",
            "path": f"data/{VIEW}.csv",
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "row_count": 1,
            "columns": ["Amount"],
            "response_framing": "content_length",
            "content_encoding": "identity",
        },
    }


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "package"
    report = root / "fabric" / "Unit.Report"
    model = root / "fabric" / "Unit.SemanticModel"
    source = root / "assets" / "Unit.twb"
    write(source, b"<workbook fixture='only'/>")
    write(
        root / "package-manifest.json",
        {
            "unit": "Unit",
            "kind": "workbook",
            "artifacts": {
                "report": "fabric/Unit.Report",
                "model": "fabric/Unit.SemanticModel",
                "asset": "assets/Unit.twb",
            },
        },
    )
    write(root / "fabric" / "Unit.pbip", {"artifacts": [{"report": {"path": "Unit.Report"}}]})
    write(report / "definition.pbir", {"datasetReference": {"byPath": {"path": "../Unit.SemanticModel"}}})
    write(report / "definition" / "report.json", {})
    write(report / "definition" / "pages" / "pages.json", {"pageOrder": ["page-1"]})
    write(report / "definition" / "pages" / "page-1" / "page.json", {"name": "page-1", "displayName": "Main"})
    write(
        report / "definition" / "pages" / "page-1" / "visuals" / "visual-1" / "visual.json",
        {
            "name": "visual-1",
            "visual": {
                "visualType": "card",
                "query": {
                    "queryState": {
                        "Values": {
                            "projections": [
                                {
                                    "queryRef": "Orders.Amount",
                                    "field": {
                                        "Measure": {
                                            "Expression": {"SourceRef": {"Entity": "Orders"}},
                                            "Property": "Amount",
                                        }
                                    },
                                }
                            ]
                        }
                    }
                },
            },
        },
    )
    write(model / "definition" / "database.tmdl", b"database\n\tcompatibilityLevel: 1604\n")
    write(model / "definition" / "model.tmdl", b"model Model\n\tref table Orders\n\tref table Customers\n")
    for table in ("Orders", "Customers"):
        write(
            model / "definition" / "tables" / f"{table}.tmdl",
            f"table {table}\n\tcolumn x\n\t\tdataType: int64\n\tmeasure Amount = SUM('{table}'[x])\n".encode(),
        )
    write(root / "migration-spec.json", spec_fixture())
    write(
        root / "source-provenance.json",
        {
            "inputs": [
                {
                    "input": {"sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                    "origin": {"workbook_luid": WORKBOOK, "matched_by": "luid", "revision_match": "same"},
                }
            ]
        },
    )
    content = f"Amount\r\n{NUMBER}\r\n".encode()
    write(root / "oracle" / "data" / f"{VIEW}.csv", content)
    write(root / "oracle" / "oracle-manifest.json", {"views": [certified_record(content)]})
    write(root / "comparison-plan.json", plan_fixture())
    monkeypatch.setattr(receipt, "assert_desktop_binding", lambda target, pid: None)
    return root


class FakeRuntime:
    """Independent scripted native outputs. Real source/revision/byte/comparison checks still run."""

    def __init__(self, mode="import"):
        self.model = model_fixture(mode)
        self.events = []
        self.counts = {"Orders": 1, "Customers": 1}
        self.on_guard = None
        self.refresh_type = "full"
        self.refresh_tables = ()
        self.persist_catalogue = CATALOGUE
        self.bound = BOUND
        self.value = NUMBER

    def bind(self, request):
        self.events.append("bind")
        return self.bound

    def witness(self, bound, model):
        self.events.append("witness")
        return ce.DefinitionWitness(blob(self.model), ce.rev.model_revision(model))

    def guard(self, bound):
        self.events.append("guard")
        if self.on_guard:
            self.on_guard()

    def refresh(self, bound):
        self.events.append("refresh")
        return refresh.RefreshObservation(
            CATALOGUE, self.refresh_type, "tables" if self.refresh_tables else "database", self.refresh_tables
        )

    def probe(self, bound, canaries):
        self.events.append("probe")
        result = []
        for table in canaries:
            query = f"EVALUATE TOPN(1, '{table}')"
            rows = tuple((ce.dax.typed_value(1),) for _ in range(self.counts[table]))
            observed = ce.dax.TypedResult(hashlib.sha256(query.encode()).hexdigest(), (("x", "int64"),), rows)
            result.append(pdq.CanaryObservation(table, query, observed, len(rows)))
        return tuple(result)

    def persist(self, bound, model, expected_revision):
        self.events.append("persist")
        assert expected_revision == ce.rev.model_revision(model)
        observed = []

        def record(commit):
            observed.append(
                refresh.PersistenceObservation(
                    self.persist_catalogue,
                    1606,
                    commit,
                    ce.rev.model_revision(model),
                )
            )

        refresh._persist_image(
            model / ".pbi" / "cache.abf", model, 1606, lambda path: path.write_bytes(ABF), on_commit=record
        )
        return observed[0]

    def execute(self, bound, query):
        self.events.append("execute")
        return ce.dax.TypedResult(
            hashlib.sha256(query.encode()).hexdigest(), (("[value]", "decimal"),), ((ce.dax.typed_value(self.value),),)
        )


def collect(package, runtime=None, **kwargs):
    request = ce.EvidenceRequest(1234, ("Orders", "Customers"), authorize_refresh=True, **kwargs)
    return ce.collect_completion_evidence(package, request, _runtime=runtime or FakeRuntime())


def test_full_refresh_and_checked_commit_bind_post_alignment_model_revision(package):
    runtime = FakeRuntime()
    result = collect(package, runtime)
    assert result.observation["data"]["status"] == "DATA_OK", result.observation
    data = result.observation["data"]
    assert runtime.events.index("refresh") < runtime.events.index("probe") < runtime.events.index("persist")
    assert data["refresh"]["tables"] == []
    assert len(data["canaries"]) == 2
    assert {row["sampled_rows"] for row in data["canaries"]} == {1}
    assert all(row["total_rows"] is None for row in data["canaries"])
    model = receipt.resolve_package(package).model_dir
    assert data["model_revision"] == ce.rev.model_revision(model)
    assert data["persistence"]["model_revision"] == data["model_revision"]
    assert data["persistence"]["intended"] == {"sha256": hashlib.sha256(ABF).hexdigest(), "byte_count": len(ABF)}
    assert not (model / ".pbi" / "cache.abf.lock").exists()
    ce.verify_payloads(result.observation, result.payloads)
    assert ce.read_observation(result.to_bytes()) == result.observation


def test_pure_directquery_earns_explicit_persistence_na_without_writes(package):
    runtime = FakeRuntime("directQuery")
    result = collect(package, runtime)
    assert result.observation["data"]["status"] == "DATA_OK", result.observation
    assert result.observation["data"]["persistence"] == {"status": "NOT_APPLICABLE", "reason": "PURE_DIRECTQUERY"}
    assert "refresh" not in runtime.events and "persist" not in runtime.events
    assert not (receipt.resolve_package(package).model_dir / ".pbi" / "cache.abf").exists()


def test_mixed_tables_require_refresh_and_both_source_legs(package):
    runtime = FakeRuntime()
    runtime.model["tables"][1]["partitions"][0]["mode"] = "directQuery"
    result = collect(package, runtime)
    assert result.observation["data"]["status"] == "DATA_OK", result.observation
    assert {row["mode"] for row in result.observation["data"]["canaries"]} == {"import", "directQuery"}
    assert "persist" in runtime.events


def test_static_import_materialization_prevents_pure_directquery_na(package):
    runtime = FakeRuntime("directQuery")
    runtime.model["tables"].append(
        {
            "name": "Parameters",
            "partitions": [
                {
                    "name": "Parameters",
                    "mode": "import",
                    "source": {"type": "calculated", "expression": "{1}"},
                }
            ],
        }
    )
    result = collect(package, runtime)
    assert result.observation["data"]["status"] == "DATA_OK", result.observation
    assert result.observation["data"]["storage_modes"] == ["directQuery", "import"]
    assert "refresh" in runtime.events and "persist" in runtime.events
    assert result.observation["data"]["persistence"]["status"] == "PERSISTED"


@pytest.mark.parametrize("canaries", [(), ("Orders", "Orders"), ("Orders", "orders"), ("Orders",), ("Parameters",)])
def test_implicit_duplicate_static_and_uncovered_canaries_refuse_before_refresh(package, canaries):
    runtime = FakeRuntime()
    request = ce.EvidenceRequest(1234, canaries, authorize_refresh=True)
    result = ce.collect_completion_evidence(package, request, _runtime=runtime)
    assert result.observation["data"]["code"] in {"CANARIES_REQUIRED", "SOURCE_COVERAGE_MISSING"}
    assert "refresh" not in runtime.events


@pytest.mark.parametrize("mode", ["dual", "default", "hybrid", "unknown", None])
def test_unknown_dual_hybrid_storage_never_earns_na(package, mode):
    runtime = FakeRuntime(mode)
    result = collect(package, runtime)
    assert result.observation["data"]["code"] == "SOURCE_COVERAGE_MISSING"
    assert "persist" not in runtime.events


def test_zero_canary_never_persists_or_earns_model_data_ok(package):
    runtime = FakeRuntime()
    runtime.counts["Customers"] = 0
    result = collect(package, runtime)
    assert result.observation["data"]["code"] == "NO_DATA"
    assert "persist" not in runtime.events


def test_missing_refresh_authorization_does_not_mutate(package):
    runtime = FakeRuntime()
    result = ce.collect_completion_evidence(
        package, ce.EvidenceRequest(1234, ("Orders", "Customers")), _runtime=runtime
    )
    assert result.observation["data"]["code"] == "FULL_REFRESH_REQUIRED"
    assert "refresh" not in runtime.events and "persist" not in runtime.events


@pytest.mark.parametrize("kind,tables", [("calculate", ()), ("full", ("Orders",)), ("measures", ())])
def test_partial_or_calculate_operation_is_not_full_refresh(package, kind, tables):
    runtime = FakeRuntime()
    runtime.refresh_type, runtime.refresh_tables = kind, tables
    result = collect(package, runtime)
    assert result.observation["data"]["code"] == "FULL_REFRESH_REQUIRED"
    assert "probe" not in runtime.events and "persist" not in runtime.events


def test_wrong_catalogue_image_is_refused(package):
    runtime = FakeRuntime()
    runtime.persist_catalogue = WORKBOOK
    result = collect(package, runtime)
    assert result.observation["data"]["code"] == "CATALOGUE_CHANGED"


@pytest.mark.parametrize("bad", [b"touched", ABF[:-1], ABF[:-8] + b"foreign!"])
def test_cache_replaced_after_observation_is_refused(package, bad):
    runtime = FakeRuntime()
    cache = receipt.resolve_package(package).model_dir / ".pbi" / "cache.abf"
    runtime.on_guard = lambda: cache.write_bytes(bad)
    result = collect(package, runtime)
    assert result.observation["data"]["code"] == "CACHE_CHANGED"


def test_final_native_recheck_is_not_skipped(package):
    runtime = FakeRuntime()

    def reused():
        raise pdq.EvidenceUnavailable("PID_REUSED")

    runtime.on_guard = reused
    result = collect(package, runtime)
    assert result.observation["data"]["code"] == "PID_REUSED"


@pytest.mark.parametrize("mutation", ["dax", "m", "relationship", "role", "culture", "expression"])
def test_same_names_different_semantics_fail_the_live_disk_witness(mutation):
    original, changed = model_fixture(), model_fixture()
    if mutation == "dax":
        changed["tables"][0]["measures"][0]["expression"] = "0"
    elif mutation == "m":
        changed["tables"][0]["partitions"][0]["source"]["expression"] = "let x = #table({}, {}) in x"
    elif mutation == "relationship":
        changed["relationships"][0]["crossFilteringBehavior"] = "bothDirections"
    elif mutation == "role":
        changed["roles"][0]["tablePermissions"][0]["filterExpression"] = "TRUE()"
    elif mutation == "culture":
        changed["cultures"][0]["linguisticMetadata"]["content"]["CustomInstructions"] = "Use a different metric."
    else:
        changed["expressions"][0]["expression"] = "2"
    ce.compare_definitions(blob(original), blob(original), "sha256:" + "a" * 64)
    with pytest.raises(ce.EvidenceError, match="^DEFINITION_MISMATCH$"):
        ce.compare_definitions(blob(original), blob(changed), "sha256:" + "a" * 64)


def test_unknown_definition_feature_is_not_silently_normalized():
    model = model_fixture()
    model["futureSemanticFeature"] = {"expression": "TRUE()"}
    with pytest.raises(ce.EvidenceError, match="^DEFINITION_UNSUPPORTED$"):
        ce.compare_definitions(blob(model), blob(model), "sha256:" + "a" * 64)


def test_semantic_witness_never_rounds_json_metadata_before_comparing_or_hashing():
    original = (
        b'{"tables":[{"name":"Orders","extendedProperties":[{"name":"numericHint","type":"json",'
        b'"value":{"precision":1.00000000000000001}}]}]}'
    )
    changed = original.replace(b"1.00000000000000001", b"1.00000000000000002")
    observed = ce.compare_definitions(original, original, "sha256:" + "a" * 64)
    assert observed.model_blob == original
    assert observed.facts()["sha256"] == hashlib.sha256(original).hexdigest()
    with pytest.raises(ce.EvidenceError, match="^DEFINITION_MISMATCH$"):
        ce.compare_definitions(original, changed, "sha256:" + "a" * 64)


def test_wrong_endpoint_and_unreachable_connector_mention_do_not_cover_source():
    model = model_fixture()
    witness = ce.DefinitionWitness(blob(model), "sha256:" + "a" * 64)
    assert len(ce.source_bindings(spec_fixture(), witness, ("Orders", "Customers"))) == 2
    expression = model["tables"][1]["partitions"][0]["source"]
    expression["expression"] = expression["expression"].replace("second.invalid", "first.invalid")
    with pytest.raises(ce.EvidenceError, match="^SOURCE_COVERAGE_MISSING$"):
        ce.source_bindings(
            spec_fixture(), ce.DefinitionWitness(blob(model), witness.model_revision), ("Orders", "Customers")
        )
    expression["expression"] = 'let Source = Sql.Database("second.invalid", "db"), Data = #table({"x"}, {{1}}) in Data'
    with pytest.raises(ce.EvidenceError, match="^SOURCE_COVERAGE_MISSING$"):
        ce.source_bindings(
            spec_fixture(), ce.DefinitionWitness(blob(model), witness.model_revision), ("Orders", "Customers")
        )


def identity_for(package):
    source = package / "assets" / "Unit.twb"
    return ce.reference.UnitIdentity(
        "Unit", source, hashlib.sha256(source.read_bytes()).hexdigest(), WORKBOOK, ce.reference.REVISION_CONFIRMED
    )


def test_certified_csv_reads_and_hashes_identical_original_bytes(package):
    observed = ce.read_tableau_csv(package, identity_for(package), "oracle/oracle-manifest.json", VIEW, "worksheet")
    assert observed.blob == f"Amount\r\n{NUMBER}\r\n".encode()
    assert hashlib.sha256(observed.blob).hexdigest() == observed.record["data"]["sha256"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("uncertified", "CSV_UNCERTIFIED"),
        ("html", "CSV_INVALID"),
        ("ragged", "CSV_INVALID"),
        ("header_only", "CSV_EMPTY"),
        ("duplicate", "CSV_INVALID"),
        ("foreign", "CSV_IDENTITY"),
        ("hash", "CSV_HASH_MISMATCH"),
        ("image_only", "CSV_UNCERTIFIED"),
    ],
)
def test_uncertified_empty_foreign_and_malformed_csv_refuse(package, mutation, code):
    content = f"Amount\r\n{NUMBER}\r\n".encode()
    if mutation == "html":
        content = b"<html>\r\nerror\r\n</html>\r\n"
    elif mutation == "ragged":
        content = b"Amount,Region\r\n1\r\n"
    elif mutation == "header_only":
        content = b"Amount\r\n"
    elif mutation == "duplicate":
        content = b"Amount,Amount\r\n1,1\r\n"
    record = certified_record(content)
    if mutation == "uncertified":
        record["data"]["certification"] = "content_type_unspecific"
    elif mutation == "foreign":
        record["workbook_luid"] = CATALOGUE
    elif mutation == "hash":
        record["data"]["sha256"] = "0" * 64
    elif mutation == "image_only":
        record["image"] = record.pop("data")
    write(package / "oracle" / "data" / f"{VIEW}.csv", content)
    write(package / "oracle" / "oracle-manifest.json", {"views": [record]})
    with pytest.raises(ce.EvidenceError, match=f"^{code}$"):
        ce.read_tableau_csv(package, identity_for(package), "oracle/oracle-manifest.json", VIEW, "worksheet")


def test_unconfirmed_source_revision_is_not_inferred_from_a_luid(package):
    identity = replace(identity_for(package), revision=ce.reference.REVISION_UNCONFIRMED)
    with pytest.raises(ce.EvidenceError, match="^SOURCE_REVISION_UNKNOWN$"):
        ce.read_tableau_csv(package, identity, "oracle/oracle-manifest.json", VIEW, "worksheet")


@pytest.mark.parametrize(
    "field,value",
    [
        ("observed", 123),
        ("expected_scalar", "9007199254740993"),
        ("status", "match"),
        ("catalogue", CATALOGUE),
        ("source_path", "oracle/replacement.csv"),
        ("dax", 'EVALUATE ROW("value", 1)'),
    ],
)
def test_plan_is_input_only_not_an_observed_claim(field, value):
    plan = plan_fixture()
    plan["cases"][0][field] = value
    with pytest.raises(ce.EvidenceError, match="^PLAN_INVALID$"):
        ce.read_plan(blob(plan))


@pytest.mark.parametrize("tolerance", ["NaN", "Infinity", "-1", "1E10000"])
def test_nonfinite_negative_and_unbounded_tolerance_refuse(tolerance):
    plan = plan_fixture()
    plan["cases"][0]["normalization"]["absolute_tolerance"] = tolerance
    with pytest.raises(ce.EvidenceError, match="^PLAN_INVALID$"):
        ce.read_plan(blob(plan))


def test_plan_object_mutation_cannot_change_held_original_input():
    plan = ce.read_plan(blob(plan_fixture()))
    changed = plan.cases[0]
    changed["normalization"]["absolute_tolerance"] = "100000000"
    assert plan.cases[0]["normalization"]["absolute_tolerance"] == "0"


def typed_result(query: bytes, values) -> ce.dax.TypedResult:
    return ce.dax.TypedResult(
        hashlib.sha256(query).hexdigest(),
        (("[value]", "decimal"),),
        tuple((ce.dax.typed_value(value),) for value in values),
    )


def test_decimal_comparison_is_exact_even_with_low_global_precision():
    query = b"EVALUATE ROW(\"value\", 'Orders'[Amount])"
    case = plan_fixture()["cases"][0]
    content = f"Amount\r\n{NUMBER}\r\n".encode()
    with localcontext() as context:
        context.prec = 6
        compared, left, right = ce.recompute_comparison(case, content, query, typed_result(query, [NUMBER]).to_bytes())
    assert compared["outcome"] == "match"
    assert str(NUMBER).encode() in left and str(NUMBER).encode() in right
    mismatch, _, _ = ce.recompute_comparison(
        case, content, query, typed_result(query, [Decimal("9007199254740992")]).to_bytes()
    )
    assert mismatch["outcome"] == "mismatch"


def test_blank_is_not_zero_and_duplicate_rows_cannot_disappear():
    query = b"EVALUATE 'Orders'"
    case = plan_fixture()["cases"][0]
    blank = typed_result(query, [None])
    zero = typed_result(query, [Decimal(0)])
    assert ce.compare_results(blank, zero, case)["outcome"] == "mismatch"
    twice = typed_result(query, [Decimal(1), Decimal(1)])
    once = typed_result(query, [Decimal(1)])
    case["normalization"]["row_order"] = "unordered"
    assert ce.compare_results(twice, once, case)["outcome"] == "mismatch"


def test_only_declared_unordered_order_is_normalized():
    query = b"EVALUATE 'Orders'"
    case = plan_fixture()["cases"][0]
    first = typed_result(query, [Decimal(1), Decimal(2)])
    reversed_rows = typed_result(query, [Decimal(2), Decimal(1)])
    assert ce.compare_results(first, reversed_rows, case)["outcome"] == "mismatch"
    case["normalization"]["row_order"] = "unordered"
    assert ce.compare_results(first, reversed_rows, case)["outcome"] == "match"
    case["normalization"]["absolute_tolerance"] = "0.01"
    with pytest.raises(ce.EvidenceError, match="^NUMERIC_CONTEXT_UNESTABLISHED$"):
        ce.compare_results(first, reversed_rows, case)


def test_computed_numeric_match_remains_context_unestablished_not_image_grade(package):
    result = collect(package, plan_role="comparison-plan.json")
    assert result.observation["data"]["status"] == "DATA_OK", result.observation
    numeric = result.observation["numeric"]
    assert numeric["code"] == "NUMERIC_CONTEXT_UNESTABLISHED", numeric
    assert numeric["cases"][0]["comparison"]["outcome"] == "match"
    assert numeric["status"] == "CANNOT_ESTABLISH"
    ce.verify_payloads(result.observation, result.payloads)


@pytest.mark.parametrize("change", ["filter", "kpi", "period", "grain", "foreign_projection", "dashboard"])
def test_unknown_filter_kpi_period_and_projection_do_not_become_visual_proof(package, change):
    plan = plan_fixture()
    case = plan["cases"][0]
    if change == "filter":
        case["context"]["filters"] = [{"field": "Region", "value": "West"}]
    elif change == "kpi":
        case["context"]["kpi"] = "Not Amount"
    elif change == "period":
        case["context"]["period"] = "previous-period"
    elif change == "grain":
        case["grain"] = ["Amount"]
    elif change == "foreign_projection":
        case["projection"] = "Other.Amount"
    else:
        case["view_kind"] = "dashboard"
    write(package / "comparison-plan.json", plan)
    runtime = FakeRuntime()
    result = collect(package, runtime, plan_role="comparison-plan.json")
    assert result.observation["numeric"]["code"] in {"NUMERIC_CONTEXT_UNESTABLISHED", "NUMERIC_COVERAGE_MISSING"}
    assert "execute" not in runtime.events


def test_payload_rehash_refuses_query_and_result_byte_replacement(package):
    result = collect(package, plan_role="comparison-plan.json")
    for suffix, code in (("query.dax", "QUERY_HASH_MISMATCH"), ("result.json", "RESULT_HASH_MISMATCH")):
        selected = next(payload for payload in result.payloads if payload.role.endswith(suffix))
        changed = tuple(
            replace(payload, blob=payload.blob + b" ") if payload is selected else payload
            for payload in result.payloads
        )
        with pytest.raises(ce.EvidenceError, match=f"^{code}$"):
            ce.verify_payloads(result.observation, changed)


def test_result_cannot_name_a_different_executed_query():
    with pytest.raises(ce.EvidenceError, match="^QUERY_HASH_MISMATCH$"):
        ce.recompute_comparison(
            plan_fixture()["cases"][0],
            b"Amount\r\n1\r\n",
            b"EVALUATE 'Orders'",
            typed_result(b"EVALUATE 'Other'", [Decimal(1)]).to_bytes(),
        )


def test_privacy_never_echoes_native_exception_or_raw_samples(package):
    runtime = FakeRuntime()

    def explode(_request):
        raise RuntimeError(r"C:\fixture\secret.pbip https://secret.invalid/?token=secret")

    runtime.bind = explode
    result = collect(package, runtime)
    encoded = result.to_bytes()
    assert result.observation["data"]["code"] == "TOOL_UNAVAILABLE"
    assert b"Private" not in encoded and b"secret" not in encoded and b"https:" not in encoded
    assert not result.payloads


@pytest.mark.parametrize("field,value", [("sampled_rows", True), ("sampled_rows", 1.0), ("total_rows", 1)])
def test_closed_observation_counts_are_not_coerced(package, field, value):
    result = collect(package)
    observation = copy.deepcopy(result.observation)
    observation["data"]["canaries"][0][field] = value
    with pytest.raises(ce.EvidenceError, match="^EVIDENCE_INVALID$"):
        ce.validate_observation(observation)


def test_same_byte_json_reader_rejects_duplicates_and_nonfinite_numbers():
    for content in (
        b'{"schema_version":1,"schema_version":1,"cases":[]}',
        b'{"schema_version":1,"cases":[],"extra":NaN}',
    ):
        with pytest.raises(ce.EvidenceError, match="^PLAN_INVALID$"):
            ce.read_plan(content)


def test_manifest_toctou_is_detected_while_csv_hash_is_from_held_bytes(package, monkeypatch):
    original = ce._held_file
    reads = 0

    def changed(root, role):
        nonlocal reads
        content = original(root, role)
        if role == "oracle/oracle-manifest.json":
            reads += 1
            if reads == 2:
                return content + b" "
        return content

    monkeypatch.setattr(ce, "_held_file", changed)
    with pytest.raises(ce.EvidenceError, match="^INPUT_CHANGED$"):
        ce.read_tableau_csv(package, identity_for(package), "oracle/oracle-manifest.json", VIEW, "worksheet")


def test_touched_cache_and_boolean_success_never_derive_persisted():
    verdict = refresh.derive_data_verdict([("Orders", 1)], False, wanted_save=True, commit=True)
    assert verdict.code == "NOT_PERSISTED"
    assert verdict.persisted is False


def test_missing_native_witness_fails_closed_without_running_source_queries(package, monkeypatch):
    monkeypatch.setattr(ce._Runtime, "bind", lambda self, request: BOUND)

    def unavailable(*_args):
        raise ce.EvidenceError("DEFINITION_UNSUPPORTED")

    monkeypatch.setattr(ce._Runtime, "witness", unavailable)
    monkeypatch.setattr(ce._Runtime, "probe", lambda *_args: pytest.fail("no native witness, no source query"))
    result = ce.collect_completion_evidence(package, ce.EvidenceRequest(1234, ("Orders", "Customers"), True))
    assert result.observation["data"]["code"] == "DEFINITION_UNSUPPORTED"
    assert result.observation["data"]["status"] == "CANNOT_ESTABLISH"


def test_private_query_with_a_host_location_is_withheld_not_redacted():
    table = "https://example.invalid"
    query = f"EVALUATE TOPN(1, '{table}')"
    observed = ce.dax.TypedResult(
        hashlib.sha256(query.encode()).hexdigest(), (("x", "int64"),), ((ce.dax.typed_value(1),),)
    )
    binding = ce.SourceBinding("source-key:" + "a" * 16, table, "part", "import")
    with pytest.raises(ce.EvidenceError, match="^PRIVACY$"):
        ce._canary_payloads(binding, pdq.CanaryObservation(table, query, observed, 1))
