"""Production-route controls for PR #604's six blind-review reproductions.

Current writers supply the positive audit corpus; malformed copies change one fact at a time.
Legacy unkeyed attempts are readable but cannot earn current proof. Unscoped, incomplete or
noncanonical legacy rows deliberately yield cannot_establish rather than being silently dropped.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_probe_earned_clear import cg as gate, pls as probe
from test_package_role_identity import DS_LUID, PUBLISHED_KEY, datasource_package, workbook_package
from credential_gate import _audit, _audit_entries, _DuplicateJsonKey, _override_is_authentic, _reject_duplicate_keys
from package_filesystem import is_canonical_key
from package_role_identity import verify_phase1_role_identity
from preflight_source_credentials import _leg_key
from probe_live_source import _probe_leg, _probe_one_table

LIVE = {"class": "sqlserver", "server": "source.example", "database": "db", "powerbi_target": "live_source"}
OTHER = {**LIVE, "server": "other.example"}
FLAT = {"class": "excel-direct", "powerbi_target": "flat_file"}
REVIEW = {"class": "unknown", "server": "review.example", "powerbi_target": "unknown"}
KEY = _leg_key({}, 0, LIVE)
OTHER_KEY = _leg_key({}, 0, OTHER)
LOCAL = {
    "self_contained": True,
    "omissions": [],
    "neutralized": [],
    "retained_network": [],
    "shipped": [],
    "binding": None,
    "parameter": None,
    "bytes": 0,
}


def _spec(*connections: dict) -> dict:
    return {
        "data_sources": [
            {
                "name": f"source{index}",
                "connection": dict(connection),
                "tables": [{"name": "Orders"}],
                "fields": [{"kind": "column", "internal_name": "[ID]"}],
            }
            for index, connection in enumerate(connections)
        ]
    }


def _assess(
    root: Path, *, spec: dict | None = None, local: dict | None = None, authorized: bool = False
) -> gate.DataAccessAssessment:
    return gate.assess_data_access(
        root,
        package_spec=_spec(LIVE) if spec is None else spec,
        package_data_sources=copy.deepcopy(LOCAL) if local is None else local,
        fallback_authorization="model_only_unvalidated" if authorized else "stop",
        requested_scope="model_only" if authorized else "model_and_report",
    )


@pytest.fixture(name="root")
def _root_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use real marker/audit writers without changing this machine's ACLs or process lineage."""
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate, "_ancestry", lambda: [])
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / gate.MIGRATION_SPEC).write_text(json.dumps(_spec(LIVE, OTHER)), encoding="utf-8")
    return unit


@pytest.fixture(name="desktop")
def _desktop_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only external primitives; model construction, audit and gate-lift code stay real."""
    monkeypatch.setattr(probe, "_host_resolves", lambda _host: True)
    monkeypatch.setattr(probe, "_network_fault_observed", lambda _connection: False)
    monkeypatch.setattr(probe, "_open_desktop", lambda _path: 4242)
    monkeypatch.setattr(probe, "_record_desktop_lifecycle", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(probe, "_wait_for_catalog", lambda _pid: True)
    monkeypatch.setattr(probe, "_refresh_and_classify", lambda *_args: (0, "DATA_OK"))
    monkeypatch.setattr(probe, "_close", lambda *_args: True)


def _rows(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / gate.AUDIT).read_text(encoding="utf-8").splitlines()]


def _write_rows(root: Path, rows: list[dict]) -> None:
    (root / gate.AUDIT).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _earn(root: Path) -> None:
    assert gate.apply_block(root, [KEY]) == 0
    assert _probe_leg(root, KEY, LIVE, ([{"name": "Orders"}], "ID"), (1, False)) == (0, "DATA_OK")
    assert gate.clear_block(root, "probe-cleared: DATA_OK from one leg", earned=True, sources=[KEY]) == 0
    assert _assess(root).state == "live_data_ok", "positive control: real keyed writers must earn"


def _authorize(root: Path, sources: list[str] | None = None) -> None:
    assert gate.apply_block(root, [KEY] if sources is None else sources) == 0
    assert gate.authorize(root, "Fixture Human") == 0


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("action", ["block-marker-only", "probe-data_ok", "probe-cleared"])
@pytest.mark.parametrize("field", ["detail", "user"])
def test_audit_requires_each_production_field(root: Path, action: str, field: str) -> None:
    """Removing one base field from otherwise earned production history invalidates the trail."""
    _earn(root)
    rows = _rows(root)
    next(row for row in rows if row["action"] == action).pop(field)
    _write_rows(root, rows)
    result = _assess(root)
    assert (result.state, result.codes) == ("cannot_establish", ("audit-malformed",))


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize(
    "field,value",
    [
        ("detail", True),
        ("user", False),
        ("user", ""),
        ("user", " "),
        ("action", "unknown-action"),
        ("action", "probe-unknown"),
        ("sources", None),
        ("sources", True),
        ("sources", [True]),
        ("sources", [""]),
        ("sources", [" "]),
        ("sources", [KEY, KEY]),
        ("sources", ["source-key:invalid"]),
        ("sources", ["source-key:" + "a" * 16 + "\n"]),
        ("unexpected", "field"),
    ],
)
def test_audit_rejects_noncanonical_action_shapes(root: Path, field: str, value: object) -> None:
    """Each mutated field must fail independently of missing fields or unrelated bad rows."""
    _earn(root)
    rows = _rows(root)
    rows[1][field] = value
    _write_rows(root, rows)
    result = _assess(root)
    assert (result.state, result.codes) == ("cannot_establish", ("audit-malformed",))


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize(
    "detail",
    ["not a production arm", "", "sources=[True]", "sources_json=[false]", "sources=[]"],
)
def test_arm_detail_is_canonical_and_agrees_with_sources(root: Path, detail: str) -> None:
    """An arm's structured sources and legacy detail cannot make contradictory identity claims."""
    _earn(root)
    rows = _rows(root)
    rows[0]["detail"] = detail
    _write_rows(root, rows)
    result = _assess(root)
    assert (result.state, result.codes) == ("cannot_establish", ("audit-malformed",))


