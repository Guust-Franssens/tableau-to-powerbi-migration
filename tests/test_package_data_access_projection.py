"""Package data-access projection (`phase1-data-access/v1`) controls, independent of any gate.

Run: pytest -q tests/test_package_data_access_projection.py
Moved from tests/test_credential_gate.py when the credential gate was retired (S3): the projection
lives in `package_data_access.py` and never reads gate artifacts. The `.credential-gate-*` names below
are LEGACY artifacts a pre-retirement unit may still carry; they are written only to prove the
projection ignores them.
"""

# Private seams are the deliberate subject of several controls.
# pylint: disable=protected-access
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import package_data_access as pda  # noqa: E402  # pylint: disable=wrong-import-position
import preflight_source_credentials as pf  # noqa: E402  # pylint: disable=wrong-import-position

# Legacy gate artifact names, spelled out because the module that defined them is gone.
AUDIT = ".credential-gate-audit.log"
OVERRIDE = ".credential-gate-AUTHORIZED"
BLOCK_ACTIONS = frozenset({"block", "block-marker-only"})


def _append_audit(migration: Path, action: str, detail: str, sources: list[str] | None = None) -> None:
    """One legacy audit row in the shape the retired gate wrote (scope-stamped, optionally keyed)."""
    audit = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "detail": detail,
        "user": "test",
        "scope": str(migration.resolve()),
    }
    if sources is not None:
        audit["sources"] = list(sources)
    with (migration / AUDIT).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(audit) + "\n")


# ---------------------------------------------------------------------------------------------
# Phase-1 package-local data-access authority (#562)
#
# `assess_data_access` must be PURE: a projection that wrote while judging would change the
# evidence it is derived from, so the first test below is the one that makes the rest meaningful,
# and it carries its own positive control proving the check can fail.
# ---------------------------------------------------------------------------------------------

LIVE_A = {"class": "sqlserver", "server": "a.example", "database": "db", "powerbi_target": "live_source"}
LIVE_B = {
    "class": "snowflake",
    "server": "b.example",
    "database": "db",
    "warehouse": "wh",
    "powerbi_target": "live_source",
}
FLAT = {"class": "excel-direct", "powerbi_target": "flat_file"}
REVIEW = {"class": "unknown", "server": "u.example", "powerbi_target": "unknown"}
# Same unclassifiable leg, minus any hashable endpoint identity. `_classify_legs` converts a leg it
# cannot key into `needs-credential` with an `unstable-source[...]` placeholder - fail-closed, and
# deliberately NOT the same answer as `REVIEW` above: a key that cannot be derived can never be
# matched against audit evidence, so it is an authority failure rather than a data finding.
REVIEW_UNSTABLE = {"class": "unknown", "powerbi_target": "unknown"}

KEY_A = pf._leg_key({}, 0, LIVE_A)
KEY_B = pf._leg_key({}, 0, LIVE_B)

COMPLETE_LOCAL = {
    "self_contained": True,
    "omissions": [],
    "neutralized": [],
    "retained_network": [],
    "binding": {"state": "unbound"},
}


def _da_spec(*connections: dict) -> dict:
    """A package-local `migration-spec.json` payload declaring exactly these connection legs."""
    return {
        "data_sources": [
            {"name": f"ds{index}", "connection": dict(conn), "tables": [], "fields": []}
            for index, conn in enumerate(connections)
        ]
    }


def _da_root(tmp_path: Path, name: str, *connections: dict) -> Path:
    """A gate root that is a legitimate scope target and declares these live/flat legs itself."""
    root = tmp_path / name
    (root / "fabric").mkdir(parents=True, exist_ok=True)
    (root / "migration-spec.json").write_text(json.dumps(_da_spec(*connections)), encoding="utf-8")
    return root


def _trail(root: Path, *entries: tuple) -> None:
    """Append `(action, sources_or_None)` with the production action's canonical detail shape."""
    for action, sources in entries:
        detail = (
            "by=test; chain=['python.exe', 'pwsh.exe', 'WindowsTerminal.exe']"
            if action == "authorize"
            else f"{action} fixture"
        )
        if action in BLOCK_ACTIONS and sources is not None:
            detail = "sources_json=" + json.dumps(sources)
        _append_audit(root, action, detail, sources)


def _assess(root: Path, spec: dict, local: dict | None = None, **kwargs) -> object:
    return pda.assess_data_access(
        root,
        package_spec=spec,
        package_data_sources=COMPLETE_LOCAL if local is None else local,
        fallback_authorization=kwargs.pop("policy", "stop"),
        requested_scope=kwargs.pop("scope", "model_and_report"),
        provider=kwargs.pop("provider", None),
    )


