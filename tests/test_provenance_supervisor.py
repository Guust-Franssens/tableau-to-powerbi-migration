"""Direct controls for PR #603: bounded receive/validation, protocol state and affirmative reap."""

from __future__ import annotations

# These tests exercise deliberately private supervision seams, not a public application API.
# pylint: disable=protected-access

import json
import multiprocessing
import pickle
import queue
import socket
import struct
import subprocess
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
    state.accept({"kind": prov.MSG_OPERATION, "operation": "collect-inputs", "completed": 0, "total": 1})
    state.accept(_discovery(total))
    state.accept({"kind": prov.MSG_OPERATION, "operation": "collect-inputs", "completed": 1, "total": 1})
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


def _inventory_started() -> estate._ProvenanceState:
    state = _fingerprinted()
    state.accept({"kind": "lookup-intent", "requested": True})
    for operation, completed in (("sign-in", 0), ("sign-in", 1), ("inventory", 0)):
        state.accept({"kind": "operation", "operation": operation, "completed": completed, "total": 1})
    return state


def _facts(**changes) -> dict:
    return {
        "kind": "inventory-facts",
        "facts": {
            "returned_count": 1000,
            "requested_page_size": 1000,
            "page_number": None,
            "page_size": None,
            "total_available": None,
            "invalid_fields": 0,
            **changes,
        },
    }


def test_inventory_completion_requires_exactly_one_parse_outcome() -> None:
    state = _inventory_started()
    completion = {"kind": "operation", "operation": "inventory", "completed": 1, "total": 1}
    try:
        state.accept(completion)
    except estate.ProvenanceProtocolError:
        pass
    else:
        pytest.fail("INVENTORY_EVENT_REQUIRED: completion without a parse outcome was accepted")
    assert state.counters["inventory"] == 0
    state.accept(_facts(returned_count=0, total_available=0))
    state.accept(completion)
    assert state.inventory.status == "complete" and state.counters["inventory"] == 1


@pytest.mark.parametrize("kind", ["inventory-facts", "inventory-failed"])
def test_successful_and_failed_inventory_outcomes_are_exclusive(kind: str) -> None:
    state = _inventory_started()
    state.accept({"kind": "inventory-failed"})
    message = _facts() if kind == "inventory-facts" else {"kind": "inventory-failed"}
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept(message)
    state.accept({"kind": "operation", "operation": "inventory", "completed": 1, "total": 1})
    assert state.inventory is None and state.inventory_failed


@pytest.mark.parametrize(
    "field", ["returned_count", "requested_page_size", "page_number", "page_size", "total_available", "invalid_fields"]
)
@pytest.mark.parametrize("value", [True, False, -1, 1.0, float("nan"), float("inf"), "1000", 1 << 63])
def test_raw_inventory_facts_are_numeric_bounded_and_never_coerced(field: str, value: object) -> None:
    with pytest.raises(estate.ProvenanceProtocolError):
        estate._validated_message(_facts(**{field: value}))


@pytest.mark.parametrize(
    "changes",
    [
        {"returned_count": None},
        {"requested_page_size": None},
        {"invalid_fields": None},
        {"invalid_fields": 4},
        {"invalid_fields": 1, "page_number": 1, "page_size": 1000, "total_available": 1000},
        {"requested_page_size": 999},
        {"status": "complete"},
        {"url": "https://private.invalid"},
        {"name": "private-workbook"},
    ],
)
def test_raw_facts_have_a_closed_shape_and_consistent_validity_count(changes: dict) -> None:
    with pytest.raises(estate.ProvenanceProtocolError):
        estate._validated_message(_facts(**changes))


@pytest.mark.parametrize("missing", ["returned_count", "requested_page_size", "page_number", "invalid_fields"])
def test_raw_inventory_facts_require_the_whole_numeric_envelope(missing: str) -> None:
    message = _facts()
    del message["facts"][missing]
    with pytest.raises(estate.ProvenanceProtocolError):
        estate._validated_message(message)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"returned_count": 0}, "complete"),
        ({"returned_count": 999}, "complete"),
        ({"total_available": 1000}, "complete"),
        ({}, "cannot_establish"),
        ({"total_available": 1001}, "truncated"),
        ({"total_available": 1001, "invalid_fields": 1}, "truncated"),
        ({"total_available": 1000, "invalid_fields": 1}, "cannot_establish"),
        ({"total_available": 999}, "cannot_establish"),
    ],
)
def test_parent_classifies_at_the_raw_event_before_any_result(changes: dict, expected: str) -> None:
    state = _inventory_started()
    state.accept(_facts(**changes))
    assert state.inventory.status == expected, "PARENT_RAW_EVENT_CLASSIFICATION"
    assert state.snapshot is None and state.terminal is None