@pytest.mark.usefixtures("desktop")
def test_audit_rejects_future_times_with_fixed_skew(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound clock skew without refusing an otherwise canonical boundary timestamp."""
    now = datetime.now(timezone.utc).replace(microsecond=0)

    class Clock(datetime):
        """A fixed wall clock, independent of the audit parser's allowed skew."""

        @classmethod
        def now(cls, tz=None) -> datetime:
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(gate, "datetime", Clock)
    _earn(root)
    rows = _rows(root)
    for row in rows:
        row["ts"] = (now + timedelta(minutes=5)).isoformat()
    _write_rows(root, rows)
    assert _assess(root).state == "live_data_ok", "the fixed five-minute boundary is inclusive"
    for future in ((now + timedelta(minutes=5, seconds=1)).isoformat(), "9999-12-31T23:59:59+00:00"):
        rows[-1]["ts"] = future
        _write_rows(root, rows)
        result = _assess(root)
        assert (result.state, result.codes) == ("cannot_establish", ("audit-malformed",))


@pytest.mark.parametrize(
    "detail",
    [
        "authorized",
        "by=human",
        "by=; chain=[]",
        "by=human; chain=True",
        "by=human; chain=[True]",
        "by=human; chain=[' ']",
        "by=human; chain=()",
        "by=human; chain=[ 'python.exe' ]",
        True,
    ],
)
def test_authorization_requires_production_detail(root: Path, detail: object) -> None:
    """The authorize action must carry the writer's actual authorizer/lineage contract."""
    _authorize(root)
    assert _assess(root, authorized=True).state == "authorized_model_only"
    rows = _rows(root)
    next(row for row in rows if row["action"] == "authorize")["detail"] = detail
    _write_rows(root, rows)
    result = _assess(root, authorized=True)
    assert (result.state, result.codes) == ("cannot_establish", ("audit-malformed",))
    assert not _override_is_authentic(root)


@pytest.mark.parametrize("shape", ["minimal", "bool-user", "future", "forbidden-sources", "directory"])
def test_forged_authorization_never_qualifies(root: Path, shape: str) -> None:
    """An action label plus a forged or directory-shaped override confers no authority."""
    _authorize(root)
    rows = _rows(root)
    row = next(row for row in rows if row["action"] == "authorize")
    if shape == "minimal":
        row.pop("user")
        row.pop("detail")
    elif shape == "bool-user":
        row["user"] = True
    elif shape == "future":
        row["ts"] = "9999-01-01T00:00:00+00:00"
    elif shape == "forbidden-sources":
        row["sources"] = [KEY]
    else:
        (root / gate.OVERRIDE).unlink()
        (root / gate.OVERRIDE).mkdir()
    _write_rows(root, rows)
    result = _assess(root, authorized=True)
    expected = (
        ("blocked", ("authorization-mismatch", "stale-clear"))
        if shape == "directory"
        else ("cannot_establish", ("audit-malformed",))
    )
    assert (result.state, result.codes) == expected


@pytest.mark.parametrize(
    "system,chain",
    [("Linux", []), ("Darwin", []), ("Windows", ["python.exe", "pwsh.exe", "WindowsTerminal.exe"])],
)
def test_authentic_authorization_written_on_each_platform(
    root: Path, monkeypatch: pytest.MonkeyPatch, system: str, chain: list[str]
) -> None:
    """Both current platform writers must produce readable, structural-only authorization."""
    monkeypatch.setattr(gate.platform, "system", lambda: system)
    monkeypatch.setattr(gate, "_ancestry", lambda: chain)
    monkeypatch.setattr(gate, "_icacls", lambda _args: (0, ""))
    _authorize(root)
    result = _assess(root, authorized=True)
    assert (result.state, result.validation, result.max_phase2_claim) == (
        "authorized_model_only",
        "unvalidated",
        "structural_only",
    )
    assert gate.parse_data_access(result.dumps()) == result
    assert _audit_entries(root) == _rows(root)
    assert gate.verify(root) == 0


@pytest.mark.parametrize(
    "system,chain,writer_exit",
    [
        pytest.param("Windows", [], 2, id="windows-empty"),
        pytest.param("Windows", ["<lineage-unavailable>"], 2, id="windows-unavailable"),
        pytest.param("Windows", ["python.exe", "<lineage-unavailable>"], 2, id="windows-partly-unavailable"),
        pytest.param("Windows", ["python.exe", "Copilot.EXE"], 2, id="windows-copilot"),
        pytest.param("Windows", ["python.exe", "copilot-helper.exe"], 2, id="windows-copilot-substring"),
        pytest.param("Windows", ["python.exe", "pwsh.exe", "explorer.exe"], 0, id="windows-human"),
        pytest.param("Linux", [], 0, id="posix-empty"),
        pytest.param("Linux", ["copilot"], 0, id="posix-writer-policy"),
        pytest.param("Darwin", [], 0, id="darwin-empty"),
    ],
)
def test_authorization_writer_reader_platform_parity(
    root: Path, monkeypatch: pytest.MonkeyPatch, system: str, chain: list[str], writer_exit: int
) -> None:
    """A real writer refusal is the independent oracle for a forged row's lack of authority."""
    monkeypatch.setattr(gate.platform, "system", lambda: system)
    monkeypatch.setattr(gate, "_ancestry", lambda: chain)
    monkeypatch.setattr(gate, "_icacls", lambda _args: (0, ""))
    assert gate.apply_block(root, [KEY]) == 0
    assert gate.authorize(root, "Fixture Human") == writer_exit
    detail = f"by=Fixture Human; chain={chain}"
    if writer_exit:
        assert not (root / gate.OVERRIDE).exists()
        assert all(row["action"] != "authorize" for row in _rows(root))
        _audit(root, "authorize", detail)
        (root / gate.OVERRIDE).write_text("TEST-ONLY forged override\n", encoding="utf-8")
    else:
        assert next(row["detail"] for row in _rows(root) if row["action"] == "authorize") == detail
    before = _rows(root)
    result = _assess(root, authorized=True)
    if writer_exit:
        assert (result.state, result.codes) == ("cannot_establish", ("audit-malformed",))
        assert not _override_is_authentic(root)
        assert _audit_entries(root) is None
    else:
        assert (result.state, result.validation, result.max_phase2_claim) == (
            "authorized_model_only",
            "unvalidated",
            "structural_only",
        )
        assert _override_is_authentic(root)
        assert _audit_entries(root) == before
    assert _rows(root) == before, "the reader must not rerun authorization or write evidence"


@pytest.mark.parametrize("inherited", [False, True], ids=["direct", "inherited"])
def test_projection_requires_current_keys_for_authorization(root: Path, inherited: bool) -> None:
    """Neither serialized authorization nor a supplied provider may invent an empty source set."""
    _authorize(root)
    authority = _assess(root, authorized=True)
    if inherited:
        authority = gate.assess_data_access(
            root,
            package_spec={},
            package_data_sources={},
            fallback_authorization="stop",
            requested_scope="model_only",
            provider=(gate.provider_reference("Exact_S2_unit"), authority),
        )
    assert gate.parse_data_access(authority.dumps()) == authority
    empty = authority._replace(source_keys=())
    with pytest.raises(gate.DataAccessProjectionError, match="illegal-combination"):
        gate.parse_data_access(empty.dumps())
    result = gate.assess_data_access(
        root,
        package_spec={},
        package_data_sources={},
        fallback_authorization="stop",
        requested_scope="model_only",
        provider=(gate.provider_reference("Exact_S2_unit"), empty),
    )
    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))