def _tree_snapshot(root: Path) -> list[tuple[str, int, bytes]]:
    """Every file beneath `root` with its size and bytes - enough to catch any write at all."""
    return sorted(
        (str(path.relative_to(root)), path.stat().st_size, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    )


def test_the_assessor_never_writes_a_byte(tmp_path: Path) -> None:
    """The purity invariant, with the controls that stop it being vacuous.

    A test that only asserts "nothing changed" passes just as happily against a function that was
    never called, so the states it produced are pinned too; and a one-byte append afterwards must
    move the snapshot, or the comparison could not have failed.
    """
    root = _da_root(tmp_path, "pure", LIVE_A)
    _trail(root, ("block", [KEY_A]), ("probe-no_credential", [KEY_A]), ("manual-clear", None))
    (root / "fabric" / "Model.tmdl").write_text("table T\n", encoding="utf-8")
    before = _tree_snapshot(root)

    states = set()
    for scope in ("model_and_report", "model_only"):
        for policy in ("stop", "model_only_unvalidated"):
            states.add(_assess(root, _da_spec(LIVE_A), scope=scope, policy=policy).state)
    states.add(_assess(root, _da_spec(FLAT)).state)
    states.add(_assess(root, _da_spec(REVIEW)).state)

    assert _tree_snapshot(root) == before, "assess_data_access must not write, rename or truncate anything"
    assert states == {"live_unproven", "authorized_model_only", "local_import_ready", "blocked"}
    with (root / AUDIT).open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert _tree_snapshot(root) != before, "control: the snapshot must detect a one-byte append"


def test_an_all_flat_package_with_complete_bytes_is_local_import_ready(tmp_path: Path) -> None:
    """No live leg, complete packaged bytes, and deliberately NO audit log at all.

    An extract-only unit is legitimately never gated (`_gate_was_ever_applied`), so demanding audit
    evidence here would block every flat-file package. `binding.state=unbound` is Phase-2 work and
    must not read as a dependency failure.
    """
    root = _da_root(tmp_path, "flat", FLAT, FLAT)
    assert not (root / AUDIT).exists(), "fixture must have no audit trail, or it proves nothing"

    result = _assess(root, _da_spec(FLAT, FLAT))

    assert (result.state, result.codes) == ("local_import_ready", ("all-flat-file", "package-self-contained"))
    assert result.source_keys == ()
    assert (result.validation, result.max_phase2_claim) == ("validated", "data_validated")
    assert result.effective_scope == "model_and_report"


def test_an_untrusted_audit_does_not_block_a_package_with_no_live_leg(tmp_path: Path) -> None:
    """Negative control for the poison suite: the audit is only load-bearing when something needs it."""
    root = _da_root(tmp_path, "poison-irrelevant", FLAT)
    (root / AUDIT).write_text("{ not json at all\n", encoding="utf-8")

    assert _assess(root, _da_spec(FLAT)).state == "local_import_ready"


@pytest.mark.parametrize(
    ("root_legs", "package_legs", "expected"),
    [
        pytest.param((LIVE_A, LIVE_A), (LIVE_A, LIVE_A), "source-key-invalid", id="key-duplicated"),
    ],
)
def test_a_package_key_the_gate_root_never_covered_cannot_be_established(
    tmp_path: Path, root_legs: tuple, package_legs: tuple, expected: str
) -> None:
    """Root evidence is bound to package evidence by KEY, in both directions."""
    root = _da_root(tmp_path, f"keys-{expected}-{len(package_legs)}", *root_legs)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))

    result = _assess(root, _da_spec(*package_legs))

    assert (result.state, result.codes) == ("cannot_establish", (expected,))


def test_an_unknown_or_review_leg_blocks_and_is_never_dropped_from_the_denominator(tmp_path: Path) -> None:
    """A leg nobody can classify is not a flat file, and an earned sibling does not cover it."""
    root = _da_root(tmp_path, "review", LIVE_A, REVIEW)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))

    with_review = _assess(root, _da_spec(LIVE_A, REVIEW))
    without_review = _assess(root, _da_spec(LIVE_A))

    assert (with_review.state, with_review.codes) == ("blocked", ("unknown-target",))
    assert without_review.state == "live_unproven", "control: the review leg is what blocked, nothing else"


def test_an_unclassifiable_leg_with_no_stable_identity_is_an_authority_failure(tmp_path: Path) -> None:
    """Contradicts the tidy "review blocks" reading, and the canonical classifier is right.

    `_classify_legs` promotes a leg it cannot KEY to `needs-credential` with an
    `unstable-source[...]` placeholder rather than leaving it as `review`. That placeholder can
    never be matched against audit evidence, so the honest answer is `cannot_establish`, not a
    `blocked` finding about data. Pinned here because it is a real behaviour of the shared
    classifier, not of this assessor, and a future "simplification" would silently downgrade it.
    """
    root = _da_root(tmp_path, "unstable-review", LIVE_A)

    result = _assess(root, _da_spec(REVIEW_UNSTABLE))

    assert (result.state, result.codes) == ("cannot_establish", ("source-key-invalid",))


