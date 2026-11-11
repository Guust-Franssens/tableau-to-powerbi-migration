"""Phase-1 data-access projection contract after the credential-gate ledger was retired."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import traceback
from collections.abc import Callable
from pathlib import Path

import pytest

import package_data_access as pda
from test_package_role_identity import DS_LUID, PUBLISHED_KEY, datasource_package, workbook_package
from package_data_access import _DuplicateJsonKey, _reject_duplicate_keys
from package_filesystem import is_canonical_key
from package_role_identity import verify_phase1_role_identity

LIVE = {
    "class": "sqlserver",
    "server": "source.example",
    "database": "db",
    "schema": "dbo",
    "name": "orders-leg",
    "powerbi_target": "live_source",
}
OTHER = {**LIVE, "server": "other.example"}
FLAT = {"class": "excel-direct", "powerbi_target": "flat_file"}
REVIEW = {"class": "unknown", "server": "review.example", "powerbi_target": "unknown"}
# Root files the retired credential gate wrote; an old unit can still carry them, and they are inert.
LEGACY_GATE_FILES = (".credential-gate-BLOCKED.json", ".credential-gate-AUTHORIZED", ".credential-gate-audit.log")


def _ordinary_key(server: str, ordinary_tables: tuple[str, ...] | None = ("Orders",), *, schema: str = "dbo") -> str:
    """Independent SHA-256 oracle for the documented dbo.Orders identity."""
    identity = {
        "class": "sqlserver",
        "server": server,
        "database": "db",
        "schema": schema,
        "ordinary_tables": list(ordinary_tables) if ordinary_tables is not None else None,
    }
    return (
        "source-key:"
        + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    )


KEY = _ordinary_key("source.example")
OTHER_KEY = _ordinary_key("other.example")
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
                "tables": [
                    {"name": "Orders", "table": "[dbo].[Orders]", "connection": connection.get("name", "orders-leg")}
                ],
                "fields": [{"kind": "column", "internal_name": "[ID]"}],
            }
            for index, connection in enumerate(connections)
        ]
    }


def _assess(
    root: Path, *, spec: dict | None = None, local: dict | None = None, authorized: bool = False
) -> pda.DataAccessAssessment:
    return pda.assess_data_access(
        root,
        package_spec=_spec(LIVE) if spec is None else spec,
        package_data_sources=copy.deepcopy(LOCAL) if local is None else local,
        fallback_authorization="model_only_unvalidated" if authorized else "stop",
        requested_scope="model_only" if authorized else "model_and_report",
    )


@pytest.fixture(name="root")
def _root_fixture(tmp_path: Path) -> Path:
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / pda.MIGRATION_SPEC).write_text(json.dumps(_spec(LIVE, OTHER)), encoding="utf-8")
    return unit


def _legacy(state: str, codes: list[str], **fields: object) -> str:
    """A stored projection as the retired ledger-era writer emitted it."""
    payload = {
        "schema": "phase1-data-access/v1",
        "state": state,
        "source_keys": [KEY],
        "provider_unit": None,
        "provider_state": None,
        "validation": "validated",
        "effective_scope": "model_and_report",
        "max_phase2_claim": "data_validated",
        "codes": codes,
        **fields,
    }
    return json.dumps(payload)


LEGACY_LIVE = _legacy("live_data_ok", ["probe-cleared", "probe-data-ok"])
LEGACY_AUTHORIZED = _legacy(
    "authorized_model_only",
    ["brief-model-only", "human-authorize"],
    validation="unvalidated",
    effective_scope="model_only",
    max_phase2_claim="structural_only",
)


def test_live_source_is_admitted_unproven_never_validated(root: Path) -> None:
    """Live keys yield a dispatchable but unvalidated state unless the brief degrades to model-only."""
    unproven = ("live_unproven", ("live-unproven",), "unvalidated", "data_validated")
    degraded = ("authorized_model_only", ("brief-model-only",), "unvalidated", "structural_only")
    for scope, fallback, expected in (
        ("model_and_report", "stop", unproven),
        ("model_and_report", "model_only_unvalidated", unproven),
        ("model_only", "model_only_unvalidated", degraded),
    ):
        result = pda.assess_data_access(
            root,
            package_spec=_spec(LIVE),
            package_data_sources=copy.deepcopy(LOCAL),
            fallback_authorization=fallback,
            requested_scope=scope,
        )
        assert (result.state, result.codes, result.validation, result.max_phase2_claim) == expected
        assert (result.source_keys, result.effective_scope, result.provider_unit) == ((KEY,), scope, None)
        assert result.state in pda.DIRECT_ACCEPTED_STATES
        assert pda.parse_data_access(result.dumps()) == result
        facts = pda.package_spec_facts(_spec(LIVE))
        assert (
            pda.reconcile_package_data_access(result, facts, requested_scope=scope, fallback_authorization=fallback)
            is result
        )


def test_local_only_package_stays_validated(root: Path) -> None:
    """Flat-file packages keep the only producer-side validated state."""
    result = _assess(root, spec=_spec(FLAT))
    assert (result.state, result.validation, result.source_keys) == ("local_import_ready", "validated", ())


def test_assessment_reads_no_gate_evidence(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Marker, override and audit files neither block nor clear; no root file is read at all."""
    authorized = False
    expected = _assess(root, authorized=authorized)
    for name in LEGACY_GATE_FILES:
        (root / name).write_text("{not json", encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the projection read root evidence")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert _assess(root, authorized=authorized) == expected
    assert _assess(root.parent / "absent", authorized=authorized) == expected


def test_incomplete_local_bytes_block_every_direct_state(root: Path) -> None:
    """Neither admission nor the model-only fallback waives the presence of local input bytes."""
    for field, value, authorized in (
        ("omissions", [{"file": "rows.csv", "reason": "missing"}], True),
        ("neutralized", ["rows.csv"], False),
        ("retained_network", ["rows.csv"], False),
        ("self_contained", False, False),
    ):
        result = _assess(root, authorized=authorized, local={**LOCAL, field: value})
        assert (result.state, result.codes, result.source_keys) == ("blocked", ("local-import-incomplete",), (KEY,))


def test_review_legs_block_every_direct_state(root: Path) -> None:
    """Unknown/review legs stay in the denominator even when no live key was classifiable."""
    for connections, authorized in (((REVIEW,), False), ((LIVE, REVIEW), True)):
        result = _assess(root, spec=_spec(*connections), authorized=authorized)
        assert (result.state, result.codes) == ("blocked", ("unknown-target",))


@pytest.mark.parametrize("fault", ["duplicate-package", "invalid-key"])
def test_invalid_package_identities_cannot_establish(root: Path, fault: str) -> None:
    """Duplicate or unkeyable live legs are an untrusted identity, not an admitted source."""
    spec = _spec(LIVE, LIVE) if fault == "duplicate-package" else _spec({**LIVE, "server": None})
    for authorized in (False, True):
        result = _assess(root, spec=spec, authorized=authorized)
        assert (result.state, result.codes, result.source_keys) == ("cannot_establish", ("source-key-invalid",), ())


def test_reconcile_refuses_a_live_projection_whose_keys_moved() -> None:
    """A stored live_unproven projection binds to the package's current key set."""
    stored = pda.parse_data_access(
        _legacy("live_unproven", ["live-unproven"], validation="unvalidated", source_keys=[KEY])
    )
    facts = pda.package_spec_facts(_spec(OTHER))
    result = pda.reconcile_package_data_access(
        stored, facts, requested_scope="model_and_report", fallback_authorization="stop"
    )
    assert (result.state, result.codes) == ("cannot_establish", ("source-key-set-changed",))


@pytest.mark.parametrize("document", [LEGACY_LIVE, LEGACY_AUTHORIZED], ids=["live_data_ok", "human-authorize"])
def test_legacy_stored_documents_still_parse_and_reconcile(document: str) -> None:
    """Old runs' data-access.json keeps its exact meaning; nothing is rewritten or downgraded."""
    stored = pda.parse_data_access(document)
    assert json.loads(stored.dumps()) == json.loads(document)
    assert stored.state in pda.DIRECT_ACCEPTED_STATES
    scope, fallback = (
        ("model_only", "model_only_unvalidated")
        if stored.state == "authorized_model_only"
        else ("model_and_report", "stop")
    )
    result = pda.reconcile_package_data_access(
        stored, pda.package_spec_facts(_spec(LIVE)), requested_scope=scope, fallback_authorization=fallback
    )
    assert result is stored


def test_legacy_provider_inherited_live_data_ok_still_parses() -> None:
    """A consumer inherited from a legacy validated provider stays readable."""
    stored = pda.parse_data_access(
        _legacy(
            "provider_inherited",
            ["provider-exact"],
            provider_unit=pda.provider_reference("Shared"),
            provider_state="live_data_ok",
            effective_scope="report_only_shared_model",
        )
    )
    assert (stored.provider_state, stored.validation) == ("live_data_ok", "validated")


def test_unproven_states_cannot_be_serialized_as_validated() -> None:
    """No stored document may pair live_unproven with validation, or a legacy state with the new code."""
    provider = {"provider_unit": pda.provider_reference("Shared"), "provider_state": "live_unproven"}
    model_only = {"effective_scope": "model_only", "max_phase2_claim": "structural_only"}
    for state, codes, fields in (
        ("live_unproven", ["live-unproven"], {"validation": "validated"}),
        ("live_unproven", ["live-unproven"], {"validation": "unvalidated", "source_keys": []}),
        ("live_unproven", ["live-unproven"], {"validation": "unvalidated", "max_phase2_claim": "structural_only"}),
        ("live_unproven", ["live-unproven"], {"validation": "unvalidated", "effective_scope": None}),
        ("live_unproven", ["live-unproven", "probe-cleared"], {"validation": "unvalidated"}),
        ("live_unproven", ["probe-cleared", "probe-data-ok"], {"validation": "unvalidated"}),
        ("live_data_ok", ["live-unproven"], {}),
        ("live_data_ok", ["probe-cleared", "probe-data-ok"], {"validation": "unvalidated"}),
        ("authorized_model_only", ["human-authorize"], {"validation": "unvalidated", **model_only}),
        ("authorized_model_only", ["brief-model-only"], {"validation": "validated", **model_only}),
        ("provider_inherited", ["provider-exact"], {**provider, "validation": "validated"}),
        ("provider_inherited", ["provider-exact"], {**provider, "validation": "unvalidated", "source_keys": []}),
        (
            "provider_inherited",
            ["provider-exact"],
            {**provider, "validation": "unvalidated", "max_phase2_claim": "structural_only"},
        ),
    ):
        with pytest.raises(pda.DataAccessProjectionError) as error:
            pda.parse_data_access(_legacy(state, codes, **fields))
        assert error.value.reason == "illegal-combination", (state, codes, fields)


def test_inheriting_an_unproven_provider_stays_unvalidated(root: Path) -> None:
    """A consumer of a live_unproven provider inherits its unvalidated state, never a stronger one."""
    scope = "report_only_shared_model"
    provider = _assess(root)
    assert provider.state == "live_unproven"
    reference = pda.provider_reference("Shared")
    result = pda.assess_data_access(
        root,
        package_spec={},
        package_data_sources={},
        fallback_authorization="stop",
        requested_scope=scope,
        provider=(reference, provider),
    )
    assert (result.state, result.provider_state, result.validation, result.max_phase2_claim) == (
        "provider_inherited",
        "live_unproven",
        "unvalidated",
        "data_validated",
    )
    assert result.source_keys == provider.source_keys
    assert pda.parse_data_access(result.dumps()) == result


@pytest.mark.parametrize("inherited", [False, True], ids=["direct", "inherited"])
def test_projection_requires_current_keys_for_model_only(root: Path, inherited: bool) -> None:
    """Neither serialized model-only authority nor a supplied provider may invent an empty source set."""
    authority = _assess(root, authorized=True)
    assert authority.state == "authorized_model_only"
    if inherited:
        authority = pda.assess_data_access(
            root,
            package_spec={},
            package_data_sources={},
            fallback_authorization="stop",
            requested_scope="model_only",
            provider=(pda.provider_reference("Exact_S2_unit"), authority),
        )
    assert pda.parse_data_access(authority.dumps()) == authority
    empty = authority._replace(source_keys=())
    with pytest.raises(pda.DataAccessProjectionError, match="illegal-combination"):
        pda.parse_data_access(empty.dumps())
    result = pda.assess_data_access(
        root,
        package_spec={},
        package_data_sources={},
        fallback_authorization="stop",
        requested_scope="model_only",
        provider=(pda.provider_reference("Exact_S2_unit"), empty),
    )
    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))


