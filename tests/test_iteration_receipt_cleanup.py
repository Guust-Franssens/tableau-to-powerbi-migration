"""Direct controls for PR #605's final backup-cleanup race, without a second publication protocol."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from test_iteration_receipt import (
    _change_during_finalization,
    _code,
    _finalize,
    _path,
    _pending_successor,
    _review,
    _status,
    build_package,
    receipt,
)

# pylint: disable=protected-access


@pytest.fixture(name="package")
def package_fixture(tmp_path: Path) -> Path:
    """Reuse the coherent package, not another module's collected tests."""
    return build_package(tmp_path)


@pytest.mark.parametrize(
    "kind",
    ["report", "page", "model", "cache", "predecessor-receipt", "predecessor-png", "current-receipt", "current-png"],
)
def test_backup_unlink_mutation_cannot_return_stale_success(
    package: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A change inside the last cleanup must refuse and restore the captured pending bytes."""
    pending = _pending_successor(package)
    path = _path(package, "002")
    original = path.read_bytes()
    backup = path.parent / receipt.PENDING_BACKUP_NAME
    unlink = Path.unlink
    calls = []

    def remove_then_change(target: Path, *args: Any, **kwargs: Any) -> None:
        unlink(target, *args, **kwargs)
        if target == backup:
            calls.append(kind)
            _change_during_finalization(package, pending, kind)

    monkeypatch.setattr(Path, "unlink", remove_then_change)
    assert _code(lambda: _finalize(package, pending)) == "FINALIZATION_CHANGED"
    assert calls == [kind]
    assert path.read_bytes() == original
    assert json.loads(original)["state"] == "pending"
    assert not backup.exists()
    assert not (path.parent / ".iteration.writing").exists()


def test_unchanged_backup_cleanup_ends_with_snapshot_and_no_filesystem_work(  # pylint: disable=too-many-locals
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success has a post-cleanup snapshot and returns without another read, unlink or write."""
    pending = _pending_successor(package)
    path = _path(package, "002")
    original, predecessor = path.read_bytes(), _path(package).read_bytes()
    backup = path.parent / receipt.PENDING_BACKUP_NAME
    unlink, snapshot = Path.unlink, receipt._assert_snapshot
    events = []
    finished = False

    def remove_backup(target: Path, *args: Any, **kwargs: Any) -> None:
        if target == backup:
            assert target.read_bytes() == original
            events.append("cleanup")
        unlink(target, *args, **kwargs)

    def final_snapshot(*args: Any, **kwargs: Any) -> None:
        nonlocal finished
        snapshot(*args, **kwargs)
        if events:
            assert not backup.exists()
            events.append("final-snapshot")
            finished = True

    def before_success(operation: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            assert not finished, f"filesystem work after final snapshot: {operation.__name__}"
            return operation(*args, **kwargs)

        return guarded

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", remove_backup)
        for owner, name in (
            (Path, "open"),
            (Path, "unlink"),
            (os, "stat"),
            (os, "lstat"),
            (os, "scandir"),
            (os, "link"),
            (os, "replace"),
        ):
            scoped.setattr(owner, name, before_success(getattr(owner, name)))
        scoped.setattr(receipt, "_assert_snapshot", final_snapshot)
        final = _finalize(package, pending)

    assert events == ["cleanup", "final-snapshot"]
    assert [item.receipt_bytes for item in receipt.read_chain(package, receipt.receipt_sha256(final))] == [
        predecessor,
        path.read_bytes(),
    ]
    assert path.read_bytes() == receipt.receipt_bytes(final)
    assert final["state"] == "final" and final["outcome"] == "incomplete"
    assert not backup.exists() and not (path.parent / ".iteration.writing").exists()


@pytest.mark.parametrize("failure", ["changed", "unreadable"])
def test_failed_post_cleanup_snapshot_restores_exact_pending_atomically(
    package: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Rollback uses retained bytes, including noncanonical whitespace, and an atomic replacement."""
    pending = _pending_successor(package)
    path = _path(package, "002")
    original = path.read_bytes() + b" \n"
    path.write_bytes(original)
    backup = path.parent / receipt.PENDING_BACKUP_NAME
    snapshot, replace = receipt._assert_snapshot, os.replace
    failures, restored = [], []

    def fail_last(root: Path, chain: list[receipt.Iteration], pending_backup: receipt.Iteration | None = None) -> None:
        snapshot(root, chain, pending_backup)
        if chain[-1].payload["state"] == "final" and pending_backup is None:
            assert not backup.exists()
            failures.append(failure)
            if failure == "unreadable":
                raise PermissionError("synthetic final snapshot read denial")
            raise receipt.ReceiptError("GENERATED_CHANGED")

    def atomic_restore(source: Path, destination: Path) -> None:
        if source == backup:
            assert destination == path and source.read_bytes() == original
            restored.append(True)
        replace(source, destination)

    monkeypatch.setattr(receipt, "_assert_snapshot", fail_last)
    monkeypatch.setattr(receipt.os, "replace", atomic_restore)
    assert (
        _code(
            lambda: receipt.finalize(
                package,
                hashlib.sha256(original).hexdigest(),
                _review(pending),
                state_reader=lambda _pid: _status(package),
            )
        )
        == "FINALIZATION_CHANGED"
    )
    assert failures == [failure] and restored == [True]
    assert path.read_bytes() == original
    assert receipt.read_history(package)[-1].receipt_bytes == original
    assert _code(lambda: receipt.read_chain(package, hashlib.sha256(original).hexdigest())) == "NO_FINAL_ITERATION"
    assert not backup.exists() and not (path.parent / ".iteration.writing").exists()


@pytest.mark.parametrize("failure", ["create", "write", "replace"])
def test_post_cleanup_rollback_failure_blocks_ordinary_chain_readers(  # pylint: disable=too-many-locals
    package: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A failed reconstruction or replacement leaves a deterministic non-authoritative iteration."""
    pending = _pending_successor(package)
    path = _path(package, "002")
    original, predecessor = path.read_bytes(), _path(package).read_bytes()
    backup = path.parent / receipt.PENDING_BACKUP_NAME
    unlink, opened, replace = Path.unlink, Path.open, os.replace
    failures = []

    def remove_then_change(target: Path, *args: Any, **kwargs: Any) -> None:
        unlink(target, *args, **kwargs)
        if target == backup:
            _change_during_finalization(package, pending, "report")

    def fail_reconstruction(target: Path, *args: Any, **kwargs: Any) -> Any:
        mode = args[0] if args else kwargs.get("mode", "r")
        if target == backup and mode == "xb" and failure in {"create", "write"}:
            failures.append(failure)
            if failure == "write":
                with opened(target, *args, **kwargs):
                    raise PermissionError("synthetic restoration write denial")
            raise PermissionError("synthetic restoration create denial")
        return opened(target, *args, **kwargs)

    def fail_restore(source: Path, destination: Path) -> None:
        if source == backup and failure == "replace":
            failures.append(failure)
            raise PermissionError("synthetic restoration replace denial")
        replace(source, destination)

    monkeypatch.setattr(Path, "unlink", remove_then_change)
    monkeypatch.setattr(Path, "open", fail_reconstruction)
    monkeypatch.setattr(receipt.os, "replace", fail_restore)
    assert _code(lambda: _finalize(package, pending)) == "FINALIZATION_ROLLBACK_FAILED"
    assert failures == [failure] and backup.is_file()
    assert _path(package).read_bytes() == predecessor
    if failure == "replace":
        assert backup.read_bytes() == original
    elif failure == "write":
        assert backup.read_bytes() == b""
    else:
        assert not path.exists() and json.loads(backup.read_bytes())["state"] == "final"
    expected = "INPUT_UNREADABLE" if failure == "create" else "EXTRA_FILE"
    assert _code(lambda: receipt.read_chain(package, receipt.receipt_sha256(pending))) == expected
    assert _code(lambda: _finalize(package, pending)) == expected
    assert _code(lambda: receipt.allocate_iteration(package, receipt.receipt_sha256(pending))) == expected
