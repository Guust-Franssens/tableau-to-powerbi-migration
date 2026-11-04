"""Descriptor/transaction controls for `package_data_access`, confined to pytest-owned roots.

Run: pytest -q tests/test_package_transactions.py --basetemp _package_tx_tests
Moved from tests/test_credential_gate_transactions.py with the transaction code (#702, #801): these
controls exercise only the per-root coordination transaction, never a credential-gate transition.
"""

# Test names describe the controls; private seams and dynamic code are deliberate mutation inputs.
# pylint: disable=protected-access,missing-function-docstring
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import inspect
from functools import partial
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import package_data_access as pda  # noqa: E402  # pylint: disable=wrong-import-position

SCRIPTS = Path(pda.__file__).parent


def _child(root: Path, code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); " + code, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_native_descriptor_contention_death_and_persistent_external_file(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    child = _child(
        root,
        "from pathlib import Path; import package_data_access as g; "
        "tx=g.gate_transaction(Path(sys.argv[1])); tx.__enter__(); print('HELD', flush=True); "
        "sys.stdin.read()",
    )
    # A pipe owned by this test, not a PID-age/stale-lock recovery scheme.
    try:
        assert child.stdout.readline().strip() == "HELD"
        authority = pda.coordination_root(root)
        assert authority.companion.parent == root.parent
        assert authority.companion.stat().st_size == 1
        with pytest.raises(pda.GateTransactionError, match="busy"):
            with pda.gate_transaction(root, timeout=0.1):
                pytest.fail("contending descriptor was admitted")
        root.rename(tmp_path / "renamed")
        child.kill()
        child.wait(timeout=10)
        with pda.gate_transaction(root, timeout=1):
            assert authority.companion.exists(), "death releases the descriptor, never deletes the companion"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.parametrize("tamper", ["grown", "truncated"])
def test_companion_resized_while_held_is_refused(tmp_path: Path, tamper: str) -> None:
    """Only the pre-lock look may see an empty companion; once held, size is exactly one byte again."""
    root = tmp_path / "unit"
    root.mkdir()
    companion = pda.coordination_root(root).companion
    with pytest.raises(pda.GateTransactionError, match="transaction-companion-changed"):
        with pda.gate_transaction(root):
            with companion.open("r+b") as stream:
                if tamper == "grown":
                    stream.seek(1)
                    stream.write(b"\0")
                else:
                    stream.truncate(0)
            pda._check_transaction(root)


def test_replacement_cannot_be_credited_as_serialized(tmp_path: Path) -> None:
    root = tmp_path / "unit"
    root.mkdir()
    authority = pda.coordination_root(root)
    if os.name == "nt":
        with pda.gate_transaction(root):
            with pytest.raises(PermissionError):
                authority.companion.unlink()
        return
    with pytest.raises(pda.GateTransactionError, match="changed"):
        with pda.gate_transaction(root):
            authority.companion.unlink()
            authority.companion.write_bytes(b"\0")
    assert authority.companion.exists()


def test_canonical_case_and_relative_aliases_share_one_token(tmp_path: Path) -> None:
    root = tmp_path / "Unit"
    root.mkdir()
    authority = pda.coordination_root(root)
    assert authority == pda.coordination_root(root.parent / "." / root.name)
    if os.name == "nt":
        assert authority.canonical == pda.coordination_root(Path(str(root).swapcase())).canonical
        assert authority.companion.name == pda.coordination_root(Path(str(root).swapcase())).companion.name
        with pytest.raises(pda.GateTransactionError):
            pda.coordination_root(root.with_name("UNIT~1"))
        with pytest.raises(pda.GateTransactionError, match="alias-unproved"):
            pda.coordination_root(Path(r"\\uncontacted.example.invalid\share\Unit"))


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
        assert pda.coordination_root(alias).canonical == pda.coordination_root(root).canonical
        assert pda.coordination_root(alias).companion.name == pda.coordination_root(root).companion.name

        def contend():
            with pda.gate_transaction(alias, timeout=0.1):
                return "admitted"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with pda.gate_transaction(root):
                with pytest.raises(pda.GateTransactionError, match="busy"):
                    pool.submit(contend).result(timeout=5)
        with pda.gate_transaction(alias):
            assert pda.coordination_root(root).companion.exists()
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
        with pytest.raises(pda.GateTransactionError):
            pda.coordination_root(alias / "Unit")
    finally:
        alias.rmdir()


def test_parent_replacement_and_reparse_are_not_silent_aliases(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    root = parent / "unit"
    root.mkdir(parents=True)
    with pytest.raises(OSError):
        with pda.gate_transaction(root):
            parent.rename(tmp_path / "old")
            root.mkdir(parents=True)


def test_release_never_unlinks_companion(tmp_path: Path) -> None:
    root = tmp_path / "persistent-control"
    root.mkdir()
    companion = pda.coordination_root(root).companion
    with pda.gate_transaction(root):
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


def test_projection_module_never_imports_the_credential_gate() -> None:
    """S3 deletes credential_gate.py whole; the projection and transactions must not depend on it."""
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import package_data_access; "
            "print('credential_gate' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False", "package_data_access must not import credential_gate"


@pytest.mark.parametrize("mutation", ["in-root-lock", "transient-package-token", "unlink-on-release"])
def test_required_transaction_mutations_fail_their_intended_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Each defect must reach its named invariant assertion, not an import/timeout/unrelated error."""
    control = tmp_path / "control"
    control.mkdir()
    if mutation == "in-root-lock":
        _mutant(pda, "coordination_root", 'root.parent / f"{TX_PREFIX}', 'root / f"{TX_PREFIX}', monkeypatch)

        def check():
            root = tmp_path / "external-control"
            root.mkdir()
            assert pda.coordination_root(root).companion.parent == root.parent, "lock must be external"

        message = "lock must be external"
    elif mutation == "transient-package-token":
        from test_package_data_access_swap import (  # pylint: disable=import-outside-toplevel
            test_first_package_generation_receives_final_token,
        )

        _mutant(pda, "_authority", "in _COORDINATION.get():", "in ():", monkeypatch)
        check = partial(test_first_package_generation_receives_final_token, control)
        message = "package generation must retain final token"
    else:
        release = pda.GateTransaction.__exit__

        def unlink(transaction, *args):
            release(transaction, *args)
            transaction.authority.companion.unlink()

        monkeypatch.setattr(pda.GateTransaction, "__exit__", unlink)
        check = partial(test_release_never_unlinks_companion, control)
        message = "release must never unlink"
    with pytest.raises((AssertionError, pytest.fail.Exception), match=message):
        check()