@pytest.mark.parametrize(
    "local",
    [
        pytest.param({**COMPLETE_LOCAL, "self_contained": False}, id="not-self-contained"),
        pytest.param({**COMPLETE_LOCAL, "omissions": [{"reason": "unshippable"}]}, id="omission"),
        pytest.param({**COMPLETE_LOCAL, "neutralized": ["Sales.xlsx"]}, id="neutralized"),
        pytest.param({**COMPLETE_LOCAL, "retained_network": ["rows.csv"]}, id="retained-network"),
        pytest.param({"self_contained": True}, id="fields-absent"),
    ],
)
def test_incomplete_packaged_bytes_block_a_flat_package(tmp_path: Path, local: dict) -> None:
    """Local access is a claim about BYTES THAT SHIPPED; a missing field is incomplete, not silent."""
    root = _da_root(tmp_path, f"incomplete-{len(local)}-{local.get('self_contained')}", FLAT)

    result = _assess(root, _da_spec(FLAT), local)

    assert (result.state, result.codes) == ("blocked", ("local-import-incomplete",))


def test_an_unreadable_localization_record_is_cannot_establish_not_blocked(tmp_path: Path) -> None:
    """A record that is not even a mapping is an authority failure, not a data finding."""
    root = _da_root(tmp_path, "bad-local-record", FLAT)

    result = _assess(root, _da_spec(FLAT), ["not", "a", "mapping"])

    assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",))


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param("not a mapping", id="spec-not-a-mapping"),
        pytest.param({"data_sources": "nope"}, id="data-sources-not-a-list"),
        pytest.param({"data_sources": ["nope"]}, id="source-not-a-mapping"),
    ],
)
def test_an_unreadable_package_spec_is_cannot_establish(tmp_path: Path, spec: object) -> None:
    root = _da_root(tmp_path, "bad-spec", FLAT)

    result = _assess(root, spec)

    assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",))


def test_a_live_leg_with_no_hashable_identity_is_source_key_invalid(tmp_path: Path) -> None:
    """`_classify_legs` returns an `unstable-source[...]` placeholder; it is not a source key."""
    root = _da_root(tmp_path, "unstable", LIVE_A)
    unstable = {"class": "snowflake", "server": "s.example", "powerbi_target": "live_source"}

    result = _assess(root, _da_spec(unstable))

    assert (result.state, result.codes) == ("cannot_establish", ("source-key-invalid",))


UPSTREAM_REF = pda.provider_reference("Upstream")
OTHER_REF = pda.provider_reference("Other")
SUPERSTORE_REF = pda.provider_reference("Superstore")
UP_REF = pda.provider_reference("Up")
S2_UNIT_REF = pda.provider_reference("Exact_S2_unit")

PROVIDER_LIVE = pda.DataAccessAssessment(
    state="live_data_ok",
    source_keys=(KEY_A,),
    provider_unit=None,
    provider_state=None,
    validation="validated",
    effective_scope="model_and_report",
    max_phase2_claim="data_validated",
    codes=("probe-cleared", "probe-data-ok"),
)
PROVIDER_LOCAL = pda.DataAccessAssessment(
    state="local_import_ready",
    source_keys=(),
    provider_unit=None,
    provider_state=None,
    validation="validated",
    effective_scope="model_and_report",
    max_phase2_claim="data_validated",
    codes=("all-flat-file", "package-self-contained"),
)
PROVIDER_AUTHORIZED = pda.DataAccessAssessment(
    state="authorized_model_only",
    source_keys=(KEY_A,),
    provider_unit=None,
    provider_state=None,
    validation="unvalidated",
    effective_scope="model_only",
    max_phase2_claim="structural_only",
    codes=("brief-model-only", "human-authorize"),
)
PROVIDER_BLOCKED = pda.DataAccessAssessment(
    state="blocked",
    source_keys=(),
    provider_unit=None,
    provider_state=None,
    validation="not_established",
    effective_scope=None,
    max_phase2_claim="none",
    codes=("marker-only",),
)
PROVIDER_RECURSIVE = pda.DataAccessAssessment(
    state="provider_inherited",
    source_keys=(KEY_A,),
    provider_unit=UPSTREAM_REF,
    provider_state="live_data_ok",
    validation="validated",
    effective_scope="model_and_report",
    max_phase2_claim="data_validated",
    codes=("provider-exact",),
)


