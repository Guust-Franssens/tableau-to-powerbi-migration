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
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

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


def test_release_never_unlinks_companion(tmp_path: Path) -> None:
    root = tmp_path / "persistent-control"
    root.mkdir()
    companion = pda.coordination_root(root).companion
    with pda.gate_transaction(root):
        pass
    assert companion.is_file(), "release must never unlink the persistent companion"


def _transacts(root: Path) -> None:
    with pda.gate_transaction(root, timeout=pda.TX_READER_TIMEOUT):
        pass


def test_peer_arriving_before_the_creator_locks_never_sees_a_half_made_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#801: a peer that opens the companion right after its exclusive creation must still transact."""
    root = tmp_path / "unit"
    root.mkdir()
    companion = pda.coordination_root(root).companion
    real_open, seen, peer = os.open, [], {}

    def run_peer() -> None:
        try:
            with pda.gate_transaction(root):
                peer["size"] = companion.stat().st_size
        except OSError as exc:
            peer["error"] = exc

    def creating(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        # Deterministic interleaving: the peer runs to completion between creation and anything after it.
        if flags & os.O_EXCL and Path(path) == companion and not seen:
            seen.append(os.fstat(descriptor).st_size)
            thread = threading.Thread(target=run_peer)
            thread.start()
            thread.join(timeout=30)
            assert not thread.is_alive(), "peer must not wait on a creator that holds no lock"
        return descriptor

    monkeypatch.setattr(pda.os, "open", creating)
    with pda.gate_transaction(root):
        pass
    assert seen == [0], "the seam must open the companion while it is still empty"
    assert "error" not in peer, f"peer observed a partially initialized companion: {peer.get('error')!r}"
    assert peer == {"size": 1}, "the lock holder must initialize the companion before its strict check"
    assert companion.stat().st_size == 1


@pytest.mark.parametrize("kind", ["directory", "extra-byte", "symlink", "hardlink"])
def test_unsafe_companion_is_refused(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "unit"
    root.mkdir()
    companion = pda.coordination_root(root).companion
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
    # Windows refuses to open a directory as a file (PermissionError) before the entry check runs.
    with pytest.raises(OSError):
        _transacts(root)


def test_unavailable_native_object_identity_is_not_a_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "Unit"
    root.mkdir()
    _transacts(root)
    safe, fstat = pda._safe_entry, os.fstat

    def unknown(info):
        return SimpleNamespace(st_dev=info.st_dev, st_ino=0, st_size=info.st_size)

    monkeypatch.setattr(pda, "_safe_entry", lambda path, **kwargs: unknown(safe(path, **kwargs)))
    monkeypatch.setattr(pda.os, "fstat", lambda descriptor: unknown(fstat(descriptor)))
    with pytest.raises(pda.GateTransactionError, match="identity-unproved"):
        _transacts(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific path spelling")
@pytest.mark.parametrize(("readable", "flags", "refusal"), [(False, 0, "case-unproved"), (True, 1, "case-sensitive")])
def test_unproved_or_case_sensitive_parent_is_not_a_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, readable: bool, flags: int, refusal: str
) -> None:
    import ctypes  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    root = tmp_path / "Unit"
    root.mkdir()
    _transacts(root)
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
    with pytest.raises(pda.GateTransactionError, match=refusal):
        _transacts(root)


def test_dotdot_cannot_erase_an_unproved_parent_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "parent"
    (parent / "ordinary").mkdir(parents=True)
    root = parent / "Unit"
    root.mkdir()
    path = parent / "ordinary" / ".." / "Unit"
    assert pda.coordination_root(path).canonical == pda.coordination_root(root).canonical
    safe = pda._safe_entry

    def unproved(location, **kwargs):
        if location == parent / "ordinary":
            raise pda.GateTransactionError("fixture reparse component")
        return safe(location, **kwargs)

    with pda.gate_transactions([root]):
        monkeypatch.setattr(pda, "_safe_entry", unproved)
        with pytest.raises(pda.GateTransactionError, match="reparse component"):
            pda.coordination_root(path)
        with pytest.raises(pda.GateTransactionError, match="reparse component"):
            with pda.gate_transaction(path):
                pytest.fail("an alias binding must not bypass validation of the original parent chain")


def test_nested_transactions_join_without_reacquiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "unit"
    root.mkdir()
    count = []
    acquire = pda._descriptor_lock

    def counted(descriptor, lock):
        count.append(lock)
        return acquire(descriptor, lock)

    monkeypatch.setattr(pda, "_descriptor_lock", counted)
    with pda.gate_transaction(root):
        with pda.gate_transaction(root):
            pda._check_transaction(root)
        with pda.gate_transactions([root]):
            pda._check_transaction(root)
    assert count == [True], "a nested transaction must not recursively reacquire the same byte"


def test_changed_companion_identity_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "identity-control"
    root.mkdir()
    companion = pda.coordination_root(root).companion
    entry = pda._safe_entry
    with pda.gate_transaction(root):
        with monkeypatch.context() as patch:

            def changed(path, **kwargs):
                info = entry(path, **kwargs)
                if path == companion:
                    return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1, st_size=info.st_size)
                return info

            patch.setattr(pda, "_safe_entry", changed)
            refusal = None
            try:
                pda._check_transaction(root)
            except pda.GateTransactionError as exc:
                refusal = str(exc)
            assert refusal == "transaction-companion-changed", "replaced companion must not be credited as serialized"


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
