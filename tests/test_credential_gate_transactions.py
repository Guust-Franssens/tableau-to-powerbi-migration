"""#702 descriptor/transaction controls, confined to pytest-owned roots.

Run: pytest -q tests/test_credential_gate_transactions.py --basetemp _gate_tx_tests
Non-Windows/marker fixtures establish serialization only, never kernel write enforcement.
"""

# Test names describe the controls; private seams and dynamic code are deliberate mutation inputs.
# pylint: disable=protected-access,missing-function-docstring
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import inspect
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import credential_gate as gate  # noqa: E402  # pylint: disable=wrong-import-position
import preflight_source_credentials as preflight  # noqa: E402  # pylint: disable=wrong-import-position
import probe_live_source as probe  # noqa: E402  # pylint: disable=wrong-import-position

SCRIPTS = Path(gate.__file__).parent


@pytest.fixture(name="unit")
def _unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    """Real files and descriptor locks; no ACL syscall in the marker-only controls."""
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    root = tmp_path / "unit"
    root.mkdir()
    sources = [
        {
            "name": name,
            "connection": {
                "class": "sqlserver",
                "server": f"{name}.example.invalid",
                "database": "fixture",
                "schema": "dbo",
                "powerbi_target": "live_source",
            },
            "tables": [{"name": "Orders", "table": "[dbo].[Orders]"}],
        }
        for name in ("A", "B", "C")
    ]
    (root / gate.MIGRATION_SPEC).write_text(json.dumps({"data_sources": sources}), encoding="utf-8")
    keys = [preflight._classify_legs(source, index)[0][0] for index, source in enumerate(sources)]
    return root, keys


def _marker(root: Path) -> list[str]:
    return json.loads((root / gate.MARKER).read_text(encoding="utf-8"))["sources"]


def _earn(root: Path, keys: list[str]) -> None:
    gate.record_probe_attempt(root, "DATA_OK", "fixture DATA_OK", keys)
    assert gate.clear_block(root, "fixture DATA_OK", earned=True, sources=keys) == 0


@pytest.mark.parametrize("action", [*gate.PROBE_ATTEMPT_CODES, gate.PROBE_DATA_OK])
def test_latest_historical_attempt_invalidates_every_clear_projection(unit, action: str) -> None:
    root, keys = unit
    _earn(root, keys)
    with gate.gate_transaction(root):
        gate._append_audit(root, action, "historical keyed attempt without its physical stop", [keys[0]])
    assert gate._clear_was_earned(root, [keys[0]]) is None, "latest attempt must invalidate the earned projection"
    assert gate.apply_block(root, [keys[0]]) == 0
    assert _marker(root) == [keys[0]], "latest attempt must restore exactly its pending physical stop"
    assert gate.status(root) != 0
    _earn(root, [keys[0]])
    assert gate.status(root) == 0, "a fresh keyed pair must restore proof"


@pytest.mark.parametrize("history", ["unarmed", "arm", "manual-clear", "probe-cleared", "probe-data_ok", "probe-error"])
def test_every_non_none_canonical_code_stays_pending(unit, history: str) -> None:
    root, keys = unit
    if history != "unarmed":
        assert gate.apply_block(root, keys) == 0
        if history not in ("arm",):
            with gate.gate_transaction(root):
                detail = gate._sources_detail(keys) if history == "probe-cleared" else "historical observation"
                gate._append_audit(root, history, detail, None if history == "manual-clear" else keys)
    assert set(gate._transaction_pending(root)) == set(keys), "every non-None canonical code must remain pending"


@pytest.mark.parametrize("scope", ["empty", "legacy", "foreign", "substitution", "duplicate", "malformed"])
def test_supported_arm_closes_current_source_domain_without_effects(unit, scope: str) -> None:
    root, keys = unit
    supplied = {
        "empty": [],
        "legacy": ["fixture display"],
        "foreign": ["source-key:ffffffffffffffff"],
        "substitution": [keys[0], keys[1], "source-key:ffffffffffffffff"],
        "duplicate": [keys[0], keys[0]],
        "malformed": ["source-key:broken"],
    }[scope]
    assert gate.apply_block(root, supplied) == 2, "membership, not display or cardinality, defines supported scope"
    assert not (root / gate.MARKER).exists(), "refused scope must not acquire a physical stop"
    assert not (root / "fabric").exists()


@pytest.mark.parametrize("covered", [False, True])
def test_exact_skip_requires_omitted_pending_root_coverage(unit, covered: bool) -> None:
    root, keys = unit
    _earn(root, keys)
    with gate.gate_transaction(root):
        gate._append_audit(root, gate.PROBE_DATA_OK, "historical unmatched sibling success", [keys[1]])
    if covered:
        assert gate.apply_block(root, [keys[1]]) == 0
    before = (root / gate.MARKER).read_bytes() if covered else None
    assert gate.apply_block(root, [keys[0]]) == (0 if covered else 2), "omitted pending root veto must survive"
    assert ((root / gate.MARKER).read_bytes() if covered else None) == before
    arms = [row["sources"] for row in gate._audit_entries(root) if row["action"] in gate.BLOCK_ACTIONS]
    assert arms == [keys, [keys[1]]] if covered else arms == [keys], "skip must not synthesize omitted arm scope"


def test_new_unarmed_root_key_vetoes_an_earned_subset_skip(unit) -> None:
    root, keys = unit
    path = root / gate.MIGRATION_SPEC
    complete = path.read_text(encoding="utf-8")
    sources = json.loads(complete)["data_sources"]
    path.write_text(json.dumps({"data_sources": sources[:1]}), encoding="utf-8")
    _earn(root, keys[:1])
    path.write_text(complete, encoding="utf-8")
    assert gate.apply_block(root, keys[:1]) == 2, "a newly discovered unarmed root key requires complete-scope retry"
    assert not (root / gate.MARKER).exists(), "refusal must not invent an omitted key's arm epoch"
    assert gate.apply_block(root, keys) == 0
    assert _marker(root) == keys[1:], "complete retry must preserve the earned sibling"


