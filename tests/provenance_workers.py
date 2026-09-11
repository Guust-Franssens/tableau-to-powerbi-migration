"""Leaf-worker stand-ins for the supervised provenance phase (issue #576).

These live in their own module for one mechanical reason: the supervisor starts the worker with
``multiprocessing.get_context("spawn")``, so the target is pickled BY REFERENCE and re-imported in a
fresh interpreter. A closure or a local function in a test cannot cross that boundary; a top-level
function in an importable module can, and ``functools.partial`` over one carries its scenario with
it.

They import stdlib only, on purpose - the child pays the import cost on every spawn, and pulling
pytest into it would make each of these tests a second slower for no benefit.

⚠️ Nothing here is production code. What each one simulates is a BLOCK the parent must preempt: an
operation that never returns, a worker that dies mid-flight, a result that arrives after the latch.
"""

from __future__ import annotations

import copy
import os
import struct
import time
from pathlib import Path
from typing import Any

MESSAGE_INPUTS_DISCOVERED = "inputs-discovered"
MESSAGE_OPERATION = "operation"
MESSAGE_CHECKPOINT = "checkpoint"
MESSAGE_LOOKUP_INTENT = "lookup-intent"
MESSAGE_INVENTORY_FACTS = "inventory-facts"
MESSAGE_INVENTORY_FAILED = "inventory-failed"
MESSAGE_SAFE_SNAPSHOT = "safe-snapshot"
MESSAGE_TERMINAL = "terminal"

#: The derived-only shape a real checkpoint has: sizes, digests and CRCs, never a filename.
CHECKPOINT_SHA = "a" * 64
SECOND_CHECKPOINT_SHA = "b" * 64


def _checkpoint(index: int, digest: str = CHECKPOINT_SHA) -> dict[str, Any]:
    return {
        "kind": MESSAGE_CHECKPOINT,
        "index": index,
        "record": {"input": {"size_bytes": 11 + index, "sha256": digest}},
    }


def _operation(operation: str, completed: int = 0, total: int | None = None) -> dict[str, Any]:
    return {"kind": MESSAGE_OPERATION, "operation": operation, "completed": completed, "total": total}


def _scrubbed_result(files: tuple[str, ...] = ("unit.twb",)) -> dict[str, Any]:
    """A complete, already-scrubbed result - what the worker holds once scrub has succeeded."""
    return {
        "schema": "tableau-source-provenance/1",
        "stamped_at": "2026-09-10T00:00:00Z",
        "input_count": len(files),
        "inputs": [
            {
                "input": {
                    "file": name,
                    "size_bytes": 11 + index,
                    "sha256": CHECKPOINT_SHA if index == 0 else SECOND_CHECKPOINT_SHA,
                },
                "origin": None,
            }
            for index, name in enumerate(files)
        ],
        "phase": {"status": "local_only", "errors": []},
    }


def protocol_messages(
    files: tuple[str, ...] = ("unit.twb",),
    *,
    live: bool = True,
    matches: tuple[int | None, ...] | None = None,
) -> list[dict[str, Any]]:
    """Independent wire fixture: all applicable stages, including cache hits and inventory misses."""
    result = _scrubbed_result(files)
    messages = [
        _operation("collect-inputs", 0, 1),
        {"kind": MESSAGE_INPUTS_DISCOVERED, "total": len(files)},
        _operation("collect-inputs", 1, 1),
    ]
    for index, record in enumerate(result["inputs"]):
        messages.extend(
            [
                _operation("fingerprint", index, len(files)),
                _operation("fingerprint", index + 1, len(files)),
                _checkpoint(index, record["input"]["sha256"]),
            ]
        )
    messages.append({"kind": MESSAGE_LOOKUP_INTENT, "requested": live})
    if live:
        result["phase"]["status"] = "success"
        messages.extend(
            [
                _operation("sign-in", 0, 1),
                _operation("sign-in", 1, 1),
                _operation("inventory", 0, 1),
                {
                    "kind": MESSAGE_INVENTORY_FACTS,
                    "facts": {
                        "returned_count": len(files),
                        "requested_page_size": 1000,
                        "page_number": 1,
                        "page_size": 1000,
                        "total_available": len(files),
                        "invalid_fields": 0,
                    },
                },
                _operation("inventory", 1, 1),
            ]
        )
        attempted = set()
        for record, match in zip(result["inputs"], matches if matches is not None else range(len(files))):
            messages.append(_operation("content", len(attempted), len(attempted)))
            if match is None:
                record["origin_note"] = "no workbook of this LUID or name on the site - local-only input"
            else:
                attempted.add(match)
                record["origin"] = {
                    "server": "https://tableau.invalid",
                    "site": "fixture",
                    "workbook_luid": f"00000000-0000-0000-0000-{match:012d}",
                    "workbook_name": "Fixture",
                    "project": None,
                    "owner_luid": None,
                    "created_at": None,
                    "updated_at": None,
                    "tableau_product_version": None,
                    "rest_api_version": "3.21",
                    "matched_by": "luid",
                    "match": "sha256" if record["input"]["sha256"] == CHECKPOINT_SHA else "name_only",
                    "content_unavailable": None,
                    "revision_match": None,
                    "remote_revision_key": None,
                    "remote_sha256": CHECKPOINT_SHA,
                    "same_name_count": 1,
                }
            messages.append(_operation("content", len(attempted), len(attempted)))
        messages.extend(
            [
                _operation("scrub", 0, 1),
                _operation("scrub", 1, 1),
                {"kind": MESSAGE_SAFE_SNAPSHOT, "result": copy.deepcopy(result)},
                _operation("sign-out", 0, 1),
                _operation("sign-out", 1, 1),
            ]
        )
    messages.append({"kind": MESSAGE_TERMINAL, "result": result})
    return messages