@pytest.mark.parametrize(
    "field,value",
    [
        ("omissions", [{"file": "rows.csv", "reason": "missing"}]),
        ("neutralized", ["rows.csv"]),
        ("retained_network", ["rows.csv"]),
        ("self_contained", False),
    ],
)
def test_authorization_cannot_override_incomplete_local_bytes(root: Path, field: str, value: object) -> None:
    """The fallback waives a live measurement, never the presence of local input bytes."""
    _authorize(root)
    assert _assess(root, authorized=True).state == "authorized_model_only"
    result = _assess(root, authorized=True, local={**LOCAL, field: value})
    assert result.state == "blocked"
    assert "local-import-incomplete" in result.codes


@pytest.mark.parametrize("connections", [(REVIEW,), (LIVE, REVIEW)])
def test_authorization_cannot_override_review_legs(root: Path, connections: tuple[dict, ...]) -> None:
    """Unknown/review legs stay in the denominator even when no live key was classifiable."""
    _authorize(root)
    result = _assess(root, spec=_spec(*connections), authorized=True)
    assert result.state == "blocked"
    assert "unknown-target" in result.codes


@pytest.mark.parametrize("arms", [[], [OTHER_KEY], ["legacy display"]])
def test_authorization_requires_current_keyed_arm(root: Path, arms: list[str]) -> None:
    """Readable legacy/empty/sibling arms cannot invent this package's current epoch."""
    _authorize(root, arms)
    assert _audit_entries(root) is not None, "a readable historic arm is not necessarily current key coverage"
    result = _assess(root, authorized=True)
    assert (result.state, result.codes) == ("cannot_establish", ("source-key-set-changed",))