@pytest.mark.parametrize("layout", ["parser", "native"])
@pytest.mark.parametrize("coverage", ["complete", "missing-deny", "readback-error", "unnamed"])
def test_exact_skip_checks_marker_names_and_every_resolved_anchor(
    unit, monkeypatch: pytest.MonkeyPatch, layout: str, coverage: str
) -> None:
    from test_data_access_contract import _StatefulIcacls  # pylint: disable=import-outside-toplevel

    root, keys = unit
    _earn(root, keys)
    if layout == "native":
        targets = [root / "Fixture.SemanticModel", root / "Fixture.Report"]
    else:
        targets = [root / "fabric"]
    for target in targets:
        target.mkdir(exist_ok=True)
    permissions = _StatefulIcacls(*targets)
    monkeypatch.setattr(gate.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gate, "_user", lambda: permissions.PRINCIPAL)
    monkeypatch.setattr(gate, "_icacls", permissions)
    with gate.gate_transaction(root):
        gate._append_audit(root, gate.PROBE_DATA_OK, "historical unpaired sibling", [keys[1]])
    assert gate.apply_block(root, [keys[1]]) == 0
    if coverage == "missing-deny":
        permissions.states[permissions.key(targets[-1])] = "clear"
    elif coverage == "readback-error":
        permissions.inject("query", targets[-1], "nonzero")
    elif coverage == "unnamed":
        payload = json.loads((root / gate.MARKER).read_text(encoding="utf-8"))
        payload["sources"] = [keys[2]]
        (root / gate.MARKER).write_text(json.dumps(payload), encoding="utf-8")
    assert gate.apply_block(root, [keys[0]]) == (
        0 if coverage == "complete" else (3 if coverage == "readback-error" else 2)
    ), "omitted-key coverage needs marker membership and successful readback of every anchor"
    assert _marker(root) == ([keys[2]] if coverage == "unnamed" else [keys[1]])


@pytest.mark.parametrize(
    "verdict", [action.removeprefix("probe-").upper() for action in gate.PROBE_ATTEMPT_CODES] + ["DATA_OK"]
)
def test_authorized_observation_preserves_exact_permission_until_new_arm(
    unit, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    root, keys = unit
    monkeypatch.setattr(gate, "_ancestry", lambda: [])
    assert gate.apply_block(root, keys) == 0
    assert gate.authorize(root, "Fixture Human") == 0
    arms = [row for row in gate._audit_entries(root) if row["action"] in gate.BLOCK_ACTIONS]
    gate.record_probe_attempt(root, verdict, "exact authorized observation", [keys[0]])
    assert not (root / gate.MARKER).exists(), "authorized measurement must not synthesize a new arm"
    assert gate._override_is_authentic(root), "measurement alone must not revoke human permission"
    assert [row for row in gate._audit_entries(root) if row["action"] in gate.BLOCK_ACTIONS] == arms
    assert gate._clear_was_earned(root, [keys[0]]) is None, "human permission must never become measured proof"
    assert gate.status(root) == 0
    with gate.gate_transaction(root):
        assert gate._apply_pending_block(root, [keys[0]]) == 0
    assert not gate._override_is_authentic(root), "a real new arm epoch needs renewed permission"
    assert gate.status(root) != 0


@pytest.mark.parametrize("kind", ["file-only", "audit-only", "before-arm", "legacy", "new-root"])
def test_permission_is_never_global_or_future(unit, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    root, keys = unit
    monkeypatch.setattr(gate, "_ancestry", lambda: [])
    if kind in ("before-arm", "legacy"):
        with gate.gate_transaction(root):
            if kind == "legacy":
                gate._append_audit(root, "block-marker-only", gate._sources_detail(["legacy"]), ["legacy"])
            gate._append_audit(root, "authorize", "by=Fixture Human; chain=[]")
        (root / gate.OVERRIDE).write_text("historical permission\n", encoding="utf-8")
    elif kind == "file-only":
        (root / gate.OVERRIDE).write_text("unbacked file\n", encoding="utf-8")
    elif kind == "audit-only":
        assert gate.apply_block(root, keys) == 0
        with gate.gate_transaction(root):
            gate._append_audit(root, "authorize", "by=Fixture Human; chain=[]")
    else:
        spec = json.loads((root / gate.MIGRATION_SPEC).read_text(encoding="utf-8"))
        (root / gate.MIGRATION_SPEC).write_text(json.dumps({"data_sources": spec["data_sources"][:1]}))
        assert gate.apply_block(root, [keys[0]]) == 0
        assert gate.authorize(root, "Fixture Human") == 0
        (root / gate.MIGRATION_SPEC).write_text(json.dumps(spec))
    assert not gate._override_is_authentic(root), "permission must cover exact current keyed arm epochs"
    assert gate.apply_block(root, keys) == 0
    assert (root / gate.MARKER).exists(), "global/future authorization must not bypass a current pending key"
    assert set(_marker(root)) == set(keys if kind != "new-root" else keys[1:])


def _child(root: Path, code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); " + code, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_concurrent_first_arms_preserve_union_and_exact_rows(unit) -> None:
    root, keys = unit
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda key: gate.apply_block(root, [key]), keys))
    assert results == [0, 0, 0]
    assert set(_marker(root)) == set(keys), "assignment must not lose a sibling's physical obligation"
    arms = [entry["sources"] for entry in gate._audit_entries(root) if entry["action"] in gate.BLOCK_ACTIONS]
    assert sorted(arms) == sorted([[key] for key in keys]), "root-wide veto must not fabricate caller scope"