def test_malformed_local_and_spec_facts_cannot_establish(root: Path) -> None:
    """Falsey or mistyped facts must not masquerade as empty/default ones, even beside a false flag."""
    assert _assess(root, spec=_spec(FLAT)).state == "local_import_ready"
    for field, value in (
        ("omissions", ()),
        ("neutralized", {}),
        ("retained_network", None),
        ("shipped", ""),
        ("self_contained", "true"),
        ("binding", False),
        ("bytes", True),
        ("omissions", [False]),
        ("neutralized", [{}]),
        ("shipped", ["rows.csv"]),
    ):
        result = _assess(root, spec=_spec(FLAT), local={**LOCAL, "self_contained": False, field: value})
        assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",)), (field, value)
    for path, value in (
        (("data_sources",), ()),
        (("data_sources", 0, "connection"), []),
        (("data_sources", 0, "connection", "connections"), ()),
        (("data_sources", 0, "connection", "connections"), [False]),
        (("data_sources", 0, "connection", "class"), False),
        (("data_sources", 0, "connection", "server"), True),
        (("data_sources", 0, "connection", "port"), True),
        (("data_sources", 0, "tables"), ()),
        (("data_sources", 0, "fields"), ()),
    ):
        spec = _spec(FLAT)
        parent = spec
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = value
        result = _assess(root, spec=spec)
        assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",)), (path, value)