@pytest.mark.parametrize(
    ("provider_assessment", "expected_keys"),
    [
        pytest.param(PROVIDER_LIVE, (KEY_A,), id="live-provider"),
        pytest.param(PROVIDER_LOCAL, (), id="local-provider"),
    ],
)
def test_exactly_one_direct_provider_is_inherited_field_for_field(
    tmp_path: Path, provider_assessment: object, expected_keys: tuple
) -> None:
    """The consumer copies the provider's semantic fields verbatim and names it exactly."""
    root = _da_root(tmp_path, f"consumer-{len(expected_keys)}", FLAT)

    result = _assess(
        root, _da_spec(FLAT), scope="report_only_shared_model", provider=(SUPERSTORE_REF, provider_assessment)
    )

    assert (result.state, result.codes) == ("provider_inherited", ("provider-exact",))
    assert (result.provider_unit, result.provider_state) == (SUPERSTORE_REF, provider_assessment.state)
    assert result.source_keys == expected_keys
    assert result.validation == provider_assessment.validation
    assert result.max_phase2_claim == provider_assessment.max_phase2_claim
    assert result.effective_scope == "report_only_shared_model"


def test_a_model_only_provider_cannot_authorize_a_report_only_consumer(tmp_path: Path) -> None:
    """Intersecting `model_only` with `report_only_shared_model` is empty - but only for the REPORT.

    The same provider still inherits fine at model-only scope, which is the control that proves the
    refusal is about the topology intersection rather than about the provider being unusable.
    """
    root = _da_root(tmp_path, "model-only-provider", FLAT)
    provider = (pda.provider_reference("SharedModel"), PROVIDER_AUTHORIZED)

    report_consumer = _assess(root, _da_spec(FLAT), scope="report_only_shared_model", provider=provider)
    model_consumer = _assess(root, _da_spec(FLAT), scope="model_only", provider=provider)

    assert (report_consumer.state, report_consumer.codes) == ("blocked", ("provider-model-only",))
    assert (model_consumer.state, model_consumer.provider_state) == ("provider_inherited", "authorized_model_only")
    assert (model_consumer.validation, model_consumer.max_phase2_claim) == ("unvalidated", "structural_only")


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        pytest.param(None, "provider-missing", id="absent"),
        pytest.param([], "provider-missing", id="resolved-nothing"),
        pytest.param(
            [(UPSTREAM_REF, PROVIDER_LIVE), (OTHER_REF, PROVIDER_LOCAL)], "provider-ambiguous", id="two-candidates"
        ),
        pytest.param(
            [(UPSTREAM_REF, PROVIDER_LIVE), (UPSTREAM_REF, PROVIDER_LIVE)], "provider-ambiguous", id="two-equal"
        ),
        pytest.param((UPSTREAM_REF, PROVIDER_RECURSIVE), "provider-ambiguous", id="recursive"),
        pytest.param((UPSTREAM_REF, PROVIDER_BLOCKED), "provider-missing", id="not-an-authority"),
        pytest.param(("", PROVIDER_LIVE), "provider-foreign", id="empty-unit"),
        pytest.param((UPSTREAM_REF, {"state": "live_data_ok"}), "provider-foreign", id="foreign-shape"),
        pytest.param((UPSTREAM_REF, PROVIDER_LIVE, "extra"), "provider-foreign", id="wrong-arity"),
        pytest.param([(UPSTREAM_REF, "not an assessment")], "provider-foreign", id="one-malformed-candidate"),
        pytest.param("Upstream", "provider-foreign", id="bare-string"),
    ],
)
def test_an_unusable_provider_is_named_rather_than_searched_for(
    tmp_path: Path, provider: object, expected: str
) -> None:
    """No registry lookup, no name match, no ancestor walk - each failure gets its own code.

    The LIST cases are how a caller reports what its cohort resolution actually found. Without
    them `provider-ambiguous` is unreachable through the API and a caller holding two LUID matches
    has to collapse them into `None`, which reads as "no provider exists" and understates it.
    """
    root = _da_root(tmp_path, f"provider-{expected}-{type(provider).__name__}-{len(str(provider))}", FLAT)

    result = pda.assess_data_access(
        root,
        package_spec=_da_spec(FLAT),
        package_data_sources=COMPLETE_LOCAL,
        fallback_authorization="stop",
        requested_scope="report_only_shared_model",
        provider=provider,
    )

    assert (result.state, result.codes) == ("cannot_establish", (expected,))


