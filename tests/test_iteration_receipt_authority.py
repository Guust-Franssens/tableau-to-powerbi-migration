"""Direct current-authority controls for PR #605's compound rollback-write failure."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from test_iteration_receipt import (
    _change_during_finalization,
    _code,
    _finalize,
    _iterate,
    _path,
    _pending_successor,
    _review,
    _runtime,
    build_package,
    capture,
    receipt,
    write_json,
)

ARTIFACT_CHANGES = (
    ("report", "GENERATED_CHANGED"),
    ("page", "GENERATED_CHANGED"),
    ("model", "GENERATED_CHANGED"),
    ("cache", "GENERATED_CHANGED"),
    ("predecessor-receipt", "PREVIOUS_RECEIPT_MISMATCH"),
    ("predecessor-png", "SCREENSHOT_CHANGED"),
    ("current-receipt", "FINAL_RECEIPT_MISMATCH"),
    ("current-png", "SCREENSHOT_CHANGED"),
)


@pytest.fixture(name="package")
def package_fixture(tmp_path: Path) -> Path:
    """Use the existing coherent package and independently decodable PNG fixture."""
    return build_package(tmp_path)


def _change_final(package: Path, pending: dict[str, Any], kind: str) -> None:
    if kind == "current-receipt":
        path = _path(package, "002")
        before = path.read_bytes()
        path.write_bytes(before + b" ")
        assert json.loads(path.read_bytes()) == json.loads(before)
    else:
        _change_during_finalization(package, pending, kind)


def _compound_rollback(  # pylint: disable=too-many-locals
    package: Path, pending: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str
) -> tuple[list[str], list[bytes]]:
    path = _path(package, "002")
    backup = path.parent / receipt.PENDING_BACKUP_NAME
    unlink, opened, replace = Path.unlink, Path.open, os.replace
    events: list[str] = []
    published: list[bytes] = []

    def retire_then_change(target: Path, *args: Any, **kwargs: Any) -> None:
        if events:
            raise PermissionError("synthetic cleanup denial after retirement")
        unlink(target, *args, **kwargs)
        if target == backup:
            published.append(path.read_bytes())
            assert json.loads(published[0])["state"] == "final"
            _change_final(package, pending, kind)
            events.append("retired")

    def deny_recreation(target: Path, *args: Any, **kwargs: Any) -> Any:
        mode = args[0] if args else kwargs.get("mode", "r")
        if events and any(flag in mode for flag in "wax+"):
            assert target == backup and mode == "xb", "unexpected third fallback write"
            events.append("recreation-denied")
            raise PermissionError("synthetic marker creation denial")
        return opened(target, *args, **kwargs)

    def deny_withdrawal(source: Path, destination: Path) -> None:
        if events:
            assert source == path and destination == backup, "unexpected third fallback move"
            events.append("withdrawal-denied")
            raise PermissionError("synthetic final withdrawal denial")
        replace(source, destination)

    monkeypatch.setattr(Path, "unlink", retire_then_change)
    monkeypatch.setattr(Path, "open", deny_recreation)
    monkeypatch.setattr(receipt.os, "replace", deny_withdrawal)
    return events, published


@pytest.mark.parametrize("change", ARTIFACT_CHANGES, ids=[row[0] for row in ARTIFACT_CHANGES])
@pytest.mark.parametrize("route", ["library", "cli"])
def test_compound_rollback_failure_never_leaves_current_authority(  # pylint: disable=too-many-locals
    package: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: tuple[str, str],
    route: str,
) -> None:
    """The exact failed writes are observable; the surviving final refuses even without a marker."""
    kind, expected = change
    pending = _pending_successor(package)
    path = _path(package, "002")
    review = package.parent / "judgement.json"
    write_json(review, _review(pending))
    args = capture.parse_args(
        [
            "finalize",
            "--package",
            str(package),
            "--capture-sha256",
            receipt.receipt_sha256(pending),
            "--judgement",
            str(review),
        ]
    )
    events, published = _compound_rollback(package, pending, monkeypatch, kind)
    capsys.readouterr()
    if route == "library":
        assert _code(lambda: _finalize(package, pending)) == "FINALIZATION_ROLLBACK_FAILED"
    else:
        assert capture.cmd_finalize(args, _runtime(package)) == capture.EXIT_REFUSED
        output = capsys.readouterr().out
        assert "REFUSED: FINALIZATION_ROLLBACK_FAILED:" in output
        assert "FINALIZED " not in output and "FINAL_SHA256=" not in output

    assert events == ["retired", "recreation-denied", "withdrawal-denied"]
    assert json.loads(path.read_bytes())["state"] == "final"
    assert not (path.parent / receipt.PENDING_BACKUP_NAME).exists()
    assert not (path.parent / ".iteration.writing").exists()
    # The failed producer returns no final token. Even giving the reader the intended candidate's
    # checksum (stronger than an ordinary caller can supply) must not authorize stale disk state.
    candidate_sha256 = hashlib.sha256(published[0]).hexdigest()
    assert _code(lambda: receipt.read_chain(package, candidate_sha256)) == expected
    assert events == ["retired", "recreation-denied", "withdrawal-denied"]


@pytest.mark.parametrize("kind,expected", ARTIFACT_CHANGES)
@pytest.mark.parametrize("marker", ["none", "pending", "staged"])
def test_current_authority_revalidates_artifacts_and_history(
    package: Path, kind: str, expected: str, marker: str
) -> None:
    """A once-successful final token cannot authorize changed bytes, with or without a marker."""
    pending = _pending_successor(package)
    final = _finalize(package, pending)
    final_sha256 = receipt.receipt_sha256(final)
    _change_final(package, pending, kind)
    if marker != "none":
        name = receipt.PENDING_BACKUP_NAME if marker == "pending" else ".iteration.writing"
        (_path(package, "002").parent / name).write_bytes(b"retained rollback evidence")
        if expected in {"GENERATED_CHANGED", "FINAL_RECEIPT_MISMATCH"}:
            expected = "EXTRA_FILE"
    assert _code(lambda: receipt.read_chain(package, final_sha256)) == expected


@pytest.mark.parametrize("missing_pin", [None, "", "0" * 64])
def test_current_authority_requires_the_returned_final_token(package: Path, missing_pin: str | None) -> None:
    """No final token is issued on failure; history/disk state alone cannot substitute for one."""
    final = _finalize(package, _iterate(package))
    assert _code(lambda: receipt.read_chain(package, missing_pin)) == "FINAL_RECEIPT_MISMATCH"
    selected = receipt.read_chain(package, receipt.receipt_sha256(final))[-1]
    assert selected.receipt_bytes == receipt.receipt_bytes(final)
    assert selected.payload["outcome"] == "incomplete"
    assert selected.payload["generated"]["data_evidence"]["status"] == "pending"
    assert all(
        row["status"] == "unverified"
        for page in selected.payload["judgement"]["pages"]
        for row in page["numeric_results"]
    )


def test_pending_allocation_and_historical_verification_remain_non_authoritative(package: Path) -> None:
    """Historical artifacts may differ, but retained predecessor receipt and PNG bytes stay pinned."""
    first = _finalize(package, _iterate(package))
    first_bytes = _path(package).read_bytes()
    _change_during_finalization(package, first, "report")
    assert _code(lambda: receipt.read_chain(package, receipt.receipt_sha256(first))) == "GENERATED_CHANGED"
    assert receipt.read_history(package)[-1].receipt_bytes == first_bytes

    second = _iterate(package, previous=first)
    history = receipt.read_history(package)
    assert [row.payload["state"] for row in history] == ["final", "pending"]
    assert history[0].receipt_bytes == first_bytes
    assert history[0].payload["generated"]["artifact"] != history[1].payload["generated"]["artifact"]
    assert _code(lambda: receipt.read_chain(package, receipt.receipt_sha256(second))) == "NO_FINAL_ITERATION"
    assert _code(lambda: receipt.allocate_iteration(package, receipt.receipt_sha256(second))) == "PREVIOUS_NOT_FINAL"

    final = _finalize(package, second)
    chain = receipt.read_chain(package, receipt.receipt_sha256(final))
    assert [row.name for row in chain] == ["001", "002"]
    assert chain[0].receipt_bytes == first_bytes
    assert chain[-1].payload["generated"]["previous"]["receipt_sha256"] == hashlib.sha256(first_bytes).hexdigest()
    _path(package).write_bytes(first_bytes + b" ")
    assert _code(lambda: receipt.read_history(package)) == "PREVIOUS_RECEIPT_MISMATCH"
    assert _code(lambda: receipt.read_chain(package, receipt.receipt_sha256(final))) == "PREVIOUS_RECEIPT_MISMATCH"


@pytest.mark.parametrize("refuse", [False, True])
def test_finalize_cli_uses_the_shared_authoritative_reader(
    package: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], refuse: bool
) -> None:
    """The production completion call site cannot return success by merely parsing final history."""
    pending = _iterate(package)
    original = _path(package).read_bytes()
    review = package.parent / "judgement.json"
    write_json(review, _review(pending))
    args = capture.parse_args(
        [
            "finalize",
            "--package",
            str(package),
            "--capture-sha256",
            receipt.receipt_sha256(pending),
            "--judgement",
            str(review),
        ]
    )
    reader = receipt.read_chain
    calls = []

    def authoritative_read(root: Path, expected_sha256: str) -> list[receipt.Iteration]:
        assert root == package
        assert not (_path(root).parent / receipt.PENDING_BACKUP_NAME).exists()
        calls.append(expected_sha256)
        if refuse:
            raise receipt.ReceiptError("GENERATED_CHANGED")
        return reader(root, expected_sha256)

    monkeypatch.setattr(receipt, "read_chain", authoritative_read)
    capsys.readouterr()
    result = capture.cmd_finalize(args, _runtime(package))
    output = capsys.readouterr().out
    assert len(calls) == 1 and calls[0] != receipt.receipt_sha256(pending)
    if refuse:
        assert result == capture.EXIT_REFUSED
        assert "REFUSED: FINALIZATION_CHANGED:" in output and "FINAL_SHA256=" not in output
        assert _path(package).read_bytes() == original
    else:
        assert result == capture.EXIT_OK
        assert f"FINAL_SHA256={calls[0]}" in output and "outcome incomplete" in output
        assert reader(package, calls[0])[-1].receipt_bytes == _path(package).read_bytes()