def test_authorization_without_any_arm_cannot_establish(root: Path) -> None:
    """The production authorization writer does not itself create a current arm."""
    assert gate.authorize(root, "Fixture Human") == 0
    assert _audit_entries(root) is not None
    result = _assess(root, authorized=True)
    assert (result.state, result.codes) == ("cannot_establish", ("source-key-set-changed",))


@pytest.mark.parametrize("fault", ["duplicate-package", "duplicate-root", "invalid-key", "new-key", "forced-scope"])
def test_authorization_cannot_override_authority_failures(root: Path, fault: str) -> None:
    """Invalid identities, unmatched source sets and forced scope outrank authorization."""
    spec = _spec(LIVE)
    if fault == "forced-scope":
        (root / gate.MIGRATION_SPEC).unlink()
        assert gate.apply_block(root, [KEY], force_scope=True) == 0
        (root / gate.MIGRATION_SPEC).write_text(json.dumps(spec), encoding="utf-8")
    _authorize(root)
    if fault == "duplicate-package":
        spec = _spec(LIVE, LIVE)
    elif fault == "duplicate-root":
        (root / gate.MIGRATION_SPEC).write_text(json.dumps(_spec(LIVE, LIVE)), encoding="utf-8")
    elif fault == "invalid-key":
        spec = _spec({**LIVE, "server": None})
    elif fault == "new-key":
        spec = _spec(OTHER)
    result = _assess(root, spec=spec, authorized=True)
    code = (
        "forced-scope"
        if fault == "forced-scope"
        else ("source-key-set-changed" if fault == "new-key" else "source-key-invalid")
    )
    assert (result.state, result.codes) == ("cannot_establish", (code,))


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("authorized", [False, True], ids=["earned", "authorized"])
@pytest.mark.parametrize(
    "fault,code",
    [
        ("identical-duplicate", "source-key-invalid"),
        ("invalid-element", "spec-unreadable"),
        ("duplicate-json-field", "spec-unreadable"),
    ],
)
def test_current_root_facts_are_not_silently_normalized(root: Path, authorized: bool, fault: str, code: str) -> None:
    """Loader deduplication/filtering must not hide invalid current keys from either authority."""
    if authorized:
        _authorize(root)
    else:
        _earn(root)
    assert _assess(root, authorized=authorized).state in {"live_data_ok", "authorized_model_only"}
    spec = _spec(LIVE)
    if fault == "identical-duplicate":
        spec["data_sources"].append(copy.deepcopy(spec["data_sources"][0]))
    elif fault == "invalid-element":
        spec["data_sources"].append(False)
    text = json.dumps(spec)
    if fault == "duplicate-json-field":
        text = '{"data_sources": [],' + text[1:]
    (root / gate.MIGRATION_SPEC).write_text(text, encoding="utf-8")
    result = _assess(root, authorized=authorized)
    assert (result.state, result.codes) == ("cannot_establish", (code,))


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("authorized", [False, True], ids=["earned", "authorized"])
def test_native_engine_root_keeps_the_canonical_adapter(root: Path, authorized: bool) -> None:
    """A native engine root with no parser spec must retain its existing data-source adapter."""
    (root / gate.MIGRATION_SPEC).unlink()
    (root / "report.json").write_text(json.dumps(_spec(LIVE)), encoding="utf-8")
    (root / "input_manifest.json").write_text("{}", encoding="utf-8")
    if authorized:
        _authorize(root)
    else:
        _earn(root)
    assert _assess(root, authorized=authorized).state == ("authorized_model_only" if authorized else "live_data_ok")