def test_a_single_candidate_list_inherits_exactly_like_a_bare_pair(tmp_path: Path) -> None:
    """Control for the ambiguity rule: one candidate is not ambiguous, however it was passed."""
    root = _da_root(tmp_path, "one-candidate", FLAT)

    as_list = _assess(
        root, _da_spec(FLAT), scope="report_only_shared_model", provider=[(SUPERSTORE_REF, PROVIDER_LIVE)]
    )
    as_pair = _assess(root, _da_spec(FLAT), scope="report_only_shared_model", provider=(SUPERSTORE_REF, PROVIDER_LIVE))

    assert as_list == as_pair
    assert (as_list.state, as_list.provider_unit) == ("provider_inherited", SUPERSTORE_REF)


@pytest.mark.parametrize("policy", ["stop", "model_only_unvalidated"])
@pytest.mark.parametrize("scope", ["model_and_report", "model_only", "report_only_shared_model"])
def test_the_typed_inputs_are_a_closed_vocabulary(tmp_path: Path, policy: str, scope: str) -> None:
    """Accepted members must work; anything else is a CALLER bug and raises rather than degrading."""
    root = _da_root(tmp_path, f"vocab-{policy}-{scope}", FLAT)
    _assess(root, _da_spec(FLAT), policy=policy, scope=scope, provider=(pda.provider_reference("U"), PROVIDER_LOCAL))

    with pytest.raises(ValueError):
        _assess(root, _da_spec(FLAT), policy="whatever")
    with pytest.raises(ValueError):
        _assess(root, _da_spec(FLAT), scope="everything")


def _projection(**overrides) -> dict:
    payload = {
        "schema": "phase1-data-access/v1",
        "state": "live_data_ok",
        "source_keys": [KEY_A],
        "provider_unit": None,
        "provider_state": None,
        "validation": "validated",
        "effective_scope": "model_and_report",
        "max_phase2_claim": "data_validated",
        "codes": ["probe-cleared", "probe-data-ok"],
    }
    payload.update(overrides)
    return payload


ROUND_TRIP_CASES = (PROVIDER_LIVE, PROVIDER_LOCAL, PROVIDER_AUTHORIZED, PROVIDER_BLOCKED, PROVIDER_RECURSIVE)


@pytest.mark.parametrize("assessment", ROUND_TRIP_CASES, ids=[a.state for a in ROUND_TRIP_CASES])
def test_every_producible_assessment_survives_its_own_strict_parser(tmp_path: Path, assessment: object) -> None:
    """Producer and parser must agree by construction, and the bytes must be stable."""
    text = assessment.dumps()

    assert pda.parse_data_access(text) == assessment
    assert assessment.dumps() == text, "serialization must be deterministic, or S1 hashes churn"

    path = tmp_path / "data-access.json"
    path.write_text(text, encoding="utf-8")
    assert pda.read_data_access(path) == assessment


def test_a_cannot_establish_projection_round_trips_too(tmp_path: Path) -> None:
    """The refusal states ship as artifacts as well; they must survive the same parser."""
    root = _da_root(tmp_path, "refusal-round-trip", LIVE_A)
    refusal = _assess(root, _da_spec(REVIEW_UNSTABLE))

    assert refusal.state == "cannot_establish"
    assert pda.parse_data_access(refusal.dumps()) == refusal


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        pytest.param("{ nope", "malformed-json", id="malformed-json"),
        pytest.param("[]", "not-an-object", id="not-an-object"),
        pytest.param('{"schema": 1, "schema": 2}', "duplicate-key", id="duplicate-key-shallow"),
        pytest.param(
            json.dumps(_projection())[:-1] + ', "codes": ["probe-cleared"]}', "duplicate-key", id="duplicate-key-real"
        ),
    ],
)
def test_the_projection_parser_refuses_malformed_text_by_name(text: str, reason: str) -> None:
    """Textual refusals, each identified. `.code` is always the shipped `projection-invalid`."""
    with pytest.raises(pda.DataAccessProjectionError) as excinfo:
        pda.parse_data_access(text)

    assert excinfo.value.reason == reason
    assert excinfo.value.code == "projection-invalid"


