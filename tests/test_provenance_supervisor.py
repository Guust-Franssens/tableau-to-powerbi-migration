"""Direct controls for PR #603: bounded receive/validation, protocol state and affirmative reap."""

from __future__ import annotations

# These tests exercise deliberately private supervision seams, not a public application API.
# pylint: disable=protected-access

import json
import queue
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_estate as estate  # noqa: E402  # pylint: disable=wrong-import-position
import stamp_tableau_provenance as prov  # noqa: E402  # pylint: disable=wrong-import-position


def _state() -> estate._ProvenanceState:
    return estate._ProvenanceState(emit=lambda *_args: None)


def _discovery(total: object = 1) -> dict:
    return {"kind": prov.MSG_INPUTS_DISCOVERED, "total": total}


def _checkpoint(index: int = 0, digest: str = "a" * 64) -> dict:
    return {"kind": prov.MSG_CHECKPOINT, "index": index, "record": {"input": {"size_bytes": 11, "sha256": digest}}}


def _fingerprinted(total: int = 1) -> estate._ProvenanceState:
    state = _state()
    state.accept(_discovery(total))
    state.accept({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": total})
    state.accept(_checkpoint())
    return state


def _result(count: int = 1) -> dict:
    return {
        "schema": prov.SCHEMA,
        "stamped_at": "2026-09-11T00:00:00Z",
        "input_count": count,
        "inputs": [{"input": {"size_bytes": 11, "sha256": "a" * 64}} for _ in range(count)],
        "phase": {"status": "local_only", "errors": []},
    }


@pytest.mark.parametrize("value", [True, False, -1, 1.0, float("nan"), float("inf"), "1", 1 << 64])
def test_count_validator_rejects_non_counts_for_its_own_reason(value: object) -> None:
    """Removing the count validator must fail here, not happen to trip a later sequence guard."""
    with pytest.raises(estate.ProvenanceProtocolError, match="^$"):
        estate._validated_message(_discovery(value))


@pytest.mark.parametrize("index", [-1, True, 0, 2, 100000])
def test_checkpoint_index_cannot_duplicate_skip_or_amplify(index: int) -> None:
    """An index never determines allocation size."""
    state = _fingerprinted(2)
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept(_checkpoint(index))
    assert len(state.document("test")["inputs"]) == 2


@pytest.mark.parametrize("kind", [prov.MSG_TERMINAL, prov.MSG_SAFE_SNAPSHOT])
def test_result_counts_must_reconcile_with_discovery(kind: str) -> None:
    """One internally consistent record is not evidence for two discovered physical inputs."""
    state = _fingerprinted(2)
    if kind == prov.MSG_SAFE_SNAPSHOT:
        # Exercise the count validator, not the unrelated requirement to finish fingerprints first.
        state.operation = prov.OP_SCRUB
        state.counters = {prov.OP_SCRUB: 1}
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept({"kind": kind, "result": _result()})
    result = state.document(estate.PROVENANCE_DEADLINE_CODE)
    assert result["input_count"] == len(result["inputs"]) == 2
    assert result["inputs"][1]["input"] == {"status": "unavailable"}


def test_unsafe_digest_is_rejected_without_echoing_the_payload() -> None:
    """A permitted key does not make a host path a derived digest."""
    state = _state()
    state.accept(_discovery())
    state.accept({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": 1})
    with pytest.raises(estate.ProvenanceProtocolError) as caught:
        state.accept(_checkpoint(digest=r"C:\Users\private\credential"))
    assert str(caught.value) == ""
    assert state.checkpoints == {}


def test_clock_is_checked_after_validation_before_state_commit(monkeypatch) -> None:
    """A deterministic late candidate must fail on acceptance, not on the following loop's clock."""
    state = _state()
    mailbox = queue.Queue()
    mailbox.put(state.prepare(_discovery()))
    receiver = SimpleNamespace(mailbox=mailbox, acknowledged=threading.Event())
    ticks = iter((10.0, 11.1))
    monkeypatch.setattr(estate.time, "monotonic", lambda: next(ticks))

    code = estate._drain_worker(receiver, 11.0, state)

    assert state.total is None, "COMMIT_BEFORE_CLOCK: a validated but late discovery was accepted"
    assert code == estate.PROVENANCE_DEADLINE_CODE
    assert not receiver.acknowledged.is_set()


@pytest.mark.timing
@pytest.mark.parametrize("raw", [b"\x00", struct.pack("!I", 10000)], ids=["partial-header", "partial-body"])
def test_partial_frame_cannot_hold_the_supervising_thread(raw: bytes) -> None:
    """Readiness is not body completion: the sender deliberately keeps its socket open."""
    left, right = socket.socketpair()
    receiver = estate._ProvenanceReceiver(prov.ProvenanceChannel(left), _state())
    right.sendall(raw)
    receiver.thread.start()
    started = time.monotonic()
    try:
        code = estate._drain_worker(receiver, started + 0.08, receiver.state)
        assert code == estate.PROVENANCE_DEADLINE_CODE
    finally:
        receiver.close()
        right.close()
    assert time.monotonic() - started < 0.35, "PARTIAL_FRAME_DEADLINE: receive held the parent"
    assert receiver.thread.daemon and not receiver.thread.is_alive()
    assert receiver.state.total is None


@pytest.mark.timing
def test_slow_validation_cannot_commit_after_supervisor_expiry(monkeypatch) -> None:
    """An intentionally blocked validator runs only in the daemon transport helper."""
    left, right = socket.socketpair()
    state = _state()
    receiver = estate._ProvenanceReceiver(prov.ProvenanceChannel(left), state)
    entered, release = threading.Event(), threading.Event()
    prepare = state.prepare

    def slow_prepare(message: object) -> estate._ProvenanceState:
        candidate = prepare(message)
        entered.set()
        release.wait(2)
        return candidate

    monkeypatch.setattr(state, "prepare", slow_prepare)
    prov.ProvenanceChannel(right).send(_discovery())
    receiver.thread.start()
    assert entered.wait(1), "the control never reached validation"
    started = time.monotonic()
    try:
        assert estate._drain_worker(receiver, started + 0.05, state) == estate.PROVENANCE_DEADLINE_CODE
        receiver.close()
        assert state.total is None, "LATE_VALIDATION_COMMIT: temporary state escaped its deadline"
        assert time.monotonic() - started < 0.35, "VALIDATION_DEADLINE: validation held the supervisor"
    finally:
        release.set()
        receiver.thread.join(1)
        right.close()
    assert not receiver.thread.is_alive()
    assert state.total is None, "late validation mutated evidence after the supervisor returned"


class _Process:
    """Controlled OS seam: terminate and kill have independently observable effects."""

    def __init__(self, resists_terminate: bool = False, broken: str | None = None) -> None:
        self.alive = True
        self.exitcode = None
        self.resists_terminate = resists_terminate
        self.broken = broken
        self.calls: list[object] = []

    def terminate(self) -> None:
        """Either reap promptly, resist, or raise; no other method substitutes for this assertion."""
        self.calls.append("terminate")
        if self.broken == "terminate":
            raise OSError("private-path-must-not-escape")
        if not self.resists_terminate:
            self.alive, self.exitcode = False, -15

    def kill(self) -> None:
        """The independent fallback, used only when terminate did not establish reap."""
        self.calls.append("kill")
        if self.broken == "kill":
            raise OSError("private-path-must-not-escape")
        self.alive, self.exitcode = False, -9

    def join(self, timeout: float) -> None:
        """Record the actual bounded wait allowance."""
        self.calls.append(("join", timeout))
        if self.broken == "join":
            raise OSError("private-path-must-not-escape")

    def is_alive(self) -> bool:
        """Return known liveness unless that observation itself is broken."""
        if self.broken == "liveness":
            raise OSError("private-path-must-not-escape")
        return self.alive


def test_terminate_is_required_independently_of_kill_fallback() -> None:
    """Removing terminate is a failure even if kill could ultimately reap this fake."""
    process = _Process()
    stopped = estate._stop_worker(process)
    assert process.calls == ["terminate", ("join", estate.PROVENANCE_TERMINATE_JOIN_SEC)], "TERMINATE_REQUIRED"
    assert stopped.reaped and not stopped.killed


def test_kill_is_required_when_terminate_does_not_reap() -> None:
    """A separate control cannot be satisfied by the terminate-only path."""
    process = _Process(resists_terminate=True)
    stopped = estate._stop_worker(process)
    assert process.calls == [
        "terminate",
        ("join", estate.PROVENANCE_TERMINATE_JOIN_SEC),
        "kill",
        ("join", estate.PROVENANCE_KILL_JOIN_SEC),
    ], "KILL_FALLBACK_REQUIRED"
    assert stopped.reaped and stopped.killed


@pytest.mark.parametrize("broken", ["terminate", "join", "kill", "liveness"])
def test_cleanup_exceptions_never_certify_an_accepted_success(broken: str) -> None:
    """Even later affirmative liveness cannot erase an exception during cleanup."""
    process = _Process(resists_terminate=True, broken=broken)
    stopped = estate._stop_worker(process)
    state = _fingerprinted()
    state.accept({"kind": prov.MSG_TERMINAL, "result": _result()})
    result = estate._worker_document(state, None, stopped)
    assert not stopped.reaped, "REAP_AFFIRMATIVE: cleanup exceptions were treated as success"
    assert result["phase"]["status"] == "partial"
    assert result["phase"]["errors"][-1]["code"] == estate.PROVENANCE_REAP_CODE
    assert "private-path" not in json.dumps(result)
    assert result["inputs"] == _result()["inputs"]