@pytest.mark.usefixtures("desktop")
def test_all_current_audit_writers_remain_readable(root: Path) -> None:
    """Diagnostics may lack sources, and duplicate-source refusal rows must not poison history."""
    _audit(root, "engine-receipt", "sha256=" + "a" * 64)
    _audit(root, "probe-error", "legacy unkeyed attempt")
    assert gate.apply_block(root, [KEY, OTHER_KEY]) == 0
    assert _probe_leg(root, KEY, LIVE, ([{"name": "Orders"}], "ID"), (1, False)) == (0, "DATA_OK")
    assert gate.clear_block(root, "first leg", earned=True, sources=[KEY]) == 0
    assert gate.apply_block(root, [KEY]) == 0
    assert gate.apply_block(root, [KEY, KEY]) == 2
    assert gate.clear_block(root, "duplicate refusal", earned=True, sources=[KEY, KEY]) == 1
    assert _assess(root).state == "live_data_ok"
    assert gate.clear_block(root, "manual diagnostic") == 0
    assert gate.authorize(root, "Fixture Human") == 0
    assert gate.apply_block(root, [KEY]) == 0
    rows = _rows(root)
    assert _audit_entries(root) == rows, "each real writer's schema must survive the strict reader"
    assert {row["action"] for row in rows} == {
        "engine-receipt",
        "probe-error",
        "block-marker-only",
        "probe-data_ok",
        "probe-cleared",
        "block-skipped",
        "violation",
        "manual-clear",
        "authorize",
    }


@pytest.mark.usefixtures("desktop")
def test_legacy_unkeyed_success_is_readable_but_not_earned(root: Path) -> None:
    """The pre-key probe writer remains readable, without becoming evidence for any key."""
    assert gate.apply_block(root, [KEY]) == 0
    _audit(root, "probe-data_ok", "Orders -> DATA_OK")
    assert gate.clear_block(root, "legacy success", earned=True, sources=[KEY]) == 0
    assert _audit_entries(root) == _rows(root)
    result = _assess(root)
    assert (result.state, result.codes) == ("blocked", ("stale-clear",))
    _earn(root)


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("detail", ["sources=['legacy source']", 'sources_json=["legacy source"]'])
def test_legacy_arm_names_stay_readable_without_becoming_current_coverage(root: Path, detail: str) -> None:
    """Pre-structured-source arms remain diagnostic history, never current keyed authority."""
    _audit(root, "block-marker-only", detail)
    assert _audit_entries(root) == _rows(root)
    assert _assess(root).codes == ("source-key-set-changed",)
    _earn(root)


@pytest.mark.parametrize("field", ["omissions", "neutralized", "retained_network", "shipped"])
@pytest.mark.parametrize("value", [(), {}, "", False, None])
def test_local_json_arrays_are_actual_lists(root: Path, field: str, value: object) -> None:
    """Empty non-list iterables must not masquerade as empty canonical JSON arrays."""
    assert _assess(root, spec=_spec(FLAT)).state == "local_import_ready"
    result = _assess(root, spec=_spec(FLAT), local={**LOCAL, field: value})
    assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",))


@pytest.mark.parametrize(
    "field,value",
    [
        ("self_contained", 1),
        ("self_contained", "true"),
        ("binding", False),
        ("binding", []),
        ("parameter", True),
        ("bytes", True),
        ("omissions", [False]),
        ("neutralized", [{}]),
        ("retained_network", [False]),
        ("shipped", ["rows.csv"]),
    ],
)
def test_local_fact_types_precede_completeness(root: Path, field: str, value: object) -> None:
    """A false completeness flag must not short-circuit malformed-field validation."""
    local = {**LOCAL, "self_contained": False, field: value}
    result = _assess(root, spec=_spec(FLAT), local=local)
    assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",))


@pytest.mark.parametrize(
    "path,value",
    [
        (("data_sources",), ()),
        (("data_sources", 0, "connection"), []),
        (("data_sources", 0, "connection"), False),
        (("data_sources", 0, "connection", "connections"), ()),
        (("data_sources", 0, "connection", "connections"), {}),
        (("data_sources", 0, "connection", "connections"), [False]),
        (("data_sources", 0, "connection", "class"), False),
        (("data_sources", 0, "connection", "powerbi_target"), []),
        (("data_sources", 0, "connection", "server"), True),
        (("data_sources", 0, "connection", "port"), True),
        (("data_sources", 0, "tables"), ()),
        (("data_sources", 0, "fields"), ()),
    ],
)
def test_spec_fact_shapes_precede_classifier_fallback(root: Path, path: tuple, value: object) -> None:
    """The classifier must not turn malformed falsey facts into absent/default facts."""
    spec = _spec(FLAT)
    assert _assess(root, spec=spec).state == "local_import_ready"
    parent = spec
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    result = _assess(root, spec=spec)
    assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",))


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("sql", ["", "-- comment only", "/* comment only */", None])
def test_custom_sql_preprocessing_failure_invalidates_earned_key(root: Path, sql: str | None) -> None:
    """Comment-only SQL cannot leave the same key's earlier earned state green."""
    _earn(root)
    table = {"name": "Query", "source_relation": "custom-sql", "custom_sql": sql}
    assert _probe_one_table(root, KEY, LIVE, (table, "ProbeOK"), (1, False)) == (1, "ERROR")
    last = _rows(root)[-1]
    assert (last["action"], last["sources"]) == ("probe-error", [KEY])
    assert _assess(root).state == "blocked"
    assert _assess(root).codes == ("probe-error",)