def test_a_nonfinite_number_is_refused_as_such(tmp_path: Path) -> None:
    """`NaN`/`Infinity` are not JSON; Python's decoder accepts them unless told not to."""
    payload = json.dumps(_projection())[:-1] + ', "extra": Infinity}'

    with pytest.raises(pda.DataAccessProjectionError) as excinfo:
        pda.parse_data_access(payload)

    assert excinfo.value.reason == "nonfinite"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param({**_projection(), "surprise": 1}, "unknown-field", id="unknown-field"),
        pytest.param({k: v for k, v in _projection().items() if k != "codes"}, "missing-field", id="missing-field"),
        pytest.param(_projection(schema="phase1-data-access/v2"), "unknown-value", id="wrong-schema"),
        pytest.param(_projection(state=7), "bad-type", id="state-not-a-string"),
        pytest.param(_projection(state="excellent"), "unknown-value", id="unknown-state"),
        pytest.param(_projection(validation=None), "bad-type", id="validation-not-nullable"),
        pytest.param(_projection(effective_scope="everything"), "unknown-value", id="unknown-scope"),
        pytest.param(_projection(max_phase2_claim="lots"), "unknown-value", id="unknown-ceiling"),
        pytest.param(_projection(provider_unit=""), "bad-type", id="empty-provider-unit"),
        pytest.param(_projection(provider_state="blocked"), "unknown-value", id="provider-state-not-direct"),
        pytest.param(_projection(source_keys="nope"), "bad-type", id="source-keys-not-a-list"),
        pytest.param(_projection(source_keys=[1]), "bad-type", id="source-key-not-a-string"),
        pytest.param(
            _projection(source_keys=sorted([KEY_A, KEY_B], reverse=True)), "source-keys-unsorted", id="unsorted-keys"
        ),
        pytest.param(_projection(source_keys=[KEY_A, KEY_A]), "source-keys-unsorted", id="duplicate-keys"),
        pytest.param(_projection(source_keys=["source-key:zzzz"]), "source-key-invalid", id="bad-key-syntax"),
        pytest.param(_projection(source_keys=["a.example"]), "source-key-invalid", id="display-name-as-key"),
        pytest.param(_projection(codes=["probe-data-ok", "probe-cleared"]), "codes-unsorted", id="unsorted-codes"),
        pytest.param(_projection(codes=["probe-cleared", "totally-fine"]), "unknown-value", id="unknown-code"),
    ],
)
def test_the_projection_parser_refuses_bad_fields_by_name(payload: dict, reason: str) -> None:
    """Field-level strictness. Each case must name ITS refusal, not a single catch-all."""
    with pytest.raises(pda.DataAccessProjectionError) as excinfo:
        pda.parse_data_access(json.dumps(payload))

    assert excinfo.value.reason == reason


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_projection(source_keys=[]), id="live-without-keys"),
        pytest.param(_projection(codes=["probe-cleared"]), id="live-missing-a-code"),
        pytest.param(_projection(validation="unvalidated"), id="live-not-validated"),
        pytest.param(_projection(effective_scope="report_only_shared_model"), id="direct-with-consumer-scope"),
        pytest.param(
            _projection(
                state="local_import_ready",
                codes=["all-flat-file", "package-self-contained"],
            ),
            id="local-carrying-live-keys",
        ),
        pytest.param(
            _projection(
                state="authorized_model_only",
                source_keys=[KEY_A],
                codes=["brief-model-only", "human-authorize"],
                validation="unvalidated",
                effective_scope="model_and_report",
                max_phase2_claim="structural_only",
            ),
            id="authorized-outside-model-only",
        ),
        pytest.param(
            _projection(
                state="blocked",
                source_keys=[],
                codes=["all-flat-file"],
                validation="not_established",
                effective_scope=None,
                max_phase2_claim="none",
            ),
            id="blocked-with-an-accepted-code",
        ),
        pytest.param(
            _projection(
                state="cannot_establish",
                source_keys=[KEY_A],
                codes=["audit-missing"],
                validation="not_established",
                effective_scope=None,
                max_phase2_claim="none",
            ),
            id="cannot-establish-still-publishing-keys",
        ),
        pytest.param(
            _projection(
                state="provider_inherited",
                codes=["provider-exact"],
                provider_unit=UPSTREAM_REF,
                provider_state=None,
            ),
            id="inherited-without-a-provider-state",
        ),
        pytest.param(
            _projection(
                state="provider_inherited",
                source_keys=[],
                codes=["provider-exact"],
                provider_unit=UPSTREAM_REF,
                provider_state="live_data_ok",
            ),
            id="inherited-live-provider-with-no-keys",
        ),
        pytest.param(
            _projection(
                state="provider_inherited",
                codes=["provider-exact"],
                provider_unit=None,
                provider_state="live_data_ok",
            ),
            id="inherited-without-a-provider-unit",
        ),
        pytest.param(_projection(provider_unit=UPSTREAM_REF), id="direct-state-naming-a-provider"),
    ],
)
def test_the_projection_parser_refuses_impossible_state_combinations(payload: dict) -> None:
    """Types alone cannot catch a semantically impossible projection; these are the tampered shapes."""
    with pytest.raises(pda.DataAccessProjectionError) as excinfo:
        pda.parse_data_access(json.dumps(payload))

    assert excinfo.value.reason == "illegal-combination"


