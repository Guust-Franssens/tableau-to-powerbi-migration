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
            monkeypatch.setattr(gate, "_descriptor_lock", lambda *_args: None)
            check = partial(test_stale_skip_cannot_overtake_pending_attempt, unit, monkeypatch)
            message = "attempt must not commit|pending attempt must remain"
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