@pytest.mark.usefixtures("desktop")
def test_malformed_custom_sql_type_also_records_its_terminal_error(root: Path) -> None:
    """A preprocessing exception, rather than a handled ValueError, must still invalidate proof."""
    _earn(root)
    table = {"name": "Query", "source_relation": "custom-sql", "custom_sql": True}
    with pytest.raises(TypeError):
        _probe_one_table(root, KEY, LIVE, (table, "ProbeOK"), (1, False))
    assert (_rows(root)[-1]["action"], _rows(root)[-1]["sources"]) == ("probe-error", [KEY])
    assert _assess(root).codes == ("probe-error",)


@pytest.mark.usefixtures("desktop")
def test_custom_sql_success_is_keyed_and_earns_only_after_clear(root: Path) -> None:
    """Real custom-query scaffolding reaches the same keyed attempt/clear boundary."""
    assert gate.apply_block(root, [KEY]) == 0
    table = {"name": "Query", "source_relation": "custom-sql", "custom_sql": "SELECT 'private-query-value'"}
    assert _probe_one_table(root, KEY, LIVE, (table, "ProbeOK"), (1, False)) == (0, "DATA_OK")
    last = _rows(root)[-1]
    assert (last["action"], last["sources"]) == ("probe-data_ok", [KEY])
    assert "private-query-value" not in json.dumps(last)
    assert _assess(root).state == "blocked"
    assert gate.clear_block(root, "custom query returned a row", earned=True, sources=[KEY]) == 0
    assert _assess(root).state == "live_data_ok"


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize(
    "stage", ["build_m_query", "_write_probe_model", "_open_desktop", "_wait_for_catalog", "_refresh_and_classify"]
)
def test_exceptional_attempts_record_safe_keyed_errors_before_cleanup(
    root: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Every exception keeps its original behavior but leaves a safe keyed audit first."""
    _earn(root)
    secret = str(root / "private-cause")
    cleanup = []

    def fail(*_args, **_kwargs) -> None:
        raise RuntimeError(secret)

    def close(*_args) -> bool:
        cleanup.append(_rows(root)[-1])
        return True

    monkeypatch.setattr(probe, stage, fail)
    monkeypatch.setattr(probe, "_close", close)
    with pytest.raises(RuntimeError, match="private-cause"):
        _probe_one_table(root, KEY, LIVE, ({"name": "Orders"}, "ID"), (1, False))
    last = _rows(root)[-1]
    assert (last["action"], last["sources"]) == ("probe-error", [KEY])
    assert secret not in json.dumps(last)
    assert _assess(root).codes == ("probe-error",)
    if stage in {"_wait_for_catalog", "_refresh_and_classify"}:
        assert cleanup == [last], "record must precede the slow Desktop close"
    else:
        assert not cleanup


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("source_index", [None, 0])
def test_production_resolution_error_invalidates_old_key(root: Path, source_index: int | None) -> None:
    """Both run_probe entry selections must record table-resolution errors for known legs."""
    _earn(root)
    spec = _spec(LIVE)
    spec["data_sources"][0]["tables"] = []
    (root / gate.MIGRATION_SPEC).write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        probe.run_probe(root, source_index, 1, False)
    assert error.value.code == 1
    assert (_rows(root)[-1]["action"], _rows(root)[-1]["sources"]) == ("probe-error", [KEY])
    assert _assess(root).codes == ("probe-error",)


@pytest.mark.usefixtures("desktop")
def test_live_key_skip_is_recorded_without_becoming_success(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A skipped terminal attempt invalidates old proof while retaining its original verdict."""
    _earn(root)
    monkeypatch.setattr(probe, "_refresh_and_classify", lambda *_args: (0, "SKIPPED"))
    assert _probe_one_table(root, KEY, LIVE, ({"name": "Orders"}, "ID"), (1, False)) == (0, "SKIPPED")
    assert (_rows(root)[-1]["action"], _rows(root)[-1]["sources"]) == ("probe-skipped", [KEY])
    assert _assess(root).codes == ("live-probe-skipped",)


@pytest.mark.parametrize(
    "unit",
    [
        r"C:\customer\Provider",
        r"\\host\share\Provider",
        "/customer/Provider",
        "../Provider",
        r"..\Provider",
        ".",
        "..",
        "Provider/child",
        "Provider\\child",
        "customer text",
        pytest.param("Shared Sales", id="raw-spaces"),
        pytest.param("Sales.Report", id="raw-dots"),
        "Superstore",
        "Exact_S2_unit",
        "Provider\n",
        "Provider:secret",
        " Provider",
        "Provider?token=secret",
    ],
)
def test_provider_identity_has_one_closed_syntax(root: Path, unit: str) -> None:
    """Even a valid raw S2 name must never enter the assessment or projection directly."""
    provider = _assess(root, spec=_spec(FLAT))
    result = gate.assess_data_access(
        root,
        package_spec={},
        package_data_sources={},
        fallback_authorization="stop",
        requested_scope="report_only_shared_model",
        provider=(unit, provider),
    )
    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))
    assert unit not in result.dumps()
    payload = {
        **provider.to_json(),
        "state": "provider_inherited",
        "codes": ["provider-exact"],
        "provider_unit": unit,
        "provider_state": provider.state,
        "effective_scope": "report_only_shared_model",
    }
    with pytest.raises(gate.DataAccessProjectionError) as error:
        gate.parse_data_access(json.dumps(payload))
    assert error.value.reason == "provider-unit-invalid"
    assert unit not in str(error.value)