def test_reading_an_absent_projection_is_a_refusal_not_an_empty_state(tmp_path: Path) -> None:
    """`read_data_access` must never degrade a missing artifact into a permissive default."""
    with pytest.raises(pda.DataAccessProjectionError) as excinfo:
        pda.read_data_access(tmp_path / "nope" / "data-access.json")

    assert excinfo.value.reason == "unreadable"
    assert excinfo.value.code == "projection-invalid"


def test_every_state_the_assessor_actually_produces_survives_the_strict_parser(tmp_path: Path) -> None:
    """Producer/parser agreement on ASSESSOR OUTPUT, not on hand-built constants.

    The round-trip cases above are literals a test author wrote, so they prove the parser accepts
    what a human believed the assessor emits. This builds one assessment of each producible state
    from real fixtures and re-parses its own bytes, which is the check that actually catches the
    producer and the parser drifting apart.
    """
    live = _da_root(tmp_path, "rt-live", LIVE_A)
    flat = _da_root(tmp_path, "rt-flat", FLAT)
    consumer = _da_root(tmp_path, "rt-consumer", FLAT)

    produced = [
        _assess(live, _da_spec(LIVE_A)),
        _assess(live, _da_spec(LIVE_A, REVIEW)),
        _assess(live, _da_spec(LIVE_A), policy="model_only_unvalidated", scope="model_only"),
        _assess(flat, _da_spec(FLAT)),
        _assess(flat, _da_spec(REVIEW_UNSTABLE)),
        _assess(consumer, _da_spec(FLAT), scope="report_only_shared_model", provider=(UP_REF, PROVIDER_LIVE)),
    ]

    assert {result.state for result in produced} == set(pda.DATA_ACCESS_STATES) - {"live_data_ok"}, (
        "this corpus must cover every producible state; legacy live_data_ok is parse-only now"
    )
    for result in produced:
        assert pda.parse_data_access(result.dumps()) == result, result.state


GATE_TRAILS = {
    "no-audit": (),
    "earned": (("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A])),
    "armed": (("block", [KEY_A]),),
    "regressed": (
        ("block", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
        ("probe-no_credential", [KEY_A]),
    ),
    "unkeyed-clear": (("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", None)),
    "forced-scope": (
        ("block-forced-scope", None),
        ("block", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
    ),
    "authorized": (("block", [KEY_A]), ("authorize", None), ("probe-cleared", [KEY_A])),
}


@pytest.mark.parametrize("trail", sorted(GATE_TRAILS))
@pytest.mark.parametrize("damage", [None, "malformed-audit", "override-file"])
@pytest.mark.parametrize(
    ("policy", "scope"),
    [
        ("stop", "model_and_report"),
        ("stop", "model_only"),
        ("model_only_unvalidated", "model_only"),
        ("model_only_unvalidated", "model_and_report"),
    ],
)
def test_the_gate_trail_never_moves_the_package_projection(
    tmp_path: Path, trail: str, damage: str | None, policy: str, scope: str
) -> None:
    """S2: earned, armed, regressed, forced, authorized or poisoned gate evidence is never read.

    A live package projects from its own facts and brief alone. It is never `validated` here;
    check_unit's same-run `data-evidence` PASS is the only live data proof.
    """
    root = _da_root(tmp_path, "trail", LIVE_A)
    _trail(root, *GATE_TRAILS[trail])
    if damage == "malformed-audit":
        with (root / AUDIT).open("a", encoding="utf-8") as handle:
            handle.write("{ not json\n")
    elif damage == "override-file":
        (root / OVERRIDE).write_text("authorized by a human\n", encoding="utf-8")

    result = _assess(root, _da_spec(LIVE_A), policy=policy, scope=scope)

    if policy == "model_only_unvalidated" and scope == "model_only":
        expected = ("authorized_model_only", ("brief-model-only",), "unvalidated", "structural_only")
    else:
        expected = ("live_unproven", ("live-unproven",), "unvalidated", "data_validated")
    assert (result.state, result.codes, result.validation, result.max_phase2_claim) == expected
    assert (result.source_keys, result.effective_scope) == ((KEY_A,), scope)


def test_no_projection_field_can_carry_a_host_path_or_display_name(tmp_path: Path) -> None:
    """Privacy is structural: the exported surface has no field shaped to hold customer text."""
    root = _da_root(tmp_path, "privacy", LIVE_A, FLAT)
    _trail(root, ("block", [KEY_A]), ("probe-unreachable", [KEY_A]))

    payload = json.dumps(_assess(root, _da_spec(LIVE_A, FLAT)).to_json())

    for secret in ("a.example", "sqlserver", "excel-direct", str(root), "ds0", "ds1"):
        assert secret not in payload, f"{secret!r} reached the projection"
    assert set(json.loads(payload)) == set(pda.DATA_ACCESS_FIELDS)


@pytest.mark.parametrize(
    "provider",
    [
        False,
        True,
        {},
        (),
        (UP_REF, PROVIDER_LIVE, "extra"),
        [UP_REF, PROVIDER_LIVE],
        [(UP_REF, PROVIDER_LIVE), False],
        [(UP_REF, PROVIDER_LIVE), (OTHER_REF, {})],
        ((UP_REF, PROVIDER_LIVE),),
        ((UP_REF, PROVIDER_LIVE), (OTHER_REF, PROVIDER_LOCAL)),
    ],
)
def test_data_access_provider_candidate_shapes_are_not_coerced(tmp_path: Path, provider: object) -> None:
    """A candidate list is not a list-shaped pair, tuple collection, or boolean sentinel."""
    root = _da_root(tmp_path, "provider-shape", FLAT)

    result = _assess(root, _da_spec(FLAT), scope="report_only_shared_model", provider=provider)

    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))


@pytest.mark.parametrize(
    "provider",
    [
        PROVIDER_LIVE._replace(source_keys=(KEY_A, KEY_A)),
        PROVIDER_LIVE._replace(source_keys=[KEY_A]),
        PROVIDER_LIVE._replace(source_keys=(KEY_A + "\n",)),
        PROVIDER_LIVE._replace(validation=True),
        PROVIDER_LIVE._replace(codes=("probe-cleared",)),
        PROVIDER_LIVE._replace(codes=["probe-cleared", "probe-data-ok"]),
        PROVIDER_LOCAL._replace(source_keys=""),
        PROVIDER_LIVE._replace(provider_unit=pda.provider_reference("hidden-recursion")),
    ],
)
def test_data_access_provider_assessment_must_already_be_strict(tmp_path: Path, provider: object) -> None:
    """Inheritance cannot repair, deduplicate, or amplify an invalid provider projection."""
    root = _da_root(tmp_path, "provider-semantics", FLAT)

    result = _assess(root, _da_spec(FLAT), scope="report_only_shared_model", provider=(UP_REF, provider))

    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))