@pytest.mark.parametrize("code", [estate.PROVENANCE_DEADLINE_CODE, estate.PROVENANCE_CRASH_CODE])
def test_parent_retains_known_ambiguity_without_a_worker_result(code: str) -> None:
    state = _inventory_started()
    state.accept(_facts())
    document = state.document(code)
    assert document["phase"]["errors"] == [
        {
            "code": "inventory-cannot-establish",
            "operation": "inventory",
            "returned_count": 1000,
            "requested_page_size": 1000,
        },
        {"code": code, "operation": "inventory"},
    ], "PARENT_INVENTORY_ERROR_RETENTION"
    assert not prov.is_success(document) and document["inputs"][0]["input"]["sha256"] == "a" * 64


@pytest.mark.parametrize("value", [True, False, -1, 1.0, float("nan"), float("inf"), "1", 1 << 64])
def test_count_validator_rejects_non_counts_for_its_own_reason(value: object) -> None:
    """Removing the count validator must fail here, not happen to trip a later sequence guard."""
    try:
        estate._validated_message(_discovery(value))
    except estate.ProvenanceProtocolError:
        return
    pytest.fail("COUNT_VALIDATOR: a non-count was admitted as the discovery total")


@pytest.mark.parametrize("code", ["inventory-truncated", "inventory-cannot-establish"])
def test_numeric_pagination_findings_use_the_existing_typed_error_path(code: str) -> None:
    estate._validated_error(
        {
            "code": code,
            "operation": "inventory",
            "returned_count": 1000,
            "requested_page_size": 1000,
            "page_number": 1,
            "page_size": 1000,
            "total_available": (1 << 63) - 1,
        }
    )


@pytest.mark.parametrize(
    "field", ["returned_count", "requested_page_size", "page_number", "page_size", "total_available"]
)
@pytest.mark.parametrize("value", [True, False, -1, 1.0, float("nan"), float("inf"), "1000", None, 1 << 63])
def test_pagination_protocol_facts_are_bounded_integers(field: str, value: object) -> None:
    error = {
        "code": "inventory-cannot-establish",
        "operation": "inventory",
        "returned_count": 1000,
        "requested_page_size": 1000,
        field: value,
    }
    with pytest.raises(estate.ProvenanceProtocolError):
        estate._validated_error(error)


@pytest.mark.parametrize(
    "updates",
    [
        {"code": "live-lookup-failed"},
        {"operation": "sign-in"},
        {"requested_page_size": 999},
        {"exception_class": "ValueError"},
        {"http_status": 200},
        {"url": "https://private.invalid"},
    ],
)
def test_pagination_protocol_fields_cannot_escape_the_inventory_error_shape(updates: dict) -> None:
    error = {
        "code": "inventory-cannot-establish",
        "operation": "inventory",
        "returned_count": 1000,
        "requested_page_size": 1000,
        **updates,
    }
    with pytest.raises(estate.ProvenanceProtocolError):
        estate._validated_error(error)


@pytest.mark.parametrize("field", ["returned_count", "requested_page_size"])
def test_pagination_protocol_requires_both_observed_and_requested_counts(field: str) -> None:
    error = {
        "code": "inventory-cannot-establish",
        "operation": "inventory",
        "returned_count": 1000,
        "requested_page_size": 1000,
    }
    del error[field]
    with pytest.raises(estate.ProvenanceProtocolError):
        estate._validated_error(error)


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
    try:
        state.accept({"kind": kind, "result": _result()})
    except estate.ProvenanceProtocolError:
        pass
    else:
        pytest.fail("DISCOVERY_RECONCILIATION: a one-input result replaced two discovered inputs")
    result = state.document(estate.PROVENANCE_DEADLINE_CODE)
    assert result["input_count"] == len(result["inputs"]) == 2
    assert result["inputs"][1]["input"] == {"status": "unavailable"}