@pytest.mark.parametrize(
    "unit", ["Superstore", "Exact_S2_unit", "Sales-2026_09", "9f18a6", "Shared Sales", "Sales.Report"]
)
def test_valid_provider_reference_is_preserved_exactly_without_search(
    root: Path, monkeypatch: pytest.MonkeyPatch, unit: str
) -> None:
    """Conversion and inheritance are pure; the wire carries only the opaque reference."""
    provider = _assess(root, spec=_spec(FLAT))

    def forbidden(*_args, **_kwargs) -> None:
        pytest.fail("provider path searched direct evidence instead of using S2's supplied identity")

    monkeypatch.setattr(gate, "_read_audit_trail", forbidden)
    monkeypatch.setattr(gate, "_classify_legs", forbidden)
    monkeypatch.setattr(gate, "load_bundle", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    reference = gate.provider_reference(unit)
    result = gate.assess_data_access(
        root,
        package_spec={},
        package_data_sources={},
        fallback_authorization="stop",
        requested_scope="report_only_shared_model",
        provider=(reference, provider),
    )
    assert (result.state, result.provider_unit) == ("provider_inherited", reference)
    assert unit not in result.dumps()
    assert gate.parse_data_access(result.dumps()) == result


@pytest.mark.parametrize("unit", ["Shared Sales", "Sales.Report"])
def test_s2_selected_provider_unit_converts_to_the_same_opaque_reference(tmp_path: Path, unit: str) -> None:
    """Real S2 cohort resolution, not the data-access validator, establishes the valid unit."""
    provider_root = datasource_package(tmp_path / unit, unit=unit, published_key=PUBLISHED_KEY)
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": unit, "site": "sales-site", "key": PUBLISHED_KEY, "luid": DS_LUID},
        binding=f"../../../{unit}/fabric/{unit}.SemanticModel",
    )
    provider_result, consumer_result = verify_phase1_role_identity([provider_root, consumer])
    assert (provider_result.verdict, consumer_result.verdict) == ("START_READY", "START_READY")
    selected = consumer_result.dependencies[0]
    assert (selected.state, selected.provider_unit) == ("resolved", unit)
    reference = gate.provider_reference(selected.provider_unit)
    assert reference == gate.provider_reference(provider_result.unit)
    provider = _assess(provider_root, spec=_spec(FLAT))
    result = gate.assess_data_access(
        consumer,
        package_spec={},
        package_data_sources={},
        fallback_authorization="stop",
        requested_scope="report_only_shared_model",
        provider=(reference, provider),
    )
    assert (result.state, result.provider_unit) == ("provider_inherited", reference)
    assert unit not in result.dumps()
    assert gate.parse_data_access(result.dumps()) == result