@pytest.mark.parametrize(
    "unit",
    [r"C:\customer\Provider", "../Provider", "Superstore", "Provider\n", "Provider?token=secret"],
)
def test_provider_identity_has_one_closed_syntax(root: Path, unit: str) -> None:
    """Even a valid raw S2 name must never enter the assessment or projection directly."""
    provider = _assess(root, spec=_spec(FLAT))
    result = pda.assess_data_access(
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
    with pytest.raises(pda.DataAccessProjectionError) as error:
        pda.parse_data_access(json.dumps(payload))
    assert error.value.reason == "provider-unit-invalid"
    assert unit not in str(error.value)


@pytest.mark.parametrize("unit", ["Superstore", "Shared Sales"])
def test_valid_provider_reference_is_preserved_exactly_without_search(
    root: Path, monkeypatch: pytest.MonkeyPatch, unit: str
) -> None:
    """Conversion and inheritance are pure; the wire carries only the opaque reference."""
    provider = _assess(root, spec=_spec(FLAT))

    def forbidden(*_args, **_kwargs) -> None:
        pytest.fail("provider path searched direct evidence instead of using S2's supplied identity")

    monkeypatch.setattr(pda, "_classify_legs", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    reference = pda.provider_reference(unit)
    result = pda.assess_data_access(
        root,
        package_spec={},
        package_data_sources={},
        fallback_authorization="stop",
        requested_scope="report_only_shared_model",
        provider=(reference, provider),
    )
    assert (result.state, result.provider_unit) == ("provider_inherited", reference)
    assert unit not in result.dumps()
    assert pda.parse_data_access(result.dumps()) == result


def test_s2_selected_provider_unit_converts_to_the_same_opaque_reference(tmp_path: Path) -> None:
    """Real S2 cohort resolution, not the data-access validator, establishes the valid unit."""
    unit = "Shared Sales"
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
    reference = pda.provider_reference(selected.provider_unit)
    assert reference == pda.provider_reference(provider_result.unit)
    provider = _assess(provider_root, spec=_spec(FLAT))
    result = pda.assess_data_access(
        consumer,
        package_spec={},
        package_data_sources={},
        fallback_authorization="stop",
        requested_scope="report_only_shared_model",
        provider=(reference, provider),
    )
    assert (result.state, result.provider_unit) == ("provider_inherited", reference)
    assert unit not in result.dumps()
    assert pda.parse_data_access(result.dumps()) == result


def test_provider_reference_is_versioned_stable_and_preserves_exact_s2_identity() -> None:
    """A fixed domain/full digest preserves space, dot, case and Unicode identity distinctions."""
    units = (
        "Shared Sales",
        "Shared.Sales",
        "shared Sales",
        "Shared  Sales",
        "\u00e9",
        "e\u0301",
        "\U0001f600",
        "\ud83d\ude00",
    )
    references = [pda.provider_reference(unit) for unit in units]
    assert len(set(references)) == len(units), "distinct exact S2 units must not collapse"
    for unit, reference in zip(units, references, strict=True):
        assert is_canonical_key(unit) and "/" not in unit
        expected = hashlib.sha256(
            b"phase1-data-access/provider-unit/v1\0" + unit.encode("utf-8", "surrogatepass")
        ).hexdigest()
        assert reference == "provider-ref:v1:sha256:" + expected
        assert reference == pda.provider_reference(unit)
        assert re.fullmatch(r"provider-ref:v1:sha256:[0-9a-f]{64}", reference)


@pytest.mark.parametrize(
    "unit",
    [None, Path("Provider"), "", "..", "Provider/child", "Provider\x00", "Provider:secret", "Provider.", "CON"],
)
def test_provider_reference_refuses_invalid_units_without_serializing_them(unit: object) -> None:
    """The conversion applies S2's component predicate, with a closed input-type refusal."""
    with pytest.raises(pda.DataAccessProjectionError) as raised:
        pda.provider_reference(unit)
    assert raised.value.reason == "provider-unit-invalid"
    assert raised.value.args == ("data-access projection rejected: provider-unit-invalid",)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_provider_reference_parser_accepts_only_the_closed_token(root: Path) -> None:
    """Lengths, version, case and trailing characters are not repairable."""
    for reference in (
        "provider-ref:v1:sha256:" + "a" * 63,
        "provider-ref:v1:sha256:" + "A" * 64,
        "provider-ref:v2:sha256:" + "a" * 64,
        "provider-ref:v1:sha256:" + "a" * 64 + "\n",
    ):
        test_provider_identity_has_one_closed_syntax(root, reference)


def test_provider_reference_does_not_rehash_an_existing_reference() -> None:
    """A selected unit and its serialized reference are different input contracts."""
    with pytest.raises(pda.DataAccessProjectionError, match="provider-unit-invalid"):
        pda.provider_reference(pda.provider_reference("Shared Sales"))


def _safe_error(call: Callable, reason: str, secret: str) -> None:
    with pytest.raises(pda.DataAccessProjectionError) as raised:
        call()
    error = raised.value
    assert error.reason == reason
    assert error.args == (f"data-access projection rejected: {reason}",)
    assert error.__cause__ is None
    assert error.__context__ is None, "from None inside except still retains the unsafe raw exception"
    assert error.__suppress_context__ is True
    assert not hasattr(error, "filename")
    assert secret not in "".join(traceback.format_exception(error))


def test_projection_read_and_parse_errors_retain_no_unsafe_cause(tmp_path: Path) -> None:
    """A safe wrapper must not retain a raw path- or payload-bearing exception in its context."""
    missing, directory, invalid = (tmp_path / f"private-{kind}.json" for kind in ("missing", "dir", "utf8"))
    directory.mkdir()
    invalid.write_bytes(b"\xff")
    for path in (missing, directory, invalid):
        _safe_error(lambda path=path: pda.read_data_access(path), "unreadable", str(path))
    secret = str(tmp_path / "private-key")
    key = json.dumps(secret)
    for text, reason in (
        ("{" + key + ":1," + key + ":2}", "duplicate-key"),
        ("{" + key + ":NaN}", "nonfinite"),
        ("{" + key + ":", "malformed-json"),
    ):
        _safe_error(lambda text=text: pda.parse_data_access(text), reason, secret)


def test_duplicate_detection_never_stores_the_key(tmp_path: Path) -> None:
    """The duplicate hook's own exception must not hold the key even before translation."""
    secret = str(tmp_path / "private-key")
    with pytest.raises(_DuplicateJsonKey) as error:
        _reject_duplicate_keys([(secret, 1), (secret, 2)])
    assert error.value.args == ()
    assert vars(error.value) == {}
    assert secret not in str(error.value)