def test_stale_skip_cannot_overtake_pending_attempt(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    root, keys = unit
    _earn(root, keys)
    snapshot, release = threading.Event(), threading.Event()
    read = gate._redundant_rearm

    def pause(location, sources):
        result = read(location, sources)
        snapshot.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(gate, "_redundant_rearm", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        skip = pool.submit(gate.apply_block, root, [keys[0]])
        assert snapshot.wait(5)
        pending = pool.submit(gate.record_probe_attempt, root, "ERROR", "fixture ERROR", [keys[0]])
        completed = threading.Event()
        pending.add_done_callback(lambda _future: completed.set())
        assert not completed.wait(0.15), "attempt must not commit through another participant's snapshot"
        release.set()
        assert skip.result(timeout=10) == 0
        pending.result(timeout=10)
    assert keys[0] in _marker(root)
    assert gate.status(root) != 0
    assert gate._data_access_ledger(gate._audit_entries(root), tuple(keys))[0][keys[0]] == "probe-error", (
        "pending attempt must remain the terminal failure"
    )


def test_sibling_attempt_vetoes_caller_only_clear(unit) -> None:
    root, keys = unit
    _earn(root, keys)
    gate.record_probe_attempt(root, "ERROR", "fixture A", [keys[0]])
    gate.record_probe_attempt(root, "ERROR", "fixture B", [keys[1]])
    _earn(root, [keys[0]])
    assert (root / gate.MARKER).exists(), "a sibling attempt must veto root release"
    assert _marker(root) == [keys[1]], "caller/marker-only clearance must not lose a pending sibling"
    assert gate.status(root) != 0


def test_current_uncovered_key_vetoes_full_release(unit) -> None:
    root, keys = unit
    gate.record_probe_attempt(root, "DATA_OK", "fixture A", [keys[0]])
    assert gate.clear_block(root, "fixture", earned=True, sources=[keys[0]]) == 0
    assert (root / gate.MARKER).exists(), "complete current root keys must veto release"
    assert set(_marker(root)) == set(keys[1:]), "complete current root keys, not just marker/caller, veto release"


@pytest.mark.parametrize("earned", [False, True])
def test_legacy_empty_arm_cannot_release_any_current_live_key(unit, earned: bool) -> None:
    root, keys = unit
    # Historical compatibility fixture, not a supported new empty-scope writer.
    with gate.gate_transaction(root):
        gate._append_audit(root, "block-marker-only", "sources=[]", [])
    (root / gate.MARKER).write_text(json.dumps({"sources": []}), encoding="utf-8")
    assert gate.clear_block(root, "empty scope earns no current key", earned=earned) != 0
    assert set(_marker(root)) == set(keys)
    assert gate.status(root) != 0


@pytest.mark.parametrize("first", ["clear", "attempt"])
def test_clear_and_new_attempt_serialize_in_either_order(  # pylint: disable=too-many-locals
    unit, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    root, keys = unit
    _earn(root, keys)
    gate.record_probe_attempt(root, "DATA_OK", "fixture A", [keys[0]])
    paused, release, second_done = threading.Event(), threading.Event(), threading.Event()
    append, physical = gate._append_audit, gate._release_physical

    def pause():
        paused.set()
        assert release.wait(5)

    def after_stop(location, action, detail, sources=None):
        if action == "probe-error":
            pause()
        return append(location, action, detail, sources)

    def before_release(location):
        pause()
        return physical(location)

    if first == "clear":
        monkeypatch.setattr(gate, "_release_physical", before_release)
    else:
        monkeypatch.setattr(gate, "_append_audit", after_stop)
    clear = partial(gate.clear_block, root, "fixture A", earned=True, sources=[keys[0]])
    attempt = partial(gate.record_probe_attempt, root, "ERROR", "fixture B", [keys[1]])
    with ThreadPoolExecutor(max_workers=2) as pool:
        leading = pool.submit(clear if first == "clear" else attempt)
        assert paused.wait(5)
        following = pool.submit(attempt if first == "clear" else clear)
        following.add_done_callback(lambda _future: second_done.set())
        assert not second_done.wait(0.15), "clear and pending attempt must not overlap commit"
        release.set()
        assert leading.result(timeout=10) in (None, 0)
        assert following.result(timeout=10) in (None, 0)
    assert (root / gate.MARKER).exists() and _marker(root) == [keys[1]]
    assert gate.status(root) != 0


@pytest.mark.parametrize("boundary", ["marker", "audit", "fsync", "release", "acl-remove", "acl-readback"])
def test_failed_boundary_never_credits_release(unit, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    root, keys = unit
    gate.record_probe_attempt(root, "DATA_OK", "fixture DATA_OK", keys)

    def fail(*_args, **_kwargs):
        raise OSError("fixture fault")

    if boundary == "marker":
        monkeypatch.setattr(gate, "_durable_write", fail)
        result = gate.apply_block(root, keys)
    else:
        if boundary == "audit":
            monkeypatch.setattr(gate, "_append_audit", fail)
        elif boundary == "fsync":
            monkeypatch.setattr(gate.os, "fsync", fail)
        elif boundary == "release":
            monkeypatch.setattr(gate, "_durable_unlink", fail)
        else:
            monkeypatch.setattr(gate.platform, "system", lambda: "Windows")
            monkeypatch.setattr(gate, "_clear_acl_targets", fail if boundary == "acl-remove" else lambda _root: None)
            monkeypatch.setattr(
                gate, "inspect_physical_barrier", lambda _root: ("cannot_establish", "fixture-readback")
            )
        result = gate.clear_block(root, "fixture", earned=True, sources=keys)
    assert result != 0, "boundary failure must reach the caller, not a successful transition"
    if boundary not in ("acl-remove", "acl-readback"):
        assert (root / gate.MARKER).exists()


def test_write_ahead_crash_retains_stop_and_reader_rejects_credit(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    root, keys = unit
    gate.record_probe_attempt(root, "DATA_OK", "fixture DATA_OK", keys)
    with monkeypatch.context() as patch:
        patch.setattr(gate, "_release_physical", lambda _root: (_ for _ in ()).throw(OSError("fixture crash")))
        assert gate.clear_block(root, "fixture", earned=True, sources=keys) != 0
    assert gate._data_access_ledger(gate._audit_entries(root), tuple(keys))[0] == dict.fromkeys(keys)
    assert gate.status(root) != 0
    assessment = gate.assess_data_access(
        root,
        package_spec=json.loads((root / gate.MIGRATION_SPEC).read_text()),
        package_data_sources={"self_contained": True, "omissions": [], "neutralized": [], "retained_network": []},
        fallback_authorization="stop",
        requested_scope="model_only",
    )
    assert assessment.state == "blocked", "a durable evidence row alone is not physical release"
    assert gate.clear_block(root, "retry", earned=True, sources=keys) == 0
    assert gate.status(root) == 0


@pytest.mark.parametrize("interrupted", [False, True])
def test_scoped_projection_distinguishes_completed_partial_clear_from_interruption(
    unit, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    root, keys = unit
    gate.record_probe_attempt(root, "DATA_OK", "fixture A", [keys[0]])
    with monkeypatch.context() as patch:
        if interrupted:
            patch.setattr(
                gate, "_durable_write", lambda *_args: (_ for _ in ()).throw(OSError("fixture partial update"))
            )
        result = gate.clear_block(root, "fixture A", earned=True, sources=[keys[0]])
    assert (result != 0) is interrupted
    spec = json.loads((root / gate.MIGRATION_SPEC).read_text(encoding="utf-8"))
    spec["data_sources"] = spec["data_sources"][:1]
    assessment = gate.assess_data_access(
        root,
        package_spec=spec,
        package_data_sources={"self_contained": True, "omissions": [], "neutralized": [], "retained_network": []},
        fallback_authorization="stop",
        requested_scope="model_and_report",
    )
    assert assessment.state == ("blocked" if interrupted else "live_data_ok")
    assert gate.status(root) != 0, "a scoped data projection must never release the sibling's root barrier"


def test_local_byte_projection_without_a_gate_does_not_invent_an_origin_acl_requirement(
    unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _keys = unit

    def no_physical_claim(_root):
        raise AssertionError("local-byte proof is not an origin-root writability claim")

    monkeypatch.setattr(gate, "inspect_physical_barrier", no_physical_claim)
    result = gate.assess_data_access(
        root,
        package_spec={"data_sources": [{"connection": {"class": "csv", "powerbi_target": "flat_file"}}]},
        package_data_sources={"self_contained": True, "omissions": [], "neutralized": [], "retained_network": []},
        fallback_authorization="stop",
        requested_scope="model_and_report",
    )
    assert result.state == "local_import_ready"
    assert gate.status(root) != 0, "local bytes must not declare this origin's live keys earned"


@pytest.mark.parametrize("windows", [True, False])
def test_clear_evidence_precedes_platform_release(unit, monkeypatch: pytest.MonkeyPatch, windows: bool) -> None:
    root, keys = unit
    gate.record_probe_attempt(root, "DATA_OK", "fixture DATA_OK", keys)
    seen = []
    unlink = gate._durable_unlink

    def remove(location, name):
        assert gate._audit_entries(root)[-1]["action"] == "probe-cleared"
        seen.append("marker")
        return unlink(location, name)

    monkeypatch.setattr(gate, "_durable_unlink", remove)
    monkeypatch.setattr(gate.platform, "system", lambda: "Windows" if windows else "Linux")
    monkeypatch.setattr(gate, "_clear_acl_targets", lambda _root: seen.append("acl"))
    monkeypatch.setattr(gate, "inspect_physical_barrier", lambda _root: ("clear", "physical_clear"))
    assert gate.clear_block(root, "fixture", earned=True, sources=keys) == 0
    assert seen == (["marker", "acl"] if windows else ["marker"])


@pytest.mark.parametrize("repaired", [False, True])
def test_scoped_data_proof_does_not_claim_root_clearance(unit, repaired: bool) -> None:
    root, keys = unit
    _earn(root, keys)
    gate.record_probe_attempt(root, "ERROR", "fixture B", [keys[1]])
    if repaired:
        _earn(root, [keys[1]])
    else:
        (root / gate.MARKER).unlink()  # Deliberately corrupt only this marker-only test fixture.
    spec = json.loads((root / gate.MIGRATION_SPEC).read_text(encoding="utf-8"))
    spec["data_sources"] = spec["data_sources"][:1]
    result = gate.assess_data_access(
        root,
        package_spec=spec,
        package_data_sources={"self_contained": True, "omissions": [], "neutralized": [], "retained_network": []},
        fallback_authorization="stop",
        requested_scope="model_and_report",
    )
    assert result.state == "live_data_ok", "a sibling cannot invalidate the selected key's genuine proof"
    assert gate.status(root) == (0 if repaired else 3), "the root-wide authority must still refuse the missing stop"
    assert (root / gate.MARKER).exists() is False


def test_torn_audit_never_rearms_or_clears(unit) -> None:
    root, keys = unit
    assert gate.apply_block(root, keys) == 0
    with (root / gate.AUDIT).open("ab") as stream:
        stream.write(b'{"action":')
    before = (root / gate.MARKER).read_bytes()
    assert gate.clear_block(root, "fixture", earned=True, sources=keys) != 0
    assert gate.apply_block(root, keys) != 0
    assert gate.status(root) != 0
    assert (root / gate.MARKER).read_bytes() == before


def test_generic_audit_cannot_impersonate_transition(unit) -> None:
    root, keys = unit
    with pytest.raises(ValueError, match="dedicated"):
        gate._audit(root, "probe-error", "fixture", keys)
    gate._audit(root, "engine-receipt", "sha256=" + "0" * 64)
    assert gate._audit_entries(root)[-1]["action"] == "engine-receipt"


def test_probe_attempt_cannot_bypass_the_repository_scope_refusal(unit) -> None:
    root, keys = unit
    (root / "AGENTS.md").write_text("fixture checkout marker", encoding="utf-8")
    with pytest.raises(gate.GateTransactionError, match="scope-refused"):
        gate.record_probe_attempt(root, "ERROR", "fixture refused scope", [keys[0]])
    assert not (root / gate.MARKER).exists()
    assert not (root / "fabric").exists(), "refused authority roots must never acquire a physical stop"
    assert not (root / gate.AUDIT).exists()


def test_unprovable_source_identity_retains_a_stop_without_a_success_row(unit) -> None:
    root, keys = unit
    assert gate.apply_block(root, ["source-key:unprovable"]) != 0
    assert not (root / gate.MARKER).exists(), "invalid arm scope must be refused before physical effects"
    assert not (root / gate.AUDIT).exists(), "an unprovable source must not receive a success-shaped arm row"
    assert gate.apply_block(root, keys) == 0
    assert gate.clear_block(root, "unrelated proof", earned=True, sources=[keys[0]]) != 0
    assert (root / gate.MARKER).exists() and gate.status(root) != 0


def test_native_descriptor_contention_death_and_persistent_external_file(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    child = _child(
        root,
        "from pathlib import Path; import credential_gate as g; "
        "tx=g.gate_transaction(Path(sys.argv[1])); tx.__enter__(); print('HELD', flush=True); "
        "sys.stdin.read()",
    )
    # A pipe owned by this test, not a PID-age/stale-lock recovery scheme.
    try:
        assert child.stdout.readline().strip() == "HELD"
        authority = gate.coordination_root(root)
        assert authority.companion.parent == root.parent
        assert authority.companion.stat().st_size == 1
        with pytest.raises(gate.GateTransactionError, match="busy"):
            with gate.gate_transaction(root, timeout=0.1):
                pytest.fail("contending descriptor was admitted")
        root.rename(tmp_path / "renamed")
        child.kill()
        child.wait(timeout=10)
        with gate.gate_transaction(root, timeout=1):
            assert authority.companion.exists(), "death releases the descriptor, never deletes the companion"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.skipif(
    os.name != "nt", reason="write-deny enforcement is an icacls ACL; the marker-only path cannot block a write"
)
def test_windows_death_after_marker_removal_retains_kernel_stop_and_can_retry(tmp_path: Path) -> None:
    from test_credential_gate_acl_layouts import AclLab  # pylint: disable=import-outside-toplevel

    lab = AclLab(tmp_path / "acl")
    child = None
    try:
        root = lab.root / "Unit"
        artifact = root / "fabric" / "model.tmdl"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("fixture definition", encoding="utf-8")
        source = {
            "connection": {
                "class": "sqlserver",
                "server": "fixture.example.invalid",
                "database": "fixture",
                "schema": "dbo",
                "powerbi_target": "live_source",
            },
            "tables": [{"name": "Orders", "table": "[dbo].[Orders]"}],
        }
        spec = {"data_sources": [source]}
        (root / gate.MIGRATION_SPEC).write_text(json.dumps(spec), encoding="utf-8")
        key = preflight._classify_legs(source, 0)[0][0]
        gate.record_probe_attempt(root, "DATA_OK", "fixture DATA_OK", [key])
        assert lab.denies(artifact.parent), "independent native oracle must observe the initial deny"
        child = _child(
            root,
            "from pathlib import Path; import credential_gate as g; import os; "
            "g._clear_acl_targets=lambda root: os._exit(23); "
            f"g.clear_block(Path(sys.argv[1]), 'fixture', earned=True, sources={[key]!r})",
        )
        child.communicate(timeout=40)
        assert child.returncode == 23, "crash must reach marker-before-ACL removal, not an earlier failure"
        assert not (root / gate.MARKER).exists()
        assert gate._audit_entries(root)[-1]["action"] == "probe-cleared"
        assert lab.denies(artifact.parent), "durable clear evidence must not erase a surviving kernel stop"
        with pytest.raises(PermissionError):
            artifact.write_text("not permitted", encoding="utf-8")
        assert gate.status(root) != 0
        assessment = gate.assess_data_access(
            root,
            package_spec=spec,
            package_data_sources={"self_contained": True, "omissions": [], "neutralized": [], "retained_network": []},
            fallback_authorization="stop",
            requested_scope="model_and_report",
        )
        assert assessment.state == "blocked"
        with gate.gate_transaction(root, timeout=1):
            assert gate.coordination_root(root).companion.stat().st_size == 1
        assert gate.clear_block(root, "retry fixture", earned=True, sources=[key]) == 0
        artifact.write_text("permitted after retry", encoding="utf-8")
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        lab.cleanup()


def test_reader_busy_never_reports_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "unit"
    root.mkdir()
    entered, release = threading.Event(), threading.Event()

    def holder():
        with gate.gate_transaction(root):
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(gate, "TX_READER_TIMEOUT", 0.1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        held = pool.submit(holder)
        assert entered.wait(5)
        assert gate.status(root) != 0, "a reader without the descriptor would see a false clean root"
        assert gate.inspect_physical_barrier(root)[0] == "cannot_establish"
        release.set()
        held.result(timeout=10)
    assert gate.status(root) == 0


@pytest.mark.parametrize("kind", ["directory", "extra-byte", "symlink", "hardlink"])
def test_unsafe_companion_is_refused(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "unit"
    root.mkdir()
    companion = gate.coordination_root(root).companion
    if kind == "directory":
        companion.mkdir()
    elif kind == "extra-byte":
        companion.write_bytes(b"xx")
    else:
        other = tmp_path / "other"
        other.write_bytes(b"\0")
        try:
            if kind == "symlink":
                companion.symlink_to(other)
            else:
                os.link(other, companion)
        except OSError:
            pytest.skip("this platform/account cannot create symlinks without elevation")
    assert gate.status(root) != 0


def test_replacement_cannot_be_credited_as_serialized(tmp_path: Path) -> None:
    root = tmp_path / "unit"
    root.mkdir()
    authority = gate.coordination_root(root)
    if os.name == "nt":
        with gate.gate_transaction(root):
            with pytest.raises(PermissionError):
                authority.companion.unlink()
        return
    with pytest.raises(gate.GateTransactionError, match="changed"):
        with gate.gate_transaction(root):
            authority.companion.unlink()
            authority.companion.write_bytes(b"\0")
    assert authority.companion.exists()


def test_canonical_case_and_relative_aliases_share_one_token(tmp_path: Path) -> None:
    root = tmp_path / "Unit"
    root.mkdir()
    authority = gate.coordination_root(root)
    assert authority == gate.coordination_root(root.parent / "." / root.name)
    if os.name == "nt":
        assert authority.canonical == gate.coordination_root(Path(str(root).swapcase())).canonical
        assert authority.companion.name == gate.coordination_root(Path(str(root).swapcase())).companion.name
        with pytest.raises(gate.GateTransactionError):
            gate.coordination_root(root.with_name("UNIT~1"))
        with pytest.raises(gate.GateTransactionError, match="alias-unproved"):
            gate.coordination_root(Path(r"\\uncontacted.example.invalid\share\Unit"))


def test_unavailable_native_object_identity_is_not_a_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "Unit"
    root.mkdir()
    assert gate.status(root) == 0
    safe, fstat = gate._safe_entry, os.fstat

    def unknown(info):
        return SimpleNamespace(st_dev=info.st_dev, st_ino=0, st_size=info.st_size)

    monkeypatch.setattr(gate, "_safe_entry", lambda path, **kwargs: unknown(safe(path, **kwargs)))
    monkeypatch.setattr(gate.os, "fstat", lambda descriptor: unknown(fstat(descriptor)))
    assert gate.status(root) == 3, "two unavailable object IDs are not proof of identity"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific path spelling")
@pytest.mark.parametrize(("readable", "flags"), [(False, 0), (True, 1)])
def test_unproved_or_case_sensitive_parent_is_not_a_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, readable: bool, flags: int
) -> None:
    import ctypes  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    root = tmp_path / "Unit"
    root.mkdir()
    assert gate.status(root) == 0
    library = ctypes.WinDLL
    kernel = library("kernel32", use_last_error=True)

    def query(_handle, _kind, output, _size):
        ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = flags
        return readable

    proxy = SimpleNamespace(
        CreateFileW=kernel.CreateFileW,
        GetDriveTypeW=kernel.GetDriveTypeW,
        GetFinalPathNameByHandleW=kernel.GetFinalPathNameByHandleW,
        GetFileInformationByHandleEx=query,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: proxy)
    assert gate.status(root) == 3, "unproved case semantics must refuse, not normalize by assumption"


@pytest.mark.skipif(os.name != "nt", reason="`subst` is Windows-only")
def test_windows_subst_alias_uses_the_opened_parent_identity(tmp_path: Path) -> None:
    import ctypes  # pylint: disable=import-outside-toplevel

    root = tmp_path / "Unit"
    root.mkdir()
    drives = ctypes.windll.kernel32.GetLogicalDrives()
    letter = next(chr(code) for code in range(ord("Z"), ord("D"), -1) if not drives & (1 << (code - ord("A"))))
    drive = f"{letter}:"
    created = subprocess.run(["subst", drive, str(tmp_path)], capture_output=True, timeout=10, check=False)
    assert created.returncode == 0, "test-owned subst mapping must be created, not silently skipped"
    try:
        alias = Path(f"{drive}\\Unit")
        assert gate.coordination_root(alias).canonical == gate.coordination_root(root).canonical
        assert gate.coordination_root(alias).companion.name == gate.coordination_root(root).companion.name

        def contend():
            with gate.gate_transaction(alias, timeout=0.1):
                return "admitted"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with gate.gate_transaction(root):
                with pytest.raises(gate.GateTransactionError, match="busy"):
                    pool.submit(contend).result(timeout=5)
        with gate.gate_transaction(alias):
            assert gate.coordination_root(root).companion.exists()
    finally:
        removed = subprocess.run(["subst", drive, "/D"], capture_output=True, timeout=10, check=False)
        assert removed.returncode == 0, "remove only this test's own subst mapping"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific path spelling")
def test_windows_junction_is_refused_not_given_an_independent_token(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    (parent / "Unit").mkdir(parents=True)
    alias = tmp_path / "junction"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(alias), str(parent)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, "junction fixture creation must succeed to exercise the refusal"
    try:
        with pytest.raises(gate.GateTransactionError):
            gate.coordination_root(alias / "Unit")
    finally:
        alias.rmdir()


def test_parent_replacement_and_reparse_are_not_silent_aliases(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    root = parent / "unit"
    root.mkdir(parents=True)
    with pytest.raises(OSError):
        with gate.gate_transaction(root):
            parent.rename(tmp_path / "old")
            root.mkdir(parents=True)


def test_dotdot_cannot_erase_an_unproved_parent_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "parent"
    (parent / "ordinary").mkdir(parents=True)
    root = parent / "Unit"
    root.mkdir()
    path = parent / "ordinary" / ".." / "Unit"
    assert gate.coordination_root(path).canonical == gate.coordination_root(root).canonical
    safe = gate._safe_entry

    def unproved(location, **kwargs):
        if location == parent / "ordinary":
            raise gate.GateTransactionError("fixture reparse component")
        return safe(location, **kwargs)

    with gate.gate_transactions([root]):
        monkeypatch.setattr(gate, "_safe_entry", unproved)
        with pytest.raises(gate.GateTransactionError, match="reparse component"):
            gate.coordination_root(path)
        assert gate.status(path) == 3, "an alias binding must not bypass validation of the original parent chain"


def test_public_wrappers_join_without_reacquiring(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    root, keys = unit
    count = []
    acquire = gate._descriptor_lock

    def counted(descriptor, lock):
        count.append(lock)
        return acquire(descriptor, lock)

    monkeypatch.setattr(gate, "_descriptor_lock", counted)
    with gate.gate_transaction(root):
        assert gate.apply_block(root, keys) == 0
        assert gate.status(root) != 0
        gate.record_probe_attempt(root, "DATA_OK", "fixture", keys)
        assert gate.clear_block(root, "fixture", earned=True, sources=keys) == 0
    assert count == [True], "public wrappers must not recursively reacquire the same byte"


def _hook():
    spec = importlib.util.spec_from_file_location("tx_hook", SCRIPTS / "hooks" / "credential_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("command", "denied"),
    [
        ("Remove-Item {lock}", True),
        ("rm '{lock}'", True),
        ("Write-Output x > '{lock}'", True),
        ("echo x>'{lock}'", True),
        ('Write-Output "Remove-Item {lock}"', False),
        ("python -c \"print('Remove-Item {lock}')\"", False),
        ("Get-FileHash '{lock}'", False),
        ("pytest -q tests/test_credential_gate_transactions.py", False),
        ("echo {lock}", False),
        ("Set-Content tests\\test_prose.py '{lock}'", False),
        ("Set-Content -Path tests\\test_prose.py -Value '{lock}'", False),
    ],
)
def test_companion_hook_command_position_not_quoted_prose(command: str, denied: bool) -> None:
    hook = _hook()
    lock = ".credential-gate-tx-v1-" + "a" * 64 + ".lock"
    assert hook._companion_mutation({"toolInput": {"command": command.format(lock=lock)}}) is denied
    assert not hook._writes_a_control_file(
        {"toolInput": {"path": "tests/test_control.py", "content": f"Remove-Item {lock}"}}, "edit", Path(".")
    )
    assert hook._writes_a_control_file({"toolInput": {"path": lock}}, "edit", Path("."))


def test_preflight_propagates_failed_transaction(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _keys = unit
    monkeypatch.setattr(preflight.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 3))
    assert preflight.cmd_classify(root / gate.MIGRATION_SPEC) == 3, "preflight must propagate transaction refusal"


def test_probe_attempt_does_not_swallow_transaction_failure(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    root, keys = unit
    monkeypatch.setattr(gate.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fixture fsync")))
    with pytest.raises(OSError):
        probe._record_attempt(root, "DATA_OK", "fixture", keys)
    assert not (root / gate.AUDIT).exists()


def test_audit_fsync_failure_is_not_a_successful_clear(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    root, keys = unit
    gate.record_probe_attempt(root, "DATA_OK", "fixture", keys)
    audit_identity = gate._identity((root / gate.AUDIT).stat())
    sync = os.fsync
    reached = []

    def fail_audit(descriptor):
        if gate._identity(os.fstat(descriptor)) == audit_identity:
            reached.append(True)
            raise OSError("fixture audit fsync")
        return sync(descriptor)

    monkeypatch.setattr(gate.os, "fsync", fail_audit)
    assert gate.clear_block(root, "fixture", earned=True, sources=keys) != 0, "audit fsync failure cannot earn success"
    assert reached == [True]
    assert (root / gate.MARKER).exists()


@pytest.mark.parametrize("boundary", ["authorization-file", "authorization-audit", "authorization-release"])
def test_authorization_failure_retains_physical_stop(unit, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    root, keys = unit
    assert gate.apply_block(root, keys) == 0
    write, append, release = gate._durable_write, gate._append_audit, gate._release_physical

    def write_auth(location, name, text):
        if name == gate.OVERRIDE:
            raise OSError("fixture authorization file")
        return write(location, name, text)

    def append_auth(location, action, detail, sources=None):
        if action == "authorize":
            raise OSError("fixture authorization audit")
        return append(location, action, detail, sources)

    def release_auth(location):
        assert gate._audit_entries(location)[-1]["action"] == "authorize"
        raise OSError("fixture authorization release")

    with monkeypatch.context() as patch:
        patch.setattr(gate, "_ancestry", lambda: [])
        patch.setattr(gate, "_durable_write", write_auth if boundary == "authorization-file" else write)
        patch.setattr(gate, "_append_audit", append_auth if boundary == "authorization-audit" else append)
        patch.setattr(gate, "_release_physical", release_auth if boundary == "authorization-release" else release)
        assert gate.authorize(root, "fixture human") != 0
    assert (root / gate.MARKER).exists()
    assert gate.status(root) != 0, "write-ahead authorization is not completed physical release"


def test_subprocess_crash_after_clear_evidence_retains_stop(unit) -> None:
    root, keys = unit
    gate.record_probe_attempt(root, "DATA_OK", "fixture", keys)
    code = (
        "from pathlib import Path; import credential_gate as g; import os; "
        "g.platform.system=lambda:'Linux'; "
        "g._release_physical=lambda root: os._exit(19); "
        f"g.clear_block(Path(sys.argv[1]), 'fixture', earned=True, sources={keys!r})"
    )
    child = _child(root, code)
    child.communicate(timeout=20)
    assert child.returncode == 19, "crash must occur at the intended post-evidence release boundary"
    assert gate._audit_entries(root)[-1]["action"] == "probe-cleared"
    assert (root / gate.MARKER).exists() and gate.status(root) != 0
    with gate.gate_transaction(root, timeout=1):
        assert gate.coordination_root(root).companion.exists()


def test_arm_attempt_orders_marker_acl_readback_then_audit(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    root, keys = unit
    seen = []
    append = gate._append_audit
    write = gate._durable_write
    monkeypatch.setattr(gate.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gate, "_checked_acl_tree", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(gate, "_account_aces", lambda _root: [])
    monkeypatch.setattr(gate, "_icacls", lambda _args: (seen.append("deny") or 0, ""))
    monkeypatch.setattr(gate, "_all_targets_denied", lambda _root: seen.append("readback") or True)

    def marked(location, name, text):
        seen.append("marker")
        return write(location, name, text)

    def recorded(location, action, detail, sources=None):
        assert "readback" in seen
        seen.append(action)
        return append(location, action, detail, sources)

    monkeypatch.setattr(gate, "_durable_write", marked)
    monkeypatch.setattr(gate, "_append_audit", recorded)
    gate.record_probe_attempt(root, "ERROR", "fixture", [keys[0]])
    assert seen == ["marker", "deny", "readback", "marker", "block", "probe-error"]


def test_every_anchor_is_required_not_any(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directories = [tmp_path / "Model", tmp_path / "Report"]
    deny = (frozenset({"OI", "CI", "DENY"}), frozenset({"WD", "AD", "WA"}))
    monkeypatch.setattr(gate, "denied_dirs", lambda *_args, **_kwargs: directories)
    monkeypatch.setattr(
        gate, "_checked_acl_tree", lambda *_args, **_kwargs: {directories[0]: [deny], directories[1]: []}
    )
    assert gate._all_targets_denied(tmp_path) is False, "all anchors, not any residual deny, establish enforcement"


def test_parser_transaction_failure_cannot_be_a_success(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    import parse_tableau  # pylint: disable=import-outside-toplevel

    root, _keys = unit
    source = Path(__file__).parent / "fixtures" / "federated_multi_connection.twb"
    monkeypatch.setattr(parse_tableau.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 3))
    monkeypatch.setattr(sys, "argv", ["parse_tableau.py", str(source), "-o", str(root / "parsed.json")])
    code = 0
    try:
        parse_tableau.main()
    except SystemExit as failure:
        code = failure.code
    assert code == 3, "parser must propagate the transaction refusal rather than report success"


def test_audit_descriptor_is_held_through_append(unit, monkeypatch: pytest.MonkeyPatch) -> None:
    root, keys = unit
    assert gate.apply_block(root, keys) == 0
    audit_identity = gate._identity((root / gate.AUDIT).stat())
    sync = os.fsync
    monkeypatch.setattr(gate, "TX_READER_TIMEOUT", 0.1)

    def observed(descriptor):
        if gate._identity(os.fstat(descriptor)) == audit_identity:
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(gate.status, root).result(timeout=5) == 3, "audit descriptor must remain locked"
        return sync(descriptor)

    monkeypatch.setattr(gate.os, "fsync", observed)
    gate.record_probe_attempt(root, "ERROR", "fixture", [keys[0]])


def test_changed_companion_identity_cannot_establish_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "identity-control"
    root.mkdir()
    companion = gate.coordination_root(root).companion
    entry = gate._safe_entry
    with gate.gate_transaction(root):
        with monkeypatch.context() as patch:

            def changed(path, **kwargs):
                info = entry(path, **kwargs)
                if path == companion:
                    return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1, st_size=info.st_size)
                return info

            patch.setattr(gate, "_safe_entry", changed)
            assert gate.status(root) == 3, "replaced companion must not be credited as serialized"


def test_release_never_unlinks_companion(tmp_path: Path) -> None:
    root = tmp_path / "persistent-control"
    root.mkdir()
    companion = gate.coordination_root(root).companion
    with gate.gate_transaction(root):
        pass
    assert companion.is_file(), "release must never unlink the persistent companion"


def _mutant(module, name: str, old: str, new: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """In-memory single-function mutations: never edit/rebaseline product files or score arbitrary errors."""
    source = inspect.getsource(getattr(module, name))
    assert source.count(old) == 1, "mutation must match exactly its intended production boundary"
    original = getattr(module, name)
    try:
        exec(compile(source.replace(old, new), "<controlled-transaction-mutation>", "exec"), vars(module))  # pylint: disable=exec-used
        mutant = getattr(module, name)
    finally:
        setattr(module, name, original)
    monkeypatch.setattr(module, name, mutant)


def _omitted_mutex_schedule(unit: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate marker lost updates from torn audit writes; all three writers must succeed first."""
    root, _keys = unit
    gate._audit(root, "engine-receipt", "sha256=" + "0" * 64)
    write, append = gate._durable_write, gate._append_audit
    staged = threading.Barrier(3)
    marker_mutex = threading.Lock()
    audit_mutex = threading.Lock()

    def same_snapshot(location, name, text):
        if name == gate.MARKER:
            staged.wait(timeout=10)
        with marker_mutex:
            return write(location, name, text)

    def complete_row(*args, **kwargs):
        with audit_mutex:
            return append(*args, **kwargs)

    monkeypatch.setattr(gate, "_descriptor_lock", lambda *_args: None)
    monkeypatch.setattr(gate, "_durable_write", same_snapshot)
    monkeypatch.setattr(gate, "_append_audit", complete_row)
    test_concurrent_first_arms_preserve_union_and_exact_rows(unit)


@pytest.mark.parametrize(
    "mutation",
    [
        "marker-assignment",
        "caller-only-clear",
        "reader-without-lock",
        "omitted-lock",
        "unlock-before-append",
        "swallowed-audit-fsync",
        "any-anchor",
        "in-root-lock",
        "parser-suppression",
        "preflight-suppression",
        "transient-package-token",
        "unlink-on-release",
        "replacement-credited",
        "latest-invalidation",
        "failures-only-pending",
        "current-membership",
        "count-substitution",
        "omitted-root-veto",
        "global-future-authorization",
        "authorized-attempt-rearm",
    ],
)
def test_required_mutations_fail_their_intended_assertion(  # pylint: disable=too-many-branches,too-many-statements
    unit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Each defect must reach its named invariant assertion, not an import/timeout/unrelated error."""
    control = tmp_path / "control"
    control.mkdir()
    if mutation == "marker-assignment":
        _mutant(
            gate,
            "_apply_pending_block",
            "list(dict.fromkeys([*_established_sources(migration), *pending_sources]))",
            "pending_sources",
            monkeypatch,
        )
        check = partial(test_concurrent_first_arms_preserve_union_and_exact_rows, unit)
        message = "assignment must not lose"
    elif mutation == "latest-invalidation":
        _mutant(gate, "_apply_probe_attempt", '                state["earned"] = False\n', "", monkeypatch)
        check = partial(test_latest_historical_attempt_invalidates_every_clear_projection, unit, gate.PROBE_DATA_OK)
        message = "latest attempt must invalidate"
    elif mutation == "failures-only-pending":
        _mutant(
            gate,
            "_transaction_pending",
            "codes[key] is not None",
            "codes[key] in PROBE_ATTEMPT_CODES.values()",
            monkeypatch,
        )
        check = partial(test_every_non_none_canonical_code_stays_pending, unit, "probe-data_ok")
        message = "every non-None canonical code"
    elif mutation in ("current-membership", "count-substitution"):
        _mutant(
            gate,
            "_block_refusal",
            "source_refusal or not set(sources) <= current",
            "source_refusal" if mutation == "current-membership" else "source_refusal or len(sources) != len(current)",
            monkeypatch,
        )
        check = partial(
            test_supported_arm_closes_current_source_domain_without_effects,
            unit,
            "foreign" if mutation == "current-membership" else "substitution",
        )
        message = "membership, not display or cardinality"
    elif mutation == "omitted-root-veto":
        _mutant(gate, "apply_block", "if omitted and (", "if False and omitted and (", monkeypatch)
        check = partial(test_exact_skip_requires_omitted_pending_root_coverage, unit, False)
        message = "omitted pending root veto"
    elif mutation == "global-future-authorization":
        _mutant(
            gate,
            "_data_access_ledger",
            'not forced and state["armed"] is not None and timestamp >= state["armed"]',
            "not forced",
            monkeypatch,
        )
        check = partial(test_permission_is_never_global_or_future, unit, monkeypatch, "before-arm")
        message = "permission must cover exact current"
    elif mutation == "authorized-attempt-rearm":
        _mutant(
            gate, "record_probe_attempt", "not permitted and _apply_pending_block", "_apply_pending_block", monkeypatch
        )
        check = partial(
            test_authorized_observation_preserves_exact_permission_until_new_arm, unit, monkeypatch, "ERROR"
        )
        message = "authorized measurement must not synthesize"
    elif mutation == "caller-only-clear":
        _mutant(
            gate,
            "clear_block",
            "remaining_sources = _transaction_pending(migration)",
            "remaining_sources = [key for key in marker_sources if key not in (earned_sources or [])]",
            monkeypatch,
        )
        check = partial(test_current_uncovered_key_vetoes_full_release, unit)
        message = "complete current root keys"
    elif mutation in ("reader-without-lock", "omitted-lock"):
        if mutation == "reader-without-lock":
            monkeypatch.setattr(gate, "status", gate.status.__wrapped__)
            check = partial(test_reader_busy_never_reports_clear, control, monkeypatch)
            message = "reader without"
        else:
            check = partial(_omitted_mutex_schedule, unit, monkeypatch)
            message = "assignment must not lose"
    elif mutation == "unlock-before-append":
        _mutant(
            gate,
            "_append_audit",
            "    _check_transaction(migration)\n    path = migration / AUDIT",
            "    _descriptor_lock(_TRANSACTIONS.get()[-1].descriptor, False)\n    path = migration / AUDIT",
            monkeypatch,
        )
        check = partial(test_audit_descriptor_is_held_through_append, unit, monkeypatch)
        message = "audit descriptor must remain locked"
    elif mutation == "swallowed-audit-fsync":
        _mutant(
            gate,
            "_append_audit",
            "        os.fsync(descriptor)\n",
            "        try:\n            os.fsync(descriptor)\n        except OSError:\n            pass\n",
            monkeypatch,
        )
        check = partial(test_audit_fsync_failure_is_not_a_successful_clear, unit, monkeypatch)
        message = "audit fsync failure"
    elif mutation == "any-anchor":
        _mutant(gate, "_all_targets_denied", "and all(", "and any(", monkeypatch)
        check = partial(test_every_anchor_is_required_not_any, control, monkeypatch)
        message = "all anchors"
    elif mutation == "in-root-lock":
        _mutant(gate, "coordination_root", 'root.parent / f"{TX_PREFIX}', 'root / f"{TX_PREFIX}', monkeypatch)

        def check():
            root = tmp_path / "external-control"
            root.mkdir()
            assert gate.coordination_root(root).companion.parent == root.parent, "lock must be external"

        message = "lock must be external"
    elif mutation == "parser-suppression":
        import parse_tableau  # pylint: disable=import-outside-toplevel

        _mutant(parse_tableau, "main", "if result.returncode not in (0, 1):", "if False:", monkeypatch)
        check = partial(test_parser_transaction_failure_cannot_be_a_success, unit, monkeypatch)
        message = "parser must propagate"
    elif mutation == "preflight-suppression":
        _mutant(preflight, "_write_gate_marker", "if result.returncode:", "if False:", monkeypatch)
        check = partial(test_preflight_propagates_failed_transaction, unit, monkeypatch)
        message = "preflight must propagate"
    elif mutation == "transient-package-token":
        from test_package_data_access_swap import (  # pylint: disable=import-outside-toplevel
            test_first_package_generation_receives_final_token,
        )

        _mutant(gate, "_authority", "in _COORDINATION.get():", "in ():", monkeypatch)
        check = partial(test_first_package_generation_receives_final_token, control)
        message = "package generation must retain final token"
    elif mutation == "unlink-on-release":
        release = gate.GateTransaction.__exit__

        def unlink(transaction, *args):
            release(transaction, *args)
            transaction.authority.companion.unlink()

        monkeypatch.setattr(gate.GateTransaction, "__exit__", unlink)
        check = partial(test_release_never_unlinks_companion, control)
        message = "release must never unlink"
    else:
        check_identity = gate.GateTransaction.check

        def unchecked(transaction):
            try:
                check_identity(transaction)
            except gate.GateTransactionError:
                pass

        monkeypatch.setattr(gate.GateTransaction, "check", unchecked)
        check = partial(test_changed_companion_identity_cannot_establish_clear, control, monkeypatch)
        message = "replaced companion must not be credited"
    with pytest.raises(AssertionError, match=message):
        check()