def test_data_access_provider_resolution_stays_with_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real published-source consumer inherits only the supplied S2 result; it searches nothing."""
    root = _da_root(tmp_path, "published-consumer", FLAT)
    published_spec = _da_spec({"class": "sqlproxy", "powerbi_target": "unknown"})

    def forbidden(*_args, **_kwargs):
        pytest.fail("provider inheritance attempted direct evidence or source classification")

    monkeypatch.setattr(pda, "_classify_legs", forbidden)
    inherited = _assess(root, published_spec, scope="report_only_shared_model", provider=(S2_UNIT_REF, PROVIDER_LIVE))
    missing = _assess(root, published_spec, scope="report_only_shared_model", provider=[])

    assert (inherited.state, inherited.provider_unit, inherited.source_keys) == (
        "provider_inherited",
        S2_UNIT_REF,
        PROVIDER_LIVE.source_keys,
    )
    assert (missing.state, missing.codes) == ("cannot_establish", ("provider-missing",))


@pytest.mark.parametrize("scope", ["model_and_report", "report_only_shared_model"])
def test_data_access_inherited_authorization_cannot_expand_to_reports(tmp_path: Path, scope: str) -> None:
    """Structural-only provider authority stays model-only in both producer and strict reader."""
    root = _da_root(tmp_path, "scope-ceiling", FLAT)
    result = _assess(root, _da_spec(FLAT), scope=scope, provider=(UP_REF, PROVIDER_AUTHORIZED))
    assert (result.state, result.codes) == ("blocked", ("provider-model-only",))
    payload = {
        **PROVIDER_RECURSIVE.to_json(),
        "provider_state": "authorized_model_only",
        "validation": "unvalidated",
        "max_phase2_claim": "structural_only",
        "effective_scope": scope,
    }
    with pytest.raises(pda.DataAccessProjectionError, match="illegal-combination"):
        pda.parse_data_access(json.dumps(payload))


def test_data_access_source_key_syntax_requires_the_whole_string() -> None:
    """A final newline is outside the stable key, despite the special meaning of regex `$`."""
    with pytest.raises(pda.DataAccessProjectionError, match="source-key-invalid"):
        pda.parse_data_access(json.dumps(_projection(source_keys=[KEY_A + "\n"])))


def test_data_access_unreadable_projection_still_has_a_typed_refusal(tmp_path: Path) -> None:
    """Invalid UTF-8 is a read failure, not an uncaught decoder exception."""
    path = tmp_path / "projection.json"
    path.write_bytes(b"\xff")
    with pytest.raises(pda.DataAccessProjectionError, match="unreadable"):
        pda.read_data_access(path)