def test_provider_reference_is_versioned_stable_and_preserves_exact_s2_identity() -> None:
    """A fixed domain/full digest preserves space, dot, case and Unicode identity distinctions."""
    units = (
        "Shared Sales",
        "Shared.Sales",
        "Shared_Sales",
        "shared Sales",
        "Shared sales",
        "Shared  Sales",
        "Sales.Report",
        "sales.Report",
        "Sales.report",
        "Sales Report",
        "\u00e9",
        "e\u0301",
        "\U0001f600",
        "\ud83d\ude00",
    )
    references = [gate.provider_reference(unit) for unit in units]
    assert len(set(references)) == len(units), "distinct exact S2 units must not collapse"
    for unit, reference in zip(units, references, strict=True):
        assert is_canonical_key(unit) and "/" not in unit
        expected = hashlib.sha256(
            b"phase1-data-access/provider-unit/v1\0" + unit.encode("utf-8", "surrogatepass")
        ).hexdigest()
        assert reference == "provider-ref:v1:sha256:" + expected
        assert reference == gate.provider_reference(unit)
        assert re.fullmatch(r"provider-ref:v1:sha256:[0-9a-f]{64}", reference)


@pytest.mark.parametrize(
    "unit",
    [
        None,
        False,
        True,
        0,
        [],
        {},
        Path("Provider"),
        "",
        " ",
        ".",
        "..",
        r"C:\customer\Provider",
        r"\\host\share\Provider",
        "/customer/Provider",
        "../Provider",
        r"..\Provider",
        "Provider/child",
        r"Provider\child",
        "Provider\n",
        "Provider\x00",
        "Provider:secret",
        "Provider?token=secret",
        " Provider",
        "Provider ",
        "Provider.",
        "CON",
        "COM1.txt",
    ],
)
def test_provider_reference_refuses_invalid_units_without_serializing_them(unit: object) -> None:
    """The conversion applies S2's component predicate, with a closed input-type refusal."""
    with pytest.raises(gate.DataAccessProjectionError) as raised:
        gate.provider_reference(unit)
    assert raised.value.reason == "provider-unit-invalid"
    assert raised.value.args == ("data-access projection rejected: provider-unit-invalid",)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "reference",
    [
        "provider-ref:v1:sha256:" + "a" * 63,
        "provider-ref:v1:sha256:" + "a" * 65,
        "provider-ref:v1:sha256:" + "A" * 64,
        "provider-ref:v1:sha256:" + "g" * 64,
        "provider-ref:v2:sha256:" + "a" * 64,
        "provider-ref:v1:sha512:" + "a" * 64,
        "provider-ref:v1:sha256:" + "a" * 64 + "\n",
        " provider-ref:v1:sha256:" + "a" * 64,
    ],
)
def test_provider_reference_parser_accepts_only_the_closed_token(root: Path, reference: str) -> None:
    """Lengths, version, algorithm, case and trailing characters are not repairable."""
    test_provider_identity_has_one_closed_syntax(root, reference)


def test_provider_reference_does_not_rehash_an_existing_reference() -> None:
    """A selected unit and its serialized reference are different input contracts."""
    with pytest.raises(gate.DataAccessProjectionError, match="provider-unit-invalid"):
        gate.provider_reference(gate.provider_reference("Shared Sales"))


def _safe_error(call: Callable, reason: str, secret: str) -> None:
    with pytest.raises(gate.DataAccessProjectionError) as raised:
        call()
    error = raised.value
    assert error.reason == reason
    assert error.args == (f"data-access projection rejected: {reason}",)
    assert error.__cause__ is None
    assert error.__context__ is None, "from None inside except still retains the unsafe raw exception"
    assert error.__suppress_context__ is True
    assert not hasattr(error, "filename")
    assert secret not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid-utf8"])
def test_projection_read_errors_retain_no_unsafe_cause(tmp_path: Path, kind: str) -> None:
    """A safe wrapper message must not retain a raw path-bearing exception in its context."""
    path = tmp_path / "private-filename.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "invalid-utf8":
        path.write_bytes(b"\xff")
    _safe_error(lambda: gate.read_data_access(path), "unreadable", str(path))


@pytest.mark.parametrize("kind", ["duplicate", "malformed", "nonfinite"])
def test_projection_parse_errors_retain_no_raw_payload(tmp_path: Path, kind: str) -> None:
    """JSON rejection objects and tracebacks carry only the closed safe reason."""
    secret = str(tmp_path / "private-key")
    key = json.dumps(secret)
    if kind == "duplicate":
        text, reason = "{" + key + ":1," + key + ":2}", "duplicate-key"
    elif kind == "nonfinite":
        text, reason = "{" + key + ":NaN}", "nonfinite"
    else:
        text, reason = "{" + key + ":", "malformed-json"
    _safe_error(lambda: gate.parse_data_access(text), reason, secret)


def test_duplicate_detection_never_stores_the_key(tmp_path: Path) -> None:
    """The duplicate hook's own exception must not hold the key even before translation."""
    secret = str(tmp_path / "private-key")
    with pytest.raises(_DuplicateJsonKey) as error:
        _reject_duplicate_keys([(secret, 1), (secret, 2)])
    assert error.value.args == ()
    assert vars(error.value) == {}
    assert secret not in str(error.value)