def _block_forever() -> None:
    """Stand in for an operation with no bound: a trickled read, a hung scrub, a dead sign-out.

    It deliberately ignores cancellation. Cooperative checks are an optimisation between operations;
    what has to be proved here is that the parent stops a worker that will NEVER stop itself.
    """
    while True:
        time.sleep(3600)


def blocks_in(operation: str, conn, _cancel_event, _payload) -> None:
    """Reach an operation in the production order, then never return."""
    for message in protocol_messages(("first.twb", "second.twb")):
        conn.send(message)
        if message.get("operation") == operation and message["completed"] == 0:
            _block_forever()


def blocks_in_with_evidence(operation: str, conn, _cancel_event, _payload) -> None:
    """One completed input checkpointed, then a block inside `operation` with the second unfinished."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 2})
    conn.send(_operation("fingerprint", 1, 2))
    conn.send(_checkpoint(0))
    conn.send(_operation(operation, 1, 2))
    _block_forever()


def blocks_after_safe_snapshot(conn, _cancel_event, _payload) -> None:
    """Everything finished and scrubbed, then a sign-out that never answers."""
    for message in protocol_messages():
        conn.send(message)
        if message.get("operation") == "sign-out" and message["completed"] == 0:
            _block_forever()


def sends_a_late_terminal(conn, _cancel_event, _payload) -> None:
    """One accepted checkpoint, then a SUCCESS result that arrives after the deadline has latched."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 2})
    conn.send(_operation("fingerprint", 1, 2))
    conn.send(_checkpoint(0))
    conn.send(_operation("fingerprint", 1, 2))
    time.sleep(30)
    conn.send({"kind": MESSAGE_TERMINAL, "result": _scrubbed_result(("late-a.twb", "late-b.twb"))})
    time.sleep(30)


def crashes_after_a_checkpoint(conn, _cancel_event, _payload) -> None:
    """Dies mid-phase with a distinctive code, after one input's evidence has already landed."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 2})
    conn.send(_operation("fingerprint", 1, 2))
    conn.send(_checkpoint(0))
    time.sleep(0.05)
    os._exit(7)  # noqa: SLF001  # a crash, not an orderly exit: no atexit, no flush, no terminal


def sends_an_unsafe_checkpoint(secret: str, conn, _cancel_event, _payload) -> None:
    """A checkpoint carrying a copied string - the mutation the parent's protocol must refuse."""
    conn.send({"kind": MESSAGE_INPUTS_DISCOVERED, "total": 1})
    conn.send(_operation("fingerprint", 1, 1))
    conn.send(
        {
            "kind": MESSAGE_CHECKPOINT,
            "index": 0,
            "record": {"input": {"file": secret, "size_bytes": 11, "sha256": CHECKPOINT_SHA}},
        }
    )
    _block_forever()


def sends_a_secret_bearing_snapshot(secret: str, conn, _cancel_event, _payload) -> None:
    """A legitimately scrubbed snapshot that still holds copied strings; it must never be RENDERED."""
    for message in protocol_messages((secret,)):
        conn.send(message)
        if message.get("operation") == "sign-out" and message["completed"] == 0:
            _block_forever()


def succeeds(conn, _cancel_event, _payload) -> None:
    """A true local-only run: complete local evidence and an explicit no-live intent."""
    sends_messages(protocol_messages(live=False), conn, _cancel_event, _payload)


def sends_an_unknown_message(conn, _cancel_event, _payload) -> None:
    """Speaks outside the closed protocol. Fail closed rather than interpret it."""
    conn.send({"kind": "whatever-i-like", "payload": "trust me"})
    _block_forever()


def sends_messages(messages: list[dict], conn, _cancel_event, _payload) -> None:
    """Replay exact protocol controls in a spawn-picklable target."""
    for message in messages:
        conn.send(message)
    conn.close()


def sends_partial_frame(part: str, conn, _cancel_event, _payload) -> None:
    """A live child sends an incomplete header or a valid header without its body, then stalls."""
    raw = b"\x00" if part == "header" else struct.pack("!I", 10000)
    conn.endpoint.sendall(raw)
    (Path(_payload["input"]) / "frame-sent").write_text(part, encoding="utf-8")
    _block_forever()


def sends_invalid_pickle(conn, _cancel_event, _payload) -> None:
    """Bytes the old recv() tried to unpickle; JSON transport must refuse without raising in parent."""
    raw = b"\x80\x05not-a-valid-pickle-or-json"
    conn.endpoint.sendall(struct.pack("!I", len(raw)) + raw)
    conn.close()