def test_unsafe_digest_is_rejected_without_echoing_the_payload() -> None:
    """A permitted key does not make a host path a derived digest."""
    state = _state()
    state.accept(_discovery())
    state.accept({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": 1})
    try:
        state.accept(_checkpoint(digest=r"C:\private\credential"))
    except estate.ProvenanceProtocolError as caught:
        assert str(caught) == ""
    else:
        pytest.fail("DERIVED_FORMAT: a host path was accepted as a SHA-256 digest")
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


def _bounded_drain(receiver: estate._ProvenanceReceiver, timeout: float) -> tuple[threading.Thread, list]:
    """An independent test watchdog also makes a broken blocking supervisor fail rather than hang pytest."""
    outcomes = []
    deadline = time.monotonic() + timeout

    def drain() -> None:
        try:
            outcomes.append(estate._drain_worker(receiver, deadline, receiver.state))
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            outcomes.append(type(exc).__name__)

    supervisor = threading.Thread(target=drain, daemon=True)
    supervisor.start()
    return supervisor, outcomes


@pytest.mark.timing
@pytest.mark.parametrize("raw", [b"\x00", struct.pack("!I", 10000)], ids=["partial-header", "partial-body"])
def test_partial_frame_cannot_hold_the_supervising_thread(raw: bytes) -> None:
    """Readiness is not body completion: the sender deliberately keeps its socket open."""
    left, right = socket.socketpair()
    receiver = estate._ProvenanceReceiver(prov.ProvenanceChannel(left), _state())
    right.sendall(raw)
    receiver.thread.start()
    started = time.monotonic()
    supervisor, outcomes = _bounded_drain(receiver, 0.08)
    try:
        supervisor.join(0.25)
        assert not supervisor.is_alive(), "PARTIAL_FRAME_DEADLINE: incomplete receive held the supervisor"
        assert outcomes == [estate.PROVENANCE_DEADLINE_CODE]
    finally:
        receiver.close()
        right.close()
        supervisor.join(0.1)
        # The test owns this peer; production reaps its worker before closing the receiver.
        receiver.thread.join(0.1)
    assert time.monotonic() - started < 0.35, "PARTIAL_FRAME_DEADLINE: receive held the parent"
    assert receiver.thread.daemon and not receiver.thread.is_alive()
    assert receiver.state.total is None


@pytest.mark.timing
@pytest.mark.parametrize("component", ["decode", "validate"])
def test_slow_validation_cannot_commit_after_supervisor_expiry(monkeypatch, component: str) -> None:
    """Intentionally blocked decode/validation runs only in the daemon transport helper."""
    left, right = socket.socketpair()
    state = _state()
    receiver = estate._ProvenanceReceiver(prov.ProvenanceChannel(left), state)
    entered, release = threading.Event(), threading.Event()
    original = state.prepare if component == "validate" else estate._decode_message

    def slow_prepare(message: object) -> object:
        candidate = original(message)
        entered.set()
        release.wait(2)
        return candidate

    monkeypatch.setattr(
        state if component == "validate" else estate,
        "prepare" if component == "validate" else "_decode_message",
        slow_prepare,
    )
    prov.ProvenanceChannel(right).send(_discovery())
    receiver.thread.start()
    started = time.monotonic()
    supervisor, outcomes = _bounded_drain(receiver, 0.05)
    try:
        assert entered.wait(0.2), "the control never reached decode/validation"
        supervisor.join(0.2)
        assert not supervisor.is_alive(), f"{component.upper()}_DEADLINE: work held the supervising thread"
        assert outcomes == [estate.PROVENANCE_DEADLINE_CODE]
        assert state.total is None, "LATE_VALIDATION_COMMIT: temporary state escaped its deadline"
    finally:
        receiver.close()
        release.set()
        receiver.thread.join(1)
        supervisor.join(1)
        right.close()
    assert not receiver.thread.is_alive()
    assert time.monotonic() - started < 0.35, "VALIDATION_DEADLINE: validation or cleanup held the supervisor"
    assert state.total is None, "late validation mutated evidence after the supervisor returned"


def _wire(message: dict) -> bytes:
    payload = json.dumps(message).encode()
    return struct.pack("!I", len(payload)) + payload


def _receive(raw: bytes) -> tuple[str | None, estate._ProvenanceState]:
    """Drive the real channel/decoder/state machine without paying for a spawn per malformed field."""
    left, right = socket.socketpair()
    receiver = estate._ProvenanceReceiver(prov.ProvenanceChannel(left), _state())
    receiver.start()
    try:
        right.sendall(raw)
        right.close()
        code = estate._drain_worker(receiver, time.monotonic() + 1, receiver.state)
    finally:
        receiver.close()
        right.close()
    assert not receiver.thread.is_alive(), "transport helper leaked on a completed control"
    return code, receiver.state


def test_complete_protocol_is_accepted_only_after_eof() -> None:
    """The positive wire control; refusing every message would not satisfy this test."""
    messages = [
        {"kind": prov.MSG_OPERATION, "operation": "collect-inputs", "completed": 0, "total": 1},
        _discovery(),
        {"kind": prov.MSG_OPERATION, "operation": "collect-inputs", "completed": 1, "total": 1},
        {"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": 1},
        _checkpoint(),
        {"kind": prov.MSG_LOOKUP_INTENT, "requested": False},
        {"kind": prov.MSG_TERMINAL, "result": _result()},
    ]
    code, state = _receive(b"".join(_wire(message) for message in messages))
    assert code is None and state.terminal == _result()


@pytest.mark.parametrize("trailer", [_wire(_discovery()), b"\x00"], ids=["trailing-message", "trailing-partial-header"])
def test_terminal_is_final_even_when_a_trailer_follows_in_the_same_buffer(trailer: bytes) -> None:
    """Returning at terminal instead of reading through EOF falsely passes both of these."""
    messages = [
        {"kind": prov.MSG_OPERATION, "operation": "collect-inputs", "completed": 0, "total": 1},
        _discovery(),
        {"kind": prov.MSG_OPERATION, "operation": "collect-inputs", "completed": 1, "total": 1},
        {"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": 1},
        _checkpoint(),
        {"kind": prov.MSG_LOOKUP_INTENT, "requested": False},
        {"kind": prov.MSG_TERMINAL, "result": _result()},
    ]
    code, state = _receive(b"".join(_wire(message) for message in messages) + trailer)
    assert code == estate.PROVENANCE_PROTOCOL_CODE, "TERMINAL_FINAL: trailing data was ignored"
    assert state.document(code)["phase"]["status"] == "partial"


@pytest.mark.parametrize(
    "body",
    [
        b"\x80\x05invalid-pickle",
        b"{",
        b'{"kind":"inputs-discovered","total":1,"total":2}',
        b'{"kind":"inputs-discovered","total":NaN}',
        b'{"kind":"inputs-discovered","total":1.0}',
        b'{"kind":"inputs-discovered","total":' + b"9" * 200 + b"}",
    ],
    ids=["invalid-pickle", "invalid-json", "duplicate-key", "nonfinite", "float", "huge-integer"],
)
def test_decode_failures_are_typed_and_never_escape_the_supervisor(body: bytes) -> None:
    """Malformed wire bytes produce one closed fault code, not an exception in the supervisor."""
    code, state = _receive(struct.pack("!I", len(body)) + body)
    assert code == estate.PROVENANCE_PROTOCOL_CODE, "DECODE_PROTOCOL: malformed bytes escaped typed rejection"
    assert state.total is None and state.checkpoints == {}


def test_oversized_frame_is_rejected_from_the_header_before_body_allocation() -> None:
    """The review's ten-million-byte declaration is refused without waiting for its missing body."""
    code, state = _receive(struct.pack("!I", 10000000))
    assert code == estate.PROVENANCE_PROTOCOL_CODE
    assert state.total is None


def test_aggregate_wire_budget_is_enforced_before_reading_another_body(monkeypatch) -> None:
    """Many individually small frames cannot bypass the phase-wide memory budget."""
    first = _wire(_discovery())
    monkeypatch.setattr(estate, "PROVENANCE_MAX_PHASE_BYTES", len(first) - 4)
    second = _wire({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": 1})
    code, state = _receive(first + second)
    assert code == estate.PROVENANCE_PROTOCOL_CODE
    assert state.total == 1 and not state.checkpoints


def test_message_count_limit_is_closed_even_for_repeated_safe_progress() -> None:
    """A stream of valid-looking counters is bounded independently of its byte size."""
    state = _fingerprinted()
    state.messages = estate.PROVENANCE_MAX_MESSAGES
    with pytest.raises(estate.ProvenanceProtocolError):
        state.prepare({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": 1})


def test_physical_multiplicity_is_preserved_even_for_identical_fingerprints() -> None:
    """Two copies of the same bytes are still two discovered physical inputs, never a set of hashes."""
    state = _fingerprinted(2)
    state.accept({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 2, "total": 2})
    state.accept(_checkpoint(index=1))
    state.accept({"kind": prov.MSG_LOOKUP_INTENT, "requested": False})
    state.accept({"kind": prov.MSG_TERMINAL, "result": _result(2)})
    assert state.terminal["input_count"] == len(state.terminal["inputs"]) == 2


def test_terminal_cannot_substitute_one_inputs_fingerprint_for_another() -> None:
    """The count alone is insufficient: ordinal evidence must reconcile without an identity-loss join."""
    state = _fingerprinted(2)
    state.accept({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 2, "total": 2})
    state.accept(_checkpoint(index=1, digest="b" * 64))
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept({"kind": prov.MSG_TERMINAL, "result": _result(2)})
    assert state.terminal is None


@pytest.mark.timing
def test_huge_member_list_is_rejected_before_traversal() -> None:
    """The review's 400,000 members must not turn a millisecond budget into a late accepted checkpoint."""
    state = _state()
    state.accept(_discovery())
    state.accept({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": 1})
    message = _checkpoint()
    message["record"]["input"]["members"] = [{"size_bytes": 1, "crc32": "12345678"}] * 400000
    started = time.monotonic()
    try:
        state.prepare(message)
    except estate.ProvenanceProtocolError:
        pass
    else:
        pytest.fail("MEMBER_LIMIT_BEFORE_TRAVERSAL: an oversized valid member list was admitted")
    assert time.monotonic() - started < 0.05, "MEMBER_LIMIT_BEFORE_TRAVERSAL"
    assert state.checkpoints == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size_bytes", True),
        ("size_bytes", -1),
        ("size_bytes", 1 << 64),
        ("sha256", "a" * 63),
        ("sha256", "A" * 64),
        ("sha256", r"C:\private\secret"),
        ("revision_key", {"algo": r"C:\private", "value": "a" * 64}),
        ("revision_key", {"algo": "tableau-xml-v1", "value": "not-a-digest"}),
        ("members", [{"size_bytes": True, "crc32": "12345678"}]),
        ("members", [{"size_bytes": 1, "crc32": "123"}]),
        ("status", "anything"),
    ],
)
def test_checkpoint_field_formats_are_not_just_allowed_keys(field: str, value: object) -> None:
    """Each allowed field still has a strict, bounded scalar or container format."""
    record = _checkpoint()["record"]
    record["input"][field] = value
    with pytest.raises(estate.ProvenanceProtocolError, match="^$"):
        estate._validated_checkpoint(record)


def test_discovery_is_unique_bounded_and_required_before_evidence() -> None:
    """No checkpoint can allocate from an undiscovered, repeated or over-limit input total."""
    state = _state()
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept(_checkpoint())
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept(_discovery(estate.PROVENANCE_MAX_INPUTS + 1))
    state.accept(_discovery())
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept(_discovery())
    assert state.total == 1 and state.checkpoints == {}


def test_operations_cannot_regress_or_run_ahead_of_local_checkpoints() -> None:
    """Remote work cannot precede local evidence, nor can an operation move backwards."""
    state = _state()
    state.accept(_discovery())
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept({"kind": prov.MSG_OPERATION, "operation": "sign-in", "completed": 0, "total": 1})
    state = _fingerprinted()
    state.accept({"kind": prov.MSG_LOOKUP_INTENT, "requested": True})
    state.accept({"kind": prov.MSG_OPERATION, "operation": "sign-in", "completed": 0, "total": 1})
    state.accept({"kind": prov.MSG_OPERATION, "operation": "sign-in", "completed": 1, "total": 1})
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept({"kind": prov.MSG_OPERATION, "operation": "fingerprint", "completed": 1, "total": 1})


@pytest.mark.parametrize("field", ["phase", "stamped_at", "fingerprint_error"])
def test_unsafe_status_timestamp_and_error_values_cannot_enter_an_artifact(field: str) -> None:
    """Free-form payloads cannot hide in formerly shape-only validated result fields."""
    state = _fingerprinted()
    result = _result()
    unsafe = r"C:\private\host\credential"
    if field == "phase":
        result[field]["status"] = unsafe
    elif field == "fingerprint_error":
        result["inputs"][0][field] = unsafe
    else:
        result[field] = unsafe
    with pytest.raises(estate.ProvenanceProtocolError, match="^$"):
        state.accept({"kind": prov.MSG_TERMINAL, "result": result})
    assert unsafe not in json.dumps(state.document(estate.PROVENANCE_PROTOCOL_CODE))


class _ClockProcess:
    """A process-start seam whose cost advances an independent deterministic clock."""

    def __init__(self, clock: SimpleNamespace) -> None:
        self.clock = clock
        self.pid = None

    def start(self) -> None:
        """Charge six seconds of spawn against the five-second budget."""
        self.clock.now += 6
        self.pid = 12345

    def close(self) -> None:
        """No actual OS process was created by this deterministic seam."""


def test_deadline_is_created_before_process_start(monkeypatch, tmp_path: Path) -> None:
    """Moving deadline creation after start must fail the named deadline assertion."""
    clock = SimpleNamespace(now=10.0)
    process = _ClockProcess(clock)
    cancel = SimpleNamespace(value=0)
    allocation = []

    def raw_value(*args):
        allocation.append(args)
        return cancel

    context = SimpleNamespace(
        Process=lambda **_kwargs: process,
        RawValue=raw_value,
        Event=lambda: allocation.append("event") or cancel,
    )
    seen = []
    monkeypatch.setattr(estate.multiprocessing, "get_context", lambda _method: context)
    monkeypatch.setattr(estate.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(estate, "_stop_worker", lambda _process: estate._WorkerStop(False, 0, False))
    monkeypatch.setattr(
        estate, "_ProvenanceReceiver", lambda *_args: SimpleNamespace(start=lambda: None, close=lambda: None)
    )

    def drain(_receiver, deadline: float, _state) -> str:
        seen.append(deadline)
        return estate.PROVENANCE_DEADLINE_CODE

    monkeypatch.setattr(estate, "_drain_worker", drain)
    outcome = estate.collect_provenance(tmp_path, timeout_sec=5)
    assert allocation == [("b", 0)], "CANCEL_NO_WORKER_LOCK: parent cancellation acquired a worker-shared mutex"
    assert seen == [15.0], "DEADLINE_BEFORE_SPAWN: spawn was granted a second budget"
    assert outcome.expired and not prov.is_success(outcome.result)
    assert cancel.value == 1, "CANCEL_PARENT_WRITE: cancellation was not signalled"


def _publication_spy(monkeypatch, out: Path) -> list[dict]:
    published: list[dict] = []
    writer = estate.write_source_provenance

    def publish(directory: Path, result: dict) -> Path | None:
        published.append(result)
        return writer(directory, result)

    out.mkdir()
    monkeypatch.setattr(estate, "write_source_provenance", publish)
    return published


@pytest.mark.parametrize("exception", [OSError, pickle.PicklingError])
def test_start_failure_publishes_once_and_finishes_without_raw_exception(
    monkeypatch,
    tmp_path: Path,
    capsys,
    exception: type[Exception],
) -> None:
    """An actual Process.start failure no longer skips publication and the phase-finish event."""
    out = tmp_path / "bundle"
    published = _publication_spy(monkeypatch, out)
    starts = []

    def fail_start(_process) -> None:
        starts.append(True)
        raise exception(r"C:\private\host\credential")

    monkeypatch.setattr(multiprocessing.process.BaseProcess, "start", fail_start)
    stamped = estate.stamp_inputs(tmp_path, out, timeout_sec=0.1)
    printed = capsys.readouterr()
    assert starts == [True], "the control did not reach Process.start"
    assert len(published) == 1, "PUBLISH_COUNT: startup failure did not publish exactly once"
    assert not stamped.ok
    assert published[0]["phase"]["errors"][0]["code"] == estate.PROVENANCE_START_CODE
    assert r"C:\private" not in json.dumps(published) + printed.out + printed.err + stamped.detail
    assert printed.out.count('"event": "phase-start"') == 1
    assert printed.out.count('"event": "phase-finish"') == 1


def test_publisher_count_has_an_independent_positive_control(monkeypatch, tmp_path: Path) -> None:
    """Removing or duplicating the publisher call must fail specifically on call multiplicity."""
    result = _result()
    outcome = estate.ProvenanceOutcome(result, 1, 1, None, False, 0, False)
    monkeypatch.setattr(estate, "collect_provenance", lambda *_args: outcome)
    out = tmp_path / "bundle"
    published = _publication_spy(monkeypatch, out)
    stamped = estate.stamp_inputs(tmp_path, out)
    assert len(published) == 1, "PUBLISH_COUNT: expected exactly one parent publication"
    assert stamped.ok and published == [result]


def test_unpicklable_entry_is_refused_before_a_bootstrap_child_exists(monkeypatch, tmp_path: Path) -> None:
    """A real pickling rejection cannot orphan a Windows bootstrap process or print its traceback."""
    starts = []
    monkeypatch.setattr(multiprocessing.process.BaseProcess, "start", lambda _process: starts.append(True))
    outcome = estate.collect_provenance(tmp_path, entry=lambda *_args: None)
    assert not starts, "an unpicklable target reached OS process creation"
    assert outcome.worker_pid is None and outcome.result["phase"]["errors"][0]["code"] == estate.PROVENANCE_START_CODE


@pytest.mark.timing
def test_stuck_daemon_decoder_cannot_hold_interpreter_exit(tmp_path: Path) -> None:
    """The independent outer process is the exit oracle; returning from drain alone is insufficient."""
    script = r"""
import socket, struct, sys, threading, time
sys.path.insert(0, sys.argv[1])
import run_estate as e
import stamp_tableau_provenance as p
entered = threading.Event()
def stuck(_payload):
    entered.set()
    time.sleep(30)
e._decode_message = stuck
left, right = socket.socketpair()
receiver = e._ProvenanceReceiver(p.ProvenanceChannel(left), e._ProvenanceState())
receiver.start()
right.sendall(struct.pack("!I", 2) + b"{}")
assert entered.wait(0.5), "decoder was never entered"
code = e._drain_worker(receiver, time.monotonic() + 0.1, receiver.state)
receiver.close()
right.close()
assert receiver.thread.daemon
assert code == e.PROVENANCE_DEADLINE_CODE
print("bounded-exit")
"""
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(Path(estate.__file__).parent)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("DAEMON_EXIT: the helper held interpreter shutdown")
    assert result.returncode == 0 and result.stdout.strip() == "bounded-exit", result.stderr
    assert time.monotonic() - started < 1.6, "DAEMON_EXIT: the transport helper held interpreter shutdown"


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
    state.accept({"kind": prov.MSG_LOOKUP_INTENT, "requested": False})
    state.accept({"kind": prov.MSG_TERMINAL, "result": _result()})
    result = estate._worker_document(state, None, stopped)
    assert not stopped.reaped, "REAP_AFFIRMATIVE: cleanup exceptions were treated as success"
    assert result["phase"]["status"] == "partial"
    assert result["phase"]["errors"][-1]["code"] == estate.PROVENANCE_REAP_CODE
    assert "private-path" not in json.dumps(result)
    assert result["inputs"] == _result()["inputs"]


@pytest.mark.parametrize(
    "stopped",
    [
        estate._WorkerStop(False, None, False),
        estate._WorkerStop(None, 0, False),
        estate._WorkerStop(None, None, True),
        estate._WorkerStop(False, 0, True, True),
    ],
    ids=["missing-exit-code", "unknown-liveness", "both-unknown", "exception-after-reap"],
)
def test_unknown_cleanup_never_certifies_an_accepted_result(stopped: estate._WorkerStop) -> None:
    """False liveness alone is not affirmative reap, and an exit code alone is not either."""
    state = _fingerprinted()
    state.accept({"kind": prov.MSG_LOOKUP_INTENT, "requested": False})
    state.accept({"kind": prov.MSG_TERMINAL, "result": _result()})
    result = estate._worker_document(state, None, stopped)
    assert result["phase"]["status"] == "partial", "REAP_AFFIRMATIVE: unknown cleanup certified success"
    assert result["phase"]["errors"][-1]["code"] == estate.PROVENANCE_REAP_CODE
    assert result["inputs"] == _result()["inputs"]


def test_cleanup_failure_cannot_leave_an_unbounded_automatic_exit_join() -> None:
    """Inspect CPython's actual active-child set, which its automatic shutdown handler later joins."""
    process = _Process(resists_terminate=True, broken="liveness")
    multiprocessing.process._children.add(process)
    try:
        assert not estate._stop_worker(process).reaped
        assert process not in multiprocessing.process._children, (
            "AUTOMATIC_EXIT_JOIN: an unaccounted worker remained registered"
        )
    finally:
        multiprocessing.process._children.discard(process)


@pytest.mark.parametrize("requested", [0, 1, None, "false", {}, []])
def test_lookup_intent_is_a_boolean_not_a_coercible_payload(requested: object) -> None:
    """The privacy-safe intent has one exact scalar type."""
    with pytest.raises(estate.ProvenanceProtocolError):
        estate._validated_message({"kind": prov.MSG_LOOKUP_INTENT, "requested": requested})


def test_lookup_intent_is_unique_and_precedes_live_work() -> None:
    """No worker can retract or change intent after starting a live operation."""
    state = _fingerprinted()
    state.accept({"kind": prov.MSG_LOOKUP_INTENT, "requested": True})
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept({"kind": prov.MSG_LOOKUP_INTENT, "requested": False})
    state.accept({"kind": prov.MSG_OPERATION, "operation": "sign-in", "completed": 0, "total": 1})
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept({"kind": prov.MSG_LOOKUP_INTENT, "requested": False})
    assert state.live_requested is True


@pytest.mark.parametrize("stage", ["sign-in", "inventory", "content", "scrub", "sign-out"])
def test_local_intent_cannot_start_any_live_operation(stage: str) -> None:
    """Absence of live intent is not retroactively repaired by a terminal label."""
    state = _fingerprinted()
    state.accept({"kind": prov.MSG_LOOKUP_INTENT, "requested": False})
    with pytest.raises(estate.ProvenanceProtocolError):
        state.accept(
            {"kind": prov.MSG_OPERATION, "operation": stage, "completed": 0, "total": None if stage == "content" else 1}
        )
    assert stage not in state.counters


@pytest.mark.parametrize(
    "basename",
    ["Sales:Q3.twb", r"Sales\Q3.twb", "unit.twb.", "unit.twb ", "CON.twb", "COM¹.twb", "unit\x01.twb"],
)
def test_posix_basename_rules_do_not_inherit_windows_restrictions(monkeypatch, basename: str) -> None:
    """POSIX rejects NUL and slash, not Windows punctuation, device stems, controls or trailing rules."""
    monkeypatch.setattr(estate, "_BASENAME_PLATFORM", "posix")
    try:
        estate._validated_basename(basename)
    except estate.ProvenanceProtocolError:
        pytest.fail("POSIX_PLATFORM_RULE: a bounded legal POSIX basename was rejected")


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("basename", ["", ".", "..", "dir/unit.twb", "/unit.twb", "unit\0.twb", "x" * 256, True])
def test_basename_common_rejections_are_lexical_and_bounded(monkeypatch, platform: str, basename: object) -> None:
    """Every flavour refuses paths, dot segments, NUL, non-text and over-bound names."""
    monkeypatch.setattr(estate, "_BASENAME_PLATFORM", platform)
    with pytest.raises(estate.ProvenanceProtocolError):
        estate._validated_basename(basename)


@pytest.mark.parametrize(
    "basename",
    [
        "CON",
        "con.twb",
        "CON .twb",
        "AUX.twb",
        "PRN.twb",
        "NUL.tar.twb",
        "CONIN$",
        "CONOUT$",
        "COM9.twb",
        "LPT1.twb",
        "LPT².twb",
        "unit.twb.",
        "unit.twb ",
        "unit\x01.twb",
        "Sales:Q3.twb",
        r"Sales\Q3.twb",
        "unit?.twb",
        "unit*.twb",
        "unit<.twb",
        "unit>.twb",
        'unit".twb',
        "unit|.twb",
    ],
)
def test_windows_basename_rules_reject_reserved_forms(monkeypatch, basename: str) -> None:
    """The deterministic Windows seam covers devices, punctuation, controls and trailing rules."""
    monkeypatch.setattr(estate, "_BASENAME_PLATFORM", "nt")
    try:
        estate._validated_basename(basename)
    except estate.ProvenanceProtocolError:
        return
    pytest.fail("WINDOWS_PLATFORM_RULE: a reserved Windows basename was accepted")


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize(
    "basename", ["unit.twb", ".hidden.twb", "COM0.twb", "COM10.twb", "Revenue 2026.twb", "x" * 255]
)
def test_legal_basenames_are_accepted_without_filesystem_access(monkeypatch, platform: str, basename: str) -> None:
    """Validate syntax only: even a legal name must never become an existence, resolve or open request."""
    monkeypatch.setattr(estate, "_BASENAME_PLATFORM", platform)

    def no_io(*_args, **_kwargs) -> None:
        pytest.fail("BASENAME_NO_IO: a worker basename became a filesystem lookup")

    with monkeypatch.context() as lexical:
        for name in ("stat", "resolve", "open"):
            lexical.setattr(Path, name, no_io)
        estate._validated_basename(basename)
